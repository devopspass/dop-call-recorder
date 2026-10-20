from dop_call_recorder import axparse, axpoll, doctor, speakerclass
from dop_call_recorder.speakerclass import SpeakerClass


def tile(name, muted=False, cls=(), me=False):
    return {"name": name, "muted": muted, "self": me, "classes": list(cls)}


def gallery(speaker):
    return [tile(n, n == "C", ["fui-tile", "f1", *(["spk9"] if n == speaker else [])]) for n in "ABCD"]


def test_tiles_of_container_classes():
    def item(n, cls):
        return {"role": "AXMenuItem", "title": f"Doe, {n}, Context menu is available", "class": "inner"}
    def wrap(n, cls):
        return {"role": "AXGroup", "class": cls, "children": [{"role": "AXGroup", "class": "x", "children": [item(n, cls)]}]}
    tree = {"role": "AXWindow", "children": [{"role": "AXGroup", "class": "grid", "children": [wrap("Ann", "t speak"), wrap("Bob", "t")]}]}
    t = axparse.tiles_of([tree])
    assert [x["name"] for x in t] == ["Ann Doe", "Bob Doe"]
    assert "speak" in t[0]["classes"] and "speak" not in t[1]["classes"] and "grid" not in t[0]["classes"]


def test_learns_class(tmp_path):
    c = SpeakerClass("v1", tmp_path / "s.json")
    for i in range(14):
        c.observe(gallery("ABD"[i % 3]), ["ABD"[i % 3]])
    assert c.verdict == "learned" and c.tokens == ["spk9"]
    assert c.speaking(gallery("D")) == ["D"]
    assert SpeakerClass("v1", tmp_path / "s.json").verdict == "learned"
    assert SpeakerClass("v2", tmp_path / "s.json").verdict is None  # new Teams version relearns
    assert "names" not in (tmp_path / "s.json").read_text().lower()


def test_no_class_verdict_after_cap(tmp_path):
    c = SpeakerClass("v1", tmp_path / "s.json")
    for i in range(25):
        c.observe([tile(n, False, ["fui-tile"]) for n in "ABCD"], ["ABD"[i % 3]])
    assert c.verdict == "none"


def test_muted_ringed_tile_is_not_a_sample(tmp_path):
    c = SpeakerClass("v1", tmp_path / "s.json")
    c.observe(gallery("C"), ["C"])
    assert c.rec["n_pos"] == 0


def test_disagreement_drops_class(tmp_path):
    c = SpeakerClass("v1", tmp_path / "s.json")
    c.rec.update(verdict="learned", tokens=["spk9"])
    assert c.validate(["A"], ["A"])
    assert c.validate(["A"], ["B"]) and c.validate(["A"], ["B"])
    assert not c.validate(["A"], ["B"]) and c.verdict is None


def poller(tmp_path):
    p = axpoll.AxPoller(tmp_path, {"ax": {"ring_detect": True}})
    p._cls = SpeakerClass("v1", tmp_path / "s.json")
    return p


def test_poller_uses_class_without_screenshots(tmp_path, monkeypatch):
    p = poller(tmp_path)
    p._cls.rec.update(verdict="learned", tokens=["spk9"])
    p._ring_check = 1e12
    monkeypatch.setattr(axpoll.ringdetect, "capture", lambda: (_ for _ in ()).throw(AssertionError("no screenshot")))
    assert p._speakers({"speaking": [], "tiles": gallery("B")}, 1.0) == (["B"], "class")


def test_poller_ring_teaches_class(tmp_path, monkeypatch):
    p = poller(tmp_path)
    cur = {"s": "A"}
    monkeypatch.setattr(axpoll.ringdetect, "capture", lambda: object())
    monkeypatch.setattr(axpoll.ringdetect, "speakers_from_image", lambda img, tiles: [cur["s"]])
    for i in range(40):
        cur["s"] = "ABD"[(i // 2) % 3]
        r = p._speakers({"speaking": [], "tiles": gallery(cur["s"])}, i * 10.0)
        if p._cls.verdict == "learned":
            break
    assert p._cls.verdict == "learned" and r[1] in ("ring", "class")


def test_ring_gated_when_all_muted(tmp_path, monkeypatch):
    p = poller(tmp_path)
    monkeypatch.setattr(axpoll.ringdetect, "capture", lambda: (_ for _ in ()).throw(AssertionError("gated")))
    tiles = [tile("A", True), tile("B", True)]
    assert p._speakers({"speaking": [], "tiles": tiles}, 1.0) == ([], None)


def test_doctor_line(monkeypatch):
    monkeypatch.setattr(doctor.__dict__.get("ringdetect", None) or __import__("dop_call_recorder.ringdetect", fromlist=["x"]), "capture", lambda: None)
    c = doctor.check_speaker_detection({"ax": {}})
    assert c.status == doctor.WARN and "Screen Recording" in c.detail


def test_needs_three_distinct_speakers(tmp_path):
    c = SpeakerClass("v1", tmp_path / "s.json")
    for i in range(30):
        c.observe(gallery("AB"[i % 2]), ["AB"[i % 2]])
    assert c.verdict is None


def test_none_verdict_expires(tmp_path):
    import json
    c = SpeakerClass("v1", tmp_path / "s.json")
    c.rec.update(verdict="none")
    c._save()
    d = json.loads((tmp_path / "s.json").read_text())
    d["saved"] -= 8 * 86400
    (tmp_path / "s.json").write_text(json.dumps(d))
    assert SpeakerClass("v1", tmp_path / "s.json").verdict is None
