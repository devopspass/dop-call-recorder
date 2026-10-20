"""Thin bridge to the signed Swift recorder app (app/). Used by `dop-recorder apps` and the pipeline (M4a)."""
import json
import subprocess
from pathlib import Path

import os

from dop_call_recorder import paths


def _shim() -> Path:
    for p in (paths.home() / "libexec" / "bin" / "recorder", paths.home() / "app" / "bin" / "recorder"):
        if p.exists():
            return p
    return paths.home() / "app" / "bin" / "recorder"


def _run(args, **kw):
    app = paths.app_path("recorder")
    shim = _shim()
    if not shim.exists() or not app.exists():
        raise RuntimeError("recorder app not installed: run `dop-recorder setup` (dev: app/build.sh)")
    env = {**os.environ, "DOP_RECORDER_APP": str(app)}
    if paths.isolated():
        env["DOP_STATE_DIR"] = str(paths.state_dir())
    return subprocess.run([str(shim), *args], capture_output=True, text=True, env=env, **kw)


def list_apps(all_procs: bool = False) -> dict:
    """Apps (with helper pids resolved) currently using Core Audio; all_procs=True includes idle audio clients."""
    r = _run(["list-apps", *(["--all"] if all_procs else [])], timeout=20)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "recorder list-apps failed")
    return json.loads(r.stdout)


def start(apps: list, out_dir: str, system_fallback: bool = False) -> None:
    args = ["start", "--apps", ",".join(apps), "--out", str(out_dir)]
    if system_fallback:
        args.append("--system-fallback")
    r = _run(args, timeout=20)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "recorder start failed")


def stop() -> str:
    return _run(["stop"], timeout=30).stdout.strip()


def main(argv=None) -> int:
    all_procs = "--all" in (argv or [])
    print(json.dumps(list_apps(all_procs), indent=2))
    return 0
