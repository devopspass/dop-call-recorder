"""Windowed transcription of long audio: N-minute windows with overlap, stitched by absolute timestamps."""
from __future__ import annotations

import difflib
import re
import time
import zlib
from dataclasses import dataclass, field

import numpy as np

from dop_call_recorder import config as cfgmod
from dop_call_recorder import log
from dop_call_recorder.audio import SR, loudnorm, speech_spans, vad_trim
from dop_call_recorder.backends.base import Segment
from dop_call_recorder.transcribe import _norm, _overlaps, dedupe

_log = log.get("windows")


def plan_windows(total: float, window: float = 1800.0, overlap: float = 15.0, min_tail: float = 60.0):
    """[(start, end)] covering [0, total]; consecutive windows overlap by `overlap` seconds.
    A too-short tail window is folded into the previous one."""
    if total <= window:
        return [(0.0, total)]
    out, start = [], 0.0
    while True:
        end = min(total, start + window)
        out.append((start, end))
        if end >= total:
            break
        start = end - overlap
    if len(out) > 1 and out[-1][1] - out[-1][0] < min_tail:
        out.pop()
        out[-1] = (out[-1][0], total)
    return out


def _similar(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.7


def stitch(parts: list[tuple[float, float, list[Segment]]]) -> list[Segment]:
    """parts = [(win_start, win_end, segments with ABSOLUTE times)] in order. The seam of two consecutive
    windows is the midpoint of their overlap; a segment belongs to the window containing its midpoint.
    Segments that survive on the far side of the seam but duplicate (time-overlapping + similar text)
    a kept segment of the previous window are dropped."""
    if not parts:
        return []
    seams = [((parts[i + 1][0] + parts[i][1]) / 2) for i in range(len(parts) - 1)]
    out: list[Segment] = []
    for i, (_, _, segs) in enumerate(parts):
        lo = seams[i - 1] if i > 0 else float("-inf")
        hi = seams[i] if i < len(seams) else float("inf")
        kept = [s for s in segs if lo <= (s.start + s.end) / 2 < hi]
        if i > 0:
            tail = [o for o in out if o.end > lo - 20]
            kept = [s for s in kept
                    if not any(o.end > s.start and o.start < s.end and _similar(o.text, s.text) for o in tail)]
        out.extend(kept)
    return sorted(out, key=lambda s: (s.start, s.end))


@dataclass
class Stats:
    duration: float = 0.0
    speech_seconds: float | None = 0.0   # None = no VAD info
    raw_segments: int = 0
    dropped_repeats: int = 0
    dropped_no_speech: int = 0
    windows: int = 0

    def add(self, o: "Stats"):
        self.duration += o.duration
        if self.speech_seconds is None or o.speech_seconds is None:
            self.speech_seconds = None
        else:
            self.speech_seconds += o.speech_seconds
        for k in ("raw_segments", "dropped_repeats", "dropped_no_speech", "windows"):
            setattr(self, k, getattr(self, k) + getattr(o, k))


@dataclass
class StreamResult:
    segments: list[Segment] = field(default_factory=list)
    window_langs: list[tuple[float, float, str | None]] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)

    def lang_at(self, t: float) -> str | None:
        for a, b, lang in self.window_langs:
            if a <= t < b and lang:
                return lang
        return next((l for _, _, l in self.window_langs if l), None)


def _transcribe_window(audio, cfg, backend, prompt, language):
    tc, pre = cfg["transcribe"], cfg["preprocess"]
    st = Stats(duration=len(audio) / SR, windows=1)
    if pre.get("loudnorm") or pre.get("denoise"):
        audio = loudnorm(audio, denoise=bool(pre.get("denoise")))
    spans = speech_spans(audio, tc["vad"])
    st.speech_seconds = None if spans is None else sum(b - a for a, b in spans)
    if spans is not None and not spans:
        return [], None, st
    tmap = None
    if pre.get("vad_trim") and spans is not None:
        audio, tmap = vad_trim(audio, spans)
    res = backend.transcribe(audio, prompt, language)
    segs = res.segments
    if tmap is not None:
        segs = [Segment(tmap.to_original(s.start), tmap.to_original(s.end), s.text) for s in segs]
    st.raw_segments = len(segs)
    if spans is not None:
        n = len(segs)
        segs = [s for s in segs if _overlaps(s, spans)]
        st.dropped_no_speech = n - len(segs)
    n = len(segs)
    segs = dedupe(segs)
    st.dropped_repeats = n - len(segs)
    segs = [Segment(s.start, s.end, cfgmod.apply_corrections(cfg, s.text)) for s in segs]
    return segs, res.language, st


def transcribe_stream(audio: np.ndarray, cfg: dict, backend, prompt: str = "", language: str = "",
                      window_s: float | None = None, overlap_s: float | None = None, label: str = "") -> StreamResult:
    pc = cfg["pipeline"]
    window_s = window_s if window_s is not None else pc["window_minutes"] * 60
    overlap_s = overlap_s if overlap_s is not None else pc["overlap_seconds"]
    wins = plan_windows(len(audio) / SR, window_s, overlap_s)
    res = StreamResult()
    parts = []
    for i, (a, b) in enumerate(wins):
        t0 = time.time()
        segs, lang, st = _transcribe_window(audio[int(a * SR):int(b * SR)], cfg, backend, prompt,
                                            language or cfg.get("language") or "")
        segs = [Segment(s.start + a, s.end + a, s.text) for s in segs]
        parts.append((a, b, segs))
        res.window_langs.append((a, b, lang))
        res.stats.add(st)
        from dop_call_recorder.agentstate import PROGRESS
        PROGRESS.advance(label or "stream", b)
        log.event(_log, "window_done", stream=label, window=i + 1, of=len(wins), start=round(a, 1), end=round(b, 1),
                  segments=len(segs), language=lang, seconds=round(time.time() - t0, 1))
    res.segments = stitch(parts)
    return res


def compression_ratio(text: str) -> float:
    b = text.encode("utf-8")
    return len(b) / max(1, len(zlib.compress(b)))
