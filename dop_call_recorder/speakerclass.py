"""Speaker-class learner (X3): the blue speaker ring is not in the AX tree as text, but it may be a (hashed, build-specific)
CSS class on the speaking tile's container, which the AX tree does expose. We learn such a class at runtime: the screenshot ring
(ringdetect) labels tiles, and a class token that is on (nearly) every ringed tile and (almost) no other tile is the marker.
Verdicts are stored per Teams app version in `<state>/speaker_class.json` (class tokens + counters only, no names or text).
Caps learning, so a ring that is not a class does not keep us screenshotting forever (the ring path still works then)."""
from __future__ import annotations

import json
import time
import zlib
import plistlib
from collections import Counter
from pathlib import Path

from dop_call_recorder import log, paths

_log = log.get("speakerclass")
MIN_POS, CAP_POS, MIN_SPEAKERS = 12, 20, 3
NONE_TTL = 7 * 86400  # a 'no class' verdict is retried after this long
POS_RATIO, NEG_RATIO = 0.90, 0.02
DROP_AFTER = 3  # disagreements with the screenshot ring (out of the last WINDOW checks) that invalidate a learned class
WINDOW = 5
TEAMS_PLISTS = ("/Applications/Microsoft Teams.app/Contents/Info.plist", "/Applications/Microsoft Teams (work or school).app/Contents/Info.plist")


def teams_version(plists=TEAMS_PLISTS) -> str:
    for p in plists:
        try:
            d = plistlib.loads(Path(p).read_bytes())
            return str(d.get("CFBundleVersion") or d.get("CFBundleShortVersionString") or "unknown")
        except (OSError, ValueError):
            continue
    return "unknown"


class SpeakerClass:
    def __init__(self, version: str | None = None, path: Path | None = None):
        self.version = version or teams_version()
        self.path = Path(path) if path else paths.state_dir() / "speaker_class.json"
        self.rec: dict = {"tokens": [], "verdict": None, "n_pos": 0, "n_neg": 0, "pos": {}, "neg": {}, "who": []}
        self._checks: list[bool] = []
        try:
            d = json.loads(self.path.read_text())
            if d.get("version") == self.version:
                self.rec.update(d.get("rec", {}))
                if self.rec["verdict"] == "none" and time.time() - float(d.get("saved", 0)) > NONE_TTL:
                    self.rec = {"tokens": [], "verdict": None, "n_pos": 0, "n_neg": 0, "pos": {}, "neg": {}, "who": []}
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ state
    @property
    def verdict(self) -> str | None:  # "learned" | "none" | None (still learning)
        return self.rec["verdict"]

    @property
    def tokens(self) -> list[str]:
        return list(self.rec["tokens"])

    def describe(self) -> str:
        v = self.verdict
        if v == "learned":
            return f"speaking class learned for Teams {self.version} ({len(self.tokens)} token(s))"
        if v == "none":
            return f"no speaking class exists for Teams {self.version}; using screenshot ring"
        return f"learning speaking class for Teams {self.version} ({self.rec['n_pos']}/{CAP_POS} samples)"

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"version": self.version, "saved": time.time(), "rec": self.rec}))
        except OSError:
            pass

    def reset(self) -> None:
        self.rec = {"tokens": [], "verdict": None, "n_pos": 0, "n_neg": 0, "pos": {}, "neg": {}, "who": []}
        self._checks = []
        self._save()

    # ------------------------------------------------------------------ learning
    def observe(self, tiles: list[dict], ringed: list[str]) -> None:
        """One tick where the screenshot ring named `ringed` tiles. Only unmuted ringed tiles count as positives (the AX
        cross-check); every other tile is a negative. Ticks without a usable ring are ignored."""
        if self.verdict is not None:
            return
        pos = [t for t in tiles if t["name"] in ringed and (not t["muted"] or t["self"]) and t.get("classes")]
        if not pos or len(pos) != len(ringed):
            return
        neg = [t for t in tiles if t["name"] not in ringed and t.get("classes")]
        if not neg:
            return
        P, N = Counter(self.rec["pos"]), Counter(self.rec["neg"])
        for t in pos:
            P.update(set(t["classes"]))
        for t in neg:
            N.update(set(t["classes"]))
        who = set(self.rec.get("who", []))
        who.update(zlib.crc32(t["name"].encode()) % 10007 for t in pos)  # only a count matters; no names are stored
        self.rec["who"] = sorted(who)
        self.rec["n_pos"] += len(pos)
        self.rec["n_neg"] += len(neg)
        self.rec["pos"], self.rec["neg"] = dict(P), dict(N)
        if self.rec["n_pos"] >= MIN_POS and len(self.rec["who"]) >= MIN_SPEAKERS:
            cands = sorted(k for k, v in P.items()
                           if v / self.rec["n_pos"] >= POS_RATIO and N.get(k, 0) / max(self.rec["n_neg"], 1) <= NEG_RATIO)
            if cands:
                self.rec.update(tokens=cands, verdict="learned", pos={}, neg={}, who=[])
                log.event(_log, "speaker_class_learned", version=self.version, tokens=len(cands))
            elif self.rec["n_pos"] >= CAP_POS and len(self.rec["who"]) >= MIN_SPEAKERS:
                self.rec.update(tokens=[], verdict="none", pos={}, neg={}, who=[])
                log.event(_log, "speaker_class_none", version=self.version)
        self._save()

    # ------------------------------------------------------------------ use
    def speaking(self, tiles: list[dict]) -> list[str]:
        """Tiles carrying every learned token that AX also reports as unmuted (self counts)."""
        want = set(self.tokens)
        if self.verdict != "learned" or not want:
            return []
        return [t["name"] for t in tiles if want <= set(t.get("classes") or ()) and (not t["muted"] or t["self"])]

    def validate(self, class_names: list[str], ring_names: list[str]) -> bool:
        """Compare the class result with a screenshot ring check; drop the class after repeated disagreement. Returns False
        when the class was dropped (re-learning starts)."""
        self._checks = (self._checks + [set(class_names) == set(ring_names)])[-WINDOW:]
        if self._checks.count(False) >= DROP_AFTER:
            log.event(_log, "speaker_class_dropped", version=self.version)
            self.reset()
            return False
        return True
