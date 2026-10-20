"""Agent supervisor (M5): wraps the M4a Pipeline with pause/resume, notifications, disk guard, retention,
caffeinate and the status.json heartbeat that `dop-recorder status` / the menu-bar app read.

The agent never records anything on its own: the detector feeds `on_event`; `tick()` (1 per few seconds, its own
thread in `dop-recorder run`) applies control.json (pause), guards and publishes status.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from dop_call_recorder import __version__, agentstate, guards, log, notify as notifymod, paths
from dop_call_recorder.pipeline import Pipeline

_log = log.get("agent")


def latest_transcript(cfg: dict) -> dict | None:
    best = None
    for p in Path(cfg["output_dir"]).glob("*/*/transcript.md"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if best is None or m > best[0]:
            best = (m, p)
    return None if best is None else {"path": str(best[1].parent), "title": best[1].parent.name, "ts": best[0]}


class Agent:
    def __init__(self, cfg: dict, pipeline: Pipeline, notify=notifymod.notify, caffeinate: guards.Caffeinate | None = None):
        self.cfg, self.p, self._notify = cfg, pipeline, notify
        self.caff = caffeinate or guards.Caffeinate()
        self.paused = False
        self.started_ev: dict[str, dict] = {}  # app -> start event of the call being recorded (re-armed on pause)
        self.det_active: dict[str, dict] = {}  # app -> start event the detector still considers a live call
        self._last_rearm = 0.0
        self.pending: dict[str, dict] = {}  # app -> start event skipped while paused / disk-blocked
        self.disk = {"level": "ok", "free_gb": None}
        self.disk_blocked = False
        self.last_error: dict | None = None
        self.last_transcript: dict | None = latest_transcript(cfg)
        self._last_disk = 0.0
        self._last_retention = 0.0
        self._last_warn = float("-inf")
        self._last_logrot = 0.0
        self.async_end = True  # finalize (ffmpeg) off the tick thread so status.json keeps its heartbeat
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        pipeline.on_done = self._done
        pipeline.on_fail = self._failed

    # ---------------------------------------------------------------- notifications
    def note(self, title: str, msg: str, kind: str = "info") -> None:
        self._notify(title, msg, self.cfg, kind)

    def _error(self, msg: str) -> None:
        self.last_error = {"message": msg, "ts": time.time()}

    def _done(self, session, out: Path) -> None:
        self.last_transcript = {"path": str(out), "title": out.name, "ts": time.time()}
        self.last_error = None
        self.note("Transcript ready", f"{session.data.get('title') or out.name}\n{out}", "done")

    def _failed(self, session, err: Exception) -> None:
        self._error(f"transcription failed ({session.id}): {err}")
        self.note("Transcription failed", f"{session.id}: {str(err)[:120]}", "error")

    # ---------------------------------------------------------------- detector events
    def on_event(self, ev: dict) -> None:
        app = ev.get("app")
        if ev.get("event") == "start":
            self.det_active[app] = ev
            self._start(ev)
        elif ev.get("event") == "end":
            self.pending.pop(app, None)
            self.det_active.pop(app, None)
            self.started_ev.pop(app, None)
            self.p.on_event(ev)

    def _start(self, ev: dict) -> None:
        app = ev["app"]
        if self.paused:
            self.pending[app] = ev
            log.event(_log, "start_skipped_paused", app=app)
            return
        if self.disk_blocked:
            self.pending[app] = ev
            log.event(_log, "start_skipped_disk", app=app, free_gb=self.disk["free_gb"])
            return
        busy_before = self.p.active is not None
        s = self.p.start_call(app, ev.get("title"), ev.get("since"))
        if s:
            self.started_ev[app] = ev
            self.last_error = None
            self.note("Recording started", f"{ev.get('title') or app} — recording audio for transcription. `dop-recorder pause` to stop.", "start")
        elif not busy_before:
            self._error("recorder failed to start")
            self.note("Recording FAILED to start", "Run `dop-recorder doctor` (permissions?) and check dop-call-recorder.log", "error")

    # ---------------------------------------------------------------- periodic work
    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        self._apply_pause()
        self._watchdog(now)
        ac = self.cfg.get("agent", {})
        if now - self._last_disk >= float(self.cfg.get("disk", {}).get("check_seconds", 30)):
            self._last_disk = now
            self.check_disk(now)
        if now - self._last_retention >= 86400:
            self._last_retention = now
            self.run_retention(now)
        if now - self._last_logrot >= 3600:
            self._last_logrot = now
            for f in ("launchd.out.log", "launchd.err.log"):
                guards.rotate_copytruncate(paths.logs_dir() / f)
        busy = self.p.active is not None or self.p.q.qsize() > 0 or agentstate.PROGRESS.snapshot() is not None
        if ac.get("caffeinate", True):
            self.caff.set(busy)
        self.publish(now)

    def _watchdog(self, now: float, every: float = 30.0) -> None:
        """Self-heal: detector says a call is live, we are not paused/blocked, yet nothing records -> (re)start it."""
        if self.paused or self.disk_blocked or self.p.active is not None or not self.det_active:
            return
        if now - self._last_rearm < every:
            return
        self._last_rearm = now
        app, ev = next(iter(self.det_active.items()))
        log.event(_log, "watchdog_rearm", app=app)
        self._start(ev)

    def _apply_pause(self) -> None:
        want = agentstate.is_paused()
        if want and not self.paused:
            self.paused = True
            rec = self.p.active is not None
            if rec:
                # the detector already emitted this call's start and won't again: keep it so resume re-arms a still-running call
                self.pending.update(self.started_ev)
                self._end_call()  # recorder stopped, chunks finalized and queued for transcription
            log.event(_log, "paused", was_recording=rec)
            self.note("DOP Call Recorder paused", "Recording stopped and queued for transcription." if rec else "Recording is off until `dop-recorder resume`.", "info")
        elif not want and self.paused:
            self.paused = False
            log.event(_log, "resumed", pending=list(self.pending))
            self.note("DOP Call Recorder resumed", "Listening for calls again.", "info")
            for app, ev in list(self.pending.items()):  # a call that is still going on at resume time
                self.pending.pop(app, None)
                self._start(ev)

    def _end_call(self) -> None:
        if not self.async_end:
            self.p.end_call()
            return
        threading.Thread(target=self.p.end_call, name="agent-end-call", daemon=True).start()

    def check_disk(self, now: float | None = None) -> str:
        level, gb = guards.disk_level(self.cfg)
        self.disk = {"level": level, "free_gb": round(gb, 2)}
        if level != "ok":
            guards.clean_old_raw(self.cfg, now, force=(level == "critical"))
            level, gb = guards.disk_level(self.cfg)
            self.disk = {"level": level, "free_gb": round(gb, 2)}
        now = now or time.time()
        if level == "critical":
            if not self.disk_blocked:
                self.disk_blocked = True
                if self.p.active is not None:
                    self._end_call()  # flush + finalize what we have
                self._error(f"disk critically low ({gb:.1f} GB free): recording stopped")
                self.note("Disk almost full — recording stopped", f"{gb:.1f} GB free. Free some space; recording resumes automatically.", "error")
        else:
            if self.disk_blocked:
                self.disk_blocked = False
                log.event(_log, "disk_recovered", free_gb=gb)
                for app, ev in list(self.pending.items()):
                    self.pending.pop(app, None)
                    self._start(ev)
            if level == "warn" and now - self._last_warn > 6 * 3600:
                self._last_warn = now
                self.note("Low disk space", f"{gb:.1f} GB free (warn threshold {self.cfg['disk']['warn_gb']} GB).", "warn")
        return level

    def run_retention(self, now: float | None = None) -> dict:
        res = guards.cleanup_retention(self.cfg, now)
        guards.clean_old_raw(self.cfg, now)
        return res

    # ---------------------------------------------------------------- status
    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        act = self.p.active
        prog = agentstate.PROGRESS.snapshot(now)
        queued = self.p.q.qsize()
        if act is not None:
            state = "recording"
        elif prog or queued:
            state = "transcribing"
        elif self.paused:
            state = "paused"
        elif self.last_error and now - self.last_error["ts"] < 3600:
            state = "error"
        else:
            state = "idle"
        active = None
        if act is not None:
            active = {"session": act.id, "app": act.data.get("app"), "title": act.data.get("title"),
                      "start_epoch": act.data.get("start_epoch"), "elapsed_s": round(now - (act.data.get("start_epoch") or now), 0)}
        return {"version": __version__, "state": state, "paused": self.paused, "active": active,
                "queue_len": queued + (1 if prog else 0), "progress": prog, "last_transcript": self.last_transcript,
                "last_error": self.last_error, "disk": self.disk, "caffeinate": self.caff.held,
                "apps": self.cfg.get("apps"), "output_dir": self.cfg["output_dir"], "started": getattr(self, "started", None)}

    def publish(self, now: float | None = None) -> None:
        try:
            agentstate.write_status(self.snapshot(now))
        except OSError:
            log.get("agent").exception("status_write_failed")

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.started = time.time()
        self.run_retention()
        self._last_retention = time.time()
        self._thread = threading.Thread(target=self._loop, name="agent-tick", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        every = float(self.cfg.get("agent", {}).get("status_seconds", 2))
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.get("agent").exception("tick_failed")
            self._stop.wait(every)

    def shutdown(self) -> None:
        self._stop.set()
        self.caff.release()
        try:
            agentstate.write_status({**self.snapshot(), "state": "stopped"})
        except OSError:
            pass
