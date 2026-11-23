"""The Sage kernel as the mind of a Pipecat pipeline.

The LLM is not in the body's pipeline. ``SageProcessor`` sits between STT and
TTS: a final transcript starts a kernel turn, ``reply_delta`` text goes straight
to TTS (whose first-clause aggregator starts the voice early), every delivered
effect gets one ``result``, and an interruption reports what was actually
played. ``BotSpeech`` sits after the output transport, where frames arrive as
they are played, and feeds that playback into the interruption policy.
"""

import asyncio
import contextlib
import inspect
import itertools
import logging
import math
import re
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    InterruptionWorkerFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSTextFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.text.base_text_aggregator import AggregationType
from sage_device import gate, media, reflex, vision
from sage_device.choice import must_speak
from sage_device.floor import (
    CARRY_S,
    DROP,
    WAIT,
    half_heard,
    hold_limit,
    media_floor,
    taken,
)
from sage_device.hearing import COOLDOWN_S, trustworthy, unfinished
from sage_device.kernel import KernelUnavailableError, Session, acted
from sage_device.latency import AUDIO, NO_TURN, SILENT, TURN_ID
from sage_device.names import correct

from reachy_sage_voice import health
from reachy_sage_voice.expressions import MOVES_LINE, REACTIONS, emotion
from reachy_sage_voice.grammar import (
    Command,
    Context,
    Drop,
    Gesture,
    Song,
    asks_to_see,
    is_name,
    route,
)
from reachy_sage_voice.interruption import InterruptionTracker
from reachy_sage_voice.manifest import EMOTIONS, NO_SOUND
from reachy_sage_voice.plan import Plan, dump, parse
from reachy_sage_voice.song import LISTENING
from reachy_sage_voice.speech import TOOLS, SpeechStream, is_self_echo, move_sound, trim_echo
from reachy_sage_voice.voice import ENGINES, VoiceStyleFrame, style_settings, vocal_action

logger = logging.getLogger(__name__)

#: Said locally when the kernel cannot be reached, and when another body holds it.
OFFLINE_LINE = "I can't reach my memory right now."
BUSY_LINE = "One moment."
#: Said when a fast-lane command starts, by its first step; nothing for stop or a look.
ACKS = {"search_face": "Looking for you.", "track_face": "Following you.", "dance": "Dancing!",
        "stop_tracking": "Okay."}
#: Said and shown when a search finds no one on its whole path (owner, 2026-10-23).
NOT_FOUND_LINE = "Aw, I looked everywhere and couldn't find you!"
NOT_FOUND_EMOTE = "lonely1"
#: The look (``gate.ACK``), muted: unsure a line was for it, or what it said. A plain head tilt of
#: up to 21 degrees in 2.9 s. confused1 is 7.9 s and swings the head 45 degrees: too much for noise.
#: ponytail: one clip for both doubts, and a body that cannot play it shows nothing (no body has a
#: light hook yet): split or add one when the owner's eye, or a puck, asks.
UNSURE_CLIP = "inquiring3"
FLOOR_POLL_S = 0.05
#: How long after it asked a question whatever comes back is the answer, in seconds.
ASKED_S = 20.0
#: What the kernel tells the model of a done result's text (protocol §5).
RESULT_TEXT = 120
#: Long-poll length. Short enough that a stop reaches a playing body quickly.
POLL_S = 2.0
#: Bot replies remembered for the self-echo check (an earlier voice bot's window).
RECENT_REPLIES = 5
#: Longest a short reply's move waits for the voice before playing muted.
VOICE_WAIT_S = 10.0
#: Emotes this short (seconds) finish through a new turn or a barge-in; only stop cuts them
#: (owner, 2026-10-23: 44 of 108 were cut). ponytail: tune live; at 2.5 s six offered moves
#: qualify (inquiring1, success1/2, oops1, surprised1, grateful1), at 3.5 s about thirty.
EMOTE_FINISH_S = 3.5
#: Run states after which a turn will never deliver a reply.
_ENDED = ("failed", "cancelled", "unknown")
#: How a turn came in (v1.1 ``submitted.input``). No confidence, though the STT has one: the kernel
#: reads any under 0.9 as "(unclear)" (``policy.SURE``), and nine in ten real lines are. Its
#: input takes no other field: who it was said to and whose voice it was go as headers (§3).
SPEECH = {"medium": "speech"}
#: How the kernel's own turns (greetings, nudges) are named (protocol v1 §9).
KERNEL_TURN = "kernel-"


class Playback:
    """The pieces of the current response that have actually been played.

    Word by word with Kokoro's captioned speech: the output transport releases
    each word's TTSTextFrame as it is played (``tts.py``). Without captions, a
    clause counts only once all of it has played.
    """

    def __init__(self) -> None:
        """Start with nothing played."""
        self.spoken: list[str] = []
        self.speaking = False  # audio is playing
        self.queued = False  # text is on its way to the voice
        self._boundary = asyncio.Event()
        self._voice_free = asyncio.Event()
        self._voice_free.set()

    @property
    def busy(self) -> bool:
        """The voice is speaking or about to."""
        return self.speaking or self.queued

    async def idle(self) -> None:
        """Return once the voice has nothing left to say."""
        while self.busy:
            await self._boundary.wait()

    def hold_voice(self) -> None:
        """Hold the voice while a move sounds: sounds never overlap speech."""
        self._voice_free.clear()

    def release_voice(self) -> None:
        """Let the voice speak again: the move's sound is over."""
        self._voice_free.set()

    async def voice_free(self) -> None:
        """Return once no move is sounding."""
        await self._voice_free.wait()

    async def boundary(self) -> None:
        """Return at the next audio boundary: between sentences, or now if silent."""
        if self.speaking:
            await self._boundary.wait()

    def mark_boundary(self) -> None:
        """Release everyone waiting for a boundary."""
        self._boundary.set()
        self._boundary = asyncio.Event()

    @property
    def text(self) -> str:
        """What the person heard of this response."""
        return " ".join(s.strip() for s in self.spoken)


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


