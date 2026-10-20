import numpy as np
import pytest

from dop_call_recorder import audio as A

SR = A.SR


def test_vad_trim_removes_silence_and_maps_times():
    audio = np.zeros(SR * 100, dtype=np.float32)
    spans = [(10.0, 12.0), (80.0, 83.0)]
    out, tmap = A.vad_trim(audio, spans, pad=0.3, merge_gap=1.0)
    assert len(out) / SR == pytest.approx(2.6 + 3.6, abs=0.01)
    assert tmap.to_original(0.0) == pytest.approx(9.7)
    assert tmap.to_original(2.6 + 0.3) == pytest.approx(80.0 - 0.3 + 0.3)
    assert tmap.to_original(1e6) == pytest.approx(83.3)  # past the end clamps to last piece


def test_vad_trim_merges_close_spans():
    audio = np.zeros(SR * 20, dtype=np.float32)
    out, tmap = A.vad_trim(audio, [(1.0, 2.0), (2.5, 3.0)], pad=0.3, merge_gap=1.0)
    assert len(tmap.pieces) == 1


def test_vad_trim_no_spans_is_empty():
    out, tmap = A.vad_trim(np.zeros(SR, dtype=np.float32), [])
    assert len(out) == 0 and tmap.to_original(1.0) == 1.0


def test_loudnorm_raises_quiet_signal():
    t = np.arange(SR * 5) / SR
    quiet = (0.01 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    out = A.loudnorm(quiet)
    assert abs(len(out) - len(quiet)) < SR // 10
    assert np.sqrt(np.mean(out ** 2)) > 5 * np.sqrt(np.mean(quiet ** 2))
    assert np.max(np.abs(out)) <= 1.0


def test_load_audio_errors(tmp_path):
    with pytest.raises(A.AudioError):
        A.load_audio(str(tmp_path / "nope.wav"))
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises(A.AudioError):
        A.load_audio(str(bad))
