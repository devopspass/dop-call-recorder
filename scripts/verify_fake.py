#!/usr/bin/env python
"""Compare a fake_call truth.json with the produced transcript.md: label + timestamp accuracy, echo drops, seam dupes.
usage: verify_fake.py TRUTH_JSON TRANSCRIPT_MD [--tol 8]"""
import argparse
import json
import re
import sys

ap = argparse.ArgumentParser()
ap.add_argument("truth"); ap.add_argument("md"); ap.add_argument("--tol", type=float, default=8.0)
a = ap.parse_args()
truth = json.load(open(a.truth))
lines = re.findall(r"\*\*\[(\d+):(\d\d):(\d\d)\] (Me|Others):\*\* (.*)", open(a.md).read())
found = [(int(h) * 3600 + int(m) * 60 + int(s), sp, txt) for h, m, s, sp, txt in lines]
real = [t for t in truth if not t["echo"]]
echoes = [t for t in truth if t["echo"]]
missing, bad_ts, bad_label = [], [], []
for t in real:
    hits = [f for f in found if re.search(rf"Checkpoint number {t['k']}\b", f[2])]
    if not hits:
        missing.append(t["k"]); continue
    # turns can merge several lines: accept any hit whose timestamp is near
    if not any(abs(f[0] - t["t"]) <= a.tol or (f[0] <= t["t"] <= f[0] + 40) for f in hits):
        bad_ts.append((t["k"], t["t"], [f[0] for f in hits]))
    if not any(f[1] == t["speaker"] for f in hits):
        bad_label.append(t["k"])
echo_left = [t["k"] for t in echoes if any(f[1] == "Me" and re.search(rf"Checkpoint number {t['k']}\b", f[2]) for f in found)]
dups = len(found) - len({(f[1], f[2]) for f in found})
rate = 1 - len(missing) / max(1, len(real))
print(json.dumps({"expected": len(real), "found_rate": round(rate, 3), "missing": missing[:20], "bad_timestamps": bad_ts[:10],
                  "bad_labels": bad_label[:10], "echoes_expected_dropped": len(echoes), "echoes_still_present": echo_left,
                  "duplicate_lines": dups, "last_ts": max((f[0] for f in found), default=0)}, indent=1))
sys.exit(0 if rate > 0.9 and not bad_ts and not bad_label and not echo_left and dups == 0 else 1)