@dataclass
class _Turn:
    turn_id: str
    text: str = ""  # what the person said
    speech_ended: float | None = None  # when they stopped saying it: later VAD stops are not it
    sent_at: float | None = None
    first_text_at: float | None = None
    first_audio_at: float | None = None
    reply_seq: int | None = None
    reply_text: str | None = None  # the final reply, once delivered
    reported: bool = False
    streamed: bool = False
    withdrawn: bool = False  # replaced by a turn that repeats its words: stop it, report nothing
    moment: bool = False  # the TV's big moment: words only at a gap in it, never needed
    dropped: bool = False  # watching, no gap in the TV came: its words go unsaid, not over it
    ended: bool = False  # played and reported, or never admitted
    emotes: dict[int, asyncio.Task] = field(default_factory=dict)
    speech: SpeechStream = field(default_factory=lambda: SpeechStream((*TOOLS, *EMOTIONS)))


def _stops(effect: dict, turn: _Turn) -> bool:
    """Whether a kernel effect stops this turn: a bare stop, or one naming it.

    One naming another turn answers the body's own stop of a turn this one replaced.
    """
    return effect["kind"] == "stop" and effect.get("turn_id") in (None, turn.turn_id)


def _signals(frame) -> dict:
    """Return what a transcript frame carries for the gate: the STT's scores, the pipeline's face.

    ponytail: the direction (``frame.doa``) is not handed on. It would switch on the gate's
    other-voice rule with an unmeasured 45 degrees; hand it on once labelled lines carry it.
    """
    result = getattr(frame, "result", None)
    confidence = getattr(result, "confidence", None) or {}
    return {"confidence": confidence.get("mean"), "similarity": getattr(result, "similarity", None),
            "face": getattr(frame, "face", None)}


def _speaker(similarity: float | None) -> str:
    """Return whose voice the STT's voiceprint says it was, for the kernel (v1.6 ``Sage-Speaker``).

    "" while no voiceprint is enrolled: the kernel then remembers the line as nobody's own.
    ponytail: the STT's own 0.5 threshold; an owner scored under it on a short line is a guest
    here, and that line is not remembered. Measure on the labelled set (eval/noise.py).
    """
    if similarity is None:
        return ""
    return "owner" if similarity >= gate.OWNER_SIMILARITY else "guest"


def _gate(result: str, check: str) -> None:
    """Log what became of one transcript and which check decided: one line each, no words.

    ``gate.attend``'s four, each with its rule: answer, ack, listen, overheard. And the lines it
    never judged: command, dropped, held, echo, and the TV's turn, move or overheard (media.heard).
    For the labelled set (eval/noise.py); the meter's lines ("unsure:", "overheard:") stay beside it.
    """
    logger.info("gate: %s (%s)", result, check)


