"""Call detector (M3): combined audio/window signals per app + hysteresis state machine.

Signals (see README M3 section for the reliability comparison):
  proc_in   - a process of the app holds a running CoreAudio INPUT stream (strongest)
  proc_out  - a process of the app holds a running OUTPUT stream (weak alone: notifications/media)
  window    - an app window title matches a call-ish regex (supporting only)
  mic_any   - some physical (non-virtual) input device is running anywhere (app-agnostic, supporting)
Positive = proc_in OR (proc_out AND window).  The MSTeamsAudioDevice virtual driver is excluded
from mic_any (transport 'virt') so it cannot cause false positives.

Run: python -m dop_call_recorder.detector --watch   (a `dop-recorder detect` CLI from M0 can call main()).
"""
from __future__ import annotations

import argparse, json, os, re, subprocess, sys, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from dop_call_recorder import paths

ROOT = paths.home()
PROBE_SRC = ROOT / "detector" / "audioprobe.swift"


def _log_path() -> Path:
    return paths.logs_dir() / "detector.jsonl"
VIRT = 1986622068  # 'virt' transport type
DEFAULT_APPS = ["com.microsoft.teams2"]
APP_NAMES = {"com.microsoft.teams2": "Microsoft Teams", "us.zoom.xos": "Zoom",
             "com.tinyspeck.slackmacgap": "Slack"}
DEFAULT_TITLE_RE = r"(?i)\b(meeting|call|huddle|sharing control|compact view|zoom meeting)\b"
IGNORE_TITLE_RE = r"^(Chat|Activity|Calendar|Teams|Files|Calls) \|"  # chat/tab titles never count


def ensure_probe() -> Path:
    """The probe is compiled at install time (formula / scripts/build-probe.sh); never at runtime."""
    probe = paths.probe_path()
    if not probe.exists():
        raise RuntimeError(f"audio probe not built ({probe}); run scripts/build-probe.sh (or reinstall via brew)")
    return probe


def read_probe() -> dict:
    out = subprocess.run([str(ensure_probe())], capture_output=True, text=True, timeout=10)
    return json.loads(out.stdout)


def _match(bundle: str, app: str) -> bool:
    return bundle == app or bundle.startswith(app + ".")


def compute_signals(probe: dict, app: str, title_re: str = DEFAULT_TITLE_RE) -> dict:
    procs = [p for p in probe.get("processes", []) if _match(p.get("bundle", ""), app)]
    wins = [w for w in probe.get("windows", []) if _match(w.get("bundle", ""), app)]
    call_titles = [w["title"] for w in wins
                   if w.get("onscreen", True) and re.search(title_re, w["title"]) and not re.search(IGNORE_TITLE_RE, w["title"])]
    mic_any = any(d["running"] and d["in"] and d["transport"] != VIRT for d in probe.get("devices", []))
    s = {
        "app": app,
        "running": bool(procs or wins),
        "proc_in": any(p["input"] for p in procs),
        "proc_out": any(p["output"] for p in procs),
        "window": bool(call_titles),
        "mic_any": mic_any,
        "title": call_titles[0] if call_titles else None,
    }
    s["positive"] = s["proc_in"] or (s["proc_out"] and s["window"])
    return s


@dataclass
class CallStateMachine:
    """Hysteresis: START after start_after s of continuous positive, END after end_after s of continuous negative."""
    app: str
    start_after: float = 5.0
    end_after: float = 45.0
    on_event: Optional[Callable[[dict], None]] = None
    active: bool = False
    title: Optional[str] = None
    _pos_since: Optional[float] = field(default=None, repr=False)
    _neg_since: Optional[float] = field(default=None, repr=False)
    _started_at: Optional[float] = field(default=None, repr=False)

    def update(self, t: float, positive: bool, title: Optional[str] = None) -> Optional[dict]:
        ev = None
        if positive:
            self._neg_since = None
            if self._pos_since is None:
                self._pos_since = t
            if title:
                self.title = title
            if not self.active and t - self._pos_since >= self.start_after:
                self.active, self._started_at = True, self._pos_since
                ev = {"event": "start", "app": self.app, "title": self.title, "ts": t, "since": self._pos_since}
        else:
            self._pos_since = None
            if self.active:
                if self._neg_since is None:
                    self._neg_since = t
                if t - self._neg_since >= self.end_after:
                    ev = {"event": "end", "app": self.app, "title": self.title, "ts": self._neg_since,
                          "duration": self._neg_since - (self._neg_since if self._started_at is None else self._started_at)}
                    self.active, self.title, self._neg_since = False, None, None
            else:
                self.title = None
        if ev and self.on_event:
            self.on_event(ev)
        return ev


