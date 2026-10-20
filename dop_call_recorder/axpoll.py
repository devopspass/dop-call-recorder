"""Teams Accessibility poller (X1): helper-app control + per-call speaker_events.jsonl writer.

Everything here is fail-safe: a missing/unbuilt helper, missing Accessibility permission or unreadable tree
is logged ONCE and the call is recorded/transcribed exactly as without X1.

The helper (app/build/DOPCallRecorder.app) is launched through LaunchServices (`open -n`) so the
Accessibility grant belongs to the helper app bundle, not to whatever process runs `dop-recorder`.
Files in the session dir: speaker_events.jsonl (1 line / poll, epoch ts), ax_meta.json (title/self/roster summary),
ax_samples.jsonl (sparse compact outlines of the call window, tuning data).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from dop_call_recorder import axparse, log, ringdetect, speakerclass

ROOT = Path(__file__).resolve().parent.parent
from dop_call_recorder import paths  # noqa: E402

AX_APP = paths.app_path("axhelper")
AX_ID = paths.BUNDLE_ID
_log = log.get("axpoll")
_warned: set[str] = set()


def _warn_once(key: str, **kw) -> None:
    if key not in _warned:
        _warned.add(key)
        log.event(_log, key, **kw)


SELF_CACHE = paths.state_dir() / "ax_self.json"


def cache_self(name: str | None, path: Path | None = None) -> None:
    """Remember the user's own display name (read from the idle/main window or the own tile) for later calls."""
    if not name:
        return
    try:
        p = path or SELF_CACHE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"name": name, "ts": time.time()}), encoding="utf-8")
    except OSError:
        pass


def cached_self(path: Path | None = None) -> str | None:
    try:
        return json.loads((path or SELF_CACHE).read_text(encoding="utf-8")).get("name") or None
    except (OSError, ValueError):
        return None


def helper_built(app: Path = AX_APP) -> bool:
    return (app / "Contents" / "MacOS" / paths.APP_EXE).exists()


def run_helper(args: list[str], timeout: float = 15, app: Path = AX_APP, runner=subprocess.run) -> dict | None:
    """One-shot helper call via LaunchServices (`open -W -n`); the result comes back through a temp file."""
    if not helper_built(app):
        return None
    fd, tmp = tempfile.mkstemp(prefix="dop-cr-ax-", suffix=".json")
    os.close(fd)
    try:
        runner(["open", "-W", "-n", str(app), "--args", *args, "--out", tmp], timeout=timeout, capture_output=True)
        txt = Path(tmp).read_text(encoding="utf-8").strip()
        return json.loads(txt) if txt else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        Path(tmp).unlink(missing_ok=True)


def oneshot_shot(png: str, timeout: float = 10, app: Path = AX_APP, runner=subprocess.run) -> bool:
    """One screenshot taken by the AX helper bundle (`open -W -n ... shot --png F`)."""
    if not helper_built(app):
        return False
    try:
        runner(["open", "-W", "-n", str(app), "--args", "shot", "--png", png], timeout=timeout, capture_output=True)
        return Path(png).exists() and Path(png).stat().st_size > 1000
    except (OSError, subprocess.TimeoutExpired):
        return False


def check_trusted(app: Path = AX_APP, runner=subprocess.run) -> bool | None:
    """True/False = helper's Accessibility state; None = helper unbuilt / no answer."""
    r = run_helper(["check"], 6, app, runner)
    return None if r is None else bool(r.get("trusted"))


def snapshot_state(bundle: str = "com.microsoft.teams2", app: Path = AX_APP, runner=subprocess.run) -> dict | None:
    """One parsed snapshot (title/self/roster/...) or None when unavailable."""
    r = run_helper(["snapshot", "--bundle", bundle], 8, app, runner)
    if not r or r.get("error") or not r.get("running"):
        return None
    return axparse.parse(r.get("trees", []))


def current_title(app_bundle: str) -> str | None:
    """Meeting title of the call window right now (used by the detector at call start); never raises."""
    if os.environ.get("DOP_CR_AX_DISABLE"):
        return None
    try:
        st = snapshot_state(app_bundle)
        if st and st.get("self"):
            cache_self(st["self"])
        return st["title"] if st and st.get("in_call") else None
    except Exception:
        return None


