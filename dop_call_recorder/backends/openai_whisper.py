"""openai-whisper (PyTorch). device: cpu (default) | mps. Behavior identical to the M0 implementation."""
from __future__ import annotations

from dop_call_recorder import model as modelmod
from dop_call_recorder.backends.base import Backend, BackendError, BackendResult, Segment


class OpenAIWhisperBackend(Backend):
    name = "openai-whisper"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.device = cfg.get("device") or "cpu"
        self._m = None

    def check(self):
        if self.device not in ("cpu", "mps"):
            raise BackendError(f"openai-whisper: device must be cpu or mps, got {self.device!r}")
        try:
            import whisper  # noqa: F401
        except ImportError as e:
            raise BackendError(f"openai-whisper is not installed ({e}); pip install openai-whisper") from e

    def load(self):
        if self._m is None:
            self._m = modelmod.load(self.cfg["model"], self.cfg["model_dir"], device=self.device)

    def transcribe(self, audio, prompt="", language=""):
        self.load()
        tc = self.tc
        kw = dict(
            condition_on_previous_text=tc["condition_on_previous_text"],
            no_speech_threshold=tc["no_speech_threshold"],
            compression_ratio_threshold=tc["compression_ratio_threshold"],
            logprob_threshold=tc["logprob_threshold"],
            temperature=tuple(tc["temperature"]),
            initial_prompt=prompt or None,
            fp16=self.device != "cpu",
            verbose=None,
        )
        if language:
            kw["language"] = language
        res = self._m.transcribe(audio, **kw)
        return BackendResult(res.get("language"),
                             [Segment(s["start"], s["end"], s["text"].strip()) for s in res["segments"]])

    def describe(self):
        return f"openai-whisper/{self.cfg['model']}/{self.device}"
