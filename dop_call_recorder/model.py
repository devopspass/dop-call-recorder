"""Whisper model loader.

Reuses ~/.cache/whisper via whisper.load_model(name, device=device, download_root=model_dir):
if <model_dir>/<name>.pt exists and its SHA256 matches it is hashed and loaded with
no download (a corrupt/partial file is silently re-downloaded); otherwise
openai-whisper downloads it from OpenAI's Azure CDN (openaipublic.azureedge.net),
which is not blocked (HuggingFace is) - same as copilot-bot's transcription.py.
"""
from __future__ import annotations

import time
from pathlib import Path

from dop_call_recorder import log

_log = log.get("model")
_cache: dict = {}


def cached_path(name: str, model_dir: str) -> Path:
    return Path(model_dir) / f"{name}.pt"


def _stat(p: Path):
    try:
        st = p.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def load(name: str, model_dir: str, device: str = "cpu"):
    key = (name, model_dir, device)
    if key not in _cache:
        import whisper

        p = cached_path(name, model_dir)
        t0 = time.time()
        before = _stat(p)
        log.event(_log, "model_load_start", model=name, model_dir=model_dir, device=device, cached=before is not None)
        _cache[key] = whisper.load_model(name, device=device, download_root=model_dir)
        after = _stat(p)
        if before is None:
            source = "azure-cdn-download"
        elif before != after:
            source = "redownloaded"  # existing file failed the checksum
        else:
            source = "cache"
        log.event(_log, "model_load_done", model=name, source=source, seconds=round(time.time() - t0, 2))
    return _cache[key]
