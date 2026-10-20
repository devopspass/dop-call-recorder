"""`dop-recorder permissions`: the menu-bar "Permissions & health" model. Built on `dop-recorder doctor` checks (single source of truth) plus
native permission probes that must run INSIDE the bundle holding the TCC grant (via `open -W -n <app> --args <cmd> --out F`).
The menu app adds the Notifications row itself (only it can query UNUserNotificationCenter)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dop_call_recorder import doctor, paths
from dop_call_recorder.doctor import FAIL, OK, WARN, Check

PRIV = "x-apple.systempreferences:com.apple.preference.security?"
URLS = {
    "microphone": PRIV + "Privacy_Microphone",
    "system-audio": PRIV + "Privacy_AudioCapture",
    "accessibility": PRIV + "Privacy_Accessibility",
    "notifications": "x-apple.systempreferences:com.apple.Notifications-Settings.extension",
}
MENU_APP = paths.app_path("menubar")
MENU_ID = paths.BUNDLE_ID
AX_ID = paths.BUNDLE_ID
# Items whose failure means calls cannot be recorded properly: they drive the tray "attention" icon and the startup notification.
REQUIRED = {"microphone", "recorder-app", "notifications"}


def run_app(app: Path, args: list[str], timeout: float = 15, runner=subprocess.run) -> dict | None:
    """One-shot call of a helper bundle through LaunchServices so macOS attributes TCC to that bundle."""
    if not (app / "Contents" / "MacOS").exists():
        return None
    fd, tmp = tempfile.mkstemp(prefix="dop-cr-perm-", suffix=".json")
    os.close(fd)
    try:
        runner(["open", "-W", "-n", *paths.open_env(), str(app), "--args", *args, "--out", tmp], timeout=timeout, capture_output=True)
        txt = Path(tmp).read_text(encoding="utf-8").strip()
        return json.loads(txt) if txt else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        Path(tmp).unlink(missing_ok=True)


def _signed_as(app: Path, ident: str, name: str, build: str, run=doctor._run) -> Check:
    if not (app / "Contents" / "MacOS").exists():
        return Check(name, WARN, f"{app.name} not built", f"{build}")
    d = run(["codesign", "-dv", str(app)])
    got = next((l.split("=", 1)[1] for l in d.stderr.splitlines() if l.startswith("Identifier=")), "?")
    if run(["codesign", "--verify", "--strict", str(app)]).returncode != 0:
        return Check(name, FAIL, "signature invalid", f"{build} (then re-grant permissions)")
    if got != ident:
        return Check(name, WARN, f"signed as {got}, expected {ident}", build)
    return Check(name, OK, f"built + signed ({got})")


def _worst(cs: list[Check]) -> str:
    return FAIL if any(c.status == FAIL for c in cs) else (WARN if any(c.status == WARN for c in cs) else OK)


def _merge(id_: str, name: str, group: str, cs: list[Check]) -> Check:
    bad = [c for c in cs if c.status != OK]
    st = _worst(cs)
    detail = "; ".join(f"{c.name}: {c.detail}" for c in bad[:2]) if bad else f"{len(cs)} checks ok"
    fix = next((c.fix for c in bad if c.fix), "")
    return Check(name, st, detail, fix, id=id_, group=group)


def _tag(c: Check, id_: str, group: str, name: str | None = None) -> Check:
    c.id, c.group = id_, group
    if name:
        c.name = name
    return c


def action_for(id_: str, c: Check) -> dict | None:
    """What the menu's `Fix…` does. `dop-recorder permissions fix <id>` executes it; `text` = just show the hint."""
    if c.status == OK:
        return None
    if id_ in URLS and id_ != "notifications":
        return {"type": "cli", "label": "Fix…", "command": ["permissions", "fix", id_], "url": URLS[id_]}
    if id_ == "notifications":
        return {"type": "url", "label": "Open Notifications settings…", "url": URLS[id_]}
    if id_ == "agent":
        return {"type": "cli", "label": "Start agent", "command": ["permissions", "fix", "agent"]}
    if id_ == "launchd" and ("not loaded" in c.detail or "not installed" in c.detail):
        return {"type": "cli", "label": "Install and start LaunchAgent", "command": ["permissions", "fix", "launchd"]}
    return {"type": "text", "label": "How to fix…", "text": c.fix or c.detail} if (c.fix or c.detail) else None


