#!/usr/bin/env python3
"""Benchmark ASR backends x preprocessing on the same audio; prints a markdown table.

  python scripts/benchmark.py --audio .bench/sample/clean.wav --reference .bench/sample/reference.txt \
      [--backends ow-turbo-cpu,mlx-turbo,...] [--pre none,vad,norm,vad+norm] [--out results.json]

Each combo runs in a fresh subprocess (clean load time + peak RSS, including the whisper-cli child).
Metrics: RTF = (preprocess+transcribe)/audio duration (model load excluded, reported separately),
WER vs reference text, vocab-term recall, hallucinations (consecutive repeats + segments whose words are
mostly not in the reference), load time, peak memory (max RSS of worker/children).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BENCH = ROOT / ".bench"

BACKENDS = {  # name -> cfg overrides
    "ow-turbo-cpu": {"backend": "openai-whisper", "model": "large-v3-turbo", "device": "cpu"},
    "ow-turbo-mps": {"backend": "openai-whisper", "model": "large-v3-turbo", "device": "mps"},
    "ow-small-cpu": {"backend": "openai-whisper", "model": "small", "device": "cpu"},
    "ow-small-mps": {"backend": "openai-whisper", "model": "small", "device": "mps"},
    "mlx-turbo": {"backend": "mlx-whisper", "model": "large-v3-turbo",
                  "mlx": {"model_path": str(BENCH / "models/mlx-large-v3-turbo")}},
    "mlx-small": {"backend": "mlx-whisper", "model": "small",
                  "mlx": {"model_path": str(BENCH / "models/mlx-small")}},
    "wcpp-turbo": {"backend": "whisper-cpp", "model": "large-v3-turbo",
                   "whispercpp": {"model_path": str(BENCH / "whispercpp/ggml-large-v3-turbo.bin")}},
    "wcpp-small": {"backend": "whisper-cpp", "model": "small",
                   "whispercpp": {"model_path": str(BENCH / "whispercpp/ggml-small.bin")}},
}
PRE = {  # name -> preprocess flags
    "none": {},
    "vad": {"vad_trim": True},
    "norm": {"loudnorm": True},
    "vad+norm": {"vad_trim": True, "loudnorm": True},
    "denoise": {"loudnorm": True, "denoise": True},
    "vad+denoise": {"vad_trim": True, "loudnorm": True, "denoise": True},
}


def norm_words(t: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", t.lower().replace("-", " ")).split()


def wer(ref: list[str], hyp: list[str]) -> float:
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / max(1, len(ref))


def vocab_recall(ref_text: str, hyp_text: str, vocab: list[str]) -> float:
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    R, H = squash(ref_text), squash(hyp_text)
    tot = hit = 0
    for v in vocab:
        n = R.count(squash(v))
        tot += n
        hit += min(n, H.count(squash(v)))
    return hit / tot if tot else 1.0


def hallucinations(segs: list[dict], ref_words: set[str]) -> int:
    n, last, run = 0, None, 0
    for s in segs:
        w = norm_words(s["text"])
        if not w:
            continue
        key = " ".join(w)
        run = run + 1 if key == last else 1
        last = key
        if run > 2:  # third+ identical consecutive segment (loop)
            n += 1
        elif sum(x in ref_words for x in w) / len(w) < 0.5:  # mostly invented words ("Thank you for watching")
            n += 1
    return n


def worker(a) -> None:
    from dop_call_recorder import audio as A, config as cfgmod
    from dop_call_recorder.backends import get_backend
    from dop_call_recorder.transcribe import dedupe, _overlaps
    from dop_call_recorder.backends.base import Segment

    cfg = cfgmod.load()
    cfg.update({k: v for k, v in BACKENDS[a.backend].items()})
    cfg["language"] = a.language
    pre = PRE[a.pre]
    audio = A.load_audio(a.audio)
    dur = len(audio) / A.SR
    be = get_backend(cfg)
    t = time.time(); be.load(); load_s = time.time() - t
    prompt = cfgmod.load_vocabulary(cfg) if a.prompt else ""

    t = time.time()
    if pre.get("loudnorm"):
        audio = A.loudnorm(audio, denoise=bool(pre.get("denoise")))
    spans = A.speech_spans(audio, "silero")
    tmap = None
    if pre.get("vad_trim") and spans is not None:
        audio, tmap = A.vad_trim(audio, spans)
    pre_s = time.time() - t

    t = time.time()
    res = be.transcribe(audio, prompt, a.language)
    asr_s = time.time() - t
    segs = res.segments
    if tmap is not None:
        segs = [Segment(tmap.to_original(s.start), tmap.to_original(s.end), s.text) for s in segs]
    raw = [{"start": s.start, "end": s.end, "text": s.text} for s in segs]
    # post-filter exactly as dop_call_recorder.transcribe does (speech overlap + dedupe) to get the *final* output quality
    fin = dedupe([s for s in segs if spans is None or _overlaps(s, spans)])
    ref_text = Path(a.reference).read_text()
    ref_w = norm_words(ref_text)
    hyp_text = " ".join(s.text for s in fin)
    raw_text = " ".join(s["text"] for s in raw)
    vocab = cfgmod._read_vocab({"vocabulary_file": str(ROOT / "vocabulary.yaml")})[0]
    ru = max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    print("RESULT " + json.dumps({
        "backend": a.backend, "pre": a.pre, "audio": Path(a.audio).name, "duration_s": round(dur, 1),
        "load_s": round(load_s, 2), "pre_s": round(pre_s, 2), "asr_s": round(asr_s, 2),
        "rtf": round((pre_s + asr_s) / dur, 4),
        "wer": round(wer(ref_w, norm_words(hyp_text)), 4),
        "wer_raw": round(wer(ref_w, norm_words(raw_text)), 4),
        "vocab_recall": round(vocab_recall(ref_text, hyp_text, vocab), 3),
        "halluc_raw": hallucinations(raw, set(ref_w)), "halluc_final": hallucinations(
            [{"text": s.text} for s in fin], set(ref_w)),
        "segments": len(raw), "peak_mb": round(ru / 1e6),  # macOS ru_maxrss is bytes
        "language": res.language, "text": hyp_text}, ensure_ascii=False))


def run_combo(a, backend: str, pre: str) -> dict:
    cmd = [sys.executable, __file__, "--worker", "--backend", backend, "--pre", pre, "--audio", a.audio,
           "--reference", a.reference, "--language", a.language] + ([] if a.prompt else ["--no-prompt"])
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    for line in out.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    return {"backend": backend, "pre": pre, "audio": Path(a.audio).name,
            "error": (err.strip().splitlines() or ["?"])[-1][:200]}


def table(rows: list[dict]) -> str:
    h = "| audio | backend | pre | RTF | load s | peak MB | WER | WER raw | vocab | halluc raw/final | segs |\n|---|---|---|---|---|---|---|---|---|---|---|\n"
    for r in rows:
        if "error" in r:
            h += f"| {r['audio']} | {r['backend']} | {r['pre']} | ERROR: {r['error']} |||||||\n"
        else:
            h += (f"| {r['audio']} | {r['backend']} | {r['pre']} | {r['rtf']:.3f} | {r['load_s']} | {r['peak_mb']} | "
                  f"{r['wer']*100:.1f}% | {r['wer_raw']*100:.1f}% | {r['vocab_recall']*100:.0f}% | "
                  f"{r['halluc_raw']}/{r['halluc_final']} | {r['segments']} |\n")
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", nargs="+", default=[str(BENCH / "sample/clean.wav")])
    ap.add_argument("--reference", default=str(BENCH / "sample/reference.txt"))
    ap.add_argument("--backends", default="ow-turbo-cpu,ow-small-cpu,mlx-turbo,wcpp-turbo")
    ap.add_argument("--pre", default="none,vad,norm,vad+norm")
    ap.add_argument("--language", default="en", help="'' = auto-detect")
    ap.add_argument("--no-prompt", dest="prompt", action="store_false", help="do not pass the vocabulary file")
    ap.add_argument("--out", default=str(BENCH / "results.jsonl"), help="append JSON lines here")
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--backend")
    a = ap.parse_args()
    if a.worker:
        a.audio = a.audio[0]; a.pre = a.pre
        return worker(a)
    rows = []
    for audio in a.audio:
        for b in a.backends.split(","):
            for p in a.pre.split(","):
                one = argparse.Namespace(**{**vars(a), "audio": audio})
                print(f"... {Path(audio).name} {b} {p}", file=sys.stderr, flush=True)
                r = run_combo(one, b, p)
                rows.append(r)
                with open(a.out, "a") as f:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                print("    ", {k: v for k, v in r.items() if k != "text"}, file=sys.stderr, flush=True)
    print(table(rows))


if __name__ == "__main__":
    main()
