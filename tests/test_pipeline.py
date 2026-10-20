"""M4a: state machine, windowing/stitch, quality gate, merge/dedupe/chapters, outputs/index, e2e + crash recovery."""
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from dop_call_recorder import config as cfgmod
from dop_call_recorder import merge as mg
from dop_call_recorder import outputs, pipeline, quality
from dop_call_recorder.audio import SR
from dop_call_recorder.backends.base import BackendResult, Segment
from dop_call_recorder.windows import Stats, StreamResult, plan_windows, stitch


# ---------------------------------------------------------------- helpers
def S(a, b, t):
    return Segment(a, b, t)


def write_wav(path: Path, audio: np.ndarray):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())


def tones(total_s: float, starts: list[float], dur=4.0) -> np.ndarray:
    """Tone k (amplitude encodes k) at starts[k]."""
    a = np.zeros(int(total_s * SR), np.float32)
    t = np.arange(int(dur * SR)) / SR
    for k, st in enumerate(starts):
        a[int(st * SR):int(st * SR) + len(t)] = (0.05 + 0.01 * k) * np.sin(2 * np.pi * 440 * t)[:len(a) - int(st * SR)]
    return a


class FakeBackend:
    """Segments = non-silent 4 s blocks (each 1 s step); text 'tone K' decoded from amplitude. loop=True -> hallucination loop."""
    name = "fake"

    def __init__(self, loop=False):
        self.loop, self.calls = loop, []

    def describe(self):
        return "fake/test"

    def transcribe(self, audio, prompt="", language=""):
        self.calls.append((len(audio) / SR, prompt, language))
        segs, i, n = [], 0, len(audio) // SR
        while i < n:
            blk = audio[i * SR:(i + 1) * SR]
            if np.abs(blk).max() > 0.02:
                k = int(round((np.abs(blk).max() - 0.05) / 0.01))
                segs.append(Segment(float(i), float(i + 4), "thank you for watching" if self.loop else f"tone {k}"))
                i += 4
            else:
                i += 1
        return BackendResult("en", segs)


