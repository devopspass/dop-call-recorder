"""Housekeeping guards (M5): disk-space guard, raw-WAV auto-clean, retention cleanup, caffeinate, log rotation."""
from __future__ import annotations

import json
import logging.handlers
import os
import shutil
import subprocess
import time
from pathlib import Path

from dop_call_recorder import log

_log = log.get("guards")
GB = 1024 ** 3
DAY = 86400.0


# ---------------------------------------------------------------- disk space

def free_bytes(path: str | Path) -> int:
    p = Path(path).expanduser()
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(p).free


def disk_level(cfg: dict, free: int | None = None) -> tuple[str, float]:
    """-> ("ok"|"warn"|"critical", free_gb) for the volume holding work_dir."""
    d = cfg.get("disk", {})
    free = free_bytes(cfg["work_dir"]) if free is None else free
    gb = free / GB
    if gb < float(d.get("critical_gb", 1)):
        return "critical", gb
    if gb < float(d.get("warn_gb", 5)):
        return "warn", gb
    return "ok", gb


def clean_old_raw(cfg: dict, now: float | None = None, force: bool = False) -> list[str]:
    """Delete raw WAV chunks of finished sessions (done: always; failed: older than disk.raw_max_age_days).
    force=True (critical disk) also removes raw chunks of any non-active failed session regardless of age.
    Sessions that are recording/queued/transcribing/finalizing are never touched."""
    now = now or time.time()
    max_age = float(cfg.get("disk", {}).get("raw_max_age_days", 7)) * DAY
    removed: list[str] = []
    work = Path(cfg["work_dir"])
    for sj in work.glob("*/session.json"):
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        state = data.get("state")
        if state not in ("done", "failed"):
            continue
        raw = Path(data.get("raw_dir") or sj.parent / "raw")
        age = now - (data.get("updated") or sj.stat().st_mtime)
        if state == "failed" and not force and age < max_age:
            continue
        for w in raw.glob("*_[0-9][0-9][0-9][0-9].wav"):
            try:
                w.unlink()
                removed.append(str(w))
            except OSError:
                pass
    if removed:
        log.event(_log, "raw_wavs_cleaned", count=len(removed), force=force)
    return removed


# ---------------------------------------------------------------- retention

def cleanup_retention(cfg: dict, now: float | None = None) -> dict:
    """Delete call.m4a older than retention_days (transcripts/vtt/meta are kept). Also prunes finished session dirs
    (only session.json left) older than retention_days. retention_days <= 0 disables."""
    days = float(cfg.get("retention_days", 30))
    res = {"m4a_deleted": 0, "bytes_freed": 0, "sessions_pruned": 0}
    if days <= 0:
        return res
    now = now or time.time()
    cutoff = now - days * DAY
    out = Path(cfg["output_dir"])
    for m in out.glob("*/*/call.m4a"):
        try:
            if m.stat().st_mtime < cutoff:
                size = m.stat().st_size
                m.unlink()
                res["m4a_deleted"] += 1
                res["bytes_freed"] += size
        except OSError:
            pass
    for sj in Path(cfg["work_dir"]).glob("*/session.json"):
        try:
            data = json.loads(sj.read_text(encoding="utf-8"))
            if data.get("state") == "done" and (data.get("updated") or sj.stat().st_mtime) < cutoff:
                shutil.rmtree(sj.parent)
                res["sessions_pruned"] += 1
        except (OSError, ValueError):
            pass
    if res["m4a_deleted"] or res["sessions_pruned"]:
        log.event(_log, "retention_cleanup", **res)
    return res


# ---------------------------------------------------------------- caffeinate

class Caffeinate:
    """Holds an idle-sleep power assertion (`caffeinate -i -w <our pid>`) while recording/transcribing.
    -w makes caffeinate exit by itself if we die, so a crash can never leave a stuck assertion."""

    def __init__(self, popen=subprocess.Popen):
        self._popen = popen
        self.proc = None

    @property
    def held(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def set(self, busy: bool) -> None:
        if busy and not self.held:
            try:
                self.proc = self._popen(["caffeinate", "-i", "-w", str(os.getpid())],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                log.event(_log, "caffeinate_on", pid=self.proc.pid)
            except OSError as e:
                log.event(_log, "caffeinate_failed", error=str(e))
        elif not busy and self.proc is not None:
            self.release()

    def release(self) -> None:
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            p.terminate()
            log.event(_log, "caffeinate_off")


# ---------------------------------------------------------------- log rotation

class RotatingJsonl:
    """File-like (write/flush/close, context manager) with size-based rotation, for detector.jsonl."""

    def __init__(self, path: str | Path, max_bytes: int = 10 * 1024 * 1024, backups: int = 5):
        p = Path(path).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        self._h = logging.handlers.RotatingFileHandler(p, maxBytes=max_bytes, backupCount=backups, encoding="utf-8", delay=True)
        self._h.terminator = ""

    def write(self, s: str) -> int:
        import logging as _l
        self._h.emit(_l.LogRecord("jsonl", _l.INFO, "", 0, s, None, None))
        return len(s)

    def flush(self) -> None:
        self._h.flush()

    def close(self) -> None:
        self._h.close()

    def __enter__(self): return self
    def __exit__(self, *a): self.close()


def rotate_copytruncate(path: str | Path, max_bytes: int = 10 * 1024 * 1024, backups: int = 3) -> bool:
    """Rotate a log we do not own the writer of (launchd stdout/stderr files): copy to .1 and truncate in place."""
    p = Path(path).expanduser()
    try:
        if not p.exists() or p.stat().st_size < max_bytes:
            return False
        for i in range(backups, 1, -1):
            src = p.with_name(f"{p.name}.{i - 1}")
            if src.exists():
                os.replace(src, p.with_name(f"{p.name}.{i}"))
        shutil.copyfile(p, p.with_name(p.name + ".1"))
        with open(p, "r+b") as f:
            f.truncate(0)
        return True
    except OSError:
        return False
