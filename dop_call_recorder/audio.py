"""Audio loading and pre-processing: VAD speech spans, VAD trimming (silence removal), loudness normalization."""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

import numpy as np

from dop_call_recorder import log

_log = log.get("audio")
SR = 16000


class AudioError(Exception):
    """Unreadable / empty / missing input audio."""


def load_audio(path: str) -> np.ndarray:
    """Any ffmpeg-readable file -> 16 kHz mono float32. Raises AudioError on missing/undecodable/empty input."""
    if not os.path.isfile(path):
        raise AudioError(f"audio file not found: {path}")
    p = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-f", "f32le",
                        "-ac", "1", "-ar", str(SR), "-"], capture_output=True)
    if p.returncode != 0:
        raise AudioError(f"cannot decode {path}: {p.stderr.decode(errors='replace').strip()[:300]}")
    out = p.stdout
    if not out:
        raise AudioError(f"no audio samples in {path}")
    return np.frombuffer(out, dtype=np.float32).copy()


def speech_spans(audio, vad: str) -> list[tuple[float, float]] | None:
    """Speech spans in seconds, or None when VAD is off/unavailable (= keep everything)."""
    if vad == "off":
        return None
    if vad == "silero":
        try:
            from dop_call_recorder import vad_onnx

            return vad_onnx.speech_spans(audio)
        except Exception as e:  # fall back to webrtcvad
            log.event(_log, "silero_vad_failed_fallback_webrtc", error=repr(e))
            vad = "webrtc"
    if vad == "webrtc":
        try:
            import webrtcvad

            v = webrtcvad.Vad(2)
            pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
            fl = SR * 30 // 1000
            spans, cur = [], None
            for i in range(0, len(pcm) - fl + 1, fl):
                t = i / SR
                if v.is_speech(pcm[i:i + fl].tobytes(), SR):
                    cur = [t, t + 0.03] if cur is None else [cur[0], t + 0.03]
                elif cur is not None and t - cur[1] > 0.5:
                    spans.append(tuple(cur)); cur = None
            if cur:
                spans.append(tuple(cur))
            return spans
        except Exception as e:
            log.event(_log, "webrtc_vad_failed_no_vad", error=repr(e))
    return None


@dataclass
class TrimMap:
    """Pieces of the trimmed audio: (trimmed_start, original_start, duration) in seconds."""
    pieces: list[tuple[float, float, float]]

    def to_original(self, t: float) -> float:
        if not self.pieces:
            return t
        for ts, os_, d in self.pieces:
            if t < ts + d:
                return os_ + max(0.0, t - ts)
        ts, os_, d = self.pieces[-1]
        return os_ + d


def vad_trim(audio: np.ndarray, spans: list[tuple[float, float]], pad: float = 0.3,
             merge_gap: float = 1.0) -> tuple[np.ndarray, TrimMap]:
    """Cut everything outside speech (+pad). Spans closer than merge_gap are joined so short pauses stay."""
    total = len(audio) / SR
    merged: list[list[float]] = []
    for a, b in sorted(spans):
        a, b = max(0.0, a - pad), min(total, b + pad)
        if merged and a - merged[-1][1] <= merge_gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    chunks, pieces, pos = [], [], 0.0
    for a, b in merged:
        chunks.append(audio[int(a * SR):int(b * SR)])
        pieces.append((pos, a, b - a))
        pos += b - a
    out = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    return out, TrimMap(pieces)


def loudnorm(audio: np.ndarray, denoise: bool = False, target_lufs: float = -16.0) -> np.ndarray:
    """EBU R128 loudness normalization via ffmpeg (optionally preceded by afftdn noise reduction)."""
    filt = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
    if denoise:
        filt = "afftdn=nf=-25," + filt
    out = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "f32le", "-ar", str(SR), "-ac", "1",
                          "-i", "-", "-af", filt, "-f", "f32le", "-ar", str(SR), "-ac", "1", "-"],
                         input=audio.astype("<f4").tobytes(), capture_output=True, check=True).stdout
    return np.clip(np.frombuffer(out, dtype=np.float32), -1.0, 1.0).copy()
