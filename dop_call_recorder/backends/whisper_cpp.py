"""whisper.cpp via the `whisper-cli` binary (brew install whisper-cpp; Metal). Model: ggml file converted from
the cached .pt with whisper.cpp's models/convert-pt-to-ggml.py (see README M1)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

from dop_call_recorder import paths
from dop_call_recorder.backends.base import Backend, BackendError, BackendResult, Segment


class WhisperCppBackend(Backend):
    name = "whisper-cpp"

    def __init__(self, cfg):
        super().__init__(cfg)
        o = cfg.get("whispercpp", {})
        self.binary = o.get("binary") or paths.tool("whisper-cli") or "whisper-cli"
        self.path = str(Path(o.get("model_path") or Path(cfg["model_dir"]) / f"ggml-{cfg['model']}.bin").expanduser())
        self.beam = int(o.get("beam_size", 1))  # 1 = greedy, same as openai-whisper's default

    def check(self):
        if not (Path(self.binary).is_file() and os.access(self.binary, os.X_OK)):
            raise BackendError(f"whisper-cli not found/executable ({self.binary}); brew install whisper-cpp")
        if not Path(self.path).is_file():
            raise BackendError(f"ggml model not found at {self.path}; convert the cached .pt with whisper.cpp "
                               "models/convert-pt-to-ggml.py (see README M1)")

    def load(self):
        self.check()

    def transcribe(self, audio, prompt="", language=""):
        import numpy as np

        self.load()
        tc = self.tc
        with tempfile.TemporaryDirectory() as td:
            wav, base = Path(td) / "in.wav", Path(td) / "out"
            pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm.tobytes())
            cmd = [self.binary, "-m", self.path, "-f", str(wav), "-oj", "-of", str(base), "-np",
                   "-l", language or "auto", "-mc", "-1" if tc["condition_on_previous_text"] else "0",
                   "-nth", str(tc["no_speech_threshold"]), "-et", str(tc["compression_ratio_threshold"]),
                   "-lpt", str(tc["logprob_threshold"]), "-bs", str(self.beam), "-bo", str(self.beam)]
            if prompt:
                cmd += ["--prompt", prompt]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
            except subprocess.CalledProcessError as e:
                raise BackendError(f"whisper-cli failed (rc={e.returncode}): "
                                   f"{e.stderr.decode(errors='replace').strip()[-300:]}") from e
            data = json.loads(Path(f"{base}.json").read_text())
        segs = [Segment(s["offsets"]["from"] / 1000, s["offsets"]["to"] / 1000, s["text"].strip())
                for s in data.get("transcription", [])]
        return BackendResult(data.get("result", {}).get("language"), segs)

    def describe(self):
        return f"whisper-cpp/{Path(self.path).name}"