def cfg_for(tmp_path, **pc):
    c = cfgmod.load("/nonexistent.toml")
    c.update(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"), vocabulary_file="", backend="auto")
    c["transcribe"]["vad"] = "off"
    c["silence"]["min_call_seconds"] = 5
    c["pipeline"].update(pc)
    return c


# ---------------------------------------------------------------- state machine
def test_state_machine_persists_and_rejects_illegal(tmp_path):
    s = pipeline.Session.create(tmp_path, "com.microsoft.teams2", time.time())
    assert s.state == "recording"
    with pytest.raises(pipeline.StateError):
        s.transition("transcribing")
    s.transition("finalizing"); s.transition("queued"); s.transition("transcribing"); s.transition("done")
    assert pipeline.Session(s.dir).state == "done"          # reloaded from session.json
    assert [h["state"] for h in pipeline.Session(s.dir).data["history"]] == \
        ["recording", "finalizing", "queued", "transcribing", "done"]
    with pytest.raises(pipeline.StateError):
        s.transition("queued")                              # done is terminal


def test_failed_can_requeue(tmp_path):
    s = pipeline.Session.create(tmp_path, "x.y", time.time())
    s.transition("failed", error="boom")
    s.transition("queued")
    assert s.state == "queued"


# ---------------------------------------------------------------- windowing / stitch
def test_plan_windows():
    assert plan_windows(100, 1800, 15) == [(0.0, 100)]
    w = plan_windows(4000, 1800, 15)
    assert w[0] == (0.0, 1800) and w[1][0] == 1785 and w[-1][1] == 4000
    assert all(b - a <= 1800 + 1 for a, b in w)
    assert plan_windows(1830, 1800, 15) == [(0.0, 1830)]      # tiny tail folded into previous window
    # 1h+: every second covered
    w = plan_windows(3900, 1800, 15)
    assert all(w[i][1] > w[i + 1][0] for i in range(len(w) - 1)) and w[-1][1] == 3900


def test_stitch_seam_ownership_and_dedupe():
    # windows (0,100) and (85,185): overlap 85..100, seam 92.5
    a = [S(10, 14, "a"), S(88, 92, "left of seam"), S(94, 99, "cut off at window end")]
    b = [S(86, 90, "early dup"), S(88.5, 93, "left of seam"), S(96, 99.5, "cut off at window end now complete"),
         S(120, 125, "later")]
    out = stitch([(0, 100, a), (85, 185, b)])
    texts = [s.text for s in out]
    assert texts.count("left of seam") == 1
    assert "later" in texts and texts[0] == "a"
    # the straddling duplicate on the far side is removed by the similarity dedupe, not the ownership rule
    assert len([t for t in texts if t.startswith("cut off")]) == 1
    assert [s.start for s in out] == sorted(s.start for s in out)


# ---------------------------------------------------------------- quality gate
def res(texts, speech=None, raw=None, dropped=0, lang="en", dur=600):
    segs = [S(i * 5, i * 5 + 4, t) for i, t in enumerate(texts)]
    return StreamResult(segs, [(0, dur, lang)], Stats(duration=dur, speech_seconds=speech, raw_segments=raw or len(segs),
                                                       dropped_repeats=dropped))


def test_quality_clean():
    assert quality.assess(res([f"this is sentence number {i} about something" for i in range(12)], speech=60)) == []


def test_quality_repeated_lines():
    assert "repeated_lines" in quality.assess(res(["thank you for watching the video"] * 10))
    assert "repeated_lines" in quality.assess(res([f"x {i} y" for i in range(12)], raw=20, dropped=6))


def test_recurring_phrase_over_long_call_is_not_a_loop():
    r = res([f"please update the ticket now {'' if i % 5 else 'again'}" for i in range(40)])
    for i, sg in enumerate(r.segments):
        sg.start = i * 100.0                                   # same line every 100 s: legit, not a loop
    assert "repeated_lines" not in quality.assess(r)


def test_quality_empty_output_only_when_speech_present():
    assert "empty_output" in quality.assess(res([], speech=300))
    assert quality.assess(res([], speech=0)) == []             # muted mic: legitimately empty
    assert "empty_output" in quality.assess(res([], speech=None, dur=900))


def test_quality_wrong_language():
    cyr = "Это длинное предложение на русском языке которое точно не английское"
    out = quality.assess(res([cyr, cyr + " два"], lang="en"))
    assert "wrong_language:ru" in out
    assert quality.assess(res([cyr, cyr + " два"], lang="ru")) == []


def test_quality_high_compression():
    junk = "ha " * 40
    assert "high_compression" in quality.assess(res([junk] * 3 + ["normal text here"] * 4 + ["another fine line"]))


def test_retry_config_differs():
    c = cfgmod.load("/nonexistent.toml")
    rc, kw = quality.retry_config(c, ["wrong_language:ru"], 1800)
    assert rc["preprocess"]["vad_trim"] and kw["window_s"] == 600 and kw["language"] == "ru" and kw["prompt"] == ""
    assert not c["preprocess"]["vad_trim"]                     # original untouched


# ---------------------------------------------------------------- merge
def test_merge_drops_echo_keeps_real_speech():
    others = StreamResult([S(10, 14, "We should restart the proxy server tomorrow morning"),
                           S(30, 33, "Thanks everyone")], [(0, 100, "en")], Stats())
    me = StreamResult([S(10.8, 15, "we should restart the proxy server tomorrow morning"),      # echo (0.8 s later)
                       S(20, 23, "I agree with the plan"),                                          # real
                       S(60, 64, "we should restart the proxy server tomorrow morning")],          # same text, far in time: keep
                      [(0, 100, "en")], Stats())
    segs, dropped = mg.merge(me, others, 2.0)
    assert dropped == 1
    assert [(s.speaker, s.text) for s in segs][:3] == [("Others", "We should restart the proxy server tomorrow morning"),
                                                       ("Me", "I agree with the plan"), ("Others", "Thanks everyone")]
    assert segs[-1].speaker == "Me" and segs[-1].start == 60


def test_short_me_replies_never_dropped_as_echo():
    others = StreamResult([S(10, 11, "yes")], [(0, 100, "en")], Stats())
    me = StreamResult([S(10.5, 11.5, "yes")], [(0, 100, "en")], Stats())
    assert mg.merge(me, others)[1] == 0


def test_per_segment_language_and_shares():
    r = StreamResult([S(0, 5, "the cat is on the table and we have it"), S(6, 10, "привет как дела у вас сегодня")],
                     [(0, 100, "en")], Stats())
    segs = mg.label(r, "Others")
    assert [s.lang for s in segs] == ["en", "ru"]
    assert set(mg.language_shares(segs)) == {"en", "ru"}


def test_turns_and_chapters():
    segs = [mg.Seg(i * 10, i * 10 + 5, f"t{i}", "Me" if i < 3 else "Others") for i in range(6)]
    t = mg.turns(segs, gap=6)
    assert [x.speaker for x in t] == ["Me", "Others"] and t[0].text == "t0 t1 t2"
    late = [mg.Seg(500, 505, "after a long pause", "Me")]
    ch = mg.chapters(segs + [mg.Seg(200, 205, "x", "Me")] + late, gap=60, min_len=100)
    assert len(ch) == 3 and ch[1].start == 200 and ch[2].start == 500
    long = [mg.Seg(i * 5, i * 5 + 4, "w", "Me") for i in range(400)]  # no pauses, 2000 s -> fixed-interval split
    assert len(mg.chapters(long, max_len=600)) >= 3


# ---------------------------------------------------------------- outputs
def test_markdown_front_matter_vtt_and_index(tmp_path):
    segs = [mg.Seg(0, 4, "Hello everyone, welcome to the call.", "Others", "en"),
            mg.Seg(5, 8, "Thanks for having me.", "Me", "en")]
    meta = {"title": "Weekly: sync", "date": "2026-10-15", "start": "2026-10-15T10:00:00+0200", "end": "x",
            "duration": "00:01:05", "duration_seconds": 65, "apps": ["Microsoft Teams"], "language": "en",
            "languages": ["en"], "quality": "degraded", "quality_issues": ["repeated_lines"], "model": "m", "backend": "b"}
    md = outputs.render_markdown(meta, segs, cfgmod.DEFAULTS["pipeline"])
    fm = md.split("---")[1]
    for k in ("title:", "date:", "start:", "end:", "duration:", "apps:", "language:", "languages:", "quality:", "model:", "backend:"):
        assert k in fm
    assert "**[00:00:05] Me:** Thanks for having me." in md and "## [00:00:00]" in md and "quality: degraded" in md
    vtt = outputs.render_vtt(segs)
    assert vtt.startswith("WEBVTT") and "00:00:05.000 --> 00:00:08.000" in vtt and "<v Me>Thanks for having me." in vtt
    root = tmp_path / "idx"
    e = {"date": "2026-10-15", "time": "10:00", "start": "2026-10-15T10:00:00", "duration": "00:01:05", "title": "A | B",
         "path": "2026/x", "quality": "good", "snippet": outputs.snippet(segs)}
    outputs.update_index(root, e)
    outputs.update_index(root, {**e, "start": "2026-10-16T10:00:00", "path": "2026/y", "title": "second"})
    outputs.update_index(root, {**e, "quality": "degraded"})                      # same path: replaced, not duplicated
    calls = json.loads((root / "index.json").read_text())["calls"]
    assert [c["path"] for c in calls] == ["2026/y", "2026/x"] and calls[1]["quality"] == "degraded"
    assert calls[1]["snippet"].startswith("Hello everyone, welcome to the call.")
    idx = (root / "INDEX.md").read_text()
    assert idx.count("\n| 2026") == 2 and "A \\| B" in idx


def test_slug_and_folder(tmp_path):
    assert outputs.slugify("Q3 planning / Budget: v2?") == "Q3_planning_Budget_v2"
    assert outputs.clean_title("Weekly sync | Microsoft Teams") == "Weekly sync"
    t = time.mktime((2026, 10, 15, 14, 30, 0, 0, 0, -1))
    p = outputs.output_folder(tmp_path, t, "Sync")
    assert p == tmp_path / "2026" / "2026-10-15_1430_Sync"
    p.mkdir(parents=True)
    assert outputs.output_folder(tmp_path, t, "Sync").name == "2026-10-15_1430_Sync_2"


# ---------------------------------------------------------------- end to end (fake backend)
def make_raw(raw: Path, total=180, chunk=60, others_starts=(10, 70, 130), me_starts=(40, 100, 150), heartbeat_age=None):
    raw.mkdir(parents=True, exist_ok=True)
    for name, st in (("others", others_starts), ("me", me_starts)):
        a = tones(total, list(st))
        for i in range(-(-total // chunk)):
            write_wav(raw / f"{name}_{i:04d}.wav", a[i * chunk * SR:(i + 1) * chunk * SR])
    hb = time.time() - (heartbeat_age or 0)
    (raw / "state.json").write_text(json.dumps({"pid": 1, "heartbeat_epoch": hb, "state": "recording",
                                                "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(hb - total))}))


def test_process_folder_end_to_end_with_windows(tmp_path):
    cfg = cfg_for(tmp_path, window_minutes=1, overlap_seconds=10)
    raw = tmp_path / "rec"
    make_raw(raw, total=180, chunk=60)
    be = FakeBackend()
    out = pipeline.process_folder(cfg, raw, backend=be)
    assert len(be.calls) >= 6                                   # >=3 windows per stream (60 s windows over 180 s)
    md = (out / "transcript.md").read_text()
    for needle in ("[00:00:10] Others:** tone 0", "[00:00:40] Me:** tone 0", "[00:01:10] Others:** tone 1",
                   "[00:01:40] Me:** tone 1", "[00:02:10] Others:** tone 2", "[00:02:30] Me:** tone 2"):
        assert needle in md, needle
    assert md.count("**[") == 6                                # no seam duplicates
    assert sorted(p.name for p in out.iterdir()) == ["call.m4a", "meta.json", "transcript.md", "transcript.vtt"]
    assert (out / "call.m4a").stat().st_size > 1000
    assert not list(raw.glob("*.wav"))                          # raw deleted after success
    meta = json.loads((out / "meta.json").read_text())
    assert meta["quality"] == "good" and meta["backend"] == "fake/test" and meta["segments"] == 6
    idx = json.loads((Path(cfg["output_dir"]) / "index.json").read_text())["calls"]
    assert len(idx) == 1 and idx[0]["path"] == os.path.relpath(out, cfg["output_dir"]) and idx[0]["snippet"]


def test_keep_raw_and_failure_keeps_raw(tmp_path):
    cfg = cfg_for(tmp_path)
    raw = tmp_path / "rec"
    make_raw(raw)

    class Boom(FakeBackend):
        def transcribe(self, *a, **k):
            raise RuntimeError("model exploded")

    with pytest.raises(RuntimeError):
        pipeline.process_folder(cfg, raw, backend=Boom())
    assert len(list(raw.glob("*.wav"))) == 6                    # raw kept for retry
    s = pipeline.Session(next(Path(cfg["work_dir"]).iterdir()))
    assert s.state == "failed" and "exploded" in s.data["error"]
    out = pipeline.process_folder(cfg, s.dir, keep_raw=True, backend=FakeBackend())  # failed -> retry works
    assert (out / "transcript.md").exists() and len(list(raw.glob("*.wav"))) == 6


def test_quality_loop_triggers_one_rerun_then_degraded(tmp_path):
    cfg = cfg_for(tmp_path)
    raw = tmp_path / "rec"
    make_raw(raw, total=300, chunk=100, others_starts=tuple(range(5, 280, 10)), me_starts=())
    for f in raw.glob("me_*.wav"):
        f.unlink()
    be = FakeBackend(loop=True)
    out = pipeline.process_folder(cfg, raw, backend=be)
    meta = json.loads((out / "meta.json").read_text())
    assert meta["quality"] == "degraded" and "repeated_lines" in meta["quality_issues"] and meta["retried"]["others"]
    assert len(be.calls) == 2 and be.calls[1][1] == ""          # exactly one re-run, without vocabulary prompt
    assert "quality: degraded" in (out / "transcript.md").read_text()


def test_crash_recovery_orphan_session(tmp_path):
    """Owner process dead + stale heartbeat while 'recording' -> recover() finalizes existing chunks and queues."""
    cfg = cfg_for(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    s = pipeline.Session.create(cfg["work_dir"], "com.microsoft.teams2", time.time() - 400, "Standup | Microsoft Teams")
    s.update(owner_pid=dead.pid)
    make_raw(s.raw_dir, total=120, chunk=60, others_starts=(10, 70), me_starts=(40, 100), heartbeat_age=300)
    p = pipeline.Pipeline(cfg)
    assert p.recover() == [s.id]
    s2 = pipeline.Session(s.dir)
    assert s2.state == "queued" and s2.data["recovered"] and s2.data["audio_seconds"] > 100
    assert (s.dir / "call.m4a").exists()
    be = FakeBackend()
    out = pipeline.transcribe_session(s2, cfg, backend=be)
    assert out.name.endswith("_Standup") and "tone 1" in (out / "transcript.md").read_text()
    assert pipeline.Session(s.dir).state == "done" and not list(s.raw_dir.glob("*.wav"))
    assert p.recover() == []                                     # idempotent


def test_recover_requeues_interrupted_transcription_and_skips_live_sessions(tmp_path):
    cfg = cfg_for(tmp_path)
    dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
    a = pipeline.Session.create(cfg["work_dir"], "x.app", time.time() - 100)
    make_raw(a.raw_dir, total=60, chunk=60)
    a.transition("finalizing"); a.transition("queued"); a.transition("transcribing", owner_pid=dead.pid)
    b = pipeline.Session.create(cfg["work_dir"], "y.app", time.time() - 50)          # owner = another LIVE process
    b.update(owner_pid=os.getppid())
    p = pipeline.Pipeline(cfg)
    got = p.recover()
    assert got == [a.id] and pipeline.Session(a.dir).state == "queued" and pipeline.Session(b.dir).state == "recording"


def test_too_short_call_is_skipped(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["silence"]["min_call_seconds"] = 30
    s = pipeline.Session.create(cfg["work_dir"], "com.microsoft.teams2", time.time() - 20)
    make_raw(s.raw_dir, total=10, chunk=10, others_starts=(1,), me_starts=())
    s.update(end_epoch=time.time())
    p = pipeline.Pipeline(cfg)
    p._finalize_and_queue(s)
    s = pipeline.Session(s.dir)
    assert s.state == "done" and s.data["skipped"] == "too_short" and p.q.empty()


def test_title_fallback_app_plus_hhmm(tmp_path):
    cfg = cfg_for(tmp_path)
    raw = tmp_path / "rec"
    make_raw(raw)
    out = pipeline.process_folder(cfg, raw, backend=FakeBackend())
    hhmm = out.name.split("_")[1]
    assert out.name.endswith(f"Microsoft_Teams_{hhmm}")
    assert f'title: "Microsoft Teams {hhmm}"' in (out / "transcript.md").read_text()


def test_end_call_stops_recorder_and_queues(tmp_path, monkeypatch):
    from dop_call_recorder import recorder_bridge

    cfg = cfg_for(tmp_path)
    calls = []

    def fake_start(apps, out):
        calls.append(("start", apps)); make_raw(Path(out), total=60, chunk=60)

    monkeypatch.setattr(recorder_bridge, "start", fake_start)
    monkeypatch.setattr(recorder_bridge, "stop", lambda: calls.append(("stop",)) or "")
    p = pipeline.Pipeline(cfg)
    p.on_event({"event": "start", "app": "com.microsoft.teams2", "title": "Design review | Microsoft Teams", "since": time.time() - 500})
    assert p.active and p.active.state == "recording"
    assert abs(p.active.data["start_epoch"] - (time.time() - 60)) < 5   # recorder start, not detector "since"
    p.on_event({"event": "start", "app": "us.zoom.xos", "title": None, "since": time.time()})   # busy: ignored
    assert len([c for c in calls if c[0] == "start"]) == 1
    p.on_event({"event": "end", "app": "com.microsoft.teams2", "title": None, "ts": time.time()})
    assert ("stop",) in calls and p.active is None and p.q.qsize() == 1
    p.drain  # noqa: B018
    out = pipeline.transcribe_session(pipeline.Session(p.q.get()), cfg, backend=FakeBackend())
    assert out.name.endswith("_Design_review")


def test_auto_backend_falls_back(tmp_path, caplog):
    from dop_call_recorder.backends import get_backend
    from dop_call_recorder.backends.openai_whisper import OpenAIWhisperBackend

    c = cfgmod.load("/nonexistent.toml")
    c["model_dir"] = str(tmp_path)                               # no converted mlx model there
    assert isinstance(get_backend(c), OpenAIWhisperBackend)
