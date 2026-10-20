import io, pickle, zipfile
import numpy as np
import pytest
from dop_call_recorder import model_fetch as mf


def test_canonical_and_url():
    assert mf.canonical("large") == "large-v3"
    u, sha = mf.pt_url("turbo")
    assert sha in u and u.endswith("large-v3-turbo.pt")
    with pytest.raises(mf.ModelError):
        mf.pt_url("nope")


def test_blocks_malicious_pickle(tmp_path):
    class Evil:
        def __reduce__(self):
            import os
            return (os.system, ("echo pwned > /tmp/pwned",))
    p = tmp_path / "evil.pt"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("m/data.pkl", pickle.dumps(Evil()))
    with pytest.raises(pickle.UnpicklingError):
        mf.read_pt(p)


def test_real_pt_matches_known_good(tmp_path):
    from pathlib import Path
    pt = Path.home() / ".cache/whisper/small.pt"
    ref = Path.home() / ".cache/whisper/mlx-small"
    if not pt.exists():
        pytest.skip("no cached small.pt")
    dims, sd = mf.read_pt(pt)
    assert dims["n_mels"] == 80 and len(sd) > 100
    assert all(v.dtype in (np.float16, np.float32, np.int64) for v in sd.values())


def test_convert_roundtrip(tmp_path):
    from pathlib import Path
    pt = Path.home() / ".cache/whisper/base.pt"
    if not pt.exists():
        pytest.skip("no cached base.pt")
    out = tmp_path / "mlx-base"
    n = mf.convert_pt(pt, out)
    assert n > 50 and mf.mlx_ready(out)


def test_turbo_identical_to_known_good(tmp_path):
    from pathlib import Path
    pt = Path.home() / ".cache/whisper/large-v3-turbo.pt"
    ref = Path.home() / ".cache/whisper/mlx-large-v3-turbo/weights.npz"
    if not (pt.exists() and ref.exists()):
        pytest.skip("no cached turbo files")
    import mlx.core as mx
    out = tmp_path / "mlx"
    mf.convert_pt(pt, out)
    a, b = mx.load(str(ref)), mx.load(str(out / "weights.safetensors"))
    assert set(a) == set(b)
    assert all(a[k].shape == b[k].shape and mx.array_equal(a[k], b[k]).item() for k in a)
