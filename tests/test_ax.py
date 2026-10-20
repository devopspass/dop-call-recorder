"""X1: Teams Accessibility parsing (fixture AX dumps), speaker mapping in merge, poller, doctor, pipeline integration."""
import json
import time
from pathlib import Path

import pytest

from dop_call_recorder import axparse, axpoll, doctor
from dop_call_recorder import merge as mg
from dop_call_recorder import pipeline
from test_pipeline import FakeBackend, cfg_for, make_raw

FX = Path(__file__).parent / "fixtures"


def fx(name):
    return json.loads((FX / f"ax_{name}.json").read_text())


# ---------------------------------------------------------------- parsing
def test_idle_window_gives_self_name_and_no_call():
    st = axparse.parse(fx("idle"))
    assert st["in_call"] is False and st["self"] == "Jane Doe" and st["title"] is None and st["roster"] == []


def test_call_window_title_roster_and_speaking_class():
    st = axparse.parse(fx("call_speaking_class"))
    assert st["in_call"] and st["title"] == "Weekly sync"
    assert st["roster"] == ["Jane Doe", "Richard Roe", "Edgar Poe"]
    assert st["speaking"] == ["Edgar Poe"] and st["self"] == "Jane Doe"


def test_speaking_text_label():
    assert axparse.parse(fx("call_speaking_text"))["speaking"] == ["Edgar Poe"]


def test_entry_name_and_display_name():
    assert axparse.entry_name("Doe, Jane, Muted") == "Jane Doe"
    assert axparse.entry_name("Echo is speaking") == "Echo"
    assert axparse.entry_name("Call Quality Bot, Unmuted, Organizer") == "Call Quality Bot"
    assert axparse.entry_name("Mute") is None and axparse.entry_name("Muted") is None
    assert axparse.entry_name("Some sentence that ends here.") is None
    assert axparse.display_name("Doe, Jane (You)") == "Jane Doe"
    assert axparse.display_name("Echo") == "Echo"


def test_garbage_trees_never_raise():
    assert axparse.parse([{"role": "AXWindow"}, {"children": [None]}])["in_call"] is False
    assert axparse.parse([])["roster"] == []


# ---------------------------------------------------------------- merge mapping
def seg(a, b, who, t="x"):
    return mg.Seg(a, b, t, who, "en")


def ev(t0, speaking, roster=("Jane Doe", "Richard Roe", "Edgar Poe")):
    return {"ts": t0, "speaking": list(speaking), "roster": list(roster), "self": "Jane Doe"}


def test_majority_overlap_names_others_and_me():
    t0 = 1000.0
    events = [ev(t0 + i, ["Richard Roe"] if i < 5 else ["Edgar Poe"]) for i in range(10)]
    segs = [seg(0.2, 4.8, "Others"), seg(5.2, 9.0, "Others"), seg(2, 3, "Me")]
    out, info = mg.assign_speakers(segs, events, t0)
    assert [s.speaker for s in out] == ["Richard Roe", "Edgar Poe", "Jane Doe"]
    assert info["named_others"] == 2 and info["self"] == "Jane Doe"


def test_segment_straddling_two_speakers_picks_larger_overlap():
    t0 = 0.0
    events = [ev(i, ["A B"] if i < 3 else ["C D"]) for i in range(10)]
    out, _ = mg.assign_speakers([seg(1.0, 6.0, "Others")], events, t0, slack=0)
    assert out[0].speaker == "C D"  # 3 s vs 2 s


def test_self_speaking_is_not_attributed_to_others():
    events = [ev(i, ["Jane Doe"]) for i in range(10)]
    out, _ = mg.assign_speakers([seg(2, 5, "Others")], events, 0.0)
    assert out[0].speaker == "Others"  # several roster members, nobody else speaking -> unknown


def test_single_other_participant_names_all_others():
    events = [ev(i, [], roster=("Jane Doe", "Echo")) for i in range(10)]
    out, info = mg.assign_speakers([seg(1, 2, "Others"), seg(50, 60, "Others"), seg(3, 4, "Me")], events, 0.0)
    assert [s.speaker for s in out] == ["Echo", "Echo", "Jane Doe"] and info["roster"] == ["Echo"]


def test_unknown_falls_back_and_missing_events_keep_me_others():
    out, _ = mg.assign_speakers([seg(1, 2, "Others"), seg(3, 4, "Me")], [], 0.0)
    assert [s.speaker for s in out] == ["Others", "Me"]
    out, _ = mg.assign_speakers([seg(1, 2, "Others"), seg(3, 4, "Me")], [], 0.0, self_name="Jane Doe")
    assert [s.speaker for s in out] == ["Others", "Jane Doe"]


def test_polling_gap_is_not_evidence():
    events = [ev(0, ["A B"]), ev(100, [])]
    out, _ = mg.assign_speakers([seg(50, 55, "Others")], events, 0.0, slack=0)
    assert out[0].speaker == "Others"


def test_turns_merge_named_speakers():
    segs, _ = mg.assign_speakers([seg(0, 2, "Others", "hi"), seg(2.5, 4, "Others", "there")], [ev(i, ["A B"]) for i in range(5)], 0.0)
    assert [t.text for t in mg.turns(segs)] == ["hi there"]


