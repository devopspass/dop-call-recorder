"""macOS notifications via osascript (no extra dependencies). Never raises."""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid

from dop_call_recorder import log

_log = log.get("notify")


def _q(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _via_menubar(title: str, message: str, sound: str, wait: float = 4.0) -> bool:
    """Queue the notification for the menu-bar app (so the banner carries our icon). True only if it was consumed."""
    from dop_call_recorder import agentstate
    if subprocess.run(["pgrep", "-f", "DOPCallRecorder.app/Contents/MacOS/dop-call-recorder$"], capture_output=True).returncode != 0:
        return False
    qdir = agentstate.state_dir() / "notify"
    qdir.mkdir(parents=True, exist_ok=True)
    last = message.rsplit("\n", 1)[-1]
    req = {"title": title, "message": message, "sound": sound, "open": last if last.startswith("/") and os.path.exists(last) else ""}
    f = qdir / f"{time.time():.3f}-{uuid.uuid4().hex[:6]}.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(req))
    tmp.rename(f)
    end = time.time() + wait
    while time.time() < end:
        if not f.exists():
            return True
        time.sleep(0.1)
    f.unlink(missing_ok=True)
    return False


def notify(title: str, message: str, cfg: dict | None = None, kind: str = "info", sound: str | None = None) -> bool:
    """kind: start | done | error | warn | info. Honors [notify] enabled/on_<kind>. $DOP_CR_NOTIFY_CMD overrides the sender (tests)."""
    nc = (cfg or {}).get("notify", {})
    if not nc.get("enabled", True) or not nc.get(f"on_{kind}", True):
        return False
    sound = sound if sound is not None else nc.get("sound", "")
    override = os.environ.get("DOP_CR_NOTIFY_CMD")
    try:
        if override:
            cmd = [override, title, message]
        elif _via_menubar(title, message, sound):
            log.event(_log, "notification", title=title, body=message, kind=kind, rc=0, via="menubar")
            return True
        else:
            script = f'display notification "{_q(message)}" with title "{_q(title)}"'
            if sound:
                script += f' sound name "{_q(sound)}"'
            cmd = ["osascript", "-e", script]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        log.event(_log, "notification", title=title, body=message, kind=kind, rc=r.returncode)
        return r.returncode == 0
    except Exception as e:  # notifications must never break recording
        log.event(_log, "notification_failed", title=title, error=str(e)[:200])
        return False
