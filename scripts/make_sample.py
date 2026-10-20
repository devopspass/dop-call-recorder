#!/usr/bin/env python3
"""Synthesize a realistic two-speaker call sample with macOS `say` (no real audio involved).

Outputs into --out (default .bench/sample/):
  clean.wav   16k mono, alternating voices, vocabulary terms, 20-60 s silence stretches
  hard.wav    telephone band + crosstalk echo + ~5 dB SNR pink noise
  noisy.wav   same + low-level pink-ish noise, reduced gain (quiet/noisy variant)
  reference.txt   the exact spoken text (one utterance per line) = WER ground truth
"""
from __future__ import annotations

import argparse
import random
import subprocess
import tempfile
from pathlib import Path

VOICES = ["Samantha", "Daniel"]
SENT = [
    "We need to check the {t} configuration on the {e} environment before Friday.",
    "The {t} pipeline failed again, I will look at the logs after the daily meeting.",
    "Can you update the {t} ticket and link it to the change request for {e}.",
    "I think the problem is in {t}, because the same job passed yesterday on {e}.",
    "Let us schedule the {t} upgrade for {e} next week and inform the Oracle team.",
    "Did anyone restart {t} on {e}? The monitoring dashboard shows a gap since morning.",
    "The merge request for {t} is ready for review, please approve it when you have time.",
    "We agreed that {t} will be handled by the platform team, and I will follow up in the chat.",
    "After the deployment, {t} responded slowly, so we rolled back the release on {e}.",
    "Please remember that the production change for {t} needs approval from the change manager.",
]
ENVS = ["dev", "test", "staging", "production"]
VOCAB = ["Flexcube", "Oracle", "Terragrunt", "Terraform", "Kubernetes", "ArgoCD", "Kargo", "GitLab",
         "Artifactory", "Chef", "SWIG", "eArrest", "Corebanking", "Debezium", "LogMiner", "Grafana", "Jira"]


def say(text: str, voice: str, out: Path):
    aiff = out.with_suffix(".aiff")
    subprocess.run(["say", "-v", voice, "-r", "175", "-o", str(aiff), text], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(aiff), "-ar", "16000", "-ac", "1", str(out)], check=True)
    aiff.unlink()


def silence(sec: float, out: Path):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                    "-t", str(sec), str(out)], check=True)


def make_hard(clean: Path, out: Path):
    # hard variant: telephone band (300-3400 Hz @ 8 kHz), voices partly overlapping (second copy of the call,
    # delayed 0.7 s, -9 dB: crosstalk/echo), pink noise at ~5 dB SNR, heavy low gain
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clean), "-i", str(clean), "-f", "lavfi",
                    "-i", "anoisesrc=color=pink:amplitude=0.12:r=16000",
                    "-filter_complex",
                    "[1]adelay=700,volume=-9dB[x];[0][x]amix=inputs=2:duration=first:normalize=0[m];"
                    "[m][2]amix=inputs=2:duration=first:normalize=0,highpass=f=300,lowpass=f=3400,"
                    "aresample=8000,aresample=16000,volume=-12dB",
                    "-ar", "16000", "-ac", "1", str(out / "hard.wav")], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / ".bench" / "sample"))
    ap.add_argument("--turns", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--only-hard", action="store_true", help="only derive hard.wav from existing clean.wav")
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    if a.only_hard:
        return make_hard(out / "clean.wav", out)
    rnd = random.Random(a.seed)
    parts, lines = [], []
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for i in range(a.turns):
            # 1-3 sentences per turn, alternate voices
            text = " ".join(rnd.choice(SENT).format(t=rnd.choice(VOCAB), e=rnd.choice(ENVS))
                            for _ in range(rnd.randint(1, 3)))
            wav = td / f"u{i:03d}.wav"
            say(text, VOICES[i % 2], wav)
            parts.append(wav); lines.append(text)
            gap = rnd.choice([0.8, 1.2, 2.0])
            if i in (9, 21, 33):  # long silence stretches (mute / waiting)
                gap = rnd.choice([25, 40, 60])
            g = td / f"g{i:03d}.wav"; silence(gap, g); parts.append(g)
        lst = td / "list.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in parts))
        clean = out / "clean.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(clean)], check=True)
    # noisy + quiet: -14 dB speech plus pink noise at ~-38 dBFS
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clean), "-f", "lavfi",
                    "-i", "anoisesrc=color=pink:amplitude=0.03:r=16000",
                    "-filter_complex", "[0]volume=-14dB[s];[s][1]amix=inputs=2:duration=first:normalize=0",
                    "-ar", "16000", "-ac", "1", str(out / "noisy.wav")], check=True)
    make_hard(clean, out)
    (out / "reference.txt").write_text("\n".join(lines) + "\n")
    d = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                       "-of", "csv=p=0", str(clean)]))
    print(f"clean.wav {d/60:.1f} min, words={sum(len(l.split()) for l in lines)} -> {out}")


if __name__ == "__main__":
    main()
