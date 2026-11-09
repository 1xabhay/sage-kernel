# Voice eval script

Read each line to the robot as you would talk to it. `record.py` shows one line at a time:
press Enter, say it, press Enter. At `...` pause for about a second mid-sentence, as when you
think: those lines measure whether the robot answers before you have finished.

## Before recording

The ASR beam is routed first (the robot lane's chip route, `make status` shows it), so every
clip is stereo: L the conference output, R the ASR beam, and the manifest says so. Without the
route, add `--unrouted`. Robot app stopped (the recorder needs the mic); robot awake.

## Two sessions, on different days

Session 1 is the dev set: tune on it. Session 2, on another day, is the test set: freeze it
(`uv run python eval/score.py freeze eval/recordings --session <S2 name>`, which writes its hash
to `eval/FROZEN.md`) before any scorer runs on it, and only report on it. Each session is about
20 minutes. Use one `--session` name per pass, prefixed with the day, e.g. `1-quiet-0.5`.

Per session, these passes (`make eval-voice-record ARGS="..."`, `REACHY_MINI_HOST` set):

1. Quiet room, 0.5 m, all lines: `--distance 0.5 --condition quiet --session 1-quiet-0.5`
2. Quiet room, 1.5 m, all lines: `--distance 1.5 --condition quiet --session 1-quiet-1.5`
3. Music playing (a song at normal listening level from a speaker ~2 m from the robot), 0.5 m,
   blocks W, C, X, N, S: `--distance 0.5 --condition music --session 1-music-0.5 --only ...`
4. Music playing, 1.5 m, blocks W, C, X, N, S: `--distance 1.5 --condition music ...`
5. Over the robot's voice, quiet, 1.0 m from the speaker: block T with `--tts` (it plays a
   Kokoro line through the robot; start talking while it speaks).
6. Songs, nobody talking: block M with `--speaker music --condition music --seconds 12`.
7. Session 1 only: another person reads 20 lines, quiet, 1.5 m (`--speaker other --only ...`);
   TV or a podcast, 10 untranscribed clips (`--speaker other --condition tv --clips 10`).

Impostors, unattended, any time (no owner needed): put 300 or more 16 kHz WAVs of other voices
in a folder (LibriSpeech test-clean, converted from FLAC; Kokoro's other voices, made with the
TTS service), set the speaker ~1.5 m from the robot, and run
`uv run --with sounddevice python eval/record.py --speaker impostor --condition replay
--distance 1.5 --session impostors-1 --replay <folder>`. Each file plays from the Mac and the
robot's mic records it.

Add `--mac` to record the Mac's mic at the same time (needs `sounddevice`). A fluffed line:
record it again; the second take is kept apart (`-2`), delete the bad WAV and its manifest line.
The recordings are your voice, which is biometric: they stay in `eval/recordings/`, git-ignored.
The ledger holds metrics only, never transcripts.

## Replay set (no owner needed)

A labelled robot-mic set from public speech (`eval/replay.py`): 150 LibriSpeech test-clean
utterances (CC BY 4.0, 40 readers, 75 female and 75 male, about 2 to 10 s each) and 30 command
and name lines from this script read by twelve Kokoro voices (`"synthetic": true`), played from
a Mac speaker and recorded by the robot. All of it lands in `eval/replay/`, git-ignored.

Once, with the speaker that will play plugged in and chosen as the Mac's output (the clips are
resampled to its rate), and Kokoro up for the synthetic lines (`make -C services up`; without
it add `ARGS=--skip-synthetic`):

    make eval-voice-replay-fetch        # 346 MB download, md5-checked, cached in eval/replay/.cache

Per run (the voice stopped, `REACHY_MINI_HOST` set, the beam routed: `make -C robot beam-on`):

1. Put the speaker at the distance, facing the robot, at its head height, and leave the Mac's
   volume alone from here on: the preflight notes device, rate and volume in each run.json.
2. Level check, 5 clips, about 40 s, into `eval/replay/check/` (replaced each time):
   `make eval-voice-replay DIST=0.5 ARGS="--limit 5"`. Every take should print no `level:`
   line: no clipping, speech peaks between -6 and -20 dBFS on L and on R. Too hot or too quiet:
   change the Mac's volume and check again.
