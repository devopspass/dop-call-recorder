"""Pluggable ASR backends, selected by config.toml `backend`."""
from __future__ import annotations

from dop_call_recorder import log
from dop_call_recorder.backends.base import Backend, BackendError, BackendResult, Segment

NAMES = ("auto", "openai-whisper", "mlx-whisper", "whisper-cpp")
_log = log.get("backends")


def get_backend(cfg: dict) -> Backend:
    name = cfg.get("backend") or "auto"
    if name == "auto":
        from dop_call_recorder.backends.mlx_whisper import MLXWhisperBackend

        try:
            b = MLXWhisperBackend(cfg)
            b.check()
            log.event(_log, "backend_auto", chosen="mlx-whisper")
            return b
        except BackendError as e:
            log.event(_log, "backend_auto_fallback", chosen="openai-whisper", reason=str(e)[:200])
            name = "openai-whisper"
    if name == "openai-whisper":
        from dop_call_recorder.backends.openai_whisper import OpenAIWhisperBackend as B
    elif name == "mlx-whisper":
        from dop_call_recorder.backends.mlx_whisper import MLXWhisperBackend as B
    elif name == "whisper-cpp":
        from dop_call_recorder.backends.whisper_cpp import WhisperCppBackend as B
    else:
        raise BackendError(f"unknown backend {name!r}; choose one of {', '.join(NAMES)}")
    return B(cfg)
