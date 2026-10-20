"""Offline transcription with hallucination guards."""
from __future__ import annotations

import re
import time

from dop_call_recorder import config as cfgmod
from dop_call_recorder import log
from dop_call_recorder.audio import SR, AudioError, TrimMap, load_audio, loudnorm, speech_spans, vad_trim  # noqa: F401
from dop_call_recorder.backends import get_backend
from dop_call_recorder.backends.base import BackendError, Segment  # noqa: F401

_log = log.get("transcribe")

def _norm(t: str) -> str:
    return re.sub(r"[^\w]+", " ", t.lower()).strip()


def dedupe(segments: list[Segment], max_repeat: int = 2) -> list[Segment]:
    """Drop runs where the same normalized text repeats more than max_repeat times consecutively."""
    out: list[Segment] = []
    run, last = 0, None
    for s in segments:
        n = _norm(s.text)
        if not n:
            continue
        run = run + 1 if n == last else 1
        last = n
        if run <= max_repeat:
            out.append(s)
    return out


def _overlaps(s: Segment, spans) -> bool:
    return any(s.end > a and s.start < b for a, b in spans)


def transcribe_file(path: str, cfg: dict | None = None) -> dict:
    """Returns {"text", "language", "segments": [Segment], "vocabulary", "duration"}.

    Pipeline: load -> [loudnorm(+afftdn)] -> VAD spans -> [VAD trim] -> backend -> remap times ->
    drop segments outside speech -> repeat dedupe. Backend chosen by cfg["backend"]."""
    cfg = cfg or cfgmod.load()
    tc, pre = cfg["transcribe"], cfg["preprocess"]
    t0 = time.time()
    backend = get_backend(cfg)
    backend.check()  # fail fast, before reading audio / running VAD
    audio = load_audio(path)
    duration = len(audio) / SR
    if pre.get("loudnorm") or pre.get("denoise"):
        audio = loudnorm(audio, denoise=bool(pre.get("denoise")))
    spans = speech_spans(audio, tc["vad"])
    log.event(_log, "audio_loaded", path=path, seconds=round(duration, 2), vad=tc["vad"],
              speech_spans=None if spans is None else len(spans), backend=cfg["backend"])
    if spans is not None and not spans:
        return {"text": "", "language": None, "segments": [], "vocabulary": "", "duration": duration}

    tmap = None
    if pre.get("vad_trim") and spans is not None:
        audio, tmap = vad_trim(audio, spans)

    prompt = cfgmod.load_vocabulary(cfg)
    res = backend.transcribe(audio, prompt, cfg.get("language") or "")
    segs = res.segments
    if tmap is not None:
        segs = [Segment(tmap.to_original(s.start), tmap.to_original(s.end), s.text) for s in segs]
    n_raw = len(segs)
    if spans is not None:
        segs = [s for s in segs if _overlaps(s, spans)]
    n_vad = len(segs)
    segs = dedupe(segs)
    segs = [Segment(s.start, s.end, cfgmod.apply_corrections(cfg, s.text)) for s in segs]
    log.event(_log, "transcribed", language=res.language, seconds=round(time.time() - t0, 2),
              segments_raw=n_raw, dropped_no_speech=n_raw - n_vad, dropped_repeats=n_vad - len(segs))
    return {"text": " ".join(s.text for s in segs), "language": res.language,
            "segments": segs, "vocabulary": prompt, "duration": duration}
