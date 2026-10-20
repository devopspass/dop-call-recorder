"""Call pipeline (M4a): per-session state machine, crash recovery, finalize, windowed transcription, outputs.

States (persisted in <session>/session.json, `idle` = no session exists):
  recording -> finalizing -> queued -> transcribing -> done | failed      (failed -> queued on re-process)
Layout: <work_dir>/<id>/{session.json, raw/{others,me}_NNNN.wav + state.json, call.m4a}
`raw_dir` in session.json may point elsewhere (dop-recorder process on a recorded folder).
Crash safety: owner_pid + recorder heartbeat identify orphans; `recover()` stops a still-running orphan
recorder, finalizes the chunks that exist and queues them. Raw WAVs are deleted only after success.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from dop_call_recorder import config as cfgmod
from dop_call_recorder import log
from dop_call_recorder.agentstate import PROGRESS
from dop_call_recorder import merge as mg
from dop_call_recorder import outputs, quality
from dop_call_recorder.audio import SR, AudioError, load_audio
from dop_call_recorder.backends import get_backend
from dop_call_recorder.windows import StreamResult, transcribe_stream

_log = log.get("pipeline")

STATES = ("recording", "finalizing", "queued", "transcribing", "done", "failed")
ALLOWED = {
    "recording": {"finalizing", "failed"},
    "finalizing": {"queued", "failed", "done"},
    "queued": {"transcribing", "failed"},
    "transcribing": {"done", "failed", "queued"},
    "done": set(),
    "failed": {"queued", "finalizing"},
}
APP_NAMES = {"com.microsoft.teams2": "Microsoft Teams", "us.zoom.xos": "Zoom", "com.tinyspeck.slackmacgap": "Slack"}
STREAMS = ("others", "me")


class StateError(Exception):
    pass


def app_name(bundle: str) -> str:
    return APP_NAMES.get(bundle) or bundle.rsplit(".", 1)[-1].capitalize()


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_json(p: Path) -> dict:
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Session:
    def __init__(self, dir: str | Path):
        self.dir = Path(dir)
        self.data: dict = read_json(self.dir / "session.json")

    @classmethod
    def create(cls, work_dir: str | Path, app: str, start_epoch: float, title: str | None = None,
               raw_dir: str | None = None, state: str = "recording") -> "Session":
        sid = time.strftime("%Y%m%d-%H%M%S", time.localtime(start_epoch)) + "-" + app_name(app).split()[0].lower()
        d, n = Path(work_dir) / sid, 2
        while d.exists():
            d = Path(work_dir) / f"{sid}-{n}"
            n += 1
        d.mkdir(parents=True)
        s = cls(d)
        s.data = {"id": d.name, "state": state, "app": app, "title": title, "start_epoch": start_epoch,
                  "end_epoch": None, "owner_pid": os.getpid(), "raw_dir": raw_dir or str(d / "raw"),
                  "history": [{"state": state, "ts": time.time()}], "error": None}
        s.save()
        return s

    @property
    def id(self): return self.data.get("id", self.dir.name)
    @property
    def state(self): return self.data.get("state")
    @property
    def raw_dir(self) -> Path: return Path(self.data.get("raw_dir") or self.dir / "raw")

    def save(self):
        self.data["updated"] = time.time()
        outputs.atomic_write(self.dir / "session.json", json.dumps(self.data, ensure_ascii=False, indent=2))

    def transition(self, new: str, **fields):
        if new not in ALLOWED.get(self.state, set()):
            raise StateError(f"{self.id}: illegal transition {self.state} -> {new}")
        self.data.update(fields)
        self.data["state"] = new
        self.data["history"].append({"state": new, "ts": time.time()})
        self.save()
        log.event(_log, "session_state", session=self.id, state=new, **{k: v for k, v in fields.items() if k == "error"})

    def update(self, **fields):
        self.data.update(fields)
        self.save()

    def chunks(self, stream: str) -> list[Path]:
        return sorted(self.raw_dir.glob(f"{stream}_[0-9][0-9][0-9][0-9].wav"))


# ---------------------------------------------------------------- finalize

def _ffmpeg(args: list[str]) -> None:
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {r.stderr.strip()[:300]}")


def build_m4a(session: Session) -> Path | None:
    """Mix me+others chunks into call.m4a (AAC mono 64k). Returns None when there is no audio."""
    inputs, lists = [], []
    for st in STREAMS:
        ch = [c for c in session.chunks(st) if c.stat().st_size > 44]
        if not ch:
            continue
        lst = session.dir / f"{st}.concat.txt"
        lst.write_text("".join(f"file '{str(c).replace(chr(39), chr(39) + chr(92) + chr(39) * 2)}'\n" for c in ch))
        lists.append(lst)
        inputs += ["-f", "concat", "-safe", "0", "-i", str(lst)]
    if not inputs:
        return None
    out = session.dir / "call.m4a"
    n = len(lists)
    flt = (["-filter_complex", f"amix=inputs={n}:duration=longest:normalize=0"] if n > 1 else [])
    _ffmpeg([*inputs, *flt, "-ac", "1", "-c:a", "aac", "-b:a", "64k", str(out)])
    for lst in lists:
        lst.unlink(missing_ok=True)
    return out


def load_stream(session: Session, stream: str) -> np.ndarray | None:
    parts = []
    for c in session.chunks(stream):
        try:
            parts.append(load_audio(str(c)))
        except AudioError as e:
            log.event(_log, "chunk_unreadable", chunk=str(c), error=str(e)[:200])
    return np.concatenate(parts) if parts else None


def finalize(session: Session, cfg: dict) -> None:
    """recording/failed -> finalizing -> queued (or done+skipped when there is no/too little audio)."""
    session.transition("finalizing")
    try:
        total = max((sum(1 for _ in session.chunks(s)) for s in STREAMS), default=0)
        if total == 0:
            session.transition("failed", error="no audio chunks found")
            return
        m4a = build_m4a(session)
        dur = 0.0
        if m4a:
            dur = _probe_duration(m4a)
        session.update(audio_seconds=round(dur, 2), m4a=str(m4a) if m4a else None)
        if session.data.get("end_epoch") is None:
            session.update(end_epoch=session.data["start_epoch"] + dur)
        session.transition("queued")
    except Exception as e:
        log.get("pipeline").exception("finalize_failed", extra={"session": session.id})
        session.transition("failed", error=f"finalize: {e}")


def _probe_duration(p: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- transcribe

def _run_stream(audio, cfg, backend, prompt, label) -> tuple[StreamResult, list[str], bool]:
    """Transcribe one stream with the quality gate; at most one automatic re-run with other settings."""
    wsec = cfg["pipeline"]["window_minutes"] * 60
    res = transcribe_stream(audio, cfg, backend, prompt, label=label)
    issues = quality.assess(res, cfg.get("language") or None)
    retried = False
    if issues:
        log.event(_log, "quality_issues", stream=label, issues=issues, retrying=True)
        rcfg, kw = quality.retry_config(cfg, issues, wsec)
        retried = True
        prompt2 = kw.pop("prompt", prompt)
        res2 = transcribe_stream(audio, rcfg, backend, prompt2, label=label + ":retry", **kw)
        issues2 = quality.assess(res2, kw.get("language") or cfg.get("language") or None)
        log.event(_log, "quality_retry_result", stream=label, before=issues, after=issues2)
        if quality.better(issues2, issues) or (len(issues2) == len(issues) and issues2 == []):
            res, issues = res2, issues2
    return res, issues, retried


def transcribe_session(session: Session, cfg: dict, keep_raw: bool = False, backend=None) -> Path:
    """queued -> transcribing -> done. Returns the output folder."""
    session.transition("transcribing", owner_pid=os.getpid())
    t0 = time.time()
    n_streams = max(1, sum(1 for st in STREAMS if session.chunks(st)))
    PROGRESS.begin(session.id, (session.data.get("audio_seconds") or 0) * n_streams)
    try:
        backend = backend or get_backend(cfg)
        prompt = cfgmod.load_vocabulary(cfg, _roster_terms(session, cfg))
        results: dict[str, StreamResult | None] = {}
        issues: dict[str, list[str]] = {}
        retried: dict[str, bool] = {}
        for st in STREAMS:
            audio = load_stream(session, st)
            if audio is None or len(audio) < SR:
                results[st] = None
                continue
            results[st], issues[st], retried[st] = _run_stream(audio, cfg, backend, prompt, st)
            del audio
        if all(v is None for v in results.values()):
            raise RuntimeError("no readable audio in any stream")
        segs, dropped_echo = mg.merge(results["me"], results["others"], cfg["pipeline"]["echo_window_seconds"])
        segs = apply_speaker_names(session, cfg, segs)
        out = write_outputs(session, cfg, backend, segs, results, issues, retried, dropped_echo, time.time() - t0)
        if not keep_raw:
            _cleanup_raw(session)
        session.transition("done", output_path=str(out))
        return out
    except Exception as e:
        log.get("pipeline").exception("transcribe_failed", extra={"session": session.id})
        session.transition("failed", error=f"transcribe: {type(e).__name__}: {e}")
        raise
    finally:
        PROGRESS.reset()


def _roster_terms(session: Session, cfg: dict, limit: int = 12) -> list[str]:
    """Participant names from the Teams roster (speaker_events.jsonl) -> extra prompt terms. Fail-safe."""
    try:
        from dop_call_recorder import axpoll

        src = next((d for d in (session.dir, session.raw_dir) if (d / "speaker_events.jsonl").exists()), None)
        if src is None:
            return []
        ev = axpoll.load_events(src / "speaker_events.jsonl")
        ax = cfg.get("ax", {}) if isinstance(cfg.get("ax"), dict) else {}
        me = ax.get("my_name") or mg.detect_self(ev)
        names = mg.others_roster(ev, me)[:limit]
        return list(reversed(names))  # most frequent speaker last = highest priority
    except Exception:
        return []


def apply_speaker_names(session: Session, cfg: dict, segs: list) -> list:
    """X1: real participant names from speaker_events.jsonl (Teams Accessibility poll). Optional and fail-safe:
    no events / any error -> segments stay Me/Others, title unchanged."""
    try:
        from dop_call_recorder import axpoll

        src = next((d for d in (session.dir, session.raw_dir) if (d / "speaker_events.jsonl").exists()), session.dir)
        ev = axpoll.load_events(src / "speaker_events.jsonl")
        meta = read_json(src / "ax_meta.json")
        ax = cfg.get("ax", {}) if isinstance(cfg.get("ax"), dict) else {}
        me_name = ax.get("my_name") or None
        self_name = me_name or mg.detect_self(ev) or meta.get("self") or axpoll.cached_self()  # own name is known even when the Teams tree was unreadable (e.g. Accessibility not granted)
        if not ev and not self_name:
            return segs
        segs2, info = mg.assign_speakers(segs, ev, session.data.get("start_epoch") or 0.0, self_name)
        upd = {"speakers": {"self": info["self"], "roster": info["roster"], "named_others": info["named_others"],
                            "total_others": info["total_others"]}}
        if meta.get("title"):  # clean title from the call window beats the detector's raw window title
            upd["title"] = meta["title"]
        session.update(**upd)
        log.event(_log, "speaker_names_applied", session=session.id, **upd["speakers"])
        return segs2
    except Exception:
        log.get("pipeline").exception("speaker_names_failed")
        return segs


def _cleanup_raw(session: Session) -> None:
    for st in STREAMS:
        for c in session.chunks(st):
            c.unlink(missing_ok=True)
    (session.raw_dir / "state.json").unlink(missing_ok=True)
    if session.raw_dir.parent == session.dir:
        try:
            session.raw_dir.rmdir()
        except OSError:
            pass


def write_outputs(session, cfg, backend, segs, results, issues, retried, dropped_echo, seconds) -> Path:
    d = session.data
    start, end = d["start_epoch"], d.get("end_epoch") or d["start_epoch"]
    dur = max(d.get("audio_seconds") or 0, end - start)
    lt = time.localtime(start)
    title = outputs.clean_title(d.get("title")) or f"{app_name(d['app'])} {time.strftime('%H%M', lt)}"
    out = Path(d["output_path"]) if d.get("output_path") else outputs.output_folder(cfg["output_dir"], start, title)
    out.mkdir(parents=True, exist_ok=True)
    langs = mg.language_shares(segs)
    all_issues = sorted({i for v in issues.values() for i in v})
    meta = {
        "title": title, "date": time.strftime("%Y-%m-%d", lt), "start": outputs.iso(start), "end": outputs.iso(start + dur),
        "duration": outputs.hms(dur), "duration_seconds": round(dur, 1), "apps": [app_name(d["app"])],
        "app_bundle_ids": [d["app"]], "language": next(iter(langs), None) or "", "languages": list(langs),
        "language_shares": langs, "quality": "degraded" if all_issues else "good", "quality_issues": all_issues,
        "quality_by_stream": issues, "retried": retried, "model": cfg["model"], "backend": backend.describe(),
        "segments": len(segs), "dropped_echo_segments": dropped_echo, "session_id": session.id,
        "streams": {k: None if v is None else {"segments": len(v.segments), **vars(v.stats)} for k, v in results.items()},
        "transcribe_seconds": round(seconds, 1), "created": outputs.iso(time.time()),
    }
    if d.get("speakers"):
        meta["speakers"] = d["speakers"]
        meta["participants"] = sorted({g.speaker for g in segs})
    outputs.atomic_write(out / "transcript.md", outputs.render_markdown(meta, segs, cfg["pipeline"]))
    outputs.atomic_write(out / "transcript.vtt", outputs.render_vtt(segs))
    outputs.atomic_write(out / "meta.json", json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    for d_ in (session.dir, session.raw_dir):  # X1 evidence travels with the transcript
        if (d_ / "speaker_events.jsonl").exists():
            shutil.copy(d_ / "speaker_events.jsonl", out / "speaker_events.jsonl")
            break
    m4a = d.get("m4a")
    if m4a and Path(m4a).exists():
        shutil.move(m4a, out / "call.m4a")
    rel = os.path.relpath(out, cfg["output_dir"])
    outputs.update_index(cfg["output_dir"], {
        "date": meta["date"], "time": time.strftime("%H:%M", lt), "start": meta["start"], "duration": meta["duration"],
        "duration_seconds": meta["duration_seconds"], "title": title, "path": rel, "quality": meta["quality"],
        "snippet": outputs.snippet(segs), "apps": meta["apps"], "languages": meta["languages"]})
    from dop_call_recorder import search as _search  # M4b hook
    _search.index_call(cfg, out)
    log.event(_log, "outputs_written", session=session.id, path=str(out), quality=meta["quality"], segments=len(segs))
    return out


# ---------------------------------------------------------------- orchestration

class Pipeline:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.work = Path(cfg["work_dir"])
        self.work.mkdir(parents=True, exist_ok=True)
        self.q: queue.Queue = queue.Queue()
        self.active: Session | None = None
        self.lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = False
        self.on_done = None  # M5 hooks: (session, output_path) / (session, exception)
        self.on_fail = None

    # -- recording
    def start_call(self, app: str, title: str | None = None, since: float | None = None) -> Session | None:
        from dop_call_recorder import recorder_bridge

        with self.lock:
            if self.active:
                log.event(_log, "start_ignored_busy", app=app, active=self.active.id)
                return None
            s = Session.create(self.work, app, since or time.time(), title)
            try:
                recorder_bridge.start([app], str(s.raw_dir))
            except Exception as e:
                s.transition("failed", error=f"recorder start: {e}")
                log.get("pipeline").exception("recorder_start_failed")
                return None
            st = read_json(s.raw_dir / "state.json")
            s.update(recorder_pid=st.get("pid"))
            if st.get("started"):  # audio t=0 is the recorder start, not the detector's first positive sample
                from datetime import datetime
                try:
                    s.update(start_epoch=datetime.fromisoformat(st["started"]).timestamp())
                except ValueError:
                    pass
            else:
                s.update(start_epoch=time.time())
            self.active = s
            self._ax_start(s, app)
            return s

    def _ax_start(self, s: Session, app: str) -> None:
        """X1: Teams Accessibility poller (speaker names/title). Never affects recording."""
        try:
            from dop_call_recorder import axpoll

            self._ax = axpoll.AxPoller(s.dir, self.cfg, app)
            self._ax.start()
        except Exception:
            log.get("pipeline").exception("ax_start_failed")

    def _ax_stop(self) -> None:
        ax, self._ax = getattr(self, "_ax", None), None
        if ax is not None:
            try:
                ax.stop()
            except Exception:
                log.get("pipeline").exception("ax_stop_failed")

    def end_call(self, end_epoch: float | None = None, title: str | None = None) -> None:
        from dop_call_recorder import recorder_bridge

        with self.lock:
            s, self.active = self.active, None
        if not s:
            return
        try:
            recorder_bridge.stop()
        except Exception:
            log.get("pipeline").exception("recorder_stop_failed")
        self._ax_stop()
        fields = {"end_epoch": end_epoch or time.time()}
        if title:
            fields["title"] = title
        s.update(**fields)
        self._finalize_and_queue(s)

    def _finalize_and_queue(self, s: Session) -> None:
        finalize(s, self.cfg)
        if s.state == "queued":
            if s.data.get("audio_seconds", 0) < self.cfg["silence"]["min_call_seconds"]:
                s.data["skipped"] = "too_short"
                s.transition("transcribing", owner_pid=os.getpid())
                _cleanup_raw(s)
                s.transition("done")
                log.event(_log, "session_skipped_too_short", session=s.id, seconds=s.data.get("audio_seconds"))
                return
            self.q.put(s.dir)

    def on_event(self, ev: dict) -> None:
        if ev["event"] == "start":
            self.start_call(ev["app"], ev.get("title"), ev.get("since"))
        elif ev["event"] == "end" and self.active and self.active.data["app"] == ev["app"]:
            self.end_call(ev.get("ts"), ev.get("title"))

    # -- recovery
    def recover(self) -> list[str]:
        """Find orphaned/unfinished sessions (owner process dead) and bring them to `queued`."""
        from dop_call_recorder import recorder_bridge

        stale = self.cfg["pipeline"]["stale_heartbeat_seconds"]
        recovered = []
        for sd in sorted(p.parent for p in self.work.glob("*/session.json")):
            s = Session(sd)
            if s.state not in ("recording", "finalizing", "queued", "transcribing"):
                continue
            if pid_alive(s.data.get("owner_pid")) and s.data.get("owner_pid") != os.getpid():
                continue
            if s.data.get("owner_pid") == os.getpid() and (s is self.active or (self.active and self.active.dir == sd)):
                continue
            log.event(_log, "recovering_session", session=s.id, state=s.state)
            if s.state == "recording":
                st = read_json(s.raw_dir / "state.json")
                hb = st.get("heartbeat_epoch")
                if hb and st.get("state") == "recording" and pid_alive(st.get("pid")) and time.time() - hb < stale:
                    try:
                        recorder_bridge.stop()  # orphan recorder of a dead owner: end the recording cleanly
                    except Exception:
                        log.get("pipeline").exception("orphan_recorder_stop_failed")
                    st = read_json(s.raw_dir / "state.json") or st
                end = (st.get("heartbeat_epoch") or newest_mtime(s.raw_dir) or time.time())
                s.update(end_epoch=end, recovered=True)
                finalize(s, self.cfg)
            elif s.state == "finalizing":
                s.data["state"] = "recording"  # redo finalize from scratch (idempotent)
                s.data["end_epoch"] = s.data.get("end_epoch") or newest_mtime(s.raw_dir)
                s.update(recovered=True)
                finalize(s, self.cfg)
            elif s.state == "transcribing":
                s.transition("queued", recovered=True)
            if s.state == "queued":
                s.update(owner_pid=os.getpid())
                self._enqueue_after_recover(s)
                recovered.append(s.id)
        return recovered

    def _enqueue_after_recover(self, s: Session) -> None:
        if s.data.get("audio_seconds", 0) < self.cfg["silence"]["min_call_seconds"] and s.data.get("audio_seconds") is not None:
            self._finalize_and_queue_skip(s)
        else:
            self.q.put(s.dir)

    def _finalize_and_queue_skip(self, s: Session) -> None:
        s.data["skipped"] = "too_short"
        s.transition("transcribing", owner_pid=os.getpid())
        _cleanup_raw(s)
        s.transition("done")

    # -- worker
    def start_worker(self) -> None:
        self._worker = threading.Thread(target=self._loop, name="transcriber", daemon=True)
        self._worker.start()

    def _loop(self) -> None:
        try:
            os.nice(int(self.cfg["pipeline"]["nice"]))
        except OSError:
            pass
        while not self._stop:
            try:
                d = self.q.get(timeout=1)
            except queue.Empty:
                continue
            self.run_one(d)
            self.q.task_done()

    def run_one(self, d: Path) -> None:
        s = Session(d)
        if s.state != "queued":
            return
        try:
            out = transcribe_session(s, self.cfg)
        except Exception as e:  # already logged + marked failed
            self._hook(self.on_fail, s, e)
        else:
            self._hook(self.on_done, s, out)

    @staticmethod
    def _hook(fn, *a) -> None:
        if fn:
            try:
                fn(*a)
            except Exception:
                log.get("pipeline").exception("hook_failed")

    def drain(self) -> None:
        """Process everything queued, in the calling thread (single job at a time)."""
        try:
            os.nice(int(self.cfg["pipeline"]["nice"]))
        except OSError:
            pass
        while True:
            try:
                d = self.q.get_nowait()
            except queue.Empty:
                return
            self.run_one(d)

    def shutdown(self) -> None:
        if self.active:
            self.end_call()
        self._stop = True


def newest_mtime(d: Path) -> float | None:
    ts = [p.stat().st_mtime for p in Path(d).glob("*.wav")]
    return max(ts) if ts else None


def adopt_folder(cfg: dict, folder: Path) -> Session:
    """Session for `dop-recorder process <dir>`: an existing session dir, or a recorded folder (chunks in it or in raw/)."""
    folder = folder.resolve()
    if (folder / "session.json").exists():
        return Session(folder)
    raw = folder / "raw" if list((folder / "raw").glob("*_0000.wav")) else folder
    if not list(raw.glob("*_[0-9][0-9][0-9][0-9].wav")):
        raise AudioError(f"no others_NNNN.wav / me_NNNN.wav chunks in {folder}")
    st = read_json(raw / "state.json")
    start = None
    if st.get("started"):
        from datetime import datetime
        start = datetime.fromisoformat(st["started"]).timestamp()
    if start is None:
        first = min(raw.glob("*_0000.wav"), key=lambda p: p.stat().st_mtime, default=None)
        start = (first.stat().st_birthtime if first else time.time())
    apps = st.get("apps") or ["com.microsoft.teams2"]
    s = Session.create(cfg["work_dir"], apps[0] if isinstance(apps, list) else apps, start, None, raw_dir=str(raw),
                       state="recording")
    s.update(end_epoch=st.get("heartbeat_epoch") or newest_mtime(raw))
    return s


def process_folder(cfg: dict, folder: Path, keep_raw: bool = False, backend=None) -> Path:
    s = adopt_folder(cfg, folder)
    if s.state == "transcribing":
        s.transition("queued")
    elif s.state in ("recording", "finalizing", "failed"):
        finalize_resume(s, cfg)
    if s.state == "failed":
        raise RuntimeError(f"session {s.id} failed: {s.data.get('error')}")
    if s.state == "done":
        return Path(s.data.get("output_path", ""))
    return transcribe_session(s, cfg, keep_raw=keep_raw, backend=backend)


def finalize_resume(s: Session, cfg: dict) -> None:
    s.data["state"] = "recording"
    s.save()
    finalize(s, cfg)


def _ax_title(cfg: dict):
    ax = cfg.get("ax", {}) if isinstance(cfg.get("ax"), dict) else {}
    if not ax.get("enabled", True):
        return None
    from dop_call_recorder import axpoll
    return axpoll.current_title


def run(cfg: dict, recover_only: bool = False) -> int:
    import signal

    from dop_call_recorder import detector

    p = Pipeline(cfg)
    rec = p.recover()
    log.event(_log, "recovery_done", sessions=rec)
    if recover_only:
        p.drain()
        return 0
    p.start_worker()
    from dop_call_recorder.agent import Agent

    agent = Agent(cfg, p)
    agent.start()

    def _term(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    det = cfg.get("detector", {}) if isinstance(cfg.get("detector"), dict) else {}
    try:
        detector.watch(cfg["apps"], cfg["timeouts"]["detector_poll_seconds"], det.get("start_after_seconds", 5.0),
                       det.get("end_after_seconds", 45.0), title_re=det.get("title_regex", detector.DEFAULT_TITLE_RE),
                       on_event=agent.on_event, stdout=False, title_provider=_ax_title(cfg))
    except KeyboardInterrupt:
        pass
    finally:
        p.shutdown()
        agent.shutdown()
    return 0