def _exe_mtime() -> float:
    exe = doctor.RECORDER_APP / "Contents" / "MacOS" / paths.APP_EXE
    return exe.stat().st_mtime if exe.exists() else 0.0


def _proof_file() -> Path:
    from dop_call_recorder import agentstate
    return agentstate.state_dir() / "audio_proven.json"


def _cached_proof() -> dict | None:
    """Audio capture proven once for THIS build of the recorder (a rebuild re-signs it and resets the grant, so the proof expires)."""
    try:
        d = json.loads(_proof_file().read_text())
    except (OSError, ValueError):
        return None
    return {"tap": "audio", "peak": d.get("peak", 0), "cached": True} if d.get("exe_mtime") == _exe_mtime() else None


def _probe_with_tone(runner, tone: bool) -> dict | None:
    """The tap only hears what plays, so play a very quiet tone from another process (afplay) during the 1 s probe."""
    p = None
    if tone:
        try:
            import math, struct, wave
            f = Path(tempfile.gettempdir()) / "dop-probe-tone.wav"
            with wave.open(str(f), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000 * 2)))
            p = subprocess.Popen(["afplay", "-v", "0.15", str(f)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.3)
        except OSError:
            p = None
    try:
        return run_app(doctor.RECORDER_APP, ["probe-tap"], 15, runner)
    finally:
        if p:
            p.terminate()


def collect(cfg: dict, probe: bool = False, runner=subprocess.run, recording: bool | None = None, tone: bool | None = None) -> list[dict]:
    """All items for the submenu. probe=True also does the 1 s tap probe (skipped while a call is being recorded)."""
    from dop_call_recorder import axpoll, status

    if recording is None:
        try:
            recording = str(status.gather(cfg).get("state", "")).lower() == "recording"
        except Exception:
            recording = False
    native = run_app(doctor.RECORDER_APP, ["check-permissions"], 10, runner)
    if tone is None:
        tone = runner is subprocess.run
    tap = _probe_with_tone(runner, tone) if (probe and not recording) else None
    if tap and tap.get("tap") == "audio":
        try:
            _proof_file().parent.mkdir(parents=True, exist_ok=True)
            _proof_file().write_text(json.dumps({"exe_mtime": _exe_mtime(), "peak": tap.get("peak", 0), "ts": time.time()}))
        except OSError:
            pass
    elif not tap or tap.get("tap") in ("silent", "no_buffers"):
        tap = _cached_proof() if not tap else (_cached_proof() or tap)
    perms = doctor.check_permissions(work_dir=cfg["work_dir"], native=native, tap=tap)
    items = [
        _tag(perms[0], "microphone", "Permissions", "Microphone (recorder)"),
        _tag(perms[1], "system-audio", "Permissions", "System audio capture (recorder)"),
    ]
    items += [_tag(c, "accessibility", "Permissions", "Accessibility (speaker names / titles)") for c in doctor.check_accessibility(cfg)]
    items.append(_tag(doctor.check_recorder(), "recorder-app", "Apps", "DOP Call Recorder app"))
    apps = doctor.check_apps(cfg)
    items.append(_merge("call-apps", "Call apps installed", "Apps", apps))
    items.append(_tag(doctor.check_agent(cfg), "agent", "Runtime", "Agent (`dop-recorder run`)"))
    items += [_tag(c, "launchd", "Runtime", "LaunchAgent") for c in doctor.check_launchd()]
    items.append(_tag(doctor.check_detector(), "detector", "Runtime", "Call detector probe"))
    models = doctor.check_models(cfg)
    items.append(_merge("model", "Whisper model", "Resources", models))
    items.append(_merge("ffmpeg", "ffmpeg / ffprobe", "Resources", doctor.check_ffmpeg()))
    items.append(_tag(doctor.check_disk(cfg), "disk", "Resources", "Disk space"))
    items.append(_tag(doctor.check_output_dir(cfg), "output-dir", "Resources", "Output folder writable"))
    items.append(_merge("python", "Python environment", "Resources", [doctor.check_python(), *doctor.check_deps(cfg=cfg), doctor.check_logdir(cfg)]))
    for c in items:
        c.required = c.id in REQUIRED
        c.action = action_for(c.id, c)
    return [vars(c) for c in items]


def summarize(items: list[dict]) -> dict:
    nf = sum(i["status"] == FAIL for i in items)
    nw = sum(i["status"] == WARN for i in items)
    need = [i["id"] for i in items if i["required"] and i["status"] == FAIL]
    return {"failed": nf, "warnings": nw, "attention": bool(need), "attention_ids": need,
            "title": "✅ All OK" if not nf and not nw else " ".join(x for x in (f"❌ {nf} missing" if nf else "", f"⚠️ {nw} warning" if nw else "") if x)}


def _auto_probe_due(now: float, every: float = 1800) -> bool:
    """Unproven grant (fresh install / rebuilt app): run the quiet-tone probe by itself, but at most every 30 min."""
    if _cached_proof():
        return False
    f = _proof_file().with_name("audio_probe_attempt.json")
    try:
        if now - json.loads(f.read_text()).get("ts", 0) < every:
            return False
    except (OSError, ValueError):
        pass
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"ts": now}))
    except OSError:
        pass
    return True


