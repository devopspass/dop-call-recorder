"""`dop-recorder doctor` (M5): environment checks with actionable fixes. Every check takes injectable system hooks for tests."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from dop_call_recorder import guards, paths

ROOT = paths.home()
RECORDER_APP = paths.app_path("recorder")
RECORDER_ID = paths.BUNDLE_ID
LABEL = paths.label()
OK, WARN, FAIL = "ok", "warn", "fail"
APPSUP = paths.state_dir()


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""
    id: str = ""
    group: str = ""
    required: bool = False
    action: dict | None = None


def _run(cmd: list[str], timeout: int = 15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))


def check_python(run=_run, version=sys.version_info, prefix=sys.prefix) -> Check:
    if tuple(version[:2]) < (3, 12):
        return Check("python", FAIL, f"python {version[0]}.{version[1]} < 3.12", "brew install python@3.13 && reinstall dop-call-recorder")
    return Check("python", OK, f"python {version[0]}.{version[1]} ({prefix})")


def check_deps(find_spec=importlib.util.find_spec, cfg: dict | None = None) -> list[Check]:
    out = []
    oa = (cfg or {}).get("backend") == "openai-whisper"   # whisper/torch are only required by that backend
    for mod, req in (("mlx_whisper", True), ("mlx", True), ("numpy", True), ("onnxruntime", True), ("webrtcvad", False),
                     ("whisper", False), ("torch", False)):
        ok = find_spec(mod) is not None
        if not ok and mod in ("whisper", "torch"):
            out.append(Check(f"dep:{mod}", FAIL if oa else OK, "missing" if oa else "not installed (not needed: MLX backend)",
                             "pip install openai-whisper torch" if oa else ""))
            continue
        out.append(Check(f"dep:{mod}", OK if ok else (FAIL if req else WARN), "importable" if ok else "missing" + ("" if req else " (optional: only for the openai-whisper backend)"),
                         "" if ok else "brew reinstall dop-call-recorder (dev: pip install -e .)"))
    return out


def check_models(cfg: dict) -> list[Check]:
    md, name = Path(cfg["model_dir"]), cfg["model"]
    pt = md / f"{name}.pt"
    mlx = Path(cfg.get("mlx", {}).get("model_path") or md / f"mlx-{name}")
    has = any((mlx / f).exists() for f in ("weights.npz", "weights.safetensors"))
    out = [Check("model:mlx", OK if has else FAIL, str(mlx) if has else f"{mlx} missing",
                 "" if has else "dop-recorder setup   (downloads + converts the model, no torch needed)")]
    if cfg.get("backend") == "openai-whisper":
        out.append(Check("model:openai-whisper", OK if pt.exists() else FAIL, str(pt) if pt.exists() else f"{pt} missing",
                         "" if pt.exists() else "run `dop-recorder transcribe <wav>` once (Azure CDN download) or copy the .pt there"))
    return out


def check_ffmpeg(which=paths.tool) -> list[Check]:
    return [Check(b, OK if which(b) else FAIL, which(b) or "not in PATH", "" if which(b) else "brew install ffmpeg") for b in ("ffmpeg", "ffprobe")]


def check_recorder(run=_run, app: Path = RECORDER_APP) -> Check:
    exe = app / "Contents" / "MacOS" / paths.APP_EXE
    if not exe.exists():
        return Check("recorder.app", FAIL, f"{app} not built", "`dop-recorder setup` (dev: app/build.sh)")
    r = run(["codesign", "--verify", "--strict", str(app)])
    if r.returncode != 0:
        return Check("recorder.app", FAIL, f"signature invalid: {r.stderr.strip()[:120]}", "app/build.sh  (then re-grant permissions)")
    d = run(["codesign", "-dv", str(app)])
    ident = next((l.split("=", 1)[1] for l in d.stderr.splitlines() if l.startswith("Identifier=")), "?")
    if ident != RECORDER_ID:
        return Check("recorder.app", WARN, f"signed with identifier {ident}, expected {RECORDER_ID}", "app/build.sh")
    return Check("recorder.app", OK, f"built + signed ({ident})")


def check_permissions(appsup: Path = APPSUP, work_dir: str | None = None, native: dict | None = None, tap: dict | None = None) -> list[Check]:
    """TCC state cannot be queried without Full Disk Access; use the evidence the recorder itself left behind."""
    mic, evidence = None, ""
    cands = []
    if work_dir:
        cands += sorted(Path(work_dir).glob("*/raw/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in cands[:5]:
        try:
            mic = json.loads(p.read_text()).get("mic_permission")
            evidence = str(p)
            if mic:
                break
        except (OSError, ValueError):
            pass
    log = appsup / "recorder.log"
    denied_log = False
    if log.exists():
        try:
            tail = log.read_text(errors="ignore")[-20000:].splitlines()
            denied_log = any("mic_permission_denied" in l for l in tail[-50:])
            if mic is None and any('"recording"' in l for l in tail):
                evidence = str(log)
        except OSError:
            pass
    nm = (native or {}).get("mic")
    if nm:  # asked the recorder bundle itself: authoritative, beats the evidence from past recordings
        mic = {"authorized": "authorized", "not_determined": None}.get(nm, "NOT_AUTHORIZED")
        evidence = "recorder bundle"
    if nm == "not_determined":
        c1 = Check("permission:microphone", FAIL, "not granted yet (macOS has not been asked; Fix… shows the prompt)",
                   "Fix… triggers the macOS prompt")
    elif mic == "authorized":
        c1 = Check("permission:microphone", OK, f"authorized (seen in {evidence})")
    elif mic == "NOT_AUTHORIZED" or denied_log:
        c1 = Check("permission:microphone", FAIL, "recorder reported mic permission NOT granted",
                   "System Settings > Privacy & Security > Microphone: enable DOP Call Recorder (or: tccutil reset Microphone "
                   f"{RECORDER_ID} then record once to get the prompt)")
    else:
        c1 = Check("permission:microphone", WARN, "unknown (no recording has happened yet)",
                   "start a test recording once (`dop-recorder run`, join a call) and accept the prompt; or System Settings > Privacy > Microphone")
    c2 = _system_audio(tap, work_dir)
    return [c1, c2]


def _system_audio(tap: dict | None, work_dir: str | None) -> Check:
    """No query API exists: prove it with a 1 s probe tap, else with the audio level of the last real recording."""
    fix = ("System Settings > Privacy & Security > Screen & System Audio Recording > enable the recorder app; "
           "if others.wav is silent after a call this is the cause")
    t = (tap or {}).get("tap")
    if t == "audio":
        return Check("permission:system-audio", OK, f"probe tap captured audio (peak {tap.get('peak', 0):.3f})")
    if t in ("create_failed", "aggregate_failed", "ioproc_failed", "format_invalid"):
        return Check("permission:system-audio", FAIL, f"could not create the audio tap ({t}, status {tap.get('status')})", fix)
    if work_dir:
        exe = RECORDER_APP / "Contents" / "MacOS" / paths.APP_EXE
        built = exe.stat().st_mtime if exe.exists() else 0  # a rebuild re-signs the app => older evidence says nothing about the current grant
        for p in sorted(Path(work_dir).glob("*/raw/state.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
            if p.stat().st_mtime < built:
                continue
            try:
                peak = (json.loads(p.read_text()).get("others") or {}).get("peak", 0)
            except (OSError, ValueError):
                continue
            if peak and peak > 0:
                return Check("permission:system-audio", OK, f"others audio captured in a past call (peak {peak}, {p.parent.parent.name})")
    if t in ("silent", "no_buffers"):
        return Check("permission:system-audio", WARN, "unknown: probe heard silence (nothing playing, or not granted) - confirmed only by a real call", fix)
    return Check("permission:system-audio", WARN, "unknown: not detectable without playing audio or a real call (macOS has no query API)", fix)


def check_accessibility(cfg: dict, run=None, app: Path | None = None) -> list[Check]:
    """X1: Accessibility permission of axhelper (speaker names / meeting title). Optional: WARN, never FAIL."""
    from dop_call_recorder import axpoll

    app = app or axpoll.AX_APP
    if not (cfg.get("ax") or {}).get("enabled", True):
        return [Check("accessibility", OK, "disabled in config ([ax] enabled=false)")]
    if not axpoll.helper_built(app):
        return [Check("accessibility", WARN, f"{app.name} not built - transcripts use Me/Others and generic titles", "app/build.sh")]
    kw = {"runner": run} if run else {}
    t = axpoll.check_trusted(app, **kw)
    fix = (f"System Settings > Privacy & Security > Accessibility > enable {app.stem} "
           f"(add it with + if missing: {app}); without it calls are transcribed as Me/Others")
    if t is None:
        return [Check("accessibility", WARN, "axhelper did not answer", "axhelper/build.sh; then retry")]
    if not t:
        return [Check("accessibility", WARN, f"{app.stem} has no Accessibility permission (speaker names/title disabled)", fix)]
    return [Check("accessibility", OK, f"{app.stem} trusted")]


def check_apps(cfg: dict, run=_run) -> list[Check]:
    out = []
    for b in cfg["apps"]:
        r = run(["mdfind", f"kMDItemCFBundleIdentifier == '{b}'"])
        found = [l for l in r.stdout.splitlines() if l.strip()]
        out.append(Check(f"app:{b}", OK if found else WARN, found[0] if found else "not found by Spotlight",
                         "" if found else "install the app or remove it from `apps` in config.toml"))
    return out


def check_output_dir(cfg: dict) -> Check:
    p = Path(cfg["output_dir"])
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if os.access(probe, os.W_OK):
        return Check("output_dir", OK, f"{p} writable" + ("" if p.exists() else " (will be created)"))
    return Check("output_dir", FAIL, f"{p} not writable", f"chmod/choose another output_dir in config.toml")


def check_disk(cfg: dict, free: int | None = None) -> Check:
    level, gb = guards.disk_level(cfg, free)
    st = {"ok": OK, "warn": WARN, "critical": FAIL}[level]
    return Check("disk space", st, f"{gb:.1f} GB free (warn<{cfg['disk']['warn_gb']}, critical<{cfg['disk']['critical_gb']})",
                 "" if level == "ok" else "free disk space; raw WAVs of failed sessions are auto-cleaned after disk.raw_max_age_days")


def check_launchd(run=_run, uid: int | None = None, agents_dir: Path | None = None) -> list[Check]:
    uid = os.getuid() if uid is None else uid
    plist = (agents_dir or paths.agents_dir()) / f"{LABEL}.plist"
    if not plist.exists():
        return [Check("launchd", WARN, "LaunchAgent not installed (fine if you run `dop-recorder run` manually)", "dop-recorder start")]
    r = run(["launchctl", "print", f"gui/{uid}/{LABEL}"])
    if r.returncode != 0:
        return [Check("launchd", WARN, "plist installed but not loaded", f"launchctl bootstrap gui/{uid} {plist}")]
    state = next((l.split("=", 1)[1].strip() for l in r.stdout.splitlines() if l.strip().startswith("state =")), "?")
    return [Check("launchd", OK if state == "running" else WARN, f"loaded, state={state}", "" if state == "running" else f"launchctl kickstart -k gui/{uid}/{LABEL}")]


def check_detector() -> Check:
    try:
        from dop_call_recorder import detector
        p = detector.read_probe()
        return Check("detector probe", OK, f"{len(p.get('processes', []))} audio processes, {len(p.get('windows', []))} windows visible")
    except Exception as e:
        return Check("detector probe", FAIL, f"{type(e).__name__}: {str(e)[:150]}", "reinstall: brew reinstall dop-call-recorder (dev: scripts/build-probe.sh)")


def check_speaker_detection(cfg: dict) -> Check:
    """Which mechanism names the speaker: learned CSS class, screenshot ring (needs Screen Recording), or the weak fallback."""
    from dop_call_recorder import ringdetect, speakerclass
    ax = cfg.get("ax", {})
    if not ax.get("ring_detect", True):
        return Check("speaker detection", WARN, "ring detection disabled; only AX markers / the lone unmuted participant name speakers")
    d = speakerclass.SpeakerClass().describe()
    try:
        from dop_call_recorder import axpoll
        if axpoll.helper_built() and ringdetect.SHOT_SERVICE is None:
            ringdetect.SHOT_ONESHOT = axpoll.oneshot_shot
        ok = ringdetect.capture(15) is not None
    except Exception:
        ok = False
    if not ok:
        return Check("speaker detection", WARN, f"{d}; screenshot capture FAILED (Screen Recording not granted to the AX helper app?)",
                     "System Settings > Privacy & Security > Screen Recording: enable DOP Call Recorder, then restart the agent")
    return Check("speaker detection", OK, d)


def check_logdir(cfg: dict) -> Check:
    d = Path(cfg["logging"]["file"]).expanduser().parent
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return Check("log dir", OK if os.access(d, os.W_OK) else FAIL, str(d), "" if os.access(d, os.W_OK) else f"mkdir -p {d}")


def check_agent(cfg: dict) -> Check:
    from dop_call_recorder import status
    s = status.gather(cfg)
    if s["running"]:
        return Check("agent process", OK, f"running (pid {s.get('pid')}), state {s['state']}")
    return Check("agent process", WARN, "no running `dop-recorder run` agent detected", "dop-recorder run   (or load the LaunchAgent)")


def run_all(cfg: dict) -> list[Check]:
    out = [check_python(), *check_deps(cfg=cfg), *check_models(cfg), *check_ffmpeg(), check_recorder(),
           *check_permissions(work_dir=cfg["work_dir"]), *check_accessibility(cfg), *check_apps(cfg), check_output_dir(cfg), check_disk(cfg),
           check_logdir(cfg), *check_launchd(), check_agent(cfg), check_detector(), check_speaker_detection(cfg)]
    return out


ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}


def render(checks: list[Check]) -> str:
    L = []
    for c in checks:
        L.append(f"{ICON[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != OK:
            L.append(f"     fix: {c.fix}")
    n_f = sum(c.status == FAIL for c in checks)
    n_w = sum(c.status == WARN for c in checks)
    L.append(f"\n{len(checks)} checks: {n_f} failed, {n_w} warnings")
    return "\n".join(L)


def exit_code(checks: list[Check]) -> int:
    return 1 if any(c.status == FAIL for c in checks) else 0