def watch(apps, interval=2.0, start_after=5.0, end_after=45.0, log_path=None, title_re=DEFAULT_TITLE_RE,
          on_event=None, stdout=True, max_iter=None, title_provider=None):
    log_path = Path(log_path) if log_path else _log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    machines = {a: CallStateMachine(a, start_after, end_after) for a in apps}
    n = 0
    last: dict = {}
    errs, last_err = 0, None
    from dop_call_recorder.guards import RotatingJsonl
    with RotatingJsonl(log_path) as f:
        def emit(rec):
            f.write(json.dumps(rec) + "\n"); f.flush()
            if stdout:
                print(json.dumps(rec), flush=True)
        while max_iter is None or n < max_iter:
            n += 1
            t0 = time.time()
            try:
                probe = read_probe()
            except Exception as e:  # keep watching on transient failures
                errs += 1
                if str(e) != last_err:
                    last_err = str(e)
                    emit({"ts": t0, "kind": "error", "error": last_err})
                if errs >= 30:  # persistent failure: don't leave a call "active" forever
                    for a in apps:
                        ev = machines[a].update(t0, False)
                        if ev:
                            emit({"kind": "event", **ev})
                            if on_event:
                                on_event(ev)
                time.sleep(interval); continue
            errs, last_err = 0, None
            for a in apps:
                sig = compute_signals(probe, a, title_re)
                ev = machines[a].update(t0, sig["positive"], sig["title"])
                if ev and ev["event"] == "start" and not ev.get("title") and title_provider:
                    try:  # X1: meeting title from the Teams UI (Accessibility); best effort
                        ev["title"] = machines[a].title = title_provider(a)
                    except Exception:
                        pass
                key = (tuple(sig.items()), machines[a].active)
                # log on change, and a heartbeat every 30 s, to keep the day-long log small
                if last.get(a, (None, 0))[0] != key or t0 - last[a][1] >= 30 or sig["positive"]:
                    last[a] = (key, t0)
                    emit({"ts": round(t0, 2), "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t0)),
                          "kind": "signals", **sig, "call_active": machines[a].active})
                if ev:
                    emit({"kind": "event", **ev})
                    if on_event:
                        on_event(ev)
            time.sleep(max(0, interval - (time.time() - t0)))


def _load_config() -> dict:
    import tomllib
    path = paths.config_path()
    try:
        return tomllib.loads(path.read_text())
    except Exception:
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dop-recorder detect")
    ap.add_argument("--watch", action="store_true", help="loop forever, log JSON lines")
    ap.add_argument("--app", action="append", help="bundle id (repeatable); default Teams")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--start-after", type=float, default=5.0)
    ap.add_argument("--end-after", type=float, default=45.0)
    ap.add_argument("--log", default=str(_log_path()))
    a = ap.parse_args(argv)
    cfg = _load_config()
    apps = a.app or cfg.get("apps") or DEFAULT_APPS
    det = cfg.get("detector", {})
    if "--start-after" not in (argv or sys.argv): a.start_after = det.get("start_after_seconds", a.start_after)
    if "--end-after" not in (argv or sys.argv): a.end_after = det.get("end_after_seconds", a.end_after)
    if "--interval" not in (argv or sys.argv): a.interval = cfg.get("timeouts", {}).get("detector_poll_seconds", a.interval)
    if not a.watch:
        probe = read_probe()
        for app in apps:
            print(json.dumps(compute_signals(probe, app, det.get("title_regex", DEFAULT_TITLE_RE))))
        return 0
    try:
        watch(apps, a.interval, a.start_after, a.end_after, Path(a.log), det.get("title_regex", DEFAULT_TITLE_RE))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
