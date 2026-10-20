#!/usr/bin/env python
"""Synthetic call generator for pipeline tests (M4a): macOS `say` + numpy -> chunked 16 kHz mono WAVs
(others_NNNN.wav / me_NNNN.wav + state.json) exactly like the recorder writes them.

  fake_call.py make   OUT --minutes 65          # whole recording at once (+ truth.json)
  fake_call.py record OUT --minutes 6 --speed 10  # live-ish: session.json + chunks growing, for kill -9 tests
Others (Daniel) say "Checkpoint number K ..." at t = 10 + K*gap; Me (Samantha) in between; every 7th Others line
leaks into the Me stream 0.8 s later at -10 dB (echo case, must be dropped); one pause of --pause-min minutes
at --pause-at (chapter case)."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dop_call_recorder.audio import SR, load_audio  # noqa: E402

TEXTS = ["We should check the LogMiner configuration on the staging database before the release.",
         "The eArrest team confirmed that the deployment window is Thursday morning.",
         "Please update the Jira ticket and attach the pipeline logs for the reviewers.",
         "I think the proxy server needs a restart after the certificate rotation.",
         "Let us move the retrospective to next week because several people are on holiday."]


def say(text: str, voice: str, cache: Path) -> np.ndarray:
    f = cache / (hashlib.md5((text + voice).encode()).hexdigest() + ".aiff")
    if not f.exists():
        subprocess.run(["say", "-v", voice, "-o", str(f), text], check=True)
    return load_audio(str(f))


def build(minutes: float, gap: float, pause_at: float, pause_min: float, cache: Path):
    total = int(minutes * 60 * SR)
    others, me = np.zeros(total, np.float32), np.zeros(total, np.float32)
    truth = []
    k = 0
    t = 10.0
    while t < minutes * 60 - 20:
        if pause_min and pause_at * 60 <= t < (pause_at + pause_min) * 60:
            t = (pause_at + pause_min) * 60 + 5
            continue
        for who, off in (("Others", 0.0), ("Me", gap / 2)):
            tt = t + off
            if tt >= minutes * 60 - 15:
                continue
            text = f"Checkpoint number {k}. {TEXTS[k % len(TEXTS)]}"
            clip = say(text, "Daniel" if who == "Others" else "Samantha", cache)
            buf = others if who == "Others" else me
            a = int(tt * SR)
            buf[a:a + len(clip)] += clip[:len(buf) - a]
            truth.append({"speaker": who, "t": tt, "k": k, "text": text, "echo": False})
            if who == "Others" and k % 7 == 3:
                e = a + int(0.8 * SR)
                me[e:e + len(clip)] += 0.3 * clip[:len(me) - e]
                truth.append({"speaker": "Me", "t": tt + 0.8, "k": k, "text": text, "echo": True})
            k += 1
        t += gap
    return others, me, truth


def write_chunks(out: Path, stream: str, audio: np.ndarray, chunk_s: int, upto: int | None = None):
    pcm = (np.clip(audio[:upto], -1, 1) * 32767).astype("<i2")
    n = max(1, -(-len(pcm) // (chunk_s * SR)))
    for i in range(n):
        part = pcm[i * chunk_s * SR:(i + 1) * chunk_s * SR]
        tmp = out / f".{stream}_{i:04d}.tmp"
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(part.tobytes())
        os.replace(tmp, out / f"{stream}_{i:04d}.wav")


def write_state(out: Path, started: float, secs: float, state: str):
    (out / "state.json").write_text(json.dumps({
        "pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "heartbeat_epoch": time.time(), "state": state, "mode": "fake", "apps": ["com.microsoft.teams2"],
        "streams": {s: {"total_seconds": secs} for s in ("others", "me")}}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["make", "record"])
    ap.add_argument("out")
    ap.add_argument("--minutes", type=float, default=5)
    ap.add_argument("--gap", type=float, default=25.0)
    ap.add_argument("--chunk-seconds", type=int, default=300)
    ap.add_argument("--pause-at", type=float, default=0.0, help="minute at which a pause starts")
    ap.add_argument("--pause-min", type=float, default=0.0)
    ap.add_argument("--speed", type=float, default=10.0, help="record mode: audio seconds per wall second")
    ap.add_argument("--work-dir", help="record mode: session work dir (default config work_dir)")
    a = ap.parse_args()
    out = Path(a.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(tempfile.gettempdir()) / "dop_cr_fake_say"
    cache.mkdir(exist_ok=True)
    others, me, truth = build(a.minutes, a.gap, a.pause_at, a.pause_min, cache)
    (out / "truth.json").write_text(json.dumps(truth, indent=1))
    started = time.time() - (a.minutes * 60 if a.mode == "make" else 0)
    if a.mode == "make":
        write_chunks(out, "others", others, a.chunk_seconds)
        write_chunks(out, "me", me, a.chunk_seconds)
        write_state(out, started, a.minutes * 60, "stopped")
        print(out)
        return
    from dop_call_recorder import config as cfgmod
    from dop_call_recorder.pipeline import Session
    cfg = cfgmod.load()
    s = Session.create(a.work_dir or cfg["work_dir"], "com.microsoft.teams2", started, "Fake Call", raw_dir=str(out / "raw"))
    raw = Path(s.data["raw_dir"]); raw.mkdir(parents=True, exist_ok=True)
    print(f"session {s.dir} owner_pid={os.getpid()}", flush=True)
    pos, total = 0, len(others)
    while pos < total:
        pos = min(total, pos + int(a.speed * SR))
        write_chunks(raw, "others", others, a.chunk_seconds, pos)
        write_chunks(raw, "me", me, a.chunk_seconds, pos)
        write_state(raw, started, pos / SR, "recording")
        print(f"recorded {pos / SR:.0f}s", flush=True)
        time.sleep(1)
    write_state(raw, started, total / SR, "stopped")


if __name__ == "__main__":
    main()
