"""Reachy's voice, assembled: mic -> onset VAD -> STT -> Sage -> TTS -> speaker.

Services are the bench-chosen local ones, each behind an OpenAI-compatible URL
on this machine, in Docker (services/compose.yaml): Parakeet TDT for speech
to text on 8781, Kokoro for speech on 8782, captioned so an interruption
reports the exact words heard. Every URL
and the kernel token come from the environment, with no defaults, so nothing
here can reach the internet unless someone configures it to.

    SAGE_VOICE_KERNEL_URL    the kernel's device listener (protocol v1)
    SAGE_VOICE_KERNEL_TOKEN  this robot's token from the kernel's SAGE_DEVICES
    SAGE_VOICE_STT_URL       Parakeet, OpenAI-compatible (with or without ``/v1``)
    SAGE_VOICE_TTS_URL       Kokoro (``.../v1``; captions at ``/dev/captioned_speech``)
    SAGE_VOICE_STT_MODEL     default parakeet-tdt-0.6b-v2
    SAGE_VOICE_TTS_MODEL     default kokoro
    SAGE_VOICE_TTS_VOICE     default azelma
    SAGE_VOICE_PRESENCE      1 to follow a face from the start and report seen/lost; default 0
    SAGE_VOICE_WATCH         0 for no presence when not following (the detector off); default 1
    SAGE_VOICE_TURN_END_MS   silence that ends a turn; default 600, measured on the
                             owner's live turns (``eval/turn_taking.py``)
    SAGE_VOICE_HOLD_MS       a turn whose transcript stops mid-sentence waits this long
                             in all from the end of speech for the rest; default 0 (off)
    SAGE_VOICE_BARGE_IN_MS   speech that must last before the bot yields; default 200
    SAGE_VOICE_TURN          silence (default): the VAD's silence ends a turn; smart:
                             Smart Turn v3 decides, after a 0.2 s VAD stop,
                             and the STT transcribes the whole turn once
    SAGE_VOICE_TURN_TIMEOUT_MS  smart only: silence that ends a turn Smart Turn
                             calls incomplete; default 1500
    SAGE_VOICE_MIC_CHANNEL   mean (default), 0 or 1 of the mic array's stereo
    SAGE_VOICE_RECORD_DIR    save each utterance's raw stereo there as WAV; default off
    SAGE_VOICE_IDLE          0 to turn off breathing when idle and the glance toward a voice
                             (executor.py); default 1
    SAGE_VOICE_LISTEN_CUES   0 to turn off the thinking glance before a reply, and with the
                             arbiter the antennas at a voice and the nod (executor.py);
                             default 1
    SAGE_VOICE_ADDRESSED     1 to answer only when addressed (addressed.py); default 0
    SAGE_VOICE_ALIASES       names besides Sage it answers to, comma-separated; default Reachy
    SAGE_VOICE_EXCHANGE_S    seconds after it spoke that need no name or face; default 8
    SAGE_VOICE_NAMES         names to hear and say right: written:spoken:heard|heard,...
                             (names.py); default none
    SAGE_VOICE_RESEARCH_URL  the song research service (services/research); declares
                             listen_to_song (song.py); default off
    SAGE_VOICE_MIC_RECOVERY  1 (default): a quiet mic resets the robot's media
                             (media_reset.py) before the link counts as lost; 0 off
"""

import asyncio
import inspect
import io
import logging
import math
import os
import random
import time
import wave
from collections.abc import Callable, Mapping

from pipecat.audio.turn.base_turn_analyzer import BaseTurnAnalyzer
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    ErrorFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_start import VADUserTurnStartStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner
from sage_device import names
from sage_device.kernel import KernelClient, KernelUnavailableError, Session

from reachy_sage_voice import clause, health, turn_end
from reachy_sage_voice.addressed import addressed_filter
from reachy_sage_voice.interruption import InterruptionTracker
from reachy_sage_voice.manifest import manifest
from reachy_sage_voice.onset import (
    SpeechOnsetMixin,
    build_speech_onset_silero,
    make_speech_onset_handler,
)
from reachy_sage_voice.owner import OwnerCheck, verified_onset
from reachy_sage_voice.presence import watch_presence
from reachy_sage_voice.sage import EMOTE_FINISH_S, BotSpeech, Playback, SageProcessor
from reachy_sage_voice.song import ResearchClient, SongListener
from reachy_sage_voice.transport import (
    MIC_CHANNELS,
    ReachyInputTransport,
    ReachyMedia,
    ReachyOutputTransport,
)
from reachy_sage_voice.tts import DOWN_EMOTE, KokoroTTSService