# ---------------------------------------------------------------- poller (fail-safe)
def test_poller_without_helper_is_a_noop(tmp_path):
    p = axpoll.AxPoller(tmp_path, {}, app=tmp_path / "nope.app")
    assert p.start() is False and not p.started
    p.stop()  # must not raise
    assert not (tmp_path / "speaker_events.jsonl").exists()


def test_poller_not_trusted_is_a_noop(tmp_path):
    app = tmp_path / "X.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/dop-call-recorder").write_text("")

    def runner(cmd, **kw):  # simulates `open -W -n app --args check --out F`
        Path(cmd[cmd.index("--out") + 1]).write_text(json.dumps({"trusted": False}))

    p = axpoll.AxPoller(tmp_path / "s", {}, app=app, runner=runner)
    assert p.start() is False
    assert axpoll.check_trusted(app, runner) is False


def test_poller_records_events_and_meta(tmp_path):
    p = axpoll.AxPoller(tmp_path, {"ax": {"sample_seconds": 0}})
    t = time.time()
    p.record(fx("call_speaking_class"), t)
    p.record(fx("idle"), t + 1)  # call window gone: no event line
    lines = [json.loads(x) for x in (tmp_path / "speaker_events.jsonl").read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["speaking"] == ["Edgar Poe"] and lines[0]["title"] == "Weekly sync"
    assert p.meta["roster"] == ["Jane Doe", "Richard Roe", "Edgar Poe"] and p.meta["self"] == "Jane Doe"
    assert (tmp_path / "ax_samples.jsonl").exists()
    assert axpoll.load_events(tmp_path / "speaker_events.jsonl")[0]["ts"] == lines[0]["ts"]


def test_current_title_disabled_in_tests():
    assert axpoll.current_title("com.microsoft.teams2") is None


# ---------------------------------------------------------------- doctor
def test_doctor_accessibility_states(tmp_path):
    cfg = {"ax": {"enabled": True}}
    assert doctor.check_accessibility(cfg, app=tmp_path / "none.app")[0].status == doctor.WARN
    app = tmp_path / "X.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/dop-call-recorder").write_text("")

    def mk(trusted):
        def runner(cmd, **kw):
            Path(cmd[cmd.index("--out") + 1]).write_text(json.dumps({"trusted": trusted}))
        return runner

    c = doctor.check_accessibility(cfg, run=mk(False), app=app)[0]
    assert c.status == doctor.WARN and "Accessibility" in c.fix
    assert doctor.check_accessibility(cfg, run=mk(True), app=app)[0].status == doctor.OK
    assert doctor.check_accessibility({"ax": {"enabled": False}})[0].status == doctor.OK


# ---------------------------------------------------------------- detector title
def test_detector_start_event_gets_ax_title(tmp_path, monkeypatch):
    from dop_call_recorder import detector

    probe = {"processes": [{"bundle": "com.microsoft.teams2", "input": True, "output": True}], "windows": [], "devices": []}
    monkeypatch.setattr(detector, "read_probe", lambda: probe)
    monkeypatch.setattr(detector.time, "sleep", lambda s: None)
    evs = []
    detector.watch(["com.microsoft.teams2"], 0, 0, 45, tmp_path / "d.jsonl", on_event=evs.append, stdout=False, max_iter=2,
                   title_provider=lambda app: "Weekly sync")
    assert evs and evs[0]["event"] == "start" and evs[0]["title"] == "Weekly sync"


