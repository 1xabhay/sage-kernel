"""Essentia MusiCNN mood/danceability heads.

One shared embedding pass per track feeds every classification head, so adding
a mood costs a matrix multiply rather than another forward pass over the audio.
Model weights are downloaded on first use and cached on disk.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

# Quiet TensorFlow's C++ logging before essentia pulls it in.
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

from ..config import EMBEDDING_MODEL, MOOD_HEADS, mood_head_url, settings
from ..sources.http import client

_lock = threading.Lock()
_embedder = None
_heads: dict[str, tuple[object, int]] = {}
_unavailable = False

# These graphs predate the `serving_default_*` naming; the node names below are
# the ones actually present in the msd-musicnn classification heads.
_HEAD_INPUT = "model/Placeholder"
_HEAD_OUTPUT = "model/Softmax"


def _positive_index(slug: str, mood: str) -> int:
    """Which softmax column is the positive class for this head.

    The msd-musicnn heads are NOT consistent: mood_sad/relaxed/party put the
    positive class at index 1, the others at index 0. Read it from the model's
    own metadata instead of assuming, so a model refresh can't silently invert
    a score.
    """
    try:
        meta = _download(
            mood_head_url(slug).replace(".pb", ".json"),
            settings.models_dir / f"{slug}.json",
        )
        classes = json.loads(meta.read_text())["classes"]
    except Exception:
        return 0
    for i, name in enumerate(classes):
        low = name.lower()
        if not low.startswith(("non_", "not_")):
            # The affirmative label, e.g. "danceable" in [danceable, not_danceable].
            return i
    return 0


def _download(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with client().stream("GET", url) as resp:
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_bytes():
                fh.write(chunk)
        tmp.replace(dest)
    return dest


def _load() -> bool:
    """Load the embedder and every head once. Returns False if unavailable."""
    global _embedder, _unavailable
    if _unavailable:
        return False
    if _embedder is not None:
        return True
    with _lock:
        if _embedder is not None:
            return True
        try:
            import essentia.standard as es

            emb_path = _download(
                EMBEDDING_MODEL["url"], settings.models_dir / EMBEDDING_MODEL["name"]
            )
            _embedder = es.TensorflowPredictMusiCNN(
                graphFilename=str(emb_path), output=EMBEDDING_MODEL["output"]
            )
            for mood, slug in MOOD_HEADS.items():
                path = _download(mood_head_url(slug), settings.models_dir / f"{slug}.pb")
                algo = es.TensorflowPredict2D(
                    graphFilename=str(path), input=_HEAD_INPUT, output=_HEAD_OUTPUT
                )
                _heads[mood] = (algo, _positive_index(slug, mood))
        except Exception:
            # Essentia is an optional extra; the pipeline degrades to librosa only.
            _unavailable = True
            _embedder = None
            _heads.clear()
            return False
    return True


def predict(path: Path) -> dict[str, float]:
    """Return {mood: probability} for a preview file, or {} if unavailable."""
    if not _load():
        return {}
    try:
        import essentia.standard as es

        audio = es.MonoLoader(filename=str(path), sampleRate=16000, resampleQuality=4)()
        if audio.size == 0:
            return {}
        embeddings = _embedder(audio)
        out: dict[str, float] = {}
        for mood, (head, pos) in _heads.items():
            # Each head yields (frames, 2); average over frames, then take the
            # column that metadata says is the affirmative class.
            preds = head(embeddings)
            out[mood] = round(float(preds.mean(axis=0)[pos]), 4)
        return out
    except Exception:
        return {}