class SageProcessor(FrameProcessor):
    """Final transcripts to the kernel; its reply, emotes and results back out."""

    def __init__(self, session: Session, play_emote: Callable, playback: Playback,
                 now: Callable[[], float] = time.monotonic, play_sound: Callable | None = None,
                 engine: dict = ENGINES["kokoro"], song=None, names: tuple = (), executor=None,
                 finish: frozenset = frozenset(), hold_s: float = 0.0, see=None,
                 scene_s: float = 0.0, low_confidence: float = gate.LOW_CONFIDENCE, **kwargs):
        """Talk through ``session``; ``play_emote(name, sound)`` performs a move, muted or not.

        ``play_sound(move)`` plays a move's own sound for a vocal (None: no sounds),
        and ``engine`` maps voice styles and vocals onto the TTS (``voice.ENGINES``).
        ``song`` is the ``song.SongListener`` for listen_to_song (None: not declared);
        ``names`` (``names.parse``) are corrected in transcripts before the kernel.
        ``executor`` (``executor.Executor``) moves the body: the device grammar's
        commands and the kernel's body_plan (None: no commands; all goes to the kernel).
        ``finish`` names the emotes short enough to finish through a new turn (``EMOTE_FINISH_S``).
        ``hold_s``: a transcript that stops mid-sentence (``hearing.unfinished``) waits until
        this long after the end of speech for the rest of the turn (0: never).
        ``low_confidence``: the STT confidence under which the gate steps a line down.
        ``see(question)`` returns what the camera shows now, in words (``sage_device.vision``;
        None: no sight). A sight question ("What am I holding?") looks before its turn, and every
        ``scene_s`` (0: never) a quiet body tells the kernel a changed scene.
        """
        super().__init__(**kwargs)
        self._session, self._play_emote, self._playback, self._now = (
            session, play_emote, playback, now)
        self._play_sound, self._engine, self._song = play_sound, engine, song
        self._names, self._executor, self._finish = names, executor, finish
        self._finishing: set[asyncio.Task] = set()  # short emotes: a new turn lets them finish
        self._plans: set[asyncio.Task] = set()  # not the turn's: a barge-in must not end them
        if executor is not None and song is not None:  # body_plan's listen_song: the same listener
            executor.listen = lambda: song.listen(self._say_aside, self._session.notes)
        if executor is not None:  # a search that has not found the owner calls them by name
            owner = f"{names[0].written}, where" if names else "Where"
            executor.call_out = lambda: self._say_aside(f"{owner} are you?")
        self._gestured_at, self._last_clip = -math.inf, None
        self._hold_s, self._see, self._scene_s = hold_s, see, scene_s
        self._watching: asyncio.Task | None = None  # the scene, now and then: not the turn's
        self._attention = gate.Attention()  # the conversation: open or not (sage_device.gate)
        self._low, self._signals = low_confidence, {}  # the gate's floor; the last transcript's signals
        self._held: tuple[str, asyncio.Task] | None = None  # an unfinished fragment, waiting
        self._cut: tuple[str, float] | None = None  # a statement doubted as cut off, and when
        self._listening: asyncio.Task | None = None  # not the turn's: a barge-in must not end it
        self._recent: deque[str] = deque(maxlen=RECENT_REPLIES)
        self._speech_ended: float | None = None
        self._spoke_at = self._unsure_at = -math.inf  # it began to speak; it last showed a look
        self._said_at = -math.inf  # its last word: the conversation closes counted from here
        self._voice_on_at = self._voice_off_at = self._begun_at = -math.inf
        self._turn: _Turn | None = None
        self._turn_id = NO_TURN  # for the log lines (latency.py); a command's too
        self._task: asyncio.Task | None = None
        self._audio_done = asyncio.Event()
        self._watch = media.Watch()  # watching together: what is on, as heard (sage_device.media)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Route speech, playback and interruptions; pass every frame on."""
        await super().process_frame(frame, direction)
        TURN_ID.set(self._turn_id)  # frames arrive on more than one task: each gets the turn
        if isinstance(frame, StartFrame):
            self._task = self.create_task(self._await_kernel())
            if self._see is not None and self._scene_s > 0:
                self._watching = self.create_task(self._scene())
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._voice_on_at = self._now()
            if self._executor is None:
                pass
            elif not self._playback.speaking:  # its own voice in the mic is no one to wait for
                self._executor.hold()  # motors moving ruin the STT: hold still while they speak
                self._executor.attend()  # but first a glance toward the voice
            else:
                logger.info("glance skipped: Reachy is speaking")
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._voice_off_at = self._now()
            # The VAD decides stop_secs after the person actually stopped.
            self._speech_ended = self._voice_off_at - frame.stop_secs
            if self._executor is not None:
                self._executor.resume()
        elif isinstance(frame, TranscriptionFrame):
            # The mic hears the speaker late: its own closing words can lead a turn.
            heard = "" if is_self_echo(frame.text, self._recent) else correct(
                trim_echo(frame.text, [*self._recent, self._playback.text]), self._names)
            if heard:
                self._signals = _signals(frame)
                await self._submit(heard)
            else:
                _gate("echo", "speech")
        elif isinstance(frame, BotStartedSpeakingFrame):
            logger.info(AUDIO)  # latency.py: a turn's first one ends its v2v_ms
            self._spoke_at = self._said_at = self._now()
            if self._turn and self._turn.first_audio_at is None:
                self._turn.first_audio_at = self._now()
            if self._executor is not None:
                self._executor.replying()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._said_at = self._now()
            self._audio_done.set()
        elif isinstance(frame, InterruptionFrame):
            await self._interrupt()
            self._task = self.create_task(self._await_kernel())  # the kernel may still speak first
        await self.push_frame(frame, direction)

    async def cleanup(self):
        """Stop talking to the kernel when the pipeline ends."""
        await super().cleanup()
        for task in (self._task, self._listening, self._watching, *self._plans, *self._finishing,
                     self._held and self._held[1]):
            if task:
                await self.cancel_task(task)
        for task in self._turn.emotes.values() if self._turn else ():
            await self.cancel_task(task)

    # --- a turn ----------------------------------------------------------------

    async def _submit(self, heard: str, hold: bool = True) -> None:
        if self._held:  # an unfinished fragment was waiting for exactly this
            text, task = self._held
            self._held = None
            await self.cancel_task(task)
            heard = f"{text} {heard}"
        if self._cut:  # it did not answer this, but the rest of it may be here
            text, at = self._cut
            self._cut = None
            if self._now() - at < CARRY_S:
                heard = f"{text} {heard}"
        if await self._watched(heard):
            return
        phase = self._gesture_phase()
        intent = route(heard, Context(body=self._executor is not None, song=self._song is not None,
                                      listening=self._song is not None and self._song.busy,
                                      gesture=phase != reflex.PASS))
        if isinstance(intent, Drop):
            logger.info("%s, not sent: %r", intent.reason, heard)
            _gate("dropped", "route")
            return
        if isinstance(intent, Gesture):
            self._begin("reflex")
            _gate("move" if phase == reflex.FIRE else "dropped", "route")
            if phase == reflex.FIRE:
                self._attention = gate.opened(self._attention, self._now())  # it waved back
                self._gesture(intent.reaction, heard)
            else:
                logger.info("gesture skipped: a repeat, %r", heard)
            return
        if isinstance(intent, Song):
            _gate("command", "route")
            self._attention = gate.opened(self._attention, self._now())
            await self._interrupt()
            if not (self._listening and not self._listening.done()):
                self._listening = self.create_task(self._listen(None, LISTENING))
            self._task = self.create_task(self._await_kernel())  # _interrupt ended the poll
            return
        if isinstance(intent, Command):
            words = re.findall(r"[\w'-]+", heard.lower())
            if self._watch.on and intent.plan[0].skill != "stop" and not any(map(is_name, words)):
                logger.info("media: a command while watching, not run without the name: %r", heard)
                _gate("overheard", "media.heard")
                return  # "turn", "look", "pit stop" are commentary words (S0); stop costs nothing
            self._begin("reflex")
            _gate("command", "route")
            # A command is a line to it: what follows is in a conversation. A stop ends one.
            self._attention = (gate.Attention() if intent.plan[0].skill == "stop"
                               else gate.opened(self._attention, self._now()))
            await self._command(intent.plan)
            self._task = self.create_task(self._await_kernel())  # _interrupt ended the poll
            return
        if hold and self._hold_s and unfinished(heard):
            ended = self._speech_ended if self._speech_ended is not None else self._now()
            wait = self._hold_s - (self._now() - ended)
            if wait > 0:
                self._held = (heard, self.create_task(self._release(heard, wait)))
                _gate("held", "hearing.unfinished")  # its outcome is the next gate line's
                return
        turn = self._turn
        running = turn is not None and not turn.ended
        joins = running and turn.reply_seq is None and turn.first_audio_at is None
        if not (rule := self._attended(heard, joins)):
            return  # and a turn still running runs on: talk between others does not replace it
        if joins:
            # Nothing of the reply heard yet: the kernel forgets a turn stopped before
            # its reply, so its words lead the one that replaces it, as one message.
            # No "interrupted" result: that would keep the half-turn in memory too.
            heard = f"{turn.text} {heard}"
            turn.withdrawn = True
            rule += ", replaced"
        timing = {}
        if self._speech_ended is not None:
            timing["stt_ms"] = _ms(self._now() - self._speech_ended)
        await self._interrupt()  # a new turn replaces one still running
        if running and turn.streamed:
            # Its words are queued in the TTS: interrupt the voice, as barge-in does,
            # or they lead the next reply ("OhOh, a messy screen!").
            await self.push_frame(InterruptionFrame())
        turn_id = self._begin("kernel")
        _gate(gate.ANSWER, rule)  # after the turn began: the line carries its id (latency.py)
        talk = self._converse(turn_id, heard, timing, self._speech_ended, addressed=True,
                              speaker=_speaker(self._signals.get("similarity")))
        looks = self._see is not None and asks_to_see(heard)
        self._task = self.create_task(self._looked(heard, talk) if looks else talk)
        if self._executor is not None and not self._playback.speaking:
            self._executor.think()  # it heard words, and the reply is already on its way

    async def _watched(self, heard: str) -> bool:
        """Whether ``heard`` is the TV's (``sage_device.media``): no turn of the person's, never.

        A big moment gets a cheer, and a few words now and then: a turn marked as the TV's moment.
        """
        was_on = self._watch.on
        self._watch, whose = media.heard(self._watch, self._now(), heard, is_name)
        self._session.watching = self._watch.on
        if was_on and not self._watch.on:
            logger.info("media: ended")  # pet.py: speech over a voice is counted apart while on
        if self._watch.on and not was_on:  # the lines before were the TV's too: their turn goes
            logger.info("media: watching %s", (media.context(self._watch) or {}).get("about"))
            self._attention = gate.Attention()  # and the conversation they held open closes
            turn = self._turn
            if turn is not None and not turn.ended and turn.first_audio_at is None:
                await self._interrupt()
        if whose != media.COMMENTARY:
            return False
        self._watch, react = media.moment(self._watch, self._now(), heard)
        logger.info("media: commentary (%s)%s", media.context(self._watch)["about"],
                    f", {react}" if react else "")
        free = not self._playback.busy and (self._turn is None or self._turn.ended)
        _gate("turn" if react == media.SPEAK and free else
              "move" if react and not self._playback.busy else "overheard", "media.heard")
        if react == media.SPEAK and free:
            moment = {**SPEECH, "media": {**media.context(self._watch), "moment": True}}
            self._task = self.create_task(self._converse(self._begin("kernel"), heard, {}, None,
                                                         input=moment))
        elif react and not self._playback.busy:  # a cheer may play while a turn waits, not over words
            self._last_clip = clip = reflex.pick(REACTIONS["cheer"], self._last_clip)
            logger.info("gesture: %s for cheer", clip)
            task = self.create_task(self._tilt(clip))  # muted, and the kernel never hears the TV
            self._plans.add(task)
            task.add_done_callback(self._plans.discard)
        return True

    async def _looked(self, heard: str, talk) -> None:
        """Look, then take the turn: the kernel hears what the camera saw just before."""
        try:
            began = self._now()
            seen = await self._see(heard)
            logger.info("saw %s in %s ms", f"{len(seen)} characters" if seen else "nothing",
                        _ms(self._now() - began))
            if seen:
                await self._tell_seen(seen)
        except BaseException:
            talk.close()  # cancelled while looking: the turn never began
            raise
        await talk

    async def _tell_seen(self, seen: str) -> None:
        if "saw" in (getattr(self._session, "features", None) or ()):
            await self._act("saw", "done", input=seen, origin="camera")

    async def _scene(self) -> None:
        """Now and then, while quiet, tell the kernel a changed scene (never the same twice)."""
        told = None
        while True:
            await asyncio.sleep(self._scene_s)
            busy = (self._turn and not self._turn.ended) or self._playback.speaking
            if busy or gate.is_open(self._attention, self._now(), self._said_at):
                continue
            seen = await self._see(None)
            if seen and vision.changed(told, seen):
                told = seen
                logger.info("scene changed: %s characters", len(seen))
                await self._tell_seen(seen)

    def _attended(self, heard: str, joins: bool = False) -> str:
        """Ask the gate whether ``heard`` is for it, once (``gate.attend``): the rule if it answers.

        ``joins``: a turn of theirs has had no reply yet, so a line the gate only doubts (a look,
        a scrap) is the rest of it, cut by a pause, and joins it; whole lines between others do not.

        Else "": no turn, and the kernel never hears the words. A look (ACK) is a muted tilt, at
        most one every ``COOLDOWN_S``: its motors can make the next noise. Real words it took as
        said to it but too unclear to answer lead what the person says next within ``CARRY_S``.
        A line it was only unsure of, or took for talk between others, does not: it was never
        established as said to it, and what leads an answered line is sent as said to it.
        """
        now = self._now()
        outcome, rule, self._attention = gate.attend(heard, gate.Signals(
            now, spoke_at=self._said_at, asked=self._asked(), low=self._low, named=is_name,
            **self._signals), self._attention)
        if outcome == gate.ANSWER or (joins and (outcome == gate.ACK or rule.endswith("unclear"))):
            return rule
        _gate(outcome, rule)
        if outcome == gate.OVERHEARD:
            logger.info("overheard: quiet")
            return ""
        said_to_it = rule.split(",")[0] not in ("unsure", "aside")
        if said_to_it and trustworthy(heard, is_name) and (
                outcome == gate.ACK or half_heard(heard, is_name)):
            self._cut = (heard, now)
        busy = self._playback.busy or bool(self._executor and self._executor.busy())
        if outcome == gate.ACK and not busy and now - self._unsure_at >= COOLDOWN_S:
            self._unsure_at = now
            logger.info("unsure: tilted")
            task = self.create_task(self._tilt())
            self._finishing.add(task)  # a new turn lets it finish, as any short emote
            task.add_done_callback(self._finishing.discard)
        else:
            logger.info("unsure: quiet")
        return ""

    def _asked(self) -> bool:
        """Whether it just asked something, so whatever comes back is the answer."""
        return (bool(self._recent) and self._recent[-1].rstrip().endswith("?")
                and self._now() - self._spoke_at < ASKED_S)

    async def _tilt(self, clip: str = UNSURE_CLIP) -> None:
        try:
            played = self._play_emote(clip, False)
            if inspect.isawaitable(played):
                await played
        except Exception as error:  # a gesture, not a requirement
            logger.warning("could not play %s: %s", clip, error)

    def _begin(self, lane: str) -> str:
        """Start a turn of the person's: a new id for every log line from here (latency.py)."""
        self._begun_at = self._now()
        turn_id = self._turn_id = "reachy-" + uuid.uuid4().hex
        TURN_ID.set(turn_id)  # and every task it starts
        ago = "-" if self._speech_ended is None else _ms(self._now() - self._speech_ended)
        logger.info("turn start (%s lane): voice ended %s ms ago", lane, ago)
        return turn_id

    async def _release(self, heard: str, wait: float) -> None:
        """Send a held fragment as it is once nothing more came within the hold."""
        await asyncio.sleep(wait)
        self._held = None
        await self._submit(heard, hold=False)

    async def _converse(self, turn_id: str, heard: str, timing: dict,
                        speech_ended: float | None = None, input: dict | None = None,
                        addressed: bool = False, speaker: str = "") -> None:
        """Take one turn of the person's, then listen for the kernel's own.

        ``addressed``: the gate answered this line; the TV's big moment is a turn it did not.
        ``speaker``: whose voice the voiceprint says it was (``_speaker``).
        """
        turn = self._turn = _Turn(turn_id, heard, speech_ended)
        turn.moment = bool(((input or {}).get("media") or {}).get("moment"))
        self._audio_done.clear()
        try:
            if input is None:  # the person's line: with what is on, if anything (sage_device.media)
                watching = media.context(self._watch)
                input = {**SPEECH, "media": watching} if watching else SPEECH
            admitted = await self._session.start(turn.turn_id, heard, timing, input,
                                                 addressed=addressed, speaker=speaker)
            features = sorted(getattr(self._session, "features", None) or ())
            health.report("voice.kernel", True, "features: " + (", ".join(features) or "none"))
        except KernelUnavailableError as error:
            logger.warning("kernel unreachable: %s", error)
            health.report("voice.kernel", False, f"down ({error})")
            admitted = None
            await self._say_aside(OFFLINE_LINE)
        if admitted == "busy":
            await self._say_aside(BUSY_LINE)
        if admitted == "admitted":
            turn.sent_at = self._now()
            await self._play(turn)
        turn.ended = True
        if self._executor is not None:
            self._executor.thought_over()  # no audio will end it: a silent or failed turn
        await self._await_kernel()

    async def _await_kernel(self) -> None:
        """Between turns, follow any turn the kernel starts itself: a greeting, a nudge."""
        while True:
            try:
                body = await self._session.poll(0, POLL_S)
            except KernelUnavailableError as error:
                logger.warning("kernel unreachable between turns: %s", error)
                await asyncio.sleep(POLL_S)
                continue
            turn_id = next((e["turn_id"] for e in body["effects"]
                            if (e.get("turn_id") or "").startswith(KERNEL_TURN)
                            and e["kind"] != "stop"), None)  # a stop names a turn that is over
            if turn_id is not None:
                TURN_ID.set(turn_id)
                self._turn_id = turn_id
                logger.info("initiated")  # pet.py: nothing the person said began it
                self._attention = gate.opened(self._attention, self._now())  # a reply is for it
                turn = self._turn = _Turn(turn_id)
                self._audio_done.clear()
                await self._play(turn, body)
                turn.ended = True

    async def _play(self, turn: _Turn, body: dict | None = None) -> None:
        try:
            await self._follow(turn, body)
        except KernelUnavailableError as error:
            logger.warning("kernel unreachable mid-turn: %s", error)
            if turn.streamed:
                await self.push_frame(LLMFullResponseEndFrame())

    async def _follow(self, turn: _Turn, first: dict | None = None) -> None:
        """Stream this turn's reply, then report it once it has played.

        ``first`` is a poll already in hand that holds the turn's first effects.
        """
        offset = 0
        while turn.reply_seq is None:
            body, first = first or await self._session.poll(offset, POLL_S), None
            tools = []
            for effect in body["effects"]:
                kind = effect["kind"]
                if _stops(effect, turn):
                    await self._stop_body()
                    return
                if effect.get("turn_id") != turn.turn_id:
                    continue
                if kind == "reply_delta":
                    # The kernel sends from our offset on; skip anything already had.
                    piece = effect["text"][max(0, offset - effect["offset"]):]
                    offset = effect["offset"] + len(effect["text"])
                    await self._speak(turn, piece)
                elif kind == "reply":
                    turn.reply_seq, turn.reply_text = effect["sequence"], effect["text"]
                    if effect["stale"]:
                        await self._report(turn.reply_seq, "failed", reason="stale")
                        turn.reported = True
                        break
                    self._recent.append(effect["text"])
                    await self._speak(turn, effect["text"][offset:], final=True)
                elif kind in ("voice_style", "vocal"):
                    await self._voice_tool(turn, effect)  # in order with the words
                else:
                    tools.append(effect)
            run = body.get("turn") or {}
            mine = run.get("turn_id") == turn.turn_id
            # A final reply comes in the same poll as its turn's end, so none has come: no words at all.
            silent = mine and run.get("run_status") == "completed" and turn.reply_seq is None
            if silent:  # the kernel sends no reply for silence, never an empty one
                logger.info("reply empty")  # pet.py: it chose to say nothing
                turn.reply_text, turn.reported = "", True  # nothing to report, and nothing left to stop
            # After the whole batch, so a move knows the reply delivered with it.
            for effect in tools:
                self._start_tool(turn, effect)
            if silent and not turn.streamed:
                logger.info(SILENT)  # latency.py: this turn has no audio to measure
                await self.push_frame(VoiceStyleFrame({}))  # any style asked for has no reply to last for
            if silent or (mine and run.get("run_status") in _ENDED):
                break
        if turn.streamed:
            await self.push_frame(LLMFullResponseEndFrame())
            if not await self._played(turn, offset):
                return
        if turn.reply_seq is not None and not turn.reported:
            turn.reported = True
            await self._report(turn.reply_seq, "done", timing=self._reply_timing(turn))

    async def _speak(self, turn: _Turn, piece: str, final: bool = False) -> None:
        if piece and turn.first_text_at is None:
            turn.first_text_at = self._now()
            if math.isfinite(self._begun_at):  # a turn the kernel began itself has no start of ours
                logger.info("kernel: first words %s ms after turn start",
                            _ms(turn.first_text_at - self._begun_at))
        piece = turn.speech.feed(piece) + (turn.speech.flush() if final else "")
        if not piece.strip() or turn.dropped:
            return
        if not turn.streamed:
            if not await self._before_speaking():
                turn.dropped = True
                return
            turn.streamed = True
            await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(piece))

    async def _before_speaking(self) -> bool:
        """Let a sounding move finish, then mark the voice as about to speak. False: let it go."""
        await self._playback.voice_free()
        if not await self._floor():
            return False
        self._playback.queued = True
        return True

    async def _floor(self) -> bool:
        """Hold what it is about to say while the person has the floor (``floor.taken``).

        Their next words replace this turn and cancel the wait. Polls count, not the clock, end it.
        Watching, the TV never yields: ``_media_floor`` waits for a gap, or lets the words go.
        """
        if self._watch.on:
            return await self._media_floor()
        held = False
        limit = hold_limit(self._turn.text if self._turn else "", is_name)
        for _ in range(round(limit / FLOOR_POLL_S)):
            if not taken(self._now(), self._begun_at, self._voice_on_at, self._voice_off_at):
                break
            if not held:
                logger.info("floor: holding the reply, the person is talking")
                held = True
            await asyncio.sleep(FLOOR_POLL_S)
        if held:  # latency.RELEASE: the turn is timed from here, "-" if the cap ran out on a voice
            talking = self._voice_on_at > self._voice_off_at or self._speech_ended is None
            ago = "-" if talking else _ms(self._now() - self._speech_ended)
            logger.info("floor: released, voice ended %s ms ago", ago)
        return True

    async def _media_floor(self) -> bool:
        """Watching: wait for a gap in the voice (``floor.media_floor``); False if none came."""
        turn = self._turn
        must = turn is not None and not turn.moment and (
            must_speak(turn.text, is_name) not in ("", "too long") or self._asked())
        for poll in itertools.count():
            on = self._voice_on_at > self._voice_off_at
            gap = (0.0 if on else math.inf if self._speech_ended is None
                   else self._now() - self._speech_ended)
            verdict = media_floor(poll * FLOOR_POLL_S, gap, must)
            if verdict == DROP:
                logger.info("media: reply dropped, no gap in the TV")
                return False
            if verdict != WAIT:
                if poll:  # latency.RELEASE: timed from the person's last words, "-" over a voice
                    ago = "-" if on or self._speech_ended is None else _ms(gap)
                    logger.info("floor: released, voice ended %s ms ago", ago)
                return True
            await asyncio.sleep(FLOOR_POLL_S)

    async def _say(self, line: str) -> None:
        if await self._before_speaking():
            await self.push_frame(TTSSpeakFrame(line))

    async def _played(self, turn: _Turn, offset: int) -> bool:
        """Wait for the audio to finish, still listening for a stop. False if stopped."""
        done = asyncio.ensure_future(self._audio_done.wait())
        try:
            while not done.done():
                poll = asyncio.ensure_future(self._session.poll(offset, POLL_S))
                await asyncio.wait({done, poll}, return_when=asyncio.FIRST_COMPLETED)
                if not poll.done():
                    poll.cancel()  # its effects stay in the journal for the next poll
                    break
                if any(_stops(e, turn) for e in poll.result()["effects"]):
                    await self._stop_body()
                    return False
            return True
        finally:
            done.cancel()

    async def _stop_body(self) -> None:
        """Obey a stop from the kernel: stop the body; interrupt the pipeline as barge-in does."""
        if self._executor is not None:
            await self._executor.stop()
        await self.push_frame(InterruptionWorkerFrame(), FrameDirection.UPSTREAM)

    def _reply_timing(self, turn: _Turn) -> dict:
        timing = {}
        if turn.sent_at is not None and turn.first_text_at is not None:
            timing["ttft_ms"] = _ms(turn.first_text_at - turn.sent_at)
        # Only for the person's own turns: the kernel's follow no speech of theirs.
        if turn.speech_ended is not None and turn.first_audio_at is not None:
            timing["first_audio_ms"] = _ms(turn.first_audio_at - turn.speech_ended)
        return timing

    # --- commands --------------------------------------------------------------

    async def _command(self, plan: Plan) -> None:
        """Obey a fast-lane command: it replaces the turn in hand, as a turn would, asks no one."""
        stop = plan[0].skill == "stop"
        if stop:
            await self._executor.stop()  # first: the body freezes before anything else
            if self._listening:  # and it stops listening to the song
                await self.cancel_task(self._listening)
            self.create_task(self._act("body_plan", "done", input=dump(plan[:1])))
            plan = plan[1:]
        streamed = self._turn is not None and self._turn.streamed
        await self._interrupt()
        if stop or streamed:
            await self.push_frame(InterruptionFrame())  # its words leave the voice too
        asked = any(step.skill == "moves" for step in plan)  # "what moves can you do?": a line
        plan = tuple(step for step in plan if step.skill != "moves")
        if plan:
            line = ACKS.get(plan[0].skill)
            self._run_plan(plan, said=line)  # before the line: it reads the voice's direction first
            if line:
                await self._say_aside(line, act=False)  # the plan's act says it
        if asked:
            await self._say_aside(MOVES_LINE)

    def _run_plan(self, plan: Plan, sequence: int | None = None, said: str | None = None) -> None:
        task = self.create_task(self._body_plan(plan, sequence, said))
        self._plans.add(task)
        task.add_done_callback(self._plans.discard)

    async def _body_plan(self, plan: Plan, sequence: int | None, said: str | None = None) -> None:
        """Run a plan on the body; report a kernel's (``sequence``), or say a failed search.

        A reflex plan (no ``sequence``) ends in an ``acted``, with every line said for it.
        """
        result = await self._executor.run(plan)
        if sequence is not None:
            await self._report(sequence, result.status, text=result.text[:RESULT_TEXT] or None,
                               reason=result.reason)
        reflex = sequence is None
        if result.reason == "not_found":  # looked everywhere: say so, whoever asked (owner)
            said = f"{said} {NOT_FOUND_LINE}" if said else NOT_FOUND_LINE
            await self._say_aside(NOT_FOUND_LINE, act=not reflex)
            try:
                played = self._play_emote(NOT_FOUND_EMOTE, False)  # muted: the line says it
                if inspect.isawaitable(played):
                    await played
            except Exception as error:  # a gesture, not a requirement
                logger.warning("could not play %s: %s", NOT_FOUND_EMOTE, error)
            await self._executor.home()  # and it stays at the default pose
        if reflex:
            await self._act("body_plan", result.status, input=dump(plan), reason=result.reason,
                            said=said)

    # --- gestures --------------------------------------------------------------

    def _gesture_phase(self) -> str:
        """Return what a gesture may do: a turn on its way counts as speaking, the words join it."""
        if self._executor is None:
            return reflex.PASS
        running = self._turn is not None and not self._turn.ended
        return reflex.gesture_phase(self._now(), self._gestured_at,
                                    speaking=self._playback.busy or running,
                                    busy=self._executor.reflex_busy())

    def _gesture(self, reaction: str, heard: str) -> None:
        """Answer with the body alone, muted: a clip's own sound would trip the VAD (ponytail)."""
        self._gestured_at = self._now()
        self._last_clip = clip = reflex.pick(REACTIONS[reaction], self._last_clip)
        logger.info("gesture: %s for %s", clip, reaction)
        task = self.create_task(self._gesture_played(clip, heard))
        self._plans.add(task)
        task.add_done_callback(self._plans.discard)

    async def _gesture_played(self, clip: str, heard: str) -> None:
        """Tell the kernel, which never saw the words, as it plays: the next line may come mid-clip.

        ponytail: told "done" before it is; a clip that fails is logged, and memory says it moved.
        """
        told = self.create_task(self._act("body_plan", "done", reason=f"heard: {heard}",
                                          input={"steps": [{"skill": "emote", "arg": clip}]}))
        try:
            played = self._play_emote(clip, False)
            if inspect.isawaitable(played):
                await played
        except Exception as error:  # a gesture, not a requirement
            logger.warning("could not play %s: %s", clip, error)
        await told

    # --- tools -----------------------------------------------------------------

    def _start_tool(self, turn: _Turn, effect: dict) -> None:
        if effect["kind"] == "body_plan" and self._executor is not None:
            sequence = effect["sequence"]
            plan = None if effect.get("stale") else parse(effect.get("input"))
            if plan is None:
                self.create_task(self._report(sequence, "failed", reason="stale"))
            elif isinstance(plan, str):
                self.create_task(self._report(sequence, "failed", reason="invalid",
                                              text=plan[:RESULT_TEXT]))
            else:
                self._run_plan(plan, sequence)
            return
        if effect["kind"] == "listen_to_song" and self._song is not None and not effect.get("stale"):
            self._listening = self.create_task(self._listen(effect["sequence"]))
            return
        # The model names a feeling; the body picks its clip, never the last one again
        # (expressions.emotion). A clip's own name still plays.
        name = (effect.get("input") or {}).get("name")
        name = emotion(name, last=self._last_clip) or name
        ok = effect["kind"] == "emote" and name in EMOTIONS and not effect.get("stale")
        self._last_clip = name if ok else self._last_clip
        task = turn.emotes[effect["sequence"]] = self.create_task(
            self._emote(turn, effect["sequence"], name if ok else None))
        if ok and name in self._finish:
            self._finishing.add(task)
            task.add_done_callback(self._finishing.discard)

    async def _voice_tool(self, turn: _Turn, effect: dict) -> None:
        sequence, value = effect["sequence"], next(iter((effect.get("input") or {}).values()), "")
        if effect.get("stale"):
            await self._report(sequence, "failed", reason="stale")
            return
        if effect["kind"] == "voice_style":
            settings = style_settings(self._engine, value)
            if settings is None:
                await self._report(sequence, "failed", reason="unsupported")
                return
            await self.push_frame(VoiceStyleFrame(settings))
            await self._report(sequence, "done")
            return
        action = vocal_action(self._engine, value)
        if action is None or (action[0] == "sound" and self._play_sound is None):
            await self._report(sequence, "failed", reason="unsupported")
        elif action[0] == "say":
            await self._speak(turn, action[1] + " ")
            await self._report(sequence, "done")
        else:
            turn.emotes[sequence] = self.create_task(self._vocal(sequence, action[1]))

    async def _listen(self, sequence: int | None, line: str | None = None) -> None:
        """Listen once the reply that agreed (or ``line``) has played: its own voice must not be in the snippet.

        ``sequence`` is the listen_to_song effect to report; None when asked directly.
        """
        if line:
            await self._say_aside(line)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._playback.idle(), VOICE_WAIT_S)
        status, reason = await self._song.listen(self._say_aside, self._session.notes)
        if sequence is not None:
            await self._report(sequence, status, reason=reason)

    async def _say_aside(self, line: str, act: bool = True) -> None:
        """Say a line of the body's own, outside any turn; remembered so its echo is not a turn.

        The kernel hears it as an aside (``act``), unless a plan's act already carries it.
        """
        self._recent.append(line)
        await self._say(line)
        if act:
            self.create_task(self._act("aside", "done", said=line))

    async def _act(self, what: str, status: str, **fields) -> None:
        """Tell the kernel what the body did or said on its own (v1.1 ``acted``)."""
        try:
            await self._session.act(acted("act-" + uuid.uuid4().hex, what, status, **fields))
        except KernelUnavailableError as error:  # an act is a note for the model, never the voice
            logger.warning("could not report %s %s: %s", what, status, error)

    async def _vocal(self, sequence: int, move: str) -> None:
        """Play a move's own sound between words, holding the voice while it sounds."""
        playback = self._playback
        await playback.boundary()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(playback.idle(), VOICE_WAIT_S)
        playback.hold_voice()
        status = "done"
        try:
            await self._play_sound(move)
        except Exception:
            logger.exception("vocal %s failed", move)
            status = "failed"
        finally:
            playback.release_voice()
        await self._report(sequence, status)

    async def _emote(self, turn: _Turn, sequence: int, name: str | None) -> None:
        status, duration_ms = "failed", None
        if name is not None:
            playback = self._playback
            await playback.boundary()  # a move must not land mid-word
            if move_sound(turn.reply_text, voice_busy=False) and playback.busy:
                # Little to say, and it is about to be said: say it, then sound.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(playback.idle(), VOICE_WAIT_S)
            sound = move_sound(turn.reply_text, playback.busy) and name not in NO_SOUND
            if sound:
                playback.hold_voice()
            began = self._now()
            logger.info("emote: %s", name)  # pet.py: a reaction, and a turn that moved
            try:
                result = self._play_emote(name, sound)
                if inspect.isawaitable(result):
                    await result
                status, duration_ms = "done", _ms(self._now() - began)
            except Exception:
                logger.exception("emote %s failed", name)
            finally:
                if sound:
                    playback.release_voice()
        await self._report(sequence, status, duration_ms=duration_ms)

    # --- interruption ----------------------------------------------------------

    async def _interrupt(self) -> None:
        """Cancel the turn in hand and report, off the frame path, what it got to."""
        turn, task = self._turn, self._task
        self._turn = self._task = None
        if task and not task.done():
            await self.cancel_task(task)
        if turn is not None and not (turn.reported and all(t.done() for t in turn.emotes.values())):
            self.create_task(self._report_interrupted(turn))

    async def _report_interrupted(self, turn: _Turn) -> None:
        for sequence, task in turn.emotes.items():
            if not task.done() and task not in self._finishing:  # a short one reports itself
                await self.cancel_task(task)
                await self._report(sequence, "interrupted")
        if turn.reply_seq is None and not turn.reported:
            # Still streaming. From its first piece the body is acting on the reply,
            # so it reports what was heard, by turn, then stops the turn.
            if turn.streamed and not turn.reported and not turn.withdrawn:
                turn.reported = True
                await self._report(None, "interrupted", text=self._playback.text,
                                   timing=self._reply_timing(turn), turn_id=turn.turn_id)
            try:
                await self._session.stop(turn.turn_id)
            except KernelUnavailableError as error:
                logger.warning("could not stop turn %s: %s", turn.turn_id, error)
        elif not turn.reported:
            turn.reported = True
            await self._report(turn.reply_seq, "interrupted", text=self._playback.text,
                               timing=self._reply_timing(turn))

    async def _report(self, sequence: int | None, status: str, text: str | None = None,
                      timing: dict | None = None, reason: str | None = None,
                      turn_id: str | None = None, duration_ms: int | None = None) -> None:
        try:
            await self._session.result(sequence, status, text=text, timing=timing,
                                       reason=reason, turn_id=turn_id, duration_ms=duration_ms)
        except KernelUnavailableError as error:
            # The kernel times the effect out itself (max_s + 2 s); nothing is lost here.
            logger.warning("could not report %s for effect %s: %s", status, sequence, error)