logger = logging.getLogger(__name__)

REQUIRED = ("SAGE_VOICE_KERNEL_URL", "SAGE_VOICE_KERNEL_TOKEN",
            "SAGE_VOICE_STT_URL", "SAGE_VOICE_TTS_URL")
#: One person, one mic: the speaker the interruption policy tracks.
SPEAKER = "you"
#: Local services check no key, but the OpenAI client insists on one.
_NO_KEY = "local"
#: What ends a user turn (SAGE_VOICE_TURN).
TURNS = ("silence", "smart")
#: The VAD stop Smart Turn is tuned for (Pipecat's default).
SMART_VAD_STOP_S = 0.2
#: Said when the STT cannot be reached; with the TTS down too, its emote shows it instead.
CANT_HEAR = "Sorry, I can't hear right now."
#: Said at most this often while the STT stays down: its own voice is another failed segment.
CANT_HEAR_EVERY_S = 30.0


def openai_base(url: str) -> str:
    """Return an OpenAI client base URL (``.../v1``), whether or not it had one."""
    url = url.rstrip("/")
    return url if url.endswith("/v1") else url + "/v1"


class ConfigError(Exception):
    """A required setting is missing from the environment."""


class RobotLinkLostError(Exception):
    """The robot stopped answering: its mic went quiet or a robot call lost the connection."""


def watch_link(play: Callable, link_lost: Callable[[str], None]) -> Callable:
    """``play`` (an emote or a sound), reporting a ConnectionError as the robot link lost.

    It still raises, so the effect fails as before; the report ends the run.
    """
    async def watched(*args):
        try:
            result = play(*args)
            return await result if inspect.isawaitable(result) else result
        except ConnectionError as error:  # reachy_mini: "Lost connection with the server."
            link_lost(f"the robot's motion link: {error}")
            raise
    return watched


def smart_turn_analyzer(stop_secs: float) -> BaseTurnAnalyzer:
    """Smart Turn v3, the weights bundled with Pipecat, on CPU."""
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    return LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=stop_secs))


def user_turns(analyzer: BaseTurnAnalyzer) -> UserTurnProcessor:
    """Pipecat's user turns, ended by ``analyzer`` alone. Barge-in stays with the onset policy.

    It runs before the STT, so the turn ends on the analyzer's verdict, not on a
    transcript (``wait_for_transcript=False``), and no transcript can start a turn.
    """
    return UserTurnProcessor(user_turn_strategies=UserTurnStrategies(
        start=[VADUserTurnStartStrategy(enable_interruptions=False)],
        stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer,
                                               wait_for_transcript=False)]))


def merge_segments(segments: list[bytes]) -> bytes:
    """Segments as one: WAVs (a header each) into one WAV, raw PCM end to end."""
    if not segments[0].startswith(b"RIFF"):
        return b"".join(segments)
    frames = []
    for segment in segments:
        with wave.open(io.BytesIO(segment)) as w:
            params, data = w.getparams(), w.readframes(w.getnframes())
            frames.append(data)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setparams(params)
        w.writeframes(b"".join(frames))
    return out.getvalue()


class MergeBacklog:
    """Mix in ahead of a segmented STT: segments still queued go along with the one it takes.

    Pipecat queues every VAD segment for one serial task; when a request takes longer
    than the gap between segments the queue grew without bound (stt_ms up to 52 s, live
    2026-10-22). Merged, the waiting words arrive in one transcript, one request later, numbered
    as the newest segment so Pipecat finalizes it.
    ponytail: drains SegmentedSTTService's private ``_segment_queue`` of (audio, number) and sets
    its ``_transcribing_segment`` (Pipecat 1.15, pinned).
    A failed request (the STT down) says CANT_HEAR, at most every CANT_HEAR_EVERY_S.
    """

    _cant_hear_at = -math.inf

    async def run_stt(self, audio: bytes):
        """Transcribe ``audio`` and every segment queued behind it, as one."""
        waiting, stop = [audio], False
        while not self._segment_queue.empty():
            segment = self._segment_queue.get_nowait()
            if segment is None:
                stop = True  # the stop sentinel: it must still end the task
                break
            queued, self._transcribing_segment = segment
            waiting.append(queued)
        if stop:
            self._segment_queue.put_nowait(None)
        if len(waiting) > 1:
            logger.info("STT behind: %d segments merged into one request", len(waiting))
        async for frame in super().run_stt(merge_segments(waiting)):
            if isinstance(frame, TranscriptionFrame):
                _log_scores(frame.result)
            health.report("voice.stt", not isinstance(frame, ErrorFrame),
                          "down" if isinstance(frame, ErrorFrame) else "up")
            if (isinstance(frame, ErrorFrame)
                    and time.monotonic() - self._cant_hear_at >= CANT_HEAR_EVERY_S):
                self._cant_hear_at = time.monotonic()
                yield TTSSpeakFrame(CANT_HEAR)
            yield frame


