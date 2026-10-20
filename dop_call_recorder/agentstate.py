"""Shared agent state (M5): status.json (written by `dop-recorder run`), control.json (pause flag, written by CLI/menu bar), progress.

Files live in the recorder's app-support dir (override with $DOP_STATE_DIR) so the Swift menu-bar app,
`dop-recorder status|pause|resume` and the agent all agree. All writes are atomic; readers tolerate missing/corrupt files.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

STALE_SECONDS = 20  # status.json older than this => agent is not running


def state_dir() -> Path:
    from dop_call_recorder import paths
    return paths.state_dir()


def _atomic(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def status_path() -> Path: return state_dir() / "status.json"
def control_path() -> Path: return state_dir() / "control.json"


def write_status(d: dict) -> None:
    _atomic(status_path(), {**d, "ts": time.time(), "pid": os.getpid()})


def read_status() -> dict:
    return _read(status_path())


def status_fresh(st: dict, now: float | None = None) -> bool:
    return bool(st) and (now or time.time()) - st.get("ts", 0) < STALE_SECONDS


def is_paused() -> bool:
    return bool(_read(control_path()).get("paused"))


def set_paused(paused: bool, by: str = "cli") -> None:
    _atomic(control_path(), {"paused": bool(paused), "by": by, "ts": time.time()})


# ---------------------------------------------------------------- transcription progress (in-process)

class Progress:
    """Audio-seconds based progress of the session being transcribed, with ETA from the observed rate."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.session = None
            self.total = 0.0
            self.done: dict[str, float] = {}
            self.t0 = 0.0
            self.stage = ""

    def begin(self, session: str, total_seconds: float):
        with self._lock:
            self.session, self.total, self.done, self.t0, self.stage = session, max(total_seconds, 0.001), {}, time.time(), "starting"

    def advance(self, label: str, upto_seconds: float):
        base = label.split(":")[0]  # "others:retry" must not rewind the bar
        with self._lock:
            self.done[base] = max(self.done.get(base, 0.0), upto_seconds)
            self.stage = base

    def snapshot(self, now: float | None = None) -> dict | None:
        with self._lock:
            if not self.session:
                return None
            now = now or time.time()
            frac = min(sum(self.done.values()) / self.total, 0.99)
            el = now - self.t0
            eta = (el / frac * (1 - frac)) if frac > 0.02 and el > 5 else None
            return {"session": self.session, "stream": self.stage, "fraction": round(frac, 3),
                    "elapsed_s": round(el, 1), "eta_s": None if eta is None else round(eta, 0)}


PROGRESS = Progress()
