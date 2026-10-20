import json
import subprocess

import pytest

from dop_call_recorder import config, paths, service


def test_default_locations(monkeypatch):
    for k in ("DOP_STATE_DIR", "DOP_CR_STATE_DIR"):
        monkeypatch.delenv(k, raising=False)
    assert not paths.isolated()
    assert paths.state_dir().name == "dop-call-recorder"
    assert paths.label() == "com.devopspass.call-recorder"
    assert paths.label("menubar") == "com.devopspass.call-recorder.menubar"
    assert paths.open_env() == []


def test_isolation_moves_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("DOP_STATE_DIR", str(tmp_path))
    assert paths.isolated()
    for p in (paths.logs_dir(), paths.models_dir(), paths.work_dir(), paths.apps_dir(), paths.agents_dir()):
        assert tmp_path in p.parents, p
    assert paths.label().startswith("com.devopspass.call-recorder.t")
    assert paths.label("menubar").startswith("com.devopspass.call-recorder.menubar.t")
    assert paths.open_env() == ["--env", f"DOP_STATE_DIR={tmp_path}"]


def test_config_defaults_resolved_through_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("DOP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DOP_CR_CONFIG", str(tmp_path / "none.toml"))
    cfg = config.load(None)
    assert cfg["model_dir"] == str(paths.models_dir())
    assert cfg["work_dir"] == str(paths.work_dir())
    assert cfg["logging"]["file"].startswith(str(tmp_path))


def test_start_dry_run_changes_nothing(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DOP_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(service, "gate", lambda cfg: (["microphone: not granted"], []))
    rc = service.start({}, dry_run=True, log=print)
    out = capsys.readouterr().out
    assert rc == 1 and "plist OK" in out and "BLOCKED" in out
    assert not list(paths.agents_dir().glob("*")) if paths.agents_dir().exists() else True


def test_start_refuses_when_gate_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("DOP_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(service, "gate", lambda cfg: (["x"], []))
    monkeypatch.setattr(service, "install_launchagent", lambda k: pytest.fail("must not load"))
    assert service.start({}, log=lambda m: None) == 1


def test_grants_stale_after_app_change(monkeypatch, tmp_path):
    monkeypatch.setenv("DOP_STATE_DIR", str(tmp_path))
    service._save_apps({"installed": {"recorder": "aa"}})
    assert not service.grants_stale()
    service._mark_proven()
    assert not service.grants_stale()
    service._save_apps({"installed": {"recorder": "bb"}, "proven": {"recorder": "aa"}})
    assert service.grants_stale()


def test_bootstrap_retries_on_io_error(monkeypatch, tmp_path):
    import subprocess
    from dop_call_recorder import service
    monkeypatch.setenv("DOP_LAUNCHAGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    monkeypatch.setattr(service, "render", lambda kind: "x")
    codes = iter([0, 5, 5, 0])   # bootout, bootstrap x3
    monkeypatch.setattr(service, "_run", lambda cmd, **k: subprocess.CompletedProcess(cmd, next(codes), "", ""))
    assert service.install_launchagent("agent").startswith("loaded")