def _log_scores(result) -> None:
    """Log the STT's word confidence and voiceprint score, for the noise trace (eval/noise.py)."""
    conf, score = getattr(result, "confidence", None), getattr(result, "similarity", None)
    if conf is None and score is None:
        return  # an STT that sends neither
    conf = conf or {}
    def num(x):
        return "-" if x is None else f"{x:.2f}"
    words = " ".join(num(p) for _, p in conf.get("words", []))
    logger.info("stt: confidence mean %s min %s, similarity %s, words %s",
                num(conf.get("mean")), num(conf.get("min")), num(score), words)


class SegmentSTTService(MergeBacklog, OpenAISTTService):
    """The segmented STT, one VAD segment per request unless a backlog merges them."""


class TurnSTTService(MergeBacklog, OpenAISTTService):
    """The segmented STT, segmenting on the user turn rather than on each VAD segment.

    A 0.2 s VAD stop cuts a turn into pieces too short to transcribe alone. Here
    the segment opens on ``UserStartedSpeakingFrame`` (with Pipecat's 1 s of
    pre-roll) and closes on ``UserStoppedSpeakingFrame``: one transcript per turn.
    ponytail: overrides SegmentedSTTService's two private segment hooks (Pipecat
    1.15, pinned); Pipecat has no turn-keyed segmented STT to use instead.
    """

    _in_turn = False

    async def _handle_user_started_speaking(self, frame):
        pass  # a VAD segment: the turn goes on

    async def _handle_user_stopped_speaking(self, frame):
        pass

    async def process_frame(self, frame, direction):
        """Segment on the user turn frames, then as any segmented STT."""
        await super().process_frame(frame, direction)
        if isinstance(frame, UserStartedSpeakingFrame):
            self._in_turn = True
            await SegmentedSTTService._handle_user_started_speaking(self, frame)
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._in_turn = False
            await SegmentedSTTService._handle_user_stopped_speaking(self, frame)

    async def process_audio_frame(self, frame, direction):
        """Buffer as SegmentedSTTService does, keeping the whole turn, pauses included."""
        # STTService clears _user_speaking at every VAD stop, and Segmented trims on it.
        self._user_speaking = self._in_turn
        await super().process_audio_frame(frame, direction)


