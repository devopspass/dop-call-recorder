from dop_call_recorder.detector import CallStateMachine, compute_signals

def run(seq, **kw):
    sm = CallStateMachine("x", **kw); evs = []
    for t, pos in seq:
        e = sm.update(t, pos, "T" if pos else None)
        if e: evs.append((e["event"], t))
    return evs

def test_no_call():
    assert run([(t, False) for t in range(0, 100, 2)]) == []

def test_short_blip_ignored():
    assert run([(0, True), (2, True), (4, False), (6, False)]) == []

def test_start_after_5s():
    assert run([(t, True) for t in range(0, 12, 2)]) == [("start", 6)]

def test_short_gap_does_not_end():
    seq = [(t, True) for t in range(0, 10, 2)] + [(t, False) for t in range(10, 40, 2)] + [(t, True) for t in range(40, 50, 2)]
    assert run(seq) == [("start", 6)]

def test_end_after_45s_and_restart():
    seq = [(t, True) for t in range(0, 10, 2)] + [(t, False) for t in range(10, 70, 2)]
    ev = run(seq)
    assert ev == [("start", 6), ("end", 56)]

def test_gap_resets_start_timer():
    assert run([(0, True), (4, True), (5, False), (6, True), (10, True)]) == []

def test_configurable():
    assert run([(t, True) for t in range(4)] + [(t, False) for t in range(4, 10)], start_after=2, end_after=3) == [("start", 2), ("end", 7)]

def test_end_event_has_title_and_duration():
    sm = CallStateMachine("x", 0, 2)
    sm.update(0, True, "Standup"); sm.update(1, False); e = sm.update(3, False)
    assert e["title"] == "Standup" and e["duration"] == 1

PROBE = {"processes": [{"pid": 1, "bundle": "com.microsoft.teams2.helper", "input": False, "output": True},
                       {"pid": 2, "bundle": "com.other", "input": True, "output": False}],
         "windows": [{"bundle": "com.microsoft.teams2", "title": "Chat | Bob | Microsoft Teams"}],
         "devices": [{"name": "Microsoft Teams Audio", "transport": 1986622068, "running": True, "in": True, "out": True}]}

def test_signals_no_false_positive():
    s = compute_signals(PROBE, "com.microsoft.teams2")
    assert not s["positive"] and not s["window"] and not s["mic_any"]  # virtual driver ignored, chat title ignored

def test_signals_output_plus_window():
    p = dict(PROBE, windows=[{"bundle": "com.microsoft.teams2", "title": "Sprint meeting | Microsoft Teams"}])
    assert compute_signals(p, "com.microsoft.teams2")["positive"]

def test_signals_input():
    p = dict(PROBE, processes=[{"pid": 1, "bundle": "com.microsoft.teams2.helper", "input": True, "output": False}])
    assert compute_signals(p, "com.microsoft.teams2")["positive"]


def test_offscreen_window_ignored():
    p = dict(PROBE, windows=[{"bundle": "com.microsoft.teams2", "title": "Sprint meeting | Microsoft Teams", "onscreen": False}],
             processes=[{"pid": 1, "bundle": "com.microsoft.teams2", "input": False, "output": True}])
    assert not compute_signals(p, "com.microsoft.teams2")["positive"]


def test_start_event_has_since():
    sm = CallStateMachine("x", 4, 10)
    evs = [sm.update(t, True) for t in range(0, 8, 2)]
    assert [e for e in evs if e][0]["since"] == 0


def _run_watch(monkeypatch, tmp_path, probes, **kw):
    import dop_call_recorder.detector as d
    it = iter(probes)
    def fake():
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(d, "read_probe", fake)
    monkeypatch.setattr(d.time, "sleep", lambda s: None)
    clock = iter(range(0, 10000, 2))
    monkeypatch.setattr(d.time, "time", lambda: next(clock))
    log = tmp_path / "d.jsonl"; evs = []
    d.watch(["com.microsoft.teams2"], interval=2, start_after=5, end_after=10, log_path=log,
            on_event=evs.append, stdout=False, max_iter=len(probes), **kw)
    return [json.loads(l) for l in log.read_text().splitlines()], evs


import json
ON = {"processes": [{"pid": 1, "bundle": "com.microsoft.teams2", "input": True, "output": False}], "windows": [], "devices": []}
OFF = {"processes": [], "windows": [], "devices": []}


def test_watch_start_end_events(monkeypatch, tmp_path):
    lines, evs = _run_watch(monkeypatch, tmp_path, [OFF] * 2 + [ON] * 5 + [OFF] * 8)
    assert [e["event"] for e in evs] == ["start", "end"]
    assert [l["event"] for l in lines if l["kind"] == "event"] == ["start", "end"]


def test_watch_error_dedupe_and_forced_end(monkeypatch, tmp_path):
    probes = [ON] * 4 + [RuntimeError("boom")] * 40
    lines, evs = _run_watch(monkeypatch, tmp_path, probes)
    assert sum(1 for l in lines if l["kind"] == "error") == 1
    assert [e["event"] for e in evs] == ["start", "end"]
