"""Torch-free model provisioning for the MLX backend.

Order: (1) existing <model_dir>/mlx-<model>/ -> (2) optional `model_url` (base URL holding
mlx-<model>/{config.json,weights.safetensors}) -> (3) OpenAI's Azure CDN .pt (sha256 is the URL path segment,
verified BEFORE the file is unpickled) converted to MLX with numpy only (restricted unpickler).
HuggingFace / ModelScope are deliberately not used."""
from __future__ import annotations

import collections
import hashlib
import json
import os
import pickle
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

from dop_call_recorder import log

_log = log.get("model_fetch")
_BASE = "https://openaipublic.azureedge.net/main/whisper/models"
_SHA = {
    "tiny.en": "d3dd57d32accea0b295c96e26691aa14d8822fac7d9d27d5dc00b4ca2826dd03",
    "tiny": "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
    "base.en": "25a8566e1d0c1e2231d1c762132cd20e0f96a85d16145c3a00adf5d1ac670ead",
    "base": "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e",
    "small.en": "f953ad0fd29cacd07d5a9eda5624af0f6bcf2258be67c92b79389873d91e0872",
    "small": "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794",
    "medium.en": "d7440d1dc186f76616474e0ff0b3b6b879abc9d1a4926b7adfa41db2d497ab4f",
    "medium": "345ae4da62f9b3d59415adc60127b97c714f32e89e936602e85993674d08dcb1",
    "large-v3": "e5b1a55b89c1367dacf97e3e19bfd829a01529dbfdeefa8caeb59b3f1b81dadb",
    "large-v3-turbo": "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a",
}
_ALIASES = {"large": "large-v3", "turbo": "large-v3-turbo"}
_DT = {"FloatStorage": np.float32, "HalfStorage": np.float16, "LongStorage": np.int64}


class ModelError(Exception):
    pass


def canonical(name: str) -> str:
    return _ALIASES.get(name, name)


def pt_url(name: str) -> tuple[str, str]:
    name = canonical(name)
    if name not in _SHA:
        raise ModelError(f"unknown model {name!r}; known: {', '.join(sorted(_SHA))}")
    return f"{_BASE}/{_SHA[name]}/{name}.pt", _SHA[name]


def mlx_dir(cfg: dict) -> Path:
    return Path((cfg.get("mlx") or {}).get("model_path")
                or Path(cfg["model_dir"]) / f"mlx-{canonical(cfg['model'])}").expanduser()


def mlx_ready(d: Path) -> bool:
    return (d / "config.json").is_file() and ((d / "weights.safetensors").is_file() or (d / "weights.npz").is_file())


# ---- restricted pickle reader (no torch) -------------------------------------------------------------------
class _Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if name == "_rebuild_tensor_v2" and module.startswith("torch"):
            return lambda st, off, size, stride, *a: (st, off, tuple(size), tuple(stride))
        if name in _DT and module == "torch":
            return name
        if (module, name) == ("collections", "OrderedDict"):
            return collections.OrderedDict
        raise pickle.UnpicklingError(f"blocked global {module}.{name}")

    def persistent_load(self, pid):
        return (pid[1], pid[2])  # (storage type, key)


def read_pt(path) -> tuple[dict, dict]:
    """openai-whisper .pt -> (dims, {name: np.ndarray}). Only a whitelist of pickle globals is allowed."""
    with zipfile.ZipFile(path) as z:
        pre = z.namelist()[0].split("/")[0]
        ck = _Unpickler(z.open(f"{pre}/data.pkl")).load()
        sd = {}
        for k, ((st, key), off, size, stride) in ck["model_state_dict"].items():
            if st not in _DT:
                raise ModelError(f"unsupported tensor storage {st} for {k}")
            a = np.frombuffer(z.read(f"{pre}/data/{key}"), dtype=_DT[st])
            sd[k] = np.lib.stride_tricks.as_strided(
                a[off:], shape=size, strides=[s * a.itemsize for s in stride]).copy()
        return ck["dims"], sd


def convert_pt(pt: Path, out: Path) -> int:
    import mlx.core as mx

    dims, sd = read_pt(pt)
    w = {}
    for k, v in sd.items():
        if k == "encoder.positional_embedding":
            continue  # sinusoidal, recomputed by the mlx model
        k = k.replace("mlp.0", "mlp1").replace("mlp.2", "mlp2")
        if "conv" in k and v.ndim == 3:
            v = v.swapaxes(1, 2)
        w[k] = mx.array(v).astype(mx.float16)
    tmp = out.with_name(out.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    (tmp / "config.json").write_text(json.dumps({**dims, "model_type": "whisper"}))
    mx.save_safetensors(str(tmp / "weights.safetensors"), w)
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    return len(w)


# ---- download ------------------------------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def download(url: str, dest: Path, progress=None, expect_sha: str | None = None, retries: int = 5) -> Path:
    """Resumable download (Range) honouring HTTPS_PROXY; verifies sha256 before the final rename."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    last = None
    for attempt in range(retries):
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                total = have + int(r.headers.get("Content-Length") or 0)
                mode = "ab" if r.status == 206 else "wb"
                done = have if r.status == 206 else 0
                with open(part, mode) as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                        done += len(b)
                        if progress:
                            progress(done, total)
            break
        except urllib.error.HTTPError as e:
            if e.code == 416:  # already complete
                break
            last = e
        except (OSError, urllib.error.URLError) as e:
            last = e
        log.event(_log, "download_retry", url=url, attempt=attempt + 1, error=repr(last))
    else:
        raise ModelError(f"download failed after {retries} attempts: {url}: {last}")
    if expect_sha and _sha256(part) != expect_sha:
        part.unlink(missing_ok=True)
        raise ModelError(f"sha256 mismatch for {url}")
    os.replace(part, dest)
    return dest


def _cli_progress(done, total):
    if total:
        sys.stderr.write(f"\r  {done / 1e6:8.0f} / {total / 1e6:.0f} MB  {100 * done / total:5.1f}%")
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")


def ensure(cfg: dict, progress=_cli_progress, keep_pt: bool = False) -> Path:
    """Make sure the MLX model for cfg['model'] exists; returns its directory."""
    name = canonical(cfg["model"])
    out = mlx_dir(cfg)
    if mlx_ready(out):
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    base = (cfg.get("model_url") or "").rstrip("/")
    if base:
        try:
            tmp = out.with_name(out.name + ".part")
            shutil.rmtree(tmp, ignore_errors=True)
            for fn in ("config.json", "weights.safetensors"):
                download(f"{base}/mlx-{name}/{fn}", tmp / fn, progress)
            shutil.rmtree(out, ignore_errors=True)
            tmp.rename(out)
            log.event(_log, "model_ready", source="model_url", model=name)
            return out
        except ModelError as e:
            log.event(_log, "model_url_failed_fallback_azure", error=str(e))
    url, sha = pt_url(name)
    pt = Path(cfg["model_dir"]).expanduser() / f"{name}.pt"
    if not (pt.is_file() and _sha256(pt) == sha):
        download(url, pt, progress, expect_sha=sha)
    n = convert_pt(pt, out)
    if not keep_pt and not cfg.get("keep_pt", False):
        pt.unlink(missing_ok=True)
    log.event(_log, "model_ready", source="azure-cdn+convert", model=name, tensors=n)
    return out