def build(media: ReachyMedia, play_emote: Callable, moves: Mapping,
          env: Mapping[str, str] = os.environ, play_sound: Callable | None = None,
          face: Callable[[], bool] | None = None, owner: OwnerCheck | None = None,
          link_lost: Callable[[str], None] | None = None,
          recover: Callable | None = None, executor=None,
          session: Session | None = None) -> Pipeline:
    """Assemble the pipeline. Connects to nothing until it runs.

    ``moves`` is the declared vocabulary from the moves library (``load_moves``);
    ``face`` reads the face tracker, for SAGE_VOICE_ADDRESSED; ``owner`` is the
    STT's speaker check when it holds a voiceprint, so only the owner barges in.
    ``link_lost(reason)`` hears a mic that has gone quiet, after ``recover(attempt)`` failed.
    ``executor`` moves the body on command (``executor.py``): with it the manifest declares the
    body and body_plan, and the executor hears the mic for the dance.
    ``session`` is this body's kernel session (``kernel_session``), made here if not given.
    """
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise ConfigError("set " + ", ".join(missing))
    mic_channel = env.get("SAGE_VOICE_MIC_CHANNEL", "mean")
    if mic_channel not in MIC_CHANNELS:
        raise ConfigError("SAGE_VOICE_MIC_CHANNEL must be one of " + ", ".join(MIC_CHANNELS))
    turn = env.get("SAGE_VOICE_TURN", "silence")
    if turn not in TURNS:
        raise ConfigError("SAGE_VOICE_TURN must be one of " + ", ".join(TURNS))
    smart = turn == "smart"

    tracker, playback = InterruptionTracker(), Playback()
    mic = ReachyInputTransport(media, TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000),
                               mic_channel, env.get("SAGE_VOICE_RECORD_DIR") or None,
                               on_silent=link_lost, recover=recover)
    if executor is not None:
        executor.audio = mic.snippet
    research = env.get("SAGE_VOICE_RESEARCH_URL")
    song = SongListener(ResearchClient(research), mic.snippet,
                        music_present=_music_present()) if research else None

    # Silence that ends a turn: an earlier voice bot's 450 ms until recordings of the owner set
    # it (turn_end.py), or Smart Turn (SAGE_VOICE_TURN=smart).
    # With Smart Turn, the VAD only marks pauses and the model ends the turn.
    turn_end_ms = int(env.get("SAGE_VOICE_TURN_END_MS", turn_end.DEFAULT_MS))
    stop_secs = SMART_VAD_STOP_S if smart else turn_end_ms / 1000
    barge_in_ms = int(env.get("SAGE_VOICE_BARGE_IN_MS", SpeechOnsetMixin.min_onset_ms))
    analyzer = build_speech_onset_silero(VADParams(stop_secs=stop_secs), barge_in_ms)
    vad = VADProcessor(vad_analyzer=analyzer)
    # Pushed upstream from the VAD, the interruption reaches the pipeline
    # worker, which injects it at the source ahead of any queue.
    on_onset = make_speech_onset_handler(tracker, vad.push_frame)

    def onset():
        return on_onset(SPEAKER)
    # ponytail: a verified onset can land after its own offset; the tracker's
    # STALE_ONSET_S retires it. Track verified offsets if the robot shows yields.
    checked = verified_onset(onset, owner, mic.snippet) if owner else onset
    # While it listens to a song and says what it is, the music is not someone talking.
    analyzer.set_onset_handlers(
        on_detected=lambda: None if song and song.busy else checked(),
        on_cleared=lambda: tracker.user_offset(time.monotonic(), SPEAKER))
    if executor is not None:  # not over its own voice, or a song it listens to
        def hushed() -> bool:
            return playback.busy or bool(song and song.busy)
        analyzer.set_reflex_handlers(on_voice=lambda: executor.perk(hushed()),
                                     on_pause=lambda: executor.nod(hushed()),
                                     on_quiet=lambda: executor.unperk())

    stt = (TurnSTTService if smart else SegmentSTTService)(
        api_key=_NO_KEY, base_url=openai_base(env["SAGE_VOICE_STT_URL"]),
        # The segment ends in the VAD's stop silence already: pad only up to Pipecat's 0.5 s.
        trailing_silence_secs=max(0.0, 0.5 - stop_secs),
        settings=OpenAISTTService.Settings(
            model=env.get("SAGE_VOICE_STT_MODEL", "parakeet-tdt-0.6b-v2")))

    known = names.parse(env.get("SAGE_VOICE_NAMES", ""))
    tts = KokoroTTSService(base_url=env["SAGE_VOICE_TTS_URL"], names=known,
                           voice=env.get("SAGE_VOICE_TTS_VOICE", "azelma"),
                           model=env.get("SAGE_VOICE_TTS_MODEL", "kokoro"),
                           on_down=lambda: play_emote(DOWN_EMOTE, True))
    clause.install(tts)

    session = session or kernel_session(env, moves, executor)
    gate = addressed_filter(env, face)
    return Pipeline([
        mic,
        vad,
        *([user_turns(smart_turn_analyzer(
            int(env.get("SAGE_VOICE_TURN_TIMEOUT_MS", 1500)) / 1000))] if smart else []),
        stt,
        *([gate] if gate else []),
        SageProcessor(session, play_emote, playback, play_sound=play_sound, song=song, names=known,
                      executor=executor, finish=frozenset(
                          name for name, move in moves.items()
                          if getattr(move, "duration", math.inf) <= EMOTE_FINISH_S),
                      hold_s=int(env.get("SAGE_VOICE_HOLD_MS", 0)) / 1000, rng=random),
        tts,
        ReachyOutputTransport(media, TransportParams(
            audio_out_enabled=True, audio_out_sample_rate=media.get_output_audio_samplerate())),
        BotSpeech(tracker, playback),
    ])