def document(cfg: dict, probe: bool = False) -> dict:
    items = collect(cfg, probe=probe or _auto_probe_due(time.time()))
    return {"ts": time.time(), "summary": summarize(items), "items": items}


def write(doc: dict) -> Path:
    from dop_call_recorder import agentstate
    p = agentstate.state_dir() / "health.json"
    agentstate._atomic(p, doc)
    return p


def _install_launchagent(cfg: dict, runner=subprocess.run) -> dict:
    """Render the plist (service.render), hand the agent over to launchd. Never interrupts a recording."""
    import signal
    from dop_call_recorder import status

    st = status.gather(cfg)
    if str(st.get("state", "")).lower() == "recording":
        return {"ok": False, "message": "a call is being recorded - try again after it ends"}
    from dop_call_recorder import service

    label = service.label_for("agent")
    plist = service._plist("agent")
    want = service.render("agent")
    if not plist.exists() or plist.read_text() != want:   # also refreshes a plist that points at an old binary
        plist.parent.mkdir(parents=True, exist_ok=True)
        paths.logs_dir().mkdir(parents=True, exist_ok=True)
        plist.write_text(want)
    pid = st.get("pid") if st.get("running") else None
    if pid and pid != os.getpid():  # a manually started agent would run next to the launchd one
        try:
            os.kill(int(pid), signal.SIGTERM)
            for _ in range(60):
                time.sleep(0.5)
                os.kill(int(pid), 0)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    uid = str(os.getuid())
    runner(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True, text=True, timeout=15)
    r = runner(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], capture_output=True, text=True, timeout=15)
    return {"ok": r.returncode == 0, "message": (r.stderr or "LaunchAgent installed and started").strip()[:200]}


def fix(id_: str, cfg: dict, runner=subprocess.run) -> dict:
    """Run the one-click fix for an item. Returns {ok, message}."""
    def open_url(u):
        runner(["open", u], capture_output=True, timeout=10)

    if id_ == "microphone":
        r = run_app(doctor.RECORDER_APP, ["request-mic"], 70, runner)
        if r and r.get("mic") == "authorized":
            return {"ok": True, "message": "Microphone granted"}
        open_url(URLS["microphone"])
        return {"ok": False, "message": "opened System Settings > Microphone: enable the recorder app"}
    if id_ == "system-audio":
        _probe_with_tone(runner, runner is subprocess.run)  # makes the app show up / prompts
        open_url(URLS["system-audio"])
        return {"ok": False, "message": "opened System Settings > Screen & System Audio Recording: enable the recorder app"}
    if id_ == "accessibility":
        from dop_call_recorder import axpoll
        axpoll.run_helper(["request"], timeout=60)
        open_url(URLS["accessibility"])
        return {"ok": False, "message": "opened System Settings > Accessibility: enable the Accessibility helper app"}
    if id_ == "agent":
        subprocess.Popen([sys.executable, "-m", "dop_call_recorder", "run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "message": "started `dop-recorder run`"}
    if id_ == "launchd":
        return _install_launchagent(cfg, runner)
    if id_ == "notifications":
        open_url(URLS["notifications"])
        return {"ok": False, "message": "opened Notifications settings"}
    return {"ok": False, "message": f"no automatic fix for {id_}"}
