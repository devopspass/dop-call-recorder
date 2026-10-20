"""M5: disk guard, retention, raw-WAV cleanup, log rotation, caffeinate, pause state, status/doctor logic, agent, notifications."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dop_call_recorder import agent as agentmod
from dop_call_recorder import agentstate, config as cfgmod, doctor, guards, log, notify, status

GB = 1024 ** 3
DAY = 86400


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DOP_CR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DOP_CR_CONFIG", str(tmp_path / "none.toml"))
    c = cfgmod.load()
    c["output_dir"] = str(tmp_path / "out")
    c["work_dir"] = str(tmp_path / "work")
    c["logging"]["file"] = str(tmp_path / "logs" / "dop-call-recorder.log")
    Path(c["work_dir"]).mkdir()
    Path(c["output_dir"]).mkdir()
    return c


def mk_session(cfg, sid, state, age_days=0.0, wavs=True):
    d = Path(cfg["work_dir"]) / sid
    (d / "raw").mkdir(parents=True)
    if wavs:
        (d / "raw" / "others_0000.wav").write_bytes(b"x" * 100)
        (d / "raw" / "me_0000.wav").write_bytes(b"x" * 100)
    (d / "session.json").write_text(json.dumps({"id": sid, "state": state, "raw_dir": str(d / "raw"),
                                                "updated": time.time() - age_days * DAY}))
    return d


# ---------------------------------------------------------------- disk guard
def test_disk_levels(cfg):
    assert guards.disk_level(cfg, 10 * GB)[0] == "ok"
    assert guards.disk_level(cfg, 3 * GB)[0] == "warn"
    assert guards.disk_level(cfg, int(0.5 * GB))[0] == "critical"
    cfg["disk"].update(warn_gb=20, critical_gb=10)
    assert guards.disk_level(cfg, 15 * GB)[0] == "warn"


def test_clean_old_raw_only_finished(cfg):
    done = mk_session(cfg, "done1", "done", 0)
    old_failed = mk_session(cfg, "oldfail", "failed", 10)
    new_failed = mk_session(cfg, "newfail", "failed", 1)
    queued = mk_session(cfg, "q", "queued", 30)
    rec = mk_session(cfg, "r", "recording", 30)
    removed = guards.clean_old_raw(cfg)
    assert len(removed) == 4
    assert not list((done / "raw").glob("*.wav")) and not list((old_failed / "raw").glob("*.wav"))
    assert len(list((new_failed / "raw").glob("*.wav"))) == 2
    assert len(list((queued / "raw").glob("*.wav"))) == 2 and len(list((rec / "raw").glob("*.wav"))) == 2
    guards.clean_old_raw(cfg, force=True)  # critical: recent failed ones go too, active ones never
    assert not list((new_failed / "raw").glob("*.wav"))
    assert len(list((queued / "raw").glob("*.wav"))) == 2 and len(list((rec / "raw").glob("*.wav"))) == 2


# ---------------------------------------------------------------- retention
def test_retention_keeps_transcripts(cfg):
    now = time.time()
    old = Path(cfg["output_dir"]) / "2026" / "2026-01-01_1000_old"
    new = Path(cfg["output_dir"]) / "2026" / "2026-10-01_1000_new"
    for d, age in ((old, 40), (new, 5)):
        d.mkdir(parents=True)
        for f in ("call.m4a", "transcript.md", "transcript.vtt", "meta.json"):
            (d / f).write_text("x")
            os.utime(d / f, (now - age * DAY,) * 2)
    mk_session(cfg, "olddone", "done", 40, wavs=False)
    mk_session(cfg, "newdone", "done", 1, wavs=False)
    res = guards.cleanup_retention(cfg, now)
    assert res["m4a_deleted"] == 1 and res["sessions_pruned"] == 1
    assert not (old / "call.m4a").exists() and (old / "transcript.md").exists() and (old / "meta.json").exists()
    assert (new / "call.m4a").exists()
    assert not (Path(cfg["work_dir"]) / "olddone").exists() and (Path(cfg["work_dir"]) / "newdone").exists()
    cfg["retention_days"] = 0  # disabled
    assert guards.cleanup_retention(cfg, now + 999 * DAY)["m4a_deleted"] == 0


def test_retention_custom_days(cfg):
    d = Path(cfg["output_dir"]) / "2026" / "a"
    d.mkdir(parents=True)
    (d / "call.m4a").write_text("x")
    cfg["retention_days"] = 3
    assert guards.cleanup_retention(cfg, time.time() + 2 * DAY)["m4a_deleted"] == 0
    assert guards.cleanup_retention(cfg, time.time() + 4 * DAY)["m4a_deleted"] == 1


# ---------------------------------------------------------------- log rotation
def test_rotating_jsonl_rotates(tmp_path):
    p = tmp_path / "detector.jsonl"
    with guards.RotatingJsonl(p, max_bytes=300, backups=2) as f:
        for i in range(40):
            f.write(json.dumps({"i": i, "pad": "x" * 30}) + "\n")
            f.flush()
    files = sorted(x.name for x in tmp_path.iterdir())
    assert files == ["detector.jsonl", "detector.jsonl.1", "detector.jsonl.2"]
    assert all(json.loads(l) for l in p.read_text().splitlines())  # one JSON object per line, never split
    assert max(x.stat().st_size for x in tmp_path.iterdir()) <= 400


def test_log_is_rotating(tmp_path):
    import logging.handlers
    lg = log.setup("INFO", str(tmp_path / "dop-call-recorder.log"), 200, 2)
    assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in lg.handlers)
    for i in range(50):
        log.get("t").info("event %d", i, extra={"pad": "y" * 40})
    assert (tmp_path / "dop-call-recorder.log.1").exists()
    log.setup("INFO", None)


def test_copytruncate(tmp_path):
    p = tmp_path / "launchd.err.log"
    p.write_text("a" * 500)
    assert not guards.rotate_copytruncate(p, max_bytes=1000)
    assert guards.rotate_copytruncate(p, max_bytes=100)
    assert p.stat().st_size == 0 and (tmp_path / "launchd.err.log.1").read_text() == "a" * 500


# ---------------------------------------------------------------- caffeinate
def test_caffeinate_lifecycle():
    procs = []

    class P:
        def __init__(self, cmd, **kw):
            self.cmd, self.alive, self.pid = cmd, True, 4242
            procs.append(self)
        def poll(self): return None if self.alive else 0
        def terminate(self): self.alive = False

    c = guards.Caffeinate(popen=P)
    c.set(False)
    assert not procs
    c.set(True); c.set(True)
    assert len(procs) == 1 and procs[0].cmd[:2] == ["caffeinate", "-i"] and procs[0].cmd[-1] == str(os.getpid())
    assert c.held
    c.set(False)
    assert not c.held and not procs[0].alive


# ---------------------------------------------------------------- pause state / progress
def test_pause_flag_roundtrip(cfg):
    assert agentstate.is_paused() is False
    agentstate.set_paused(True)
    assert agentstate.is_paused() is True
    agentstate.set_paused(False)
    assert agentstate.is_paused() is False
    agentstate.control_path().write_text("{garbage")
    assert agentstate.is_paused() is False


def test_progress_eta():
    pr = agentstate.Progress()
    assert pr.snapshot() is None
    pr.begin("s1", 100.0)
    pr.advance("others", 25)
    pr.advance("others:retry", 10)  # retry must not rewind
    t = pr.t0 + 20
    snap = pr.snapshot(now=t)
    assert snap["fraction"] == 0.25 and snap["eta_s"] == 60
    pr.advance("me", 80)
    assert pr.snapshot(now=t)["fraction"] == 0.99  # never 100% before done
    pr.reset()
    assert pr.snapshot() is None


def test_status_fresh_and_gather(cfg):
    assert status.gather(cfg)["running"] is False
    agentstate.write_status({"state": "idle", "queue_len": 0})
    s = status.gather(cfg)
    assert s["running"] and s["state"] == "idle"
    st = agentstate.read_status()
    assert not agentstate.status_fresh(st, now=st["ts"] + 60)
    agentstate.write_status({"state": "stopped"})
    assert status.gather(cfg)["running"] is False
    mk_session(cfg, "a", "queued", wavs=False)
    mk_session(cfg, "b", "done", wavs=False)
    g = status.gather(cfg)
    assert g["sessions"] == {"queued": 1, "done": 1} and g["queue_len"] == 1


def test_status_render(cfg):
    s = {"running": True, "pid": 1, "state": "transcribing", "paused": True, "queue_len": 2, "sessions": {},
         "progress": {"session": "x", "stream": "me", "fraction": 0.5, "elapsed_s": 90, "eta_s": 3700},
         "active": None, "disk": {"free_gb": 100, "level": "ok"}}
    out = status.render(s)
    assert "[PAUSED]" in out and "50%" in out and "ETA 1h01m" in out and "queue: 2" in out
    assert "NOT RUNNING" in status.render({"running": False, "state": "not running", "paused": False})


# ---------------------------------------------------------------- doctor (mocked system calls)
def R(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def test_doctor_python_deps_ffmpeg():
    assert doctor.check_python(version=(3, 11), prefix="/x").status == doctor.FAIL
    assert doctor.check_python(version=(3, 12), prefix="/usr").status == doctor.OK
    assert doctor.check_python(version=(3, 13), prefix="/x").status == doctor.OK
    cs = doctor.check_deps(find_spec=lambda m: None if m in ("torch", "onnxruntime") else object())
    by = {c.name: c.status for c in cs}
    assert by["dep:torch"] == doctor.OK and by["dep:onnxruntime"] == doctor.FAIL and by["dep:numpy"] == doctor.OK
    oa = {c.name: c.status for c in doctor.check_deps(find_spec=lambda m: None if m == "torch" else object(), cfg={"backend": "openai-whisper"})}
    assert oa["dep:torch"] == doctor.FAIL
    ff = doctor.check_ffmpeg(which=lambda b: "/bin/ffmpeg" if b == "ffmpeg" else None)
    assert [c.status for c in ff] == [doctor.OK, doctor.FAIL] and "brew install ffmpeg" in ff[1].fix


def test_doctor_models(cfg, tmp_path):
    cfg["model_dir"] = str(tmp_path / "models")
    (a,) = doctor.check_models(cfg)
    assert a.status == doctor.FAIL and "dop-recorder setup" in a.fix
    (tmp_path / "models" / "mlx-large-v3-turbo").mkdir(parents=True)
    (tmp_path / "models" / "mlx-large-v3-turbo" / "weights.safetensors").write_text("x")
    assert [c.status for c in doctor.check_models(cfg)] == [doctor.OK]
    cfg["backend"] = "openai-whisper"
    assert [c.status for c in doctor.check_models(cfg)] == [doctor.OK, doctor.FAIL]


def test_doctor_recorder(tmp_path):
    app = tmp_path / "Rec.app"
    assert doctor.check_recorder(app=app).status == doctor.FAIL
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "dop-call-recorder").write_text("x")
    calls = {"verify": R(1, "", "invalid signature")}
    run = lambda cmd, **k: calls["verify"] if "--verify" in cmd else R(0, "", f"Identifier={doctor.RECORDER_ID}\n")
    assert doctor.check_recorder(run=run, app=app).status == doctor.FAIL
    calls["verify"] = R(0)
    assert doctor.check_recorder(run=run, app=app).status == doctor.OK
    other = lambda cmd, **k: R(0) if "--verify" in cmd else R(0, "", "Identifier=other\n")
    assert doctor.check_recorder(run=other, app=app).status == doctor.WARN


def test_doctor_permissions(cfg, tmp_path):
    cs = doctor.check_permissions(appsup=tmp_path / "none", work_dir=cfg["work_dir"])
    assert cs[0].status == doctor.WARN
    d = mk_session(cfg, "s", "done", wavs=False)
    (d / "raw" / "state.json").write_text(json.dumps({"mic_permission": "NOT_AUTHORIZED"}))
    assert doctor.check_permissions(appsup=tmp_path / "none", work_dir=cfg["work_dir"])[0].status == doctor.FAIL
    (d / "raw" / "state.json").write_text(json.dumps({"mic_permission": "authorized"}))
    assert doctor.check_permissions(appsup=tmp_path / "none", work_dir=cfg["work_dir"])[0].status == doctor.OK


def test_doctor_apps_disk_output_launchd(cfg, tmp_path):
    assert doctor.check_apps(cfg, run=lambda c, **k: R(0, "/Applications/Microsoft Teams.app\n"))[0].status == doctor.OK
    assert doctor.check_apps(cfg, run=lambda c, **k: R(0, ""))[0].status == doctor.WARN
    assert doctor.check_disk(cfg, free=20 * GB).status == doctor.OK
    assert doctor.check_disk(cfg, free=3 * GB).status == doctor.WARN
    assert doctor.check_disk(cfg, free=GB // 2).status == doctor.FAIL
    assert doctor.check_output_dir(cfg).status == doctor.OK
    cfg["output_dir"] = "/proc/nope/x" if False else "/System/nope/x"
    assert doctor.check_output_dir(cfg).status == doctor.FAIL
    # launchd: not installed / installed-not-loaded / running
    assert doctor.check_launchd(run=lambda c, **k: R(1), agents_dir=tmp_path)[0].status == doctor.WARN
    (tmp_path / f"{doctor.LABEL}.plist").write_text("x")
    assert "not loaded" in doctor.check_launchd(run=lambda c, **k: R(113), agents_dir=tmp_path, uid=1)[0].detail
    ok = doctor.check_launchd(run=lambda c, **k: R(0, "\tstate = running\n"), agents_dir=tmp_path, uid=1)[0]
    assert ok.status == doctor.OK


def test_doctor_render_and_exit():
    cs = [doctor.Check("a", doctor.OK, "fine"), doctor.Check("b", doctor.WARN, "hmm", "do x"), doctor.Check("c", doctor.FAIL, "bad", "do y")]
    out = doctor.render(cs)
    assert "fix: do x" in out and "fix: do y" in out and "1 failed, 1 warnings" in out
    assert doctor.exit_code(cs) == 1 and doctor.exit_code(cs[:2]) == 0


# ---------------------------------------------------------------- notifications
def test_notify_calls_sender(tmp_path, monkeypatch):
    out = tmp_path / "n.txt"
    s = tmp_path / "sender.sh"
    s.write_text(f'#!/bin/sh\necho "$1|$2" >> {out}\n')
    s.chmod(0o755)
    monkeypatch.setenv("DOP_CR_NOTIFY_CMD", str(s))
    assert notify.notify("Recording started", "Teams call", {}, "start")
    assert out.read_text().strip() == "Recording started|Teams call"
    assert not notify.notify("x", "y", {"notify": {"on_done": False}}, "done")
    assert not notify.notify("x", "y", {"notify": {"enabled": False}}, "start")
    assert len(out.read_text().splitlines()) == 1


def test_notify_osascript_escaping(monkeypatch):
    monkeypatch.setattr(notify, "_via_menubar", lambda *a, **k: False)
    seen = []
    monkeypatch.delenv("DOP_CR_NOTIFY_CMD", raising=False)
    monkeypatch.setattr(notify.subprocess, "run", lambda cmd, **k: (seen.append(cmd), R(0))[1])
    assert notify.notify('He said "hi"', "a\\b\nc", {}, "info")
    assert seen[0][0] == "osascript" and r'\"hi\"' in seen[0][2] and "\n" not in seen[0][2]


# ---------------------------------------------------------------- agent supervisor
class FakePipeline:
    def __init__(self):
        self.active = None
        self.q = SimpleNamespace(qsize=lambda: 0)
        self.started, self.ended, self.events = [], 0, []
        self.on_done = self.on_fail = None
        self.fail_start = False

    def start_call(self, app, title=None, since=None):
        if self.fail_start or self.active:
            return None
        self.active = SimpleNamespace(id="s1", data={"app": app, "title": title, "start_epoch": time.time()})
        self.started.append(app)
        return self.active

    def end_call(self, *a, **k):
        self.active, self.ended = None, self.ended + 1

    def on_event(self, ev):
        self.events.append(ev)
        if ev["event"] == "end":
            self.end_call()


@pytest.fixture
def ag(cfg):
    notes = []
    p = FakePipeline()
    caff = guards.Caffeinate(popen=lambda *a, **k: SimpleNamespace(pid=1, poll=lambda: None, terminate=lambda: None))
    a = agentmod.Agent(cfg, p, notify=lambda t, m, c, k: notes.append((t, k)), caffeinate=caff)
    a.notes = notes
    a.async_end = False
    return a


START = {"event": "start", "app": "com.microsoft.teams2", "title": "Weekly"}


def test_agent_start_notifies_and_status(ag):
    ag.on_event(START)
    assert ag.p.started == ["com.microsoft.teams2"] and ("Recording started", "start") in ag.notes
    ag.tick()
    st = agentstate.read_status()
    assert st["state"] == "recording" and st["active"]["title"] == "Weekly" and st["caffeinate"] is True
    ag.on_event({"event": "end", "app": "com.microsoft.teams2"})
    ag.tick()
    assert agentstate.read_status()["state"] == "idle" and agentstate.read_status()["caffeinate"] is False


def test_agent_start_failure_notifies(ag):
    ag.p.fail_start = True
    ag.on_event(START)
    assert ("Recording FAILED to start", "error") in ag.notes
    ag.tick()
    assert agentstate.read_status()["state"] == "error"


def test_agent_pause_stops_recording_and_skips(ag):
    ag.on_event(START)
    agentstate.set_paused(True)
    ag.tick()
    assert ag.p.active is None and ag.p.ended == 1 and agentstate.read_status()["state"] == "paused"
    ag.on_event({**START, "app": "us.zoom.xos"})  # new call while paused: skipped
    assert ag.p.started == ["com.microsoft.teams2"]
    agentstate.set_paused(False)
    ag.tick()  # resume => the still-running call is picked up
    assert ag.p.started[:2] == ["com.microsoft.teams2"] * 2  # same call re-armed first; zoom is then busy-ignored


def test_agent_pause_resume_mid_call_restarts_same_call(ag):
    ag.on_event(START)
    agentstate.set_paused(True)
    ag.tick()
    agentstate.set_paused(False)
    ag.tick()
    assert ag.p.started == ["com.microsoft.teams2"] * 2 and ag.p.active is not None


def test_agent_pause_mid_call_then_call_ends_no_restart(ag):
    ag.on_event(START)
    agentstate.set_paused(True)
    ag.tick()
    ag.on_event({"event": "end", "app": "com.microsoft.teams2"})
    agentstate.set_paused(False)
    ag.tick()
    assert ag.p.started == ["com.microsoft.teams2"]


def test_agent_pause_drops_pending_if_call_ended(ag):
    agentstate.set_paused(True)
    ag.tick()
    ag.on_event(START)
    ag.on_event({"event": "end", "app": "com.microsoft.teams2"})
    agentstate.set_paused(False)
    ag.tick()
    assert ag.p.started == []


def test_agent_disk_critical_stops_recording_then_recovers(ag, monkeypatch):
    levels = iter([("critical", 0.5), ("critical", 0.5), ("ok", 50.0), ("ok", 50.0)])
    monkeypatch.setattr(guards, "disk_level", lambda cfg, free=None: next(levels))
    ag.on_event(START)
    assert ag.check_disk() == "critical"
    assert ag.p.active is None and ag.disk_blocked and any(k == "error" for _, k in ag.notes)
    ag.on_event(START)  # blocked: remembered, not started
    assert ag.p.started == ["com.microsoft.teams2"]
    assert ag.check_disk() == "ok" and not ag.disk_blocked
    assert ag.p.started == ["com.microsoft.teams2"] * 2  # pending call started after recovery


def test_agent_disk_warn_notifies_once(ag, monkeypatch):
    monkeypatch.setattr(guards, "disk_level", lambda cfg, free=None: ("warn", 3.0))
    ag.check_disk(now=1000.0)
    ag.check_disk(now=1010.0)
    assert [k for _, k in ag.notes].count("warn") == 1


def test_agent_done_and_fail_hooks(ag, tmp_path):
    s = SimpleNamespace(id="x", data={"title": "Weekly"})
    ag.p.on_done(s, tmp_path / "2026-10-15_1000_weekly")
    assert ("Transcript ready", "done") in ag.notes and ag.last_transcript["path"].endswith("weekly")
    ag.p.on_fail(s, RuntimeError("boom"))
    assert ("Transcription failed", "error") in ag.notes and ag.snapshot()["state"] == "error"


def test_agent_snapshot_transcribing(ag):
    agentstate.PROGRESS.begin("sess", 100)
    agentstate.PROGRESS.advance("others", 40)
    try:
        s = ag.snapshot()
        assert s["state"] == "transcribing" and s["queue_len"] == 1 and s["progress"]["fraction"] == 0.4
    finally:
        agentstate.PROGRESS.reset()


def test_agent_retention_at_startup(ag, cfg):
    d = Path(cfg["output_dir"]) / "2020" / "old"
    d.mkdir(parents=True)
    (d / "call.m4a").write_text("x")
    os.utime(d / "call.m4a", (1, 1))
    assert ag.run_retention()["m4a_deleted"] == 1


# ---------------------------------------------------------------- packaging
@pytest.mark.skipif(not Path("/usr/bin/plutil").exists(), reason="plutil only on macOS")
@pytest.mark.parametrize("kind", ["agent", "menubar"])
def test_plist_templates_render_and_lint(kind, tmp_path, monkeypatch):
    from dop_call_recorder import service
    monkeypatch.setenv("DOP_STATE_DIR", str(tmp_path / "st"))
    text = service.render(kind)
    ok, msg = service.lint(text)
    assert ok, msg
    assert "@" not in text.replace("com.devopspass", "") and str(tmp_path / "st") in text
    assert ".t" in service.label_for(kind)  # isolated runs never reuse the real launchd label


def test_agent_pause_finalize_does_not_block_tick(ag):
    gate, done = threading.Event(), threading.Event()
    def slow_end(*a, **k):
        ag.p.active = None
        gate.wait(5); done.set()
    ag.p.end_call = slow_end
    ag.async_end = True
    ag.on_event(START)
    agentstate.set_paused(True)
    t0 = time.time()
    ag.tick()  # must return while "ffmpeg" is still running
    assert time.time() - t0 < 2 and not done.is_set()
    assert agentstate.read_status()["paused"] is True
    gate.set(); done.wait(5)


def test_agent_watchdog_restarts_idle_while_call_live(ag):
    ag.on_event(START)
    ag.p.active = None  # recording lost for any reason while the detector still reports the call
    ag.tick(now=1000.0)
    assert ag.p.started == ["com.microsoft.teams2"] * 2
    ag.on_event({"event": "end", "app": "com.microsoft.teams2"})
    ag.tick(now=2000.0)
    assert len(ag.p.started) == 2
