"""Torch-free Silero VAD: bundled silero_vad.onnx (MIT, snakers4/silero-vad) run with onnxruntime.

The frame post-processing is a port of silero_vad.get_speech_timestamps (without the max-speech-duration
branch, which we never use). The `silero-vad` pip package is NOT used: it hard-requires torch."""
from __future__ import annotations

from pathlib import Path

import numpy as np

SR = 16000
WIN = 512
CTX = 64
MODEL = Path(__file__).with_name("data") / "silero_vad.onnx"
_session = None


def _sess():
    global _session
    if _session is None:
        import onnxruntime as ort

        o = ort.SessionOptions()
        o.inter_op_num_threads = o.intra_op_num_threads = 1
        _session = ort.InferenceSession(str(MODEL), providers=["CPUExecutionProvider"], sess_options=o)
    return _session


def speech_probs(audio: np.ndarray) -> list[float]:
    """Per-512-sample-frame speech probability for 16 kHz mono float32 audio."""
    s = _sess()
    state = np.zeros((2, 1, 128), dtype=np.float32)
    ctx = np.zeros((1, CTX), dtype=np.float32)
    sr = np.array(SR, dtype=np.int64)
    n = len(audio)
    probs = []
    for i in range(0, n, WIN):
        chunk = audio[i:i + WIN]
        if len(chunk) < WIN:
            chunk = np.pad(chunk, (0, WIN - len(chunk)))
        x = np.concatenate([ctx, chunk[None, :].astype(np.float32)], axis=1)
        out, state = s.run(None, {"input": x, "state": state, "sr": sr})
        ctx = x[:, -CTX:]
        probs.append(float(out[0][0]))
    return probs


def timestamps_from_probs(probs, n_samples: int, threshold=0.5, min_speech_ms=250, min_silence_ms=100,
                          pad_ms=30) -> list[dict]:
    min_speech = SR * min_speech_ms / 1000
    pad = SR * pad_ms / 1000
    min_silence = SR * min_silence_ms / 1000
    neg = max(threshold - 0.15, 0.01)
    triggered, speeches, cur, temp_end = False, [], {}, 0
    for i, p in enumerate(probs):
        pos = WIN * i
        if p >= threshold and temp_end:
            temp_end = 0
        if p >= threshold and not triggered:
            triggered, cur = True, {"start": pos}
            continue
        if p < neg and triggered:
            if not temp_end:
                temp_end = pos
            if pos - temp_end < min_silence:
                continue
            cur["end"] = temp_end
            if cur["end"] - cur["start"] > min_speech:
                speeches.append(cur)
            cur, temp_end, triggered = {}, 0, False
    if cur and n_samples - cur["start"] > min_speech:
        cur["end"] = n_samples
        speeches.append(cur)
    for i, sp in enumerate(speeches):
        if i == 0:
            sp["start"] = int(max(0, sp["start"] - pad))
        if i != len(speeches) - 1:
            gap = speeches[i + 1]["start"] - sp["end"]
            if gap < 2 * pad:
                sp["end"] += int(gap // 2)
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - gap // 2))
            else:
                sp["end"] = int(min(n_samples, sp["end"] + pad))
                speeches[i + 1]["start"] = int(max(0, speeches[i + 1]["start"] - pad))
        else:
            sp["end"] = int(min(n_samples, sp["end"] + pad))
    return speeches


def speech_spans(audio: np.ndarray) -> list[tuple[float, float]]:
    ts = timestamps_from_probs(speech_probs(audio), len(audio))
    return [(round(t["start"] / SR, 1), round(t["end"] / SR, 1)) for t in ts]