def _ends_clause(frame: TTSTextFrame) -> bool:
    return frame.aggregated_by != AggregationType.WORD or frame.text.rstrip()[-1:] in ",;:.!?"


class BotSpeech(FrameProcessor):
    """After the output transport: playback into the interruption policy.

    Frames reach this point as their audio is played. Each played sentence is a
    sentence boundary, where the policy re-checks whether someone is still
    talking, and is recorded as heard.
    """

    def __init__(self, tracker: InterruptionTracker, playback: Playback,
                 now: Callable[[], float] = time.monotonic, **kwargs):
        """Feed ``tracker`` and ``playback`` from what the speaker played."""
        super().__init__(**kwargs)
        self._tracker, self._playback, self._now = tracker, playback, now
        self._speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Update the policy, then pass the frame on; yield if the policy says so."""
        await super().process_frame(frame, direction)
        t, must_yield = self._now(), False
        if isinstance(frame, BotStartedSpeakingFrame):
            self._speaking, self._playback.spoken = True, []
            self._playback.speaking = True
            must_yield = self._tracker.bot_started(t)
        elif isinstance(frame, TTSTextFrame):
            self._playback.spoken.append(frame.text)
            # Captioned speech arrives word by word; the policy and the body
            # language act at clause ends, as they did when text came by clause.
            if _ends_clause(frame):
                self._playback.mark_boundary()
                # A system frame overtakes queued data, so the last clause's text can
                # land after BotStopped: it must not reopen a finished response.
                if self._speaking:
                    must_yield = self._tracker.bot_started(t)  # the next clause starts now
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Pipecat 1.15 reports TTS audio stopped once per response (TTSStoppedFrame).
            self._speaking = self._playback.speaking = self._playback.queued = False
            self._playback.mark_boundary()
            self._tracker.bot_stopped(t)
            self._tracker.assistant_turn_stopped(t)
        elif isinstance(frame, InterruptionFrame):
            self._speaking = self._playback.speaking = self._playback.queued = False
            self._playback.mark_boundary()
            self._tracker.assistant_turn_stopped(t)
        await self.push_frame(frame, direction)
        if must_yield:
            await self.push_frame(InterruptionWorkerFrame(), FrameDirection.UPSTREAM)
