"""Single source of truth for every location the app touches (install prefix, state, logs, apps, LaunchAgents).

Env overrides:
  DOP_HOME            install prefix holding libexec/apps, config.toml, vocabulary.yaml (Homebrew sets it; dev checkout = repo root)
  DOP_STATE_DIR       moves ALL per-user state into one directory (isolated test runs): state, logs, models, apps,
                      LaunchAgents and the launchd label suffix. Legacy alias: DOP_CR_STATE_DIR (state only).
  DOP_LAUNCHAGENTS_DIR  override the LaunchAgents directory
  DOP_CR_CONFIG       config file path
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
from pathlib import Path

PKG = Path(__file__).resolve().parent
BASE_LABEL = "com.devopspass.call-recorder"
# ONE app bundle = one macOS permission identity; it hosts the recorder, the Accessibility helper and the menu-bar app.
APP_NAME = "DOPCallRecorder.app"
APP_EXE = "dop-call-recorder"
BUNDLE_ID = BASE_LABEL
APP_NAMES = {"recorder": APP_NAME, "menubar": APP_NAME, "axhelper": APP_NAME}   # kind -> app (kinds kept for call sites)
LEGACY_APP_NAMES = ("DOPCallRecorderRecorder.app", "DOPCallRecorderMenu.app", "DOPCallRecorderAX.app")   # pre-merge installs
LEGACY_BUNDLE_IDS = tuple(f"{BASE_LABEL}.{k}" for k in ("recorder", "axhelper", "menubar"))


def home() -> Path:
    """Install prefix. Dev checkout: the repo root (package dir's parent)."""
    return Path(os.environ.get("DOP_HOME") or PKG.parent).expanduser()


def isolated() -> bool:
    return bool(os.environ.get("DOP_STATE_DIR"))


def state_dir() -> Path:
    e = os.environ.get("DOP_STATE_DIR") or os.environ.get("DOP_CR_STATE_DIR")
    return Path(e or "~/Library/Application Support/dop-call-recorder").expanduser()


def logs_dir() -> Path:
    return state_dir() / "logs" if isolated() else Path("~/Library/Logs/dop-call-recorder").expanduser()


def models_dir() -> Path:
    return state_dir() / "models" if isolated() else Path("~/.cache/whisper").expanduser()


def work_dir() -> Path:
    return state_dir() / "sessions"


def apps_dir() -> Path:
    return state_dir() / "Applications" if isolated() else Path("~/Applications/DOP Call Recorder").expanduser()


def agents_dir() -> Path:
    e = os.environ.get("DOP_LAUNCHAGENTS_DIR")
    if e:
        return Path(e).expanduser()
    return state_dir() / "LaunchAgents" if isolated() else Path("~/Library/LaunchAgents").expanduser()


def label(kind: str = "") -> str:
    """launchd label: com.devopspass.call-recorder[.menubar][.t<hash>] (hash only for isolated runs)."""
    base = BASE_LABEL + (f".{kind}" if kind else "")
    if isolated():
        base += ".t" + hashlib.sha1(str(state_dir()).encode()).hexdigest()[:6]
    return base


def config_path() -> Path:
    e = os.environ.get("DOP_CR_CONFIG")
    if e:
        return Path(e).expanduser()
    user = state_dir() / "config.toml"
    return user if user.exists() else packaged_config()


def packaged_config() -> Path:
    for p in (home() / "config.toml", PKG / "data" / "config.toml"):
        if p.exists():
            return p
    return home() / "config.toml"


def app_path(kind: str) -> Path:
    """Where the app (every kind is the same bundle) lives: installed copy, then install prefix, then dev build."""
    name = APP_NAMES[kind]
    cands = [apps_dir() / name, home() / "libexec" / "apps" / name, home() / "app" / "build" / name]
    for c in cands:
        if c.exists():
            return c
    return cands[-1] if not (home() / "libexec").exists() else cands[1]


def probe_path() -> Path:
    for p in (home() / "libexec" / "bin" / "audioprobe", home() / "scripts" / "audioprobe"):
        if p.exists():
            return p
    return home() / "scripts" / "audioprobe"


def tool(name: str) -> str | None:
    """Locate an external tool (ffmpeg, whisper-cli ...) on PATH or under the Homebrew prefix."""
    found = shutil.which(name)
    if found:
        return found
    for pre in (os.environ.get("HOMEBREW_PREFIX"), "/opt/homebrew", "/usr/local"):
        if pre and (Path(pre) / "bin" / name).exists():
            return str(Path(pre) / "bin" / name)
    return None


def recorder_bin() -> str:
    """Absolute path of the dop-recorder executable (for plists / menubar helper)."""
    return (os.environ.get("DOP_RECORDER_BIN") or shutil.which("dop-recorder")
            or str(Path(sys.executable).with_name("dop-recorder")))


def ensure_path() -> None:
    """Make Homebrew tools (ffmpeg, whisper-cli) resolvable even under launchd's minimal PATH."""
    parts = os.environ.get("PATH", "").split(":")
    for pre in (os.environ.get("HOMEBREW_PREFIX"), "/opt/homebrew", "/usr/local"):
        b = f"{pre}/bin" if pre else None
        if b and Path(b).is_dir() and b not in parts:
            parts.append(b)
    os.environ["PATH"] = ":".join(p for p in parts if p)


def open_env() -> list[str]:
    """`open` drops the caller's environment; forward the isolation dir to the launched bundle."""
    return ["--env", f"DOP_STATE_DIR={state_dir()}"] if isolated() else []
