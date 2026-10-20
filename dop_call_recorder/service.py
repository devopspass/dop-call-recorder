"""`dop-recorder setup | start | stop | uninstall`: install the signed apps for the current user, gate + launch the LaunchAgents.

Design: the .app bundles are built + ad-hoc signed ONCE (Homebrew formula / build.sh). `setup` only COPIES them to
~/Applications/DOP Call Recorder (no re-signing: a new signature = new cdhash = macOS drops the TCC grants), and stores
their cdhash. `start` refuses to run while required permissions/resources are missing and says when the apps changed since
the permissions were last verified (=> re-grant after an upgrade).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from dop_call_recorder import paths

KINDS = ("app",)   # a single bundle hosts recorder + Accessibility helper + menu bar
TCC_SERVICES = ("Microphone", "ScreenCapture", "Accessibility", "AudioCapture")
# health items that make `start` fail (besides the REQUIRED set in health.py)
GATE_IDS = {"microphone", "recorder-app", "model", "ffmpeg", "detector"}


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def cdhash(app: Path) -> str:
    r = _run(["codesign", "-dvvv", str(app)])
    for line in r.stderr.splitlines():
        if line.startswith("CDHash="):
            return line.split("=", 1)[1].strip()
    return ""


def _apps_file() -> Path:
    return paths.state_dir() / "apps.json"


def _load_apps() -> dict:
    try:
        return json.loads(_apps_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_apps(d: dict) -> None:
    paths.state_dir().mkdir(parents=True, exist_ok=True)
    _apps_file().write_text(json.dumps(d, indent=1))


def _source_app(kind: str) -> Path | None:
    name = paths.APP_NAME
    for p in (paths.home() / "libexec" / "apps" / name, paths.home() / "app" / "build" / name):
        if (p / "Contents" / "MacOS").exists():
            return p
    return None


def install_apps() -> list[str]:
    """Copy the built apps to the per-user apps dir (ditto keeps the signature). Returns human-readable lines."""
    out, rec = [], _load_apps()
    inst = rec.setdefault("installed", {})
    dst_dir = paths.apps_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    for legacy in paths.LEGACY_APP_NAMES:   # pre-merge installs (3 separate apps)
        if (dst_dir / legacy).exists():
            _run(["rm", "-rf", str(dst_dir / legacy)])
            out.append(f"{legacy}: removed (replaced by {paths.APP_NAME})")
    for kind in KINDS:
        src = _source_app(kind)
        if not src:
            out.append(f"{kind}: no built app found (brew reinstall dop-call-recorder; dev: app/build.sh)")
            continue
        dst = dst_dir / paths.APP_NAME
        want = cdhash(src)
        if dst.exists() and cdhash(dst) == want:
            out.append(f"{dst.name}: up to date")
        else:
            if dst.exists():
                _run(["rm", "-rf", str(dst)])
            r = _run(["ditto", str(src), str(dst)])
            if r.returncode != 0:
                raise RuntimeError(f"copy {src} -> {dst} failed: {r.stderr.strip()}")
            out.append(f"{dst.name}: installed to {dst}")
        inst[kind] = want
    _save_apps(rec)
    return out


def grants_stale() -> bool:
    """True if the installed apps differ from the ones whose permissions were last verified OK."""
    rec = _load_apps()
    proven = rec.get("proven")
    return bool(proven) and proven != rec.get("installed")


def _mark_proven() -> None:
    rec = _load_apps()
    if rec.get("installed"):
        rec["proven"] = dict(rec["installed"])
        _save_apps(rec)


def setup(cfg: dict, prompt: bool = True, fetch_model: bool = True, log=print) -> int:
    from dop_call_recorder import health, model_fetch

    paths.state_dir().mkdir(parents=True, exist_ok=True)
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    log(f"state:  {paths.state_dir()}\nlogs:   {paths.logs_dir()}")
    for line in install_apps():
        log("app:    " + line)
    if fetch_model:
        log(f"model:  {cfg['model']} -> {cfg['model_dir']}")
        mdir = model_fetch.ensure(cfg)
        log(f"model ok: {mdir}")
    if prompt:
        log("permissions: macOS will ask now; approve each prompt (System Settings panes open when needed)")
        for pid in ("microphone", "system-audio", "accessibility"):
            r = health.fix(pid, cfg)
            log(f"  {pid}: {r['message']}")
    log("\nnext: `dop-recorder start`   (it refuses to start until required permissions are granted; `dop-recorder permissions` shows what is missing)")
    return 0


# ---------------------------------------------------------------- launchd

def _plist(kind: str) -> Path:
    return paths.agents_dir() / f"{label_for(kind)}.plist"


def label_for(kind: str) -> str:
    return paths.label("" if kind == "agent" else "menubar")


def _env_block() -> str:
    env = {"DOP_RECORDER_BIN": paths.recorder_bin()}
    for k in ("DOP_STATE_DIR", "DOP_HOME", "DOP_CR_CONFIG", "DOP_LAUNCHAGENTS_DIR", "HOMEBREW_PREFIX"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;")
    return "\n".join(f"        <key>{k}</key>\n        <string>{esc(v)}</string>" for k, v in env.items())


def render(kind: str) -> str:
    tpl = (paths.PKG / "data" / f"{kind}.plist.template").read_text()
    rbin = paths.recorder_bin()
    prefixes = [str(Path(rbin).parent), os.environ.get("HOMEBREW_PREFIX", "") and os.environ["HOMEBREW_PREFIX"] + "/bin",
                "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    path = ":".join(dict.fromkeys(p for p in prefixes if p))
    menu_exe = paths.app_path("menubar") / "Contents" / "MacOS" / paths.APP_EXE
    vals = {"@LABEL@": label_for(kind), "@RECORDER_BIN@": rbin, "@HOME@": str(Path.home()), "@LOGS@": str(paths.logs_dir()),
            "@PATH@": path, "@MENU_EXE@": str(menu_exe), "@ENV@": _env_block()}
    for k, v in vals.items():
        tpl = tpl.replace(k, v)
    return tpl


def lint(text: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".plist", delete=False) as f:
        f.write(text)
    try:
        r = _run(["plutil", "-lint", f.name])
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    finally:
        Path(f.name).unlink(missing_ok=True)


def _uid() -> str:
    return str(os.getuid())


def loaded(kind: str) -> bool:
    return _run(["launchctl", "print", f"gui/{_uid()}/{label_for(kind)}"]).returncode == 0


def install_launchagent(kind: str) -> str:
    p = _plist(kind)
    p.parent.mkdir(parents=True, exist_ok=True)
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    p.write_text(render(kind))
    _run(["launchctl", "bootout", f"gui/{_uid()}/{label_for(kind)}"])
    r = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(p)])
    for _ in range(5):   # bootout is asynchronous: bootstrap right after it can fail with "Input/output error" (5)
        if r.returncode != 5:
            break
        time.sleep(1)
        r = _run(["launchctl", "bootstrap", f"gui/{_uid()}", str(p)])
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap {label_for(kind)} failed: {(r.stderr or r.stdout).strip()}")
    return f"loaded {label_for(kind)}"


def gate(cfg: dict) -> tuple[list[str], list[str]]:
    """(blocking problems, notes). Reuses the health model of the menu-bar app: no new probe logic."""
    from dop_call_recorder import health

    doc = health.document(cfg)
    bad = [f"{i['name']}: {i['detail']}" + (f"  ->  {i['fix']}" if i.get("fix") else "")
           for i in doc["items"] if i["status"] == "fail" and (i["required"] or i["id"] in GATE_IDS)]
    notes = []
    if grants_stale():
        notes.append("the apps changed since permissions were last verified (upgrade/rebuild): macOS drops grants for a new signature - "
                     "re-enable DOPCallRecorder in System Settings > Privacy & Security (Microphone, Screen & System Audio Recording, Accessibility)")
    return bad, notes


def start(cfg: dict, menubar: bool = True, dry_run: bool = False, log=print) -> int:
    bad, notes = gate(cfg)
    for n in notes:
        log("note: " + n)
    kinds = ["agent"] + (["menubar"] if menubar else [])
    if dry_run:
        for k in kinds:
            ok, msg = lint(render(k))
            log(f"[dry-run] {label_for(k)}: plist {'OK' if ok else 'INVALID ' + msg}  (would write {_plist(k)})")
        log("[dry-run] gate: " + ("OK" if not bad else "BLOCKED"))
        for b in bad:
            log("  - " + b)
        return 0 if not bad else 1
    if bad:
        log("not starting: required items are not ready")
        for b in bad:
            log("  - " + b)
        log("fix them (`dop-recorder setup`, `dop-recorder permissions`), then run `dop-recorder start` again")
        return 1
    _mark_proven()
    for k in kinds:
        log(install_launchagent(k))
    if not menubar:
        _remove("menubar")
    log("started. status: `dop-recorder status`   stop: `dop-recorder stop`")
    return 0


def _remove(kind: str) -> bool:
    _run(["launchctl", "bootout", f"gui/{_uid()}/{label_for(kind)}"])
    p = _plist(kind)
    existed = p.exists()
    p.unlink(missing_ok=True)
    return existed


def stop(log=print) -> int:
    try:
        from dop_call_recorder import recorder_bridge
        recorder_bridge.stop()  # finish a running recording cleanly (chunks stay queued)
    except Exception:
        pass
    for k in ("menubar", "agent"):
        log(f"{label_for(k)}: {'removed' if _remove(k) else 'not installed'}")
    return 0


def uninstall(cfg: dict, purge: bool = False, purge_models: bool = False, log=print) -> int:
    """Never touches transcripts (output_dir)."""
    import shutil

    stop(log)
    for kind in KINDS:
        shutil.rmtree(paths.apps_dir() / paths.APP_NAME, ignore_errors=True)
    for legacy in paths.LEGACY_APP_NAMES:
        shutil.rmtree(paths.apps_dir() / legacy, ignore_errors=True)
    try:
        paths.apps_dir().rmdir()
    except OSError:
        pass
    log(f"removed apps from {paths.apps_dir()}")
    if not paths.isolated():
        for bid in (paths.BUNDLE_ID, *paths.LEGACY_BUNDLE_IDS):
            for svc in TCC_SERVICES:
                _run(["tccutil", "reset", svc, bid])
        log("reset macOS permissions of the apps")
    if purge:
        shutil.rmtree(paths.state_dir(), ignore_errors=True)
        shutil.rmtree(paths.logs_dir(), ignore_errors=True)
        log(f"purged state {paths.state_dir()} and logs {paths.logs_dir()} (transcripts in {cfg.get('output_dir')} are kept)")
    if purge_models:
        md = Path(cfg["model_dir"])
        for p in md.glob("mlx-*"):
            shutil.rmtree(p, ignore_errors=True)
            log(f"removed {p}")
    log("done. then: brew uninstall dop-call-recorder")
    return 0