3. The run, unattended: `make eval-voice-replay DIST=0.5`. 180 clips at about 6.5 s each
   (speech, 0.5 s of silence either side, 0.5 s of tail): about 20 minutes.
4. Move the speaker and repeat for 1.5 m (`DIST=1.5`). With music under the speech, from one
   song file looped at a set SNR: `make eval-voice-replay DIST=0.5 MUSIC=$HOME/Music/song.wav SNR=10`
   (the session name says `music` and the SNR). Four runs, quiet and music at 0.5 m and 1.5 m,
   take about 80 minutes.

`make eval-voice-replay` runs the preflight first and stops if any line fails: robot daemon up,
beam routed (`make status`'s beam line), `make -C robot check` clean, no `python -m
reachy_sage_voice` running, and the Mac's output not muted.

Each take is aligned to what played by cross-correlation: `delay_s` (L and R) runs from the
Mac's `play()` to the robot's mic, and `set/<session>/run.json` keeps its median and spread
with the device's output latency (`playback.latency_s`). The median less that latency is the
room-to-robot delay the dance lead time needs. The untrimmed takes stay in `eval/replay/raw/`.

The set, `eval/replay/set/`, has record.py's manifest format (wav, text, distance_m,
condition, session, channels, sha256) with `speaker: "replay"`, so every scorer reads it; add
`--speaker replay` where the owner is the default:

- STT bake-off: `uv run python eval/score.py stt eval/replay/set --speaker replay --channel R
  --stt-url URL --option "<engine>"`, or `--hyp` transcripts made elsewhere, per engine.
- Beam against conference, the same utterances: `uv run python eval/score.py paired
  eval/replay/set --speaker replay --a L --b R --stt-url http://127.0.0.1:8781`.

Use one session per run (the default name is the date, distance and condition). A run done
twice appends twice; delete a bad session's folder and its manifest lines.

## Targets (what the two sessions add up to)

| Measure | Target | From |
|---|---|---|
| Name tokens | 200 or more | blocks W + N (43 names) x 4 conditions x 2 sessions = 344 |
| Short commands, per condition | 150 or more | block C (40) x 2 distances x 2 sessions = 160 |
| Not-command negatives | 64 | block X (8) x 4 x 2 |
| Impostor clips | 300 or more | the unattended replay |
| Held-out owner target trials | 100 or more | session 2 alone has over 300 owner clips |
| Song clips, 12 s | 12 | block M x 2 sessions |

## Lines

Short replies

```
S01  Yes.
S02  No.
S03  Stop.
S04  Wait.
S05  Okay.
S06  Thanks.
S07  Not now.
S08  Go on.
S09  Louder, please.
S10  That's enough.
S11  Never mind.
S12  Sure, why not.
```

The robot's names

```
N01  Sage, are you listening?
N02  Hey Sage, what's playing?
N03  Reachy, look at me.
N04  Good morning, Reachy.
N05  Sage, stop talking for a second.
N06  I think Reachy is a good name for you.
N07  Sage, can you hear the music?
N08  Thanks Reachy, that was fun.
N09  Is your name Sage or Reachy?
N10  Okay Sage, tell me something about this song.
```

Names, dense (the wake word in every position)

```
W01  Sage.
W02  Reachy.
W03  Hey Sage.
W04  Hey Reachy.
W05  Sage, what time is it?
W06  Reachy, are you awake?
W07  What do you think, Sage?
W08  Can you hear me, Reachy?
W09  I told my friend about you, Sage.
W10  Reachy, come on, look over here.
W11  Is that you talking, Sage?
W12  Okay Reachy, that's enough for now.
W13  Sage, Sage, are you there?
W14  Reachy, Sage, whichever name you like.
W15  So Sage, what did you hear?
W16  Tell me, Reachy, what song is this?
W17  Good night, Sage.
W18  Thanks, Reachy.
W19  Sage, play it again.
W20  Did you catch that, Reachy?
W21  Listen, Sage, I have a question.
W22  Reachy, turn the volume down.
W23  Sorry Sage, I was talking to someone else.
W24  You did great today, Reachy.
W25  Sage, do you remember my name?
W26  Hey, Reachy, over here.
W27  What's up, Sage?
W28  Reachy, wake up.
W29  I think Sage is listening now.
W30  Was that you, Reachy, or the TV?
```

Commands, one at a time or chained

```
C01  Look left.
C02  Look right.
C03  Look up.
C04  Look down.
C05  Turn around.
C06  Stop.
C07  Search for me.
C08  Track me.
C09  Listen to this song.
C10  Sage, look left.
C11  Reachy, look right.
C12  Sage, look up.
C13  Reachy, look down.
C14  Sage, turn around.
C15  Sage, stop.
C16  Reachy, stop that.
C17  Search for me, then track me.
C18  Look left, then look right.
C19  Look up and then look down.
C20  Turn around and look for me.
C21  Sage, listen to this song.
C22  Reachy, listen to this song and tell me what it is.
C23  Find me, then follow me.
C24  Stop tracking me.
C25  Look at me.
C26  Look straight ahead.
C27  Turn left a little.
C28  Turn right a bit.
C29  Look left, look right, then look at me.
C30  Stop moving.
C31  Keep following me.
C32  Search for me and then track me.
C33  Sage, look down, then look up.
C34  Turn around, then search for me.
C35  Listen to this song, then tell me who sings it.
C36  Stop, stop.
C37  Reachy, track me.
C38  Reachy, search for me.
C39  Look over there, to the left.
C40  Stop listening.
```

Not commands (they name a move, and must not cause one)

```
X01  I looked left yesterday and saw the bus.
X02  My cat always turns around before it lies down.
X03  Don't stop believing is a great song.
X04  She told me to look up the word in a dictionary.
X05  I listened to this song all morning.
X06  He keeps tracking his steps on his phone.
X07  Searching for my keys took forever.
X08  The robot in the movie looked right at the camera.
```

Over the robot's voice (with `--tts`: start talking while it speaks)

```
T01  Stop.
T02  Wait.
T03  Sage, stop.
T04  Never mind.
T05  No, that's wrong.
T06  Reachy, be quiet.
T07  Hold on a second.
T08  Okay, I get it.
T09  Sage, listen to this song.
T10  Look at me.
T11  That's enough, thanks.
T12  Wait, say that again.
```

Songs (each from a speaker about 2 m away, nobody talking, `--seconds 12`)

```
M01  Billie Jean
M02  Blinding Lights
M03  Smells Like Teen Spirit
M04  Billie Jean
M05  Blinding Lights
M06  Smells Like Teen Spirit
```

Questions about a song

```
Q01  Who produced Billie Jean?
Q02  When did Blinding Lights come out?
Q03  What does the chorus of Smells Like Teen Spirit say?
Q04  Which album is Billie Jean on?
Q05  Who wrote Blinding Lights?
Q06  What kind of music is this?
Q07  How long was Blinding Lights on the charts?
Q08  Who played drums on Smells Like Teen Spirit?
Q09  What year was Nevermind released?
Q10  Is The Weeknd touring this year?
Q11  What's this song about?
Q12  Who is singing right now?
```

Long sentences with pauses

```
L01  I was thinking about the song we heard yesterday ... and I wonder who played the bass on it.
L02  Can you tell me ... when that album came out?
L03  So the thing I really want to know is ... whether they ever played it live.
L04  My favorite part is the bridge ... where everything drops out except the drums.
L05  If you had to pick one song from the eighties ... which one would it be?
L06  I heard that the producer ... recorded the vocals in one take.
L07  Tell me about the band ... and how they got started.
L08  Before you answer ... think about what the lyrics are really saying.
L09  When this comes on the radio ... I always turn it up.
L10  The first time I heard it was in a car ... driving to the beach with my friends.
L11  Wait, I mean ... the other version, the acoustic one.
L12  Do you know if they ... ever released a remix of it?
```

Numbers and proper nouns

```
P01  It came out in 1982.
P02  That's the 3rd track on the album.
P03  It sold over 20 million copies.
P04  Play something from 2019.
P05  Quincy Jones produced Thriller.
P06  Max Martin worked on After Hours.
P07  Butch Vig produced Nevermind in Van Nuys.
P08  Kurt Cobain, Krist Novoselic and Dave Grohl.
P09  Michael Jackson recorded it in Los Angeles.
P10  The song is 4 minutes and 54 seconds long.
P11  Abel Tesfaye is The Weeknd's real name.
P12  Set a timer for 15 minutes.
```
