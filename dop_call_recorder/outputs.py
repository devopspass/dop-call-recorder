"""Output writers: transcript.md (+YAML front matter), transcript.vtt, meta.json, INDEX.md/index.json."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from dop_call_recorder import merge as mg


def hms(t: float, ms: bool = False) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    base = f"{h:02d}:{m:02d}:{s:02d}"
    return base + f".{int(round((t % 1) * 1000)) % 1000:03d}" if ms else base


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(epoch))


def slugify(title: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^\w.\- ]+", "", title, flags=re.UNICODE).strip()
    s = re.sub(r"\s+", "_", s)[:maxlen].strip("._-")
    return s or "call"


def clean_title(title: str | None) -> str | None:
    if not title:
        return None
    t = re.sub(r"\s*\|\s*Microsoft Teams\s*$", "", title).strip()
    pre = re.compile(r"(?i)^(meeting compact view|compact view|meeting|call|sharing control|calling)\s*\|\s*")
    while pre.match(t) and pre.sub("", t, count=1).strip():
        t = pre.sub("", t, count=1).strip()
    return t or None


def output_folder(out_root: str | Path, start_epoch: float, title: str) -> Path:
    lt = time.localtime(start_epoch)
    base = Path(out_root) / time.strftime("%Y", lt) / f"{time.strftime('%Y-%m-%d_%H%M', lt)}_{slugify(title)}"
    p, n = base, 2
    while p.exists():
        p = base.with_name(f"{base.name}_{n}")
        n += 1
    return p


def _y(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def render_markdown(meta: dict, segs: list[mg.Seg], cfg_pipeline: dict) -> str:
    fm = ["---",
          f"title: {_y(meta['title'])}", f"date: {meta['date']}", f"start: {meta['start']}", f"end: {meta['end']}",
          f"duration: {_y(meta['duration'])}", f"duration_seconds: {meta['duration_seconds']}",
          f"apps: {_y(meta['apps'])}", f"language: {_y(meta['language'])}", f"languages: {_y(meta['languages'])}",
          f"quality: {meta['quality']}", f"model: {_y(meta['model'])}", f"backend: {_y(meta['backend'])}", "---", ""]
    body = [f"# {meta['title']}", ""]
    if meta["quality"] != "good":
        body += [f"> ⚠️ quality: {meta['quality']} ({', '.join(meta.get('quality_issues') or [])})", ""]
    if not segs:
        body.append("_(no speech detected)_")
    chs = mg.chapters(segs, cfg_pipeline["chapter_gap_seconds"], cfg_pipeline["chapter_max_minutes"] * 60)
    for c in chs:
        body += [f"## [{hms(c.start)}] {c.title}", ""]
        for t in mg.turns(c.segs):
            body.append(f"**[{hms(t.start)}] {t.speaker}:** {t.text}")
            body.append("")
    return "\n".join(fm + body).rstrip() + "\n"


def render_vtt(segs: list[mg.Seg]) -> str:
    out = ["WEBVTT", ""]
    for i, s in enumerate(segs, 1):
        end = max(s.end, s.start + 0.5)
        out += [str(i), f"{hms(s.start, True)} --> {hms(end, True)}", f"<v {s.speaker}>{s.text}", ""]
    return "\n".join(out)


def snippet(segs: list[mg.Seg], n: int = 160) -> str:
    text = " ".join(s.text for s in segs[:6]).strip()
    m = re.match(r"(.+?[.!?])(\s+.+?[.!?])?", text)
    first = m.group(0) if m else text
    first = first if len(first) >= 20 else text
    return first[:n].rstrip() + ("…" if len(first) > n else "")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def update_index(out_root: str | Path, entry: dict) -> None:
    """One entry per call (keyed by relative path; re-processing replaces it). INDEX.md newest first."""
    root = Path(out_root)
    jp = root / "index.json"
    try:
        calls = json.loads(jp.read_text(encoding="utf-8")).get("calls", [])
    except (OSError, ValueError):
        calls = []
    calls = [c for c in calls if c.get("path") != entry["path"]] + [entry]
    calls.sort(key=lambda c: c.get("start", ""), reverse=True)
    atomic_write(jp, json.dumps({"calls": calls}, ensure_ascii=False, indent=2) + "\n")
    rows = ["# Call transcripts", "", "| Date | Duration | Title | Quality | Snippet |", "|---|---|---|---|---|"]
    for c in calls:
        esc = lambda x: str(x).replace("|", "\\|").replace("\n", " ")  # noqa: E731
        rows.append(f"| {c['date']} {c.get('time', '')} | {c['duration']} | [{esc(c['title'])}]({c['path']}/transcript.md) "
                    f"| {c['quality']} | {esc(c['snippet'])} |")
    atomic_write(root / "INDEX.md", "\n".join(rows) + "\n")
