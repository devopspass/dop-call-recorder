"""Quality gate: detect hallucination loops / empty output / wrong language / garbage segments."""
from __future__ import annotations

import copy
from dop_call_recorder import langid
from dop_call_recorder.transcribe import _norm
from dop_call_recorder.windows import StreamResult, compression_ratio


def assess(res: StreamResult, language_hint: str | None = None) -> list[str]:
    """Return issue codes (empty list = OK)."""
    issues: list[str] = []
    segs, st = res.segments, res.stats
    words = sum(len(s.text.split()) for s in segs)
    sp = st.speech_seconds
    if sp is not None:
        if sp >= 60 and words < 0.2 * sp:
            issues.append("empty_output")
    elif st.duration >= 300 and not segs:
        issues.append("empty_output")
    if len(segs) >= 8:
        # a hallucination loop = the same (>=3 word) line >=4 times within 2 minutes; a phrase that
        # legitimately recurs over a long call does not count
        by: dict[str, list[float]] = {}
        for s in segs:
            n = _norm(s.text)
            if len(n.split()) >= 3:
                by.setdefault(n, []).append(s.start)
        looped = 0
        for ts in by.values():
            ts.sort()
            if any(ts[i + 3] - ts[i] <= 120 for i in range(len(ts) - 3)):
                looped += len(ts)
        if looped > 0.25 * len(segs):
            issues.append("repeated_lines")
    if st.raw_segments >= 10 and st.dropped_repeats >= 0.15 * st.raw_segments and "repeated_lines" not in issues:
        issues.append("repeated_lines")
    if len(segs) >= 5:
        high = sum(1 for s in segs if len(s.text) >= 60 and compression_ratio(s.text) > 2.6)
        if high >= max(2, 0.1 * len(segs)):
            issues.append("high_compression")
    text = " ".join(s.text for s in segs)
    lang = language_hint or res.lang_at(0)
    sug = langid.script_mismatch(text, lang)
    if sug:
        issues.append(f"wrong_language:{sug}")
    return issues


def retry_config(cfg: dict, issues: list[str], window_s: float) -> tuple[dict, dict]:
    """Different settings for the single automatic re-run: shorter windows, VAD trim (removes the
    silence that triggers loops), no vocabulary prompt, stricter thresholds, forced language if the script
    contradicted the detected one. Returns (cfg, kwargs for transcribe_stream)."""
    c = copy.deepcopy(cfg)
    c["preprocess"]["vad_trim"] = True
    c["preprocess"]["loudnorm"] = True
    c["transcribe"]["no_speech_threshold"] = min(0.8, c["transcribe"]["no_speech_threshold"] + 0.1)
    c["transcribe"]["compression_ratio_threshold"] = 2.0
    c["transcribe"]["temperature"] = [0.0, 0.2, 0.4, 0.6]
    kw = {"window_s": max(300.0, window_s / 3), "prompt": ""}
    for i in issues:
        if i.startswith("wrong_language:"):
            kw["language"] = i.split(":", 1)[1]
    return c, kw


def better(a: list[str], b: list[str]) -> bool:
    """True if issue list a is strictly better than b."""
    return len(a) < len(b)
