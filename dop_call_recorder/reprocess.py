"""M4b: re-transcribe a finished call from its retained call.m4a (`dop-recorder reprocess`) and turn an arbitrary
recording into a call folder (`dop-recorder transcribe --save`).

Audio in call.m4a is a mono mix, so both flows transcribe ONE stream. For reprocess the Me/Others labels are
recovered by overlapping the new segments with the previous transcript.vtt; anything unmatched (and every
`--save` file) is labelled "Mixed". The previous transcript is kept as transcript.prev.md.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from dop_call_recorder import config as cfgmod
from dop_call_recorder import log
from dop_call_recorder import merge as mg
from dop_call_recorder import outputs, search
from dop_call_recorder.audio import AudioError, load_audio
from dop_call_recorder.backends import get_backend
from dop_call_recorder.windows import StreamResult

_log = log.get("reprocess")
VTT_CUE = re.compile(r"^(\d+):(\d\d):(\d\d)\.(\d{3}) --> (\d+):(\d\d):(\d\d)\.(\d{3})$")
VTT_VOICE = re.compile(r"^<v ([^>]+)>")
AUDIO_NAMES = ("call.m4a", "call.mp3", "call.wav")


class ReprocessError(Exception):
    pass


def resolve_call(cfg: dict, target: str) -> Path:
    """call dir | transcript file | path relative to output_dir | unique folder-name substring (call id)."""
    root = Path(cfg["output_dir"])
    for p in (Path(target).expanduser(), root / target):
        if p.is_file() and p.name.startswith("transcript"):
            p = p.parent
        if p.is_dir() and (p / "transcript.md").exists():
            return p.resolve()
    names = search.call_dirs(cfg)
    exact = [d for d in names if d.name == target]
    hits = exact or [d for d in names if target.lower() in d.name.lower()]
    if len(hits) == 1:
        return hits[0].resolve()
    if not hits:
        raise ReprocessError(f"no call matches {target!r} under {root}")
    raise ReprocessError(f"{target!r} is ambiguous: " + ", ".join(d.name for d in hits[:8]))


def find_audio(call_dir: Path) -> Path:
    for n in AUDIO_NAMES:
        if (call_dir / n).exists():
            return call_dir / n
    raise ReprocessError(f"no retained audio (call.m4a) in {call_dir}; raw WAVs were deleted after transcription")


def _sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def parse_vtt_speakers(path: Path) -> list[tuple[float, float, str]]:
    out, cue = [], None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for ln in lines:
        m = VTT_CUE.match(ln.strip())
        if m:
            g = m.groups()
            cue = (_sec(*g[:4]), _sec(*g[4:]))
            continue
        v = VTT_VOICE.match(ln)
        if v and cue:
            out.append((cue[0], cue[1], v.group(1)))
            cue = None
    return out


def assign_speakers(segs: list[mg.Seg], prev: list[tuple[float, float, str]], default: str = "Mixed") -> None:
    for s in segs:
        best, who = 0.0, default
        for a, b, spk in prev:
            ov = min(s.end, b) - max(s.start, a)
            if ov > best:
                best, who = ov, spk
        s.speaker = who


def _probe(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _transcribe(cfg: dict, audio_path: Path, backend, prev_speakers=None):
    from dop_call_recorder.pipeline import _run_stream  # quality gate + retry, same as the live pipeline

    audio = load_audio(str(audio_path))
    prompt = cfgmod.load_vocabulary(cfg)
    res, issues, retried = _run_stream(audio, cfg, backend, prompt, "mixed")
    segs = mg.label(res, "Mixed")
    if prev_speakers:
        assign_speakers(segs, prev_speakers)
    return res, segs, issues, retried


def _write(cfg: dict, out: Path, base: dict, backend, res: StreamResult, segs, issues, retried, seconds: float) -> dict:
    langs = mg.language_shares(segs)
    all_issues = sorted(set(issues))
    meta = dict(base)
    meta.update({
        "language": next(iter(langs), None) or "", "languages": list(langs), "language_shares": langs,
        "quality": "degraded" if all_issues else "good", "quality_issues": all_issues,
        "quality_by_stream": {"mixed": issues}, "retried": {"mixed": retried}, "model": cfg["model"],
        "backend": backend.describe(), "segments": len(segs),
        "streams": {"mixed": {"segments": len(res.segments), **vars(res.stats)}},
        "transcribe_seconds": round(seconds, 1),
    })
    outputs.atomic_write(out / "transcript.md", outputs.render_markdown(meta, segs, cfg["pipeline"]))
    outputs.atomic_write(out / "transcript.vtt", outputs.render_vtt(segs))
    outputs.atomic_write(out / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    start = meta["start"]
    outputs.update_index(cfg["output_dir"], {
        "date": meta["date"], "time": start[11:16], "start": start, "duration": meta["duration"],
        "duration_seconds": meta["duration_seconds"], "title": meta["title"],
        "path": search._rel(cfg, out), "quality": meta["quality"],
        "snippet": outputs.snippet(segs), "apps": meta["apps"], "languages": meta["languages"]})
    search.index_call(cfg, out)
    return meta


def reprocess(cfg: dict, target: str, model: str | None = None, backend_name: str | None = None,
              vocab_file: str | None = None, backend=None) -> Path:
    cfg = json.loads(json.dumps(cfg))
    if model:
        cfg["model"] = model
    if backend_name:
        cfg["backend"] = backend_name
    if vocab_file:
        vp = Path(vocab_file).expanduser()
        if not vp.exists():
            raise ReprocessError(f"vocabulary file not found: {vp}")
        cfg["vocabulary_file"] = str(vp)
    d = resolve_call(cfg, target)
    audio = find_audio(d)
    try:
        base = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        fm, _ = search.parse_transcript(d / "transcript.md")
        base = {k: fm[k] for k in ("title", "date", "start", "end", "duration", "duration_seconds", "apps") if k in fm}
    for k in ("language", "languages", "language_shares", "quality", "quality_issues", "quality_by_stream", "retried",
              "streams", "dropped_echo_segments"):
        base.pop(k, None)
    t0 = time.time()
    backend = backend or get_backend(cfg)
    log.event(_log, "reprocess_start", call=d.name, model=cfg["model"], backend=cfg["backend"])
    res, segs, issues, retried = _transcribe(cfg, audio, backend, parse_vtt_speakers(d / "transcript.vtt"))
    shutil.copy2(d / "transcript.md", d / "transcript.prev.md")  # only after the new run succeeded
    base["reprocessed"] = outputs.iso(time.time())
    _write(cfg, d, base, backend, res, segs, issues, retried, time.time() - t0)
    log.event(_log, "reprocess_done", call=d.name, segments=len(segs))
    return d


def save_file(cfg: dict, file: str, title: str | None = None, backend=None) -> Path:
    """Any audio/video file -> a call folder in output_dir (title from filename), indexed + searchable."""
    src = Path(file).expanduser()
    if not src.is_file():
        raise AudioError(f"file not found: {src}")
    t0 = time.time()
    st = src.stat()
    dur = _probe(src)
    if dur <= 0:
        raise AudioError(f"cannot read audio from {src}")
    start = getattr(st, "st_birthtime", st.st_mtime) or st.st_mtime
    title = title or re.sub(r"[_\s]+", " ", src.stem).strip() or "recording"
    out = outputs.output_folder(cfg["output_dir"], start, title)
    backend = backend or get_backend(cfg)
    log.event(_log, "save_start", file=str(src), out=str(out))
    res, segs, issues, retried = _transcribe(cfg, src, backend)
    out.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                        "-c:a", "aac", "-b:a", "64k", str(out / "call.m4a")], capture_output=True, text=True)
    if r.returncode != 0:
        log.event(_log, "save_audio_copy_failed", error=r.stderr[:200])
    base = {"title": title, "date": time.strftime("%Y-%m-%d", time.localtime(start)), "start": outputs.iso(start),
            "end": outputs.iso(start + dur), "duration": outputs.hms(dur), "duration_seconds": round(dur, 1),
            "apps": ["File"], "app_bundle_ids": [], "source_file": str(src), "created": outputs.iso(time.time())}
    _write(cfg, out, base, backend, res, segs, issues, retried, time.time() - t0)
    return out
