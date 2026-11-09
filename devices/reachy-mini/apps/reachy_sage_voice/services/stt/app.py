"""Speech to text: Parakeet TDT 0.6B v2 (int8, ONNX, CPU) behind OpenAI's transcription endpoint.

Pipecat's OpenAISTTService posts a WAV segment to POST /v1/audio/transcriptions as multipart
(`file`, `model`, and optionally `language`, `prompt`, `temperature`, `response_format`) and
reads `.text` from the JSON; `confidence` (per word) and `similarity` (to the voiceprint) ride
beside it for the noise trace (eval/noise.py), and old clients ignore them. The other fields are
accepted and ignored: this server has one model.
Adapted from an earlier Pipecat voice bot's STT service.

Owner only: with a voiceprint at SAGE_VOICEPRINT (`make enroll`), a segment in any other voice,
or music, comes back as no text, and POST /v1/speaker/verify says whether a clip is the owner
(the voice's barge-in asks it). The speaker model is WeSpeaker ResNet34-LM through onnx-asr.
"""
import io
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import onnx_asr
import soundfile as sf
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from onnx_asr.loader import Manager

MODEL = "nemo-parakeet-tdt-0.6b-v2"
SPEAKER_MODEL = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
RATE = 16000
VOICEPRINT = Path(os.environ.get("SAGE_VOICEPRINT", "/profiles/owner.npy"))
# Cosine similarity to the voiceprint that counts as the owner. ponytail: 0.5 is WeSpeaker's usual
# range on clean audio; calibrate on the robot's mic with `make enroll` (it prints each recording's).
THRESHOLD = float(os.environ.get("SAGE_SPEAKER_THRESHOLD", "0.5"))
MIN_ENROLL_S = 1.0  # shorter recordings say too little about a voice
_model = _speaker = _voiceprint = None


def load():
    """The model, from the Hugging Face cache (baked into the image; fetched on first native run)."""
    return onnx_asr.load_model(MODEL, quantization="int8", providers=["CPUExecutionProvider"])


def load_speaker():
    """The speaker embedding model, cached as the ASR model is."""
    return Manager(providers=["CPUExecutionProvider"]).create_se(SPEAKER_MODEL)


@asynccontextmanager
async def lifespan(_app):
    global _model, _speaker, _voiceprint
    _voiceprint = np.load(VOICEPRINT) if VOICEPRINT.is_file() else None
    _model, _speaker = load(), load_speaker() if _voiceprint is not None else None
    yield


app = FastAPI(lifespan=lifespan)


def _unit(vector):
    return vector / (np.linalg.norm(vector) or 1.0)


def similarity(audio) -> float:
    """How close 16 kHz mono `audio` is to the voiceprint, from -1 to 1."""
    return float(_unit(_speaker.embedding(audio)) @ _unit(_voiceprint))


def _read(file: UploadFile):
    try:
        audio, rate = sf.read(io.BytesIO(file.file.read()), dtype="float32", always_2d=True)
    except Exception:
        raise HTTPException(400, "not a readable audio file")
    audio = audio.mean(axis=1)
    if rate != RATE:  # ponytail: linear resampling; Pipecat sends 16 kHz, so this is rarely used
        audio = np.interp(np.arange(0, len(audio), rate / RATE), np.arange(len(audio)), audio).astype("float32")
    return audio


@app.get("/health")
def health():
    return {"ready": _model is not None, "enrolled": _voiceprint is not None}


@app.post("/v1/audio/transcriptions")
def transcribe(file: UploadFile = File(...), model: str = Form(""), language: str = Form(""),
               prompt: str = Form(""), temperature: str = Form(""), response_format: str = Form("json")):
    """Sync on purpose: FastAPI runs it on its thread pool and onnxruntime releases the GIL."""
    if response_format not in ("json", "verbose_json"):  # both answer {"text": ...}
        raise HTTPException(400, f"response_format {response_format!r} is not supported")
    audio = _read(file)
    if not len(audio):
        return {"text": ""}
    score = None if _voiceprint is None else round(similarity(audio), 3)
    if score is not None and score < THRESHOLD:
        print(f"stt: not the owner's voice ({score:.2f}), not transcribed", file=sys.stderr)
        return {"text": "", "confidence": None, "similarity": score}
    # The same decode as recognize(), with each token's log-probability kept: the noise trace's
    # strongest cue. ponytail: max-probability confidence; NeMo's entropy measure needs the logits.
    result = _model.with_timestamps().recognize(audio, sample_rate=RATE)
    words = confidence(result.tokens or [], result.logprobs or [])
    return {"text": result.text, "confidence": words, "similarity": score}


def confidence(tokens: list[str], logprobs: list[float]) -> dict | None:
    """Return each word's probability (its least sure token's), their mean and min; None if none."""
    words: list[list] = []
    for token, logprob in zip(tokens, logprobs, strict=False):
        if token.startswith(" ") or not words:
            words.append([token.strip(), logprob])
        else:
            words[-1] = [words[-1][0] + token, min(words[-1][1], logprob)]
    words = [[word, round(float(np.exp(lp)), 3)] for word, lp in words if word]
    if not words:
        return None
    probs = [p for _, p in words]
    return {"mean": round(sum(probs) / len(probs), 3), "min": min(probs), "words": words}


@app.post("/v1/speaker/verify")
def verify(file: UploadFile = File(...)):
    """Whether the clip is the owner's voice; with no voiceprint, everyone is."""
    if _voiceprint is None:
        return {"enrolled": False, "owner": True, "similarity": None}
    score = similarity(_read(file))
    return {"enrolled": True, "owner": score >= THRESHOLD, "similarity": round(score, 3)}


def enroll(recordings: Path, out: Path) -> list[tuple[str, float]]:
    """Average the owner's recordings into a voiceprint at `out`: (name, similarity) of each one used.

    Use only the owner's voice: SAGE_VOICE_RECORD_DIR's turn WAVs, with anyone else's deleted.
    """
    global _speaker
    _speaker = _speaker or load_speaker()
    clips = {}
    for path in sorted(recordings.glob("*.wav")):
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        if rate == RATE and len(audio) >= MIN_ENROLL_S * RATE:
            clips[path.name] = _unit(_speaker.embedding(audio.mean(axis=1)))
    if not clips:
        raise SystemExit(f"no 16 kHz recordings of {MIN_ENROLL_S:g} s or more in {recordings}")
    voiceprint = _unit(np.mean(list(clips.values()), axis=0))
    np.save(out, voiceprint.astype("float32"))
    return [(name, float(e @ voiceprint)) for name, e in clips.items()]


if __name__ == "__main__":
    if sys.argv[1:2] == ["enroll"]:
        for name, score in enroll(Path(sys.argv[2]), Path(sys.argv[3])):
            print(f"{score:.2f}  {name}")  # a low one may be someone else: delete it and enroll again
    else:
        import uvicorn

        uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