def _music_present() -> Callable[[bytes], bool] | None:
    """Return the dance lane's ``beat.music_present`` once it has landed; None until then."""
    try:
        from reachy_sage_voice.beat import music_present
    except ImportError:
        return None
    return music_present


def kernel_session(env: Mapping[str, str], moves: Mapping, executor=None) -> Session:
    """Open this body's one kernel session: one hello, one set of features, for all it sends.

    Unset settings make an unusable client; ``build`` refuses them first (``REQUIRED``).
    """
    client = KernelClient(env.get("SAGE_VOICE_KERNEL_URL", ""),
                          env.get("SAGE_VOICE_KERNEL_TOKEN", ""))
    return Session(client,
                   manifest(moves, song=bool(env.get("SAGE_VOICE_RESEARCH_URL")),
                            body=executor is not None))


async def report_presence(session, event: dict) -> None:
    """Send one presence edge; an unreachable kernel costs the edge, never the voice."""
    try:
        await session.presence(event["state"], event["confidence"])
    except KernelUnavailableError as error:
        logger.warning("could not report presence %s: %s", event["state"], error)


async def run(media: ReachyMedia, play_emote: Callable, moves: Mapping,
              env: Mapping[str, str] = os.environ, play_sound: Callable | None = None,
              presence: Callable[[], bool] | None = None, observers: list | None = None,
              face: Callable[[], bool] | None = None,
              recover: Callable | None = None, executor=None) -> None:
    """Run the voice until cancelled. Always on: no idle timeout.

    A lost robot link (a quiet mic, or a ConnectionError from an emote or a
    sound) ends it with ``RobotLinkLostError``: deaf, it must fail loudly, not linger.
    A quiet mic first gets ``recover(attempt)`` (a media reset), unless
    SAGE_VOICE_MIC_RECOVERY=0.
    ``presence`` reads the face tracker; when given, seen/lost edges go to the kernel.
    ``observers`` watch the frames. The executor breathes beside the pipeline.
    """
    lost = asyncio.get_running_loop().create_future()

    def link_lost(reason: str) -> None:
        if not lost.done():
            lost.set_result(reason)

    play_emote = watch_link(play_emote, link_lost)
    play_sound = play_sound and watch_link(play_sound, link_lost)
    # Pipecat's own SIGINT handler starts one graceful cancel and swallows every
    # Ctrl-C after it, so a stuck shutdown took SIGTERM (live 2026-10-22). Without
    # it asyncio's applies: the first Ctrl-C cancels, the next raises KeyboardInterrupt.
    runner = WorkerRunner(handle_sigint=False)
    owner = OwnerCheck(env["SAGE_VOICE_STT_URL"]) if env.get("SAGE_VOICE_STT_URL") else None
    try:
        enrolled = owner is not None and await asyncio.to_thread(owner.enrolled)
    except (OSError, ValueError):
        enrolled = False  # build() names a missing setting; an STT still starting has no voiceprint yet
    if enrolled:
        logger.info("the STT holds a voiceprint: only the owner is heard or barges in")
    if env.get("SAGE_VOICE_MIC_RECOVERY", "1") == "0":
        recover = None
    session = kernel_session(env, moves, executor)  # the voice's and presence's: one hello
    pipeline = build(media, play_emote, moves, env, play_sound, face, owner if enrolled else None,
                     link_lost, recover, executor=executor, session=session)
    await runner.add_workers(PipelineWorker(pipeline, idle_timeout_secs=None,
                                                observers=observers))
    if executor is not None:
        executor.link_lost = link_lost  # a motion link refusing commands for good ends the run
    breathing = asyncio.ensure_future(executor.breathe()) if executor is not None else None
    watch = None
    if presence is not None:
        watch = asyncio.ensure_future(
            watch_presence(presence, lambda event: report_presence(session, event),
                           muted=lambda: session.watching))
    work = asyncio.ensure_future(runner.run())
    try:
        await asyncio.wait({work, lost}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (watch, breathing):
            if task:
                task.cancel()
        if not work.done():
            await runner.cancel(lost.result() if lost.done() else "stopped")
            await work
    if lost.done():
        raise RobotLinkLostError(lost.result())
    work.result()
