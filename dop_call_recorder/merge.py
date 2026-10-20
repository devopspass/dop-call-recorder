"""Merge Me/Others streams: label, drop echoes, per-segment language, turns and chapters."""
from __future__ import annotations

import difflib
from collections import defaultdict
from dataclasses import dataclass

from dop_call_recorder import langid
from dop_call_recorder.transcribe import _norm
from dop_call_recorder.windows import StreamResult


@dataclass
class Seg:
    start: float
    end: float
    text: str
    speaker: str          # "Me" | "Others"
    lang: str | None = None


@dataclass
class Chapter:
    start: float
    segs: list[Seg]
    title: str = ""


def label(res: StreamResult, speaker: str) -> list[Seg]:
    out = []
    for s in res.segments:
        t = s.text.strip()
        if t:
            out.append(Seg(s.start, s.end, t, speaker, langid.detect(t, res.lang_at((s.start + s.end) / 2))))
    return out


def _echo(me: str, other: str) -> bool:
    a, b = _norm(me).split(), _norm(other).split()
    if len(a) < 3 or not b:
        return False
    sa, sb = set(a), set(b)
    if len(sa & sb) / min(len(sa), len(sb)) >= 0.8 and len(sa) >= 3:
        return True
    return difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio() >= 0.75


def drop_echo(me: list[Seg], others: list[Seg], window: float = 2.0) -> tuple[list[Seg], int]:
    """Remove Me segments whose text echoes an Others segment overlapping in time (+-window s)
    - the mic picking up the speakers when no headphones are used."""
    kept, dropped = [], 0
    for m in me:
        if any(o.end + window >= m.start and o.start - window <= m.end and _echo(m.text, o.text) for o in others):
            dropped += 1
        else:
            kept.append(m)
    return kept, dropped


def merge(me: StreamResult | None, others: StreamResult | None, echo_window: float = 2.0):
    """-> (segments sorted by time, dropped_echo_count)."""
    m = label(me, "Me") if me else []
    o = label(others, "Others") if others else []
    m, dropped = drop_echo(m, o, echo_window)
    return sorted(m + o, key=lambda s: (s.start, s.end)), dropped


def _norm_name(n: str) -> str:
    return " ".join(n.lower().replace(",", " ").split())


def _is_self(name: str, self_name: str | None) -> bool:
    return bool(self_name) and sorted(_norm_name(name).split()) == sorted(_norm_name(self_name).split())


def others_roster(events: list[dict], self_name: str | None) -> list[str]:
    """Other participants seen in the call (most frequent first), excluding the user."""
    cnt: dict[str, int] = defaultdict(int)
    for e in events:
        for n in e.get("roster") or []:
            if not _is_self(n, self_name):
                cnt[n] += 1
    return [n for n, _ in sorted(cnt.items(), key=lambda kv: -kv[1])]


def detect_self(events: list[dict]) -> str | None:
    cnt: dict[str, int] = defaultdict(int)
    for e in events:
        if e.get("self"):
            cnt[e["self"]] += 1
    return max(cnt, key=cnt.get) if cnt else None


def assign_speakers(segs: list[Seg], events: list[dict], start_epoch: float, self_name: str | None = None,
                    slack: float = 0.5, min_overlap: float = 0.25) -> tuple[list[Seg], dict]:
    """Name the speakers (X1). `events` = speaker_events.jsonl rows (epoch `ts`, `speaking`, `roster`).

    Me          -> self_name (config my_name > detected own name), else unchanged.
    Others      -> the active speaker with the largest overlap with the segment (+-slack s, needs >= min_overlap s
                   and > half of the overlap with any speaker), else the sole other participant when the roster
                   has exactly one entry, else "Others".
    Returns (new segs, info{self, roster, named_others, total_others}). No events -> only the Me rename.
    """
    self_name = self_name or detect_self(events)
    roster = others_roster(events, self_name)
    sole = roster[0] if len(roster) == 1 else None
    pts = [(e["ts"] - start_epoch, e) for e in events if "ts" in e]
    out, named = [], 0
    n_oth = sum(1 for g in segs if g.speaker == "Others")
    for g in segs:
        if g.speaker == "Me":
            out.append(Seg(g.start, g.end, g.text, self_name or "Me", g.lang))
            continue
        if g.speaker != "Others":
            out.append(g)
            continue
        who = None
        ov: dict[str, float] = defaultdict(float)
        a, b = g.start - slack, g.end + slack
        for i, (t, e) in enumerate(pts):
            nxt = pts[i + 1][0] if i + 1 < len(pts) else t + 1.0
            nxt = min(nxt, t + 3.0)  # a gap in polling is not evidence
            o = min(b, nxt) - max(a, t)
            if o > 0:
                for n in e.get("speaking") or []:
                    if not _is_self(n, self_name):
                        ov[n] += o
        if ov:
            best, v = max(ov.items(), key=lambda kv: kv[1])
            if v >= min_overlap and v >= 0.5 * sum(ov.values()):
                who = best
        who = who or sole
        if who:
            named += 1
        out.append(Seg(g.start, g.end, g.text, who or "Others", g.lang))
    return out, {"self": self_name, "roster": roster, "named_others": named, "total_others": n_oth}


def turns(segs: list[Seg], gap: float = 4.0) -> list[Seg]:
    """Join consecutive same-speaker segments (gap < `gap` s) into one paragraph."""
    out: list[Seg] = []
    for s in segs:
        if out and out[-1].speaker == s.speaker and s.start - out[-1].end < gap:
            p = out[-1]
            out[-1] = Seg(p.start, max(p.end, s.end), p.text + " " + s.text, p.speaker, p.lang or s.lang)
        else:
            out.append(Seg(s.start, s.end, s.text, s.speaker, s.lang))
    return out


def chapters(segs: list[Seg], gap: float = 60.0, max_len: float = 600.0, min_len: float = 120.0) -> list[Chapter]:
    """Split on long pauses (>= gap s once the chapter is >= min_len long) or by fixed interval
    (chapter older than max_len, at the next pause >= 3 s, hard cap 1.5x)."""
    out: list[Chapter] = []
    prev_end = None
    for s in segs:
        if not out:
            out.append(Chapter(s.start, [s]))
        else:
            age, pause = s.start - out[-1].start, s.start - (prev_end or s.start)
            if (pause >= gap and age >= min_len) or (age >= max_len and pause >= 3) or age >= 1.5 * max_len:
                out.append(Chapter(s.start, [s]))
            else:
                out[-1].segs.append(s)
        prev_end = max(prev_end or 0, s.end)
    for c in out:
        words = " ".join(x.text for x in c.segs).split()
        c.title = " ".join(words[:8]) + ("…" if len(words) > 8 else "")
    return out


def language_shares(segs: list[Seg]) -> dict[str, float]:
    d: dict[str, float] = defaultdict(float)
    for s in segs:
        if s.lang:
            d[s.lang] += max(0.1, s.end - s.start)
    tot = sum(d.values()) or 1
    return {k: round(v / tot, 3) for k, v in sorted(d.items(), key=lambda kv: -kv[1])}
