#!/usr/bin/env python3
"""Convert an openai-whisper .pt checkpoint to mlx-whisper layout (config.json + weights.npz).
Offline replacement for mlx-examples/whisper/convert.py (HuggingFace is blocked)."""
import argparse, json, sys
from pathlib import Path
import mlx.core as mx, numpy as np, torch
from mlx.utils import tree_flatten
from mlx_whisper import whisper


def remap(k):
    k = k.replace("mlp.0", "mlp1").replace("mlp.2", "mlp2")
    return k


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pt"); ap.add_argument("out"); ap.add_argument("--dtype", default="float16")
    a = ap.parse_args()
    ck = torch.load(a.pt, map_location="cpu", weights_only=False)
    dims, sd = ck["dims"], ck["model_state_dict"]
    cfg = dict(n_mels=dims["n_mels"], n_audio_ctx=dims["n_audio_ctx"], n_audio_state=dims["n_audio_state"],
               n_audio_head=dims["n_audio_head"], n_audio_layer=dims["n_audio_layer"], n_vocab=dims["n_vocab"],
               n_text_ctx=dims["n_text_ctx"], n_text_state=dims["n_text_state"], n_text_head=dims["n_text_head"],
               n_text_layer=dims["n_text_layer"])
    dt = getattr(mx, a.dtype)
    w = {}
    for k, v in sd.items():
        if k in ("encoder.positional_embedding",):
            continue  # sinusoidal, recomputed by mlx model
        v = v.numpy()
        if "conv" in k and v.ndim == 3:
            v = v.swapaxes(1, 2)
        w[remap(k)] = mx.array(v).astype(dt)
    model = whisper.Whisper(whisper.ModelDimensions(**cfg), dt)
    expected = {k for k, _ in tree_flatten(model.parameters())}
    missing, extra = expected - set(w) - {"alignment_heads"}, set(w) - expected
    print("missing:", sorted(missing)[:10], "extra:", sorted(extra)[:10])
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps({**cfg, "model_type": "whisper"}))
    mx.savez(str(out / "weights.npz"), **w)
    print("saved", out, len(w), "tensors; missing", len(missing), "extra", len(extra))
    return 1 if (missing or extra) else 0

sys.exit(main())