# ---------------------------------------------------------------- pipeline integration
def test_process_folder_uses_speaker_events(tmp_path):
    cfg = cfg_for(tmp_path)
    raw = tmp_path / "rec"
    make_raw(raw, total=180, chunk=60)
    start = json.loads((raw / "state.json").read_text())["started"]
    from datetime import datetime
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
    rows = [{"ts": t0 + i, "speaking": ["Richard Roe"] if i < 100 else ["Edgar Poe"],
             "roster": ["Jane Doe", "Richard Roe", "Edgar Poe"], "self": "Jane Doe"} for i in range(180)]
    (raw / "speaker_events.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (raw / "ax_meta.json").write_text(json.dumps({"title": "Weekly sync", "self": "Jane Doe"}))
    out = pipeline.process_folder(cfg, raw, backend=FakeBackend())
    md = (out / "transcript.md").read_text()
    assert "Richard Roe:** tone 0" in md and "Edgar Poe:** tone 2" in md and "Jane Doe:** tone 0" in md
    assert "Others:**" not in md and "Weekly_sync" in out.name
    meta = json.loads((out / "meta.json").read_text())
    assert meta["speakers"]["self"] == "Jane Doe" and "Richard Roe" in meta["participants"]
    assert (out / "speaker_events.jsonl").exists()


def test_process_folder_without_events_unchanged(tmp_path):
    cfg = cfg_for(tmp_path)
    raw = tmp_path / "rec"
    make_raw(raw, total=180, chunk=60)
    md = (pipeline.process_folder(cfg, raw, backend=FakeBackend()) / "transcript.md").read_text()
    assert "Others:**" in md and "Me:**" in md


def test_my_name_config_without_events(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg["ax"] = {"my_name": "Jane Doe"}
    raw = tmp_path / "rec"
    make_raw(raw, total=180, chunk=60)
    md = (pipeline.process_folder(cfg, raw, backend=FakeBackend()) / "transcript.md").read_text()
    assert "Jane Doe:**" in md and "Others:**" in md


# ---------------------------------------------------------------- real Teams test call (Echo bot) - outline from ax_samples.jsonl
def real_tree():
    """Rebuild a tree from the compact outline of the real call sample (lines '<depth> <role> | <text> | <dom> | <class>')."""
    import ast
    lines = json.loads((FX / "ax_real_samples.jsonl").read_text().splitlines()[0])["lines"]
    root = {"role": "AXWindow", "title": lines[0].split(": ", 1)[1], "children": []}
    stack = [(0, root)]
    for ln in lines[1:]:
        head, rest = ln.split(" | ", 1)
        text, dom, cls = (x.strip() for x in rest.rsplit(" | ", 2))
        d, role = head.split(" ", 1)
        node = {"role": role}
        try:
            t = ast.literal_eval(text) if text else ""
        except (SyntaxError, ValueError):  # the first sample was cut mid-string
            t = text.strip("'")
        if t:
            node["desc"] = t
        if dom:
            node["dom"] = dom
        if cls:
            node["class"] = cls
        d = int(d)
        if d == 0:
            continue  # the window itself
        while stack and stack[-1][0] >= d:
            stack.pop()
        stack[-1][1].setdefault("children", []).append(node)
        stack.append((d, node))
    return [root]


def test_real_call_sample_title_self_roster():
    st = axparse.parse(real_tree())
    assert st["in_call"] and st["title"] == "Echo"
    assert st["self"] == "Jane Doe"            # from 'Myself video, Doe, Jane, Unmuted, ...'
    assert st["roster"] == ["Jane Doe", "Echo"]
    assert "Show only" not in st["roster"] and st["speaking"] == []


def test_title_prefixes_stripped():
    assert axparse.clean_title("Meeting compact view | Echo | Microsoft Teams") == "Echo"
    assert axparse.clean_title("Meeting | Weekly sync | Microsoft Teams") == "Weekly sync"
    assert axparse.clean_title("Chat | Foo | Microsoft Teams") is None
    from dop_call_recorder import outputs
    assert outputs.clean_title("Meeting compact view | Echo | Microsoft Teams") == "Echo"


def test_ui_labels_never_become_people():
    for t in ("Show only speaking participants", "Show only", "Show only, Muted", "Filter, Unmuted", "More options"):
        assert axparse.entry_name(t) is None, t
    assert axparse.entry_name("Echo, Context menu is available") == "Echo"
    tree = [{"role": "AXWindow", "title": "Echo | Microsoft Teams", "children": [
        {"role": "AXButton", "desc": "Leave"},
        {"role": "AXGroup", "desc": "Show only speaking participants is speaking"},
        {"role": "AXMenuItem", "desc": "Echo, Context menu is available"}]}]
    st = axparse.parse(tree)
    assert st["roster"] == ["Echo"] and st["speaking"] == []


def test_self_cache_roundtrip_and_pipeline_fallback(tmp_path):
    axpoll.cache_self("Jane Doe")
    assert axpoll.cached_self() == "Jane Doe"
    cfg = cfg_for(tmp_path)
    raw = tmp_path / "rec"
    make_raw(raw, total=180, chunk=60)
    rows = [{"ts": 0, "speaking": [], "roster": ["Echo"], "self": None}]  # self unknown during the call
    (raw / "speaker_events.jsonl").write_text(json.dumps(rows[0]))
    md = (pipeline.process_folder(cfg, raw, backend=FakeBackend()) / "transcript.md").read_text()
    assert "Jane Doe:**" in md and "Echo:**" in md and "Others:**" not in md and "Me:**" not in md


def test_ax_meta_title_beats_raw_detector_title(tmp_path):
    cfg = cfg_for(tmp_path)
    raw = tmp_path / "rec2"
    make_raw(raw, total=120, chunk=60)
    (raw / "speaker_events.jsonl").write_text(json.dumps({"ts": 0, "speaking": [], "roster": ["Echo"], "self": "Jane Doe"}))
    (raw / "ax_meta.json").write_text(json.dumps({"title": "Echo", "self": "Jane Doe"}))
    out = pipeline.process_folder(cfg, raw, backend=FakeBackend(), title="Meeting compact view | Echo | Microsoft Teams") \
        if "title" in pipeline.process_folder.__code__.co_varnames else pipeline.process_folder(cfg, raw, backend=FakeBackend())
    assert "Echo" in out.name and "compact" not in out.name.lower()


def test_entry_name_production_tools_status():
    from dop_call_recorder import axparse
    assert axparse.entry_name("Smith, John, Has control of production tools, muted, Context menu is available") == "John Smith"
    assert axparse.entry_name("Myself video, Doe, Jane, Has control of production tools, Muted, Has context menu") == "Jane Doe"
