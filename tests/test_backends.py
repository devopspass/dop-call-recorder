import json
from pathlib import Path

import numpy as np
import pytest

from dop_call_recorder import config as cfgmod
from dop_call_recorder.backends import get_backend
from dop_call_recorder.backends.base import BackendError
from dop_call_recorder.backends.mlx_whisper import MLXWhisperBackend
from dop_call_recorder.backends.openai_whisper import OpenAIWhisperBackend
from dop_call_recorder.backends.whisper_cpp import WhisperCppBackend


def cfg(**kw):
    c = cfgmod.load()
    c.update(kw)
    return c


def test_default_backend_is_openai_whisper():
    assert cfgmod.DEFAULTS["backend"] == "auto"  # M4a: auto -> mlx when available, else openai-whisper
    assert isinstance(get_backend(cfg(backend="openai-whisper")), OpenAIWhisperBackend)


@pytest.mark.parametrize("name,cls", [("mlx-whisper", MLXWhisperBackend), ("whisper-cpp", WhisperCppBackend)])
def test_selection(name, cls):
    assert isinstance(get_backend(cfg(backend=name)), cls)


def test_unknown_backend():
    with pytest.raises(BackendError, match="unknown backend"):
        get_backend(cfg(backend="nope"))


def test_missing_models_raise_clear_errors(tmp_path):
    pytest.importorskip("mlx_whisper")
    with pytest.raises(BackendError, match="dop-recorder setup"):
        get_backend(cfg(backend="mlx-whisper", model_dir=str(tmp_path))).check()
    with pytest.raises(BackendError, match="whisper-cli|ggml"):
        get_backend(cfg(backend="whisper-cpp", model_dir=str(tmp_path),
                        whispercpp={"binary": str(tmp_path / "nope")})).check()
    (tmp_path / "wc").write_text("x"); (tmp_path / "wc").chmod(0o755)
    with pytest.raises(BackendError, match="ggml model not found"):
        get_backend(cfg(backend="whisper-cpp", model_dir=str(tmp_path),
                        whispercpp={"binary": str(tmp_path / "wc")})).check()


def test_mlx_import_missing_gives_hint(monkeypatch, tmp_path):
    for m in ("mlx", "mlx.core", "mlx_whisper"):
        monkeypatch.setitem(__import__("sys").modules, m, None)
    with pytest.raises(BackendError, match="brew reinstall dop-call-recorder"):
        get_backend(cfg(backend="mlx-whisper", model_dir=str(tmp_path))).check()


def test_cli_clean_error_before_audio_load(tmp_path, capsys, monkeypatch):
    from dop_call_recorder import cli
    conf = tmp_path / "c.toml"
    conf.write_text('backend = "bogus"\n[logging]\nfile = ""\n')
    monkeypatch.setattr("dop_call_recorder.transcribe.load_audio", lambda p: pytest.fail("audio loaded before backend check"))
    rc = cli.main(["--config", str(conf), "transcribe", "/nonexistent.wav"])
    err = capsys.readouterr().err
    assert rc == 1 and "dop-recorder: error: unknown backend" in err and "Traceback" not in err


def test_whisper_cpp_command_and_parse(tmp_path, monkeypatch):
    model = tmp_path / "ggml-large-v3-turbo.bin"; model.write_bytes(b"x")
    binary = tmp_path / "whisper-cli"; binary.write_bytes(b"x"); binary.chmod(0o755)
    c = cfg(backend="whisper-cpp", model_dir=str(tmp_path), whispercpp={"binary": str(binary)})
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        of = cmd[cmd.index("-of") + 1]
        Path(of + ".json").write_text(json.dumps({"result": {"language": "en"}, "transcription": [
            {"offsets": {"from": 500, "to": 2500}, "text": " Hello Flexcube."}]}))

    monkeypatch.setattr("subprocess.run", fake_run)
    r = get_backend(c).transcribe(np.zeros(16000, dtype=np.float32), "Flexcube, Oracle", "")
    cmd = seen["cmd"]
    assert cmd[cmd.index("-l") + 1] == "auto" and cmd[cmd.index("-mc") + 1] == "0"
    assert cmd[cmd.index("--prompt") + 1] == "Flexcube, Oracle"
    assert r.language == "en" and (r.segments[0].start, r.segments[0].end, r.segments[0].text) == (0.5, 2.5, "Hello Flexcube.")


def test_transcribe_file_uses_selected_backend(tmp_path, monkeypatch):
    """dop_call_recorder.transcribe goes through the configured backend, remaps VAD-trim times and drops non-speech segments."""
    import dop_call_recorder.transcribe as T
    from dop_call_recorder.backends.base import Backend, BackendResult, Segment

    class Fake(Backend):
        def transcribe(self, audio, prompt="", language=""):
            self.n = len(audio)
            return BackendResult("en", [Segment(0.0, 1.0, "hello"), Segment(0.0, 1.0, "hello")])

    fake = Fake(cfg())
    monkeypatch.setattr(T, "get_backend", lambda c: fake)
    monkeypatch.setattr(T, "load_audio", lambda p: np.zeros(16000 * 60, dtype=np.float32))
    monkeypatch.setattr(T, "speech_spans", lambda a, v: [(30.0, 32.0)])
    c = cfg(preprocess={"vad_trim": True, "loudnorm": False, "denoise": False})
    res = T.transcribe_file("x.wav", c)
    assert fake.n < 16000 * 5  # trimmed
    assert [s.text for s in res["segments"]] == ["hello", "hello"]
    assert res["segments"][0].start == pytest.approx(29.7)
