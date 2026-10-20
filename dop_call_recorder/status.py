"""`dop-recorder status` / `dop-recorder pause` / `dop-recorder resume` logic (M5)."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from dop_call_recorder import agentstate


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, TypeError, ValueError):
        return False
    except PermissionError:
        return True


def session_counts(cfg: dict) -> dict:
    counts: dict[str, int] = {}
    for sj in Path(cfg["work_dir"]).glob("*/session.json"):
        try:
            st = json.loads(sj.read_text(encoding="utf-8")).get("state", "?")
        except (OSError, ValueError):
            continue
        counts[st] = counts.get(st, 0) + 1
    return counts


def gather(cfg: dict, now: float | None = None) -> dict:
    """Merge status.json (live agent) with on-disk facts. `running` is False when the heartbeat is stale/stopped."""
    now = now or time.time()
    st = agentstate.read_status()
    running = agentstate.status_fresh(st, now) and st.get("state") != "stopped" and _pid_alive(st.get("pid"))
    counts = session_counts(cfg)
    out = dict(st) if running else {"state": "not running", "queue_len": counts.get("queued", 0) + counts.get("transcribing", 0)}
    out["running"] = running
    out["paused"] = agentstate.is_paused()  # control file is authoritative (agent applies it within seconds)
    out["sessions"] = counts
    if not running:
        out.setdefault("progress", None)
        out.setdefault("active", None)
    return out


def fmt_dur(sec) -> str:
    if sec is None:
        return "?"
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}h{m:02d}m" if h else (f"{m}m{s:02d}s" if m else f"{s}s")


def render(s: dict) -> str:
    L = []
    head = s["state"].upper()
    if s["running"]:
        L.append(f"agent: running (pid {s.get('pid')}), state: {head}" + (" [PAUSED]" if s["paused"] else ""))
    else:
        L.append("agent: NOT RUNNING (start `dop-recorder run` or load the LaunchAgent)" + (" [pause flag set]" if s["paused"] else ""))
    a = s.get("active")
    if a:
        L.append(f"recording: {a.get('title') or a.get('app')} — {fmt_dur(a.get('elapsed_s'))} (session {a.get('session')})")
    pr = s.get("progress")
    if pr:
        L.append(f"transcribing: {pr['session']} [{pr.get('stream')}] {pr['fraction'] * 100:.0f}%  elapsed {fmt_dur(pr['elapsed_s'])}  ETA {fmt_dur(pr['eta_s'])}")
    L.append(f"queue: {s.get('queue_len', 0)} waiting/in progress; sessions on disk: {s.get('sessions') or {}}")
    d = s.get("disk") or {}
    if d.get("free_gb") is not None:
        L.append(f"disk: {d['free_gb']} GB free ({d.get('level')})")
    lt = s.get("last_transcript")
    if lt:
        L.append(f"last transcript: {lt['path']}")
    e = s.get("last_error")
    if e:
        L.append(f"last error: {e['message']}")
    return "\n".join(L)
