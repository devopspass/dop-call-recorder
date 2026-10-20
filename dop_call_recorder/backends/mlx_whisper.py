"""mlx-whisper (Apple MLX/Metal). The model dir <model_dir>/mlx-<model> is provisioned by `dop-recorder setup`
(dop_call_recorder.model_fetch: Azure CDN .pt -> torch-free conversion; no HuggingFace needed)."""
from __future__ import annotations

from pathlib import Path

from dop_call_recorder.backends.base import Backend, BackendError, BackendResult, Segment


class MLXWhisperBackend(Backend):
    name = "mlx-whisper"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.path = str(Path(cfg.get("mlx", {}).get("model_path")
                             or Path(cfg["model_dir"]) / f"mlx-{cfg['model']}").expanduser())
        self._loaded = False

    def check(self):
        try:
            import mlx.core  # noqa: F401
            import mlx_whisper  # noqa: F401
        except ImportError as e:
            raise BackendError(f"mlx-whisper backend needs the mlx packages ({e}); reinstall: brew reinstall dop-call-recorder") from e
        if not (Path(self.path, "config.json").exists() and (Path(self.path, "weights.npz").exists()
                                                             or Path(self.path, "weights.safetensors").exists())):
            raise BackendError(f"MLX model not found at {self.path}; run `dop-recorder setup` to download it")

    def load(self):
        if self._loaded:
            return
        self.check()
        import mlx.core as mx
        from mlx_whisper.transcribe import ModelHolder

        ModelHolder.get_model(self.path, mx.float16)
        self._loaded = True

    def transcribe(self, audio, prompt="", language=""):
        self.load()
        import mlx_whisper

        tc = self.tc
        kw = dict(path_or_hf_repo=self.path, verbose=None, fp16=True,
                  condition_on_previous_text=tc["condition_on_previous_text"],
                  no_speech_threshold=tc["no_speech_threshold"],
                  compression_ratio_threshold=tc["compression_ratio_threshold"],
                  logprob_threshold=tc["logprob_threshold"],
                  temperature=tuple(tc["temperature"]), initial_prompt=prompt or None)
        if language:
            kw["language"] = language
        res = mlx_whisper.transcribe(audio, **kw)
        return BackendResult(res.get("language"),
                             [Segment(s["start"], s["end"], s["text"].strip()) for s in res["segments"]])

    def describe(self):
        return f"mlx-whisper/{Path(self.path).name}"