class AxPoller:
    """Polls the helper's latest snapshot ~1 Hz and appends speaker_events.jsonl to `session_dir`."""

    def __init__(self, session_dir: str | Path, cfg: dict, bundle: str = "com.microsoft.teams2", app: Path = AX_APP,
                 runner=subprocess.run):
        self.dir = Path(session_dir)
        self.ax = cfg.get("ax", {}) if isinstance(cfg.get("ax"), dict) else {}
        self.interval = float(self.ax.get("interval_seconds", 1.0))
        self.bundle, self.app, self.runner = bundle, app, runner
        self.latest = self.dir / "ax_latest.json"
        self.stopfile = self.dir / "ax_stop"
        self.shotreq = self.dir / "ax_shot.req"
        self.shotpng = self.dir / "ax_shot.png"
        self.events = self.dir / "speaker_events.jsonl"
        self.samples = self.dir / "ax_samples.jsonl"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.started = False
        self._last_ts = 0.0
        self._last_sample = 0.0
        self._n_samples = 0
        self._last_sig = None
        self.self_cache = SELF_CACHE
        self._ring_every = float(self.ax.get("ring_every_seconds", 3.0))
        self._revalidate = float(self.ax.get("class_revalidate_seconds", 60))
        self._cls = speakerclass.SpeakerClass()
        self._ring_prev, self._ring_fresh, self._ring_check = None, False, 0.0
        self._ring_on = bool(self.ax.get("ring_detect", True)) and not os.environ.get("DOP_CR_RING_DISABLE")
        self._ring_next = 0.0
        self._ring_last: list[str] = []
        self.meta: dict = {"title": None, "self": None, "roster": [], "in_call_polls": 0, "polls": 0}

    def start(self) -> bool:
        if not self.ax.get("enabled", True) or os.environ.get("DOP_CR_AX_DISABLE"):
            return False
        try:
            if not helper_built(self.app):
                _warn_once("ax_helper_missing", fix="app/build.sh")
                return False
            trusted = check_trusted(self.app, self.runner)
            if not trusted:
                _warn_once("ax_not_trusted", state=str(trusted),
                           fix=f"System Settings > Privacy & Security > Accessibility > enable {self.app.name}")
                return False
            self.dir.mkdir(parents=True, exist_ok=True)
            self.stopfile.unlink(missing_ok=True)
            self.latest.unlink(missing_ok=True)
            self.runner(["open", "-n", str(self.app), "--args", "poll", "--out", str(self.latest), "--interval", str(self.interval),
                         "--stop-file", str(self.stopfile), "--watch-pid", str(os.getpid()), "--max-seconds",
                         str(int(self.ax.get("max_seconds", 21600))), "--bundle", self.bundle,
                         "--shot-req", str(self.shotreq), "--shot-png", str(self.shotpng)], timeout=10, capture_output=True)
            if self._ring_on:
                ringdetect.SHOT_SERVICE = {"req": self.shotreq, "png": self.shotpng}
        except Exception as e:
            _warn_once("ax_start_failed", error=f"{type(e).__name__}: {e}")
            return False
        self.started = True
        self._thread = threading.Thread(target=self._loop, name="ax-poller", daemon=True)
        self._thread.start()
        log.event(_log, "ax_poll_started", dir=str(self.dir))
        return True

    def stop(self) -> None:
        if not self.started:
            return
        self._stop.set()
        ringdetect.SHOT_SERVICE = None
        try:
            self.stopfile.touch()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=5)
        self.started = False
        try:
            (self.dir / "ax_meta.json").write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
        self.latest.unlink(missing_ok=True)
        self.stopfile.unlink(missing_ok=True)
        self.shotreq.unlink(missing_ok=True)
        self.shotpng.unlink(missing_ok=True)
        log.event(_log, "ax_poll_stopped", **{k: self.meta[k] for k in ("title", "self", "polls", "in_call_polls")})

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # never let X1 hurt recording
                _warn_once("ax_poll_error", error=f"{type(e).__name__}: {e}")
            self._stop.wait(self.interval)

    def poll_once(self) -> None:
        try:
            rec = json.loads(self.latest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if rec.get("error"):
            _warn_once("ax_snapshot_error", error=rec["error"])
            return
        if rec.get("ts") == self._last_ts:
            return
        self._last_ts = rec.get("ts")
        self.record(rec.get("trees", []), rec.get("ts") or time.time())

    def _ring(self, tiles: list[dict], ts: float, every: float) -> list[str] | None:
        """Screenshot ring check, rate-limited; returns the last result (None = no usable capture)."""
        if ts >= self._ring_next:
            self._ring_next = ts + every
            try:
                img = ringdetect.capture()
                if img is None:
                    self._ring_next = ts + 60  # no Screen Recording permission / capture failed: back off
                    if not getattr(self, "_ring_warned", False):
                        self._ring_warned = True
                        log.event(_log, "ring_capture_failed")
                    self._ring_last, self._ring_fresh = None, False
                else:
                    new = ringdetect.speakers_from_image(img, tiles)
                    self._ring_fresh = new == self._ring_prev  # stable across two checks: safe to learn from
                    self._ring_prev = self._ring_last = new
            except Exception as e:  # never break the poll loop
                self._ring_last, self._ring_fresh = None, False
                log.event(_log, "ring_error", error=f"{type(e).__name__}: {e}")
        return self._ring_last

    def _speakers(self, st: dict, ts: float) -> tuple[list[str], str | None]:
        """Active speaker(s): AX text markers > learned CSS class (speakerclass) > blue ring on screen (checked against AX
        mute state; also the teacher for the class) > the only unmuted participant (weak fallback)."""
        if st["speaking"]:
            return st["speaking"], "ax"
        tiles = st.get("tiles") or []
        if self._ring_on and len(tiles) >= 2:
            if self._cls.verdict == "learned":
                names = self._cls.speaking(tiles)
                if ts >= self._ring_check:  # periodic revalidation against one screenshot
                    self._ring_check = ts + self._revalidate
                    ring = self._ring(tiles, ts, 0)
                    if ring is not None and not self._cls.validate(names, ring):
                        self._ring_next = 0.0  # relearn immediately
                        return list(ring), "ring"
                return names, "class"
            if any(not t["muted"] and not t["self"] for t in tiles):  # nobody unmuted = nobody can be the speaker
                ring = self._ring(tiles, ts, self._ring_every)
                if ring is not None:
                    if self._ring_fresh and self._cls.verdict is None:
                        self._cls.observe(tiles, ring)
                    if ring:
                        return list(ring), "ring"
        open_mics = [t["name"] for t in tiles if not t["muted"] and not t["self"]]
        if len(open_mics) == 1:
            return open_mics, "unmuted"
        return [], None

    def record(self, trees: list, ts: float) -> dict:
        st = axparse.parse(trees)
        self.meta["polls"] += 1
        if st.get("self"):
            if self.meta["self"] != st["self"]:
                cache_self(st["self"], self.self_cache)
            self.meta["self"] = st["self"]
        if st["in_call"]:
            self.meta["in_call_polls"] += 1
            if st.get("title") and not self.meta["title"]:
                self.meta["title"] = st["title"]
            for n in st["roster"]:
                if n not in self.meta["roster"]:
                    self.meta["roster"].append(n)
            speaking, src = self._speakers(st, ts)
            line = {"ts": round(ts, 2), "speaking": speaking, "src": src, "roster": st["roster"], "title": st["title"], "self": st["self"]}
            with open(self.events, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
            if self._n_samples < int(self.ax.get("max_samples", 30)) and ts - self._last_sample >= float(self.ax.get("sample_seconds", 10)):
                lines = axparse.compact_sample(trees)
                sig = hash(re.sub(r"\d+", "#", "\n".join(lines)))  # clocks/counters alone are not a change
                if sig != self._last_sig:
                    self._last_sample, self._last_sig, self._n_samples = ts, sig, self._n_samples + 1
                    with open(self.samples, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"ts": round(ts, 2), "lines": lines}, ensure_ascii=False) + "\n")
        return st


def load_events(path: str | Path) -> list[dict]:
    out = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return sorted(out, key=lambda e: e.get("ts", 0))
