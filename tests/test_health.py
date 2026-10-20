"""Permissions / health model (dop_call_recorder.health) used by the menu-bar submenu."""
import json
import subprocess
from pathlib import Path

import pytest

from dop_call_recorder import config as cfgmod, doctor, health


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


def test_native_mic_beats_evidence(tmp_path):
    f = lambda nat: doctor.check_permissions(appsup=tmp_path / "x", work_dir=str(tmp_path), native=nat)[0]
    assert f({"mic": "authorized"}).status == doctor.OK
    assert f({"mic": "denied"}).status == doctor.FAIL
    assert f({"mic": "restricted"}).status == doctor.FAIL
    assert f({"mic": "not_determined"}).status == doctor.FAIL


def test_system_audio_probe_states(tmp_path):
    sa = lambda tap: doctor.check_permissions(appsup=tmp_path / "x", work_dir=str(tmp_path), tap=tap)[1]
    assert sa({"tap": "audio", "peak": 0.2}).status == doctor.OK
    assert sa({"tap": "create_failed", "status": -1}).status == doctor.FAIL
    s = sa({"tap": "silent"})
    assert s.status == doctor.WARN and "unknown" in s.detail
    assert sa(None).status == doctor.WARN


def test_system_audio_from_past_call(tmp_path):
    d = tmp_path / "s1" / "raw"
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"others": {"peak": 9000}}))
    assert doctor.check_permissions(appsup=tmp_path / "x", work_dir=str(tmp_path))[1].status == doctor.OK


def fake_runner(mic="authorized", tap="audio"):
    def run(cmd, **k):
        args = cmd[cmd.index("--args") + 1:] if "--args" in cmd else []
        if args and args[0] in ("check-permissions", "request-mic", "probe-tap"):
            out = args[args.index("--out") + 1]
            Path(out).write_text(json.dumps({"mic": mic} if args[0] != "probe-tap" else {"tap": tap, "peak": 0.1}))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return run


def test_run_app_roundtrip(tmp_path):
    app = tmp_path / "A.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    assert health.run_app(app, ["check-permissions"], runner=fake_runner("denied")) == {"mic": "denied"}
    assert health.run_app(tmp_path / "missing.app", ["x"]) is None


def test_collect_schema_and_actions(cfg, monkeypatch, tmp_path):
    app = tmp_path / "R.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(doctor, "RECORDER_APP", app)
    items = health.collect(cfg, probe=True, runner=fake_runner("denied", "silent"), recording=False)
    by = {i["id"]: i for i in items}
    assert {"microphone", "system-audio", "accessibility", "recorder-app", "agent", "disk", "model", "ffmpeg", "output-dir"} <= set(by)
    mic = by["microphone"]
    assert mic["status"] == "fail" and mic["required"] and mic["action"]["command"] == ["permissions", "fix", "microphone"]
    assert "Privacy_Microphone" in mic["action"]["url"]
    assert by["system-audio"]["status"] == "warn" and "Privacy_AudioCapture" in by["system-audio"]["action"]["url"]
    assert by["disk"]["action"] is None  # ok items have no fix
    for i in items:
        assert i["status"] in ("ok", "warn", "fail") and i["group"] and i["name"]
    json.dumps(items)


def test_no_probe_while_recording(cfg, monkeypatch, tmp_path):
    app = tmp_path / "R.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(doctor, "RECORDER_APP", app)
    calls = []
    base = fake_runner()
    monkeypatch.setattr(health, "run_app", lambda a, args, t=0, r=None: calls.append(args[0]) or ({"mic": "authorized"} if args[0] != "probe-tap" else {"tap": "audio"}))
    health.collect(cfg, probe=True, recording=True)
    assert "probe-tap" not in calls and "check-permissions" in calls


def test_summary_attention_rules():
    mk = lambda id_, st, req=False: {"id": id_, "status": st, "required": req}
    s = health.summarize([mk("microphone", "fail", True), mk("accessibility", "fail"), mk("x", "warn")])
    assert s["attention"] and s["attention_ids"] == ["microphone"] and "2 missing" in s["title"]
    s = health.summarize([mk("accessibility", "fail"), mk("x", "warn")])
    assert not s["attention"]
    assert health.summarize([mk("a", "ok")])["title"] == "✅ All OK"


def test_fix_microphone_opens_settings_when_denied(cfg, monkeypatch, tmp_path):
    app = tmp_path / "R.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    monkeypatch.setattr(doctor, "RECORDER_APP", app)
    opened = []
    base = fake_runner("denied")
    def run(cmd, **k):
        if cmd[0] == "open" and "--args" not in cmd:
            opened.append(cmd[1])
        return base(cmd, **k)
    r = health.fix("microphone", cfg, runner=run)
    assert not r["ok"] and opened == [health.URLS["microphone"]]
    assert health.fix("microphone", cfg, runner=fake_runner("authorized"))["ok"]
    assert not health.fix("nonsense", cfg)["ok"]


def test_cli_permissions_json_and_write(cfg, monkeypatch, capsys):
    from dop_call_recorder import cli, agentstate
    monkeypatch.setattr(health, "run_app", lambda *a, **k: None)
    args = type("A", (), {"what": "show", "id": None, "json": True, "probe": False, "write": True})()
    cli.cmd_permissions(args, cfg)
    doc = json.loads(capsys.readouterr().out)
    assert doc["items"] and "summary" in doc
    assert json.loads((agentstate.state_dir() / "health.json").read_text())["summary"] == doc["summary"]


def test_audio_proof_cached_until_rebuild(cfg, monkeypatch, tmp_path):
    app = tmp_path / "R.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    exe = app / "Contents" / "MacOS" / "dop-call-recorder"
    exe.write_text("x")
    monkeypatch.setattr(doctor, "RECORDER_APP", app)
    sa = lambda **k: {i["id"]: i for i in health.collect(cfg, runner=fake_runner(), recording=False, **k)}["system-audio"]
    assert sa()["status"] == "warn"
    assert sa(probe=True)["status"] == "ok"            # probe heard audio, proof stored
    assert sa()["status"] == "ok"                      # later non-probe refresh uses the proof
    import os
    os.utime(exe, (exe.stat().st_atime, exe.stat().st_mtime + 100))  # rebuilt => grant reset => proof expires
    assert sa()["status"] == "warn"


def test_auto_probe_rate_limited(cfg, monkeypatch, tmp_path):
    app = tmp_path / "R.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "dop-call-recorder").write_text("x")
    monkeypatch.setattr(doctor, "RECORDER_APP", app)
    assert health._auto_probe_due(1000.0) is True
    assert health._auto_probe_due(1100.0) is False      # within 30 min
    assert health._auto_probe_due(1000.0 + 1801) is True


def test_launchd_fix_offers_install_and_refuses_during_call(cfg, monkeypatch):
    from dop_call_recorder import status
    c = doctor.Check("launchd", doctor.WARN, "LaunchAgent not installed (fine)", "x")
    a = health.action_for("launchd", c)
    assert a["type"] == "cli" and a["command"] == ["permissions", "fix", "launchd"]
    monkeypatch.setattr(status, "gather", lambda cfg: {"state": "recording", "running": True, "pid": 1})
    r = health.fix("launchd", cfg, runner=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not touch launchctl")))
    assert not r["ok"] and "recorded" in r["message"]
