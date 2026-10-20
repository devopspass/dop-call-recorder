"""X2: blue speaker-ring detection (synthetic Teams gallery) + speaker source precedence in AxPoller."""
import json

import numpy as np

from dop_call_recorder import axparse, axpoll, ringdetect

TILE, GAPC, RING = (45, 46, 47), (31, 31, 31), ringdetect.RING_RGB


def gallery(cols, rows, n, ringed=(), w=300, h=160, gap=8, dark_tiles=(), top=60, margin=8):
    """Flat-colour copy of the Teams gallery: gap-coloured canvas, tiles with a 1 px lighter border, 3 px ring on speakers.
    The last row is centred when partial, like Teams."""
    W = cols * w + (cols + 1) * gap
    a = np.zeros((top + rows * (h + gap) + 80, W, 3), np.uint8)
    a[:] = GAPC
    pos = []
    for i in range(n):
        r, c = divmod(i, cols)
        in_row = cols if r < rows - 1 else n - (rows - 1) * cols
        x0 = gap + c * (w + gap) + (cols - in_row) * (w + gap) // 2
        y0 = top + gap + r * (h + gap)
        pos.append((x0, y0))
        a[y0:y0 + h, x0:x0 + w] = GAPC if i in dark_tiles else TILE
        a[y0, x0:x0 + w] = (60, 60, 62); a[y0:y0 + h, x0] = (60, 60, 62)
        a[y0 + h - 1, x0:x0 + w] = (60, 60, 62); a[y0:y0 + h, x0 + w - 1] = (60, 60, 62)
        if i in ringed:
            for k in range(3):
                a[y0 + k, x0:x0 + w] = RING; a[y0 + h - 1 - k, x0:x0 + w] = RING
                a[y0:y0 + h, x0 + k] = RING; a[y0:y0 + h, x0 + w - 1 - k] = RING
    return a


def tiles(n, muted_except=()):
    return [{"name": f"P{i}", "muted": i not in muted_except, "self": i == 0} for i in range(n)]


def test_ring_maps_to_row_major_tiles():
    for i in (4, 10, 0, 11):
        a = gallery(4, 3, 12, ringed=(i,))
        assert ringdetect.speakers_from_image(a, tiles(12, muted_except=(i,))) == [f"P{i}"]


def test_two_rings_and_dark_tiles():
    a = gallery(4, 3, 12, ringed=(4, 10), dark_tiles=(3, 5, 9))
    assert ringdetect.speakers_from_image(a, tiles(12, muted_except=(4, 10))) == ["P4", "P10"]


def test_partial_centred_last_row_is_fail_safe():
    a = gallery(4, 3, 10, ringed=(9,))
    assert ringdetect.speakers_from_image(a, tiles(10, muted_except=(9,))) in ([], ["P9"])  # never a wrong name


def test_ring_on_muted_tile_is_rejected():
    a = gallery(4, 3, 12, ringed=(5,))
    assert ringdetect.speakers_from_image(a, tiles(12)) == []  # AX says everybody muted -> misaligned/stale, ignore


def test_no_ring_no_speaker():
    assert ringdetect.speakers_from_image(gallery(4, 3, 12), tiles(12)) == []


def mk_poller(tmp_path, monkeypatch, ring=True):
    p = axpoll.AxPoller(tmp_path, {"ax": {"ring_detect": ring}})
    return p


def test_speaker_precedence(tmp_path, monkeypatch):
    p = mk_poller(tmp_path, monkeypatch)
    t = tiles(4, muted_except=(2,))
    img = gallery(2, 2, 4, ringed=(3,))
    monkeypatch.setattr(ringdetect, "capture", lambda timeout=5.0: img)
    # AX marker wins
    assert p._speakers({"speaking": ["X"], "tiles": t}, 100.0) == (["X"], "ax")
    # ring (tile 3 must be unmuted for AX cross-check)
    t3 = tiles(4, muted_except=(3,))
    assert p._speakers({"speaking": [], "tiles": t3}, 200.0) == (["P3"], "ring")
    # no ring -> sole unmuted participant
    monkeypatch.setattr(ringdetect, "capture", lambda timeout=5.0: gallery(2, 2, 4))
    assert p._speakers({"speaking": [], "tiles": t}, 300.0) == (["P2"], "unmuted")
    # several unmuted, no ring -> nobody
    assert p._speakers({"speaking": [], "tiles": tiles(4, muted_except=(1, 2))}, 400.0) == ([], None)


def test_capture_failure_backs_off_and_falls_back(tmp_path, monkeypatch):
    p = mk_poller(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(ringdetect, "capture", lambda timeout=5.0: calls.append(1))
    t = tiles(4, muted_except=(2,))
    assert p._speakers({"speaking": [], "tiles": t}, 10.0) == (["P2"], "unmuted")
    assert p._speakers({"speaking": [], "tiles": t}, 20.0) == (["P2"], "unmuted")
    assert len(calls) == 1  # backed off 60 s


def test_ring_disabled(tmp_path, monkeypatch):
    p = mk_poller(tmp_path, monkeypatch, ring=False)
    monkeypatch.setattr(ringdetect, "capture", lambda timeout=5.0: (_ for _ in ()).throw(AssertionError("no capture")))
    assert p._speakers({"speaking": [], "tiles": tiles(4, muted_except=(1,))}, 1.0) == (["P1"], "unmuted")


def test_parse_tiles_order_and_mute():
    def item(t):
        return {"role": "AXMenuItem", "title": t}
    win = {"role": "AXWindow", "title": "Weekly | Microsoft Teams", "children": [
        {"role": "AXButton", "title": "Leave"},
        item("Myself video, Doe, Jane, Has control of production tools, Muted, Has context menu"),
        item("Smith, John, Has control of production tools, muted, Context menu is available"),
        item("Novak, Anna, Has control of production tools, Context menu is available")]}
    st = axparse.parse([win])
    assert [(t["name"], t["muted"], t["self"]) for t in st["tiles"]] == [
        ("Jane Doe", True, True), ("John Smith", True, False), ("Anna Novak", False, False)]


def test_capture_via_helper_service(tmp_path, monkeypatch):
    import threading, time
    from PIL import Image
    svc = {"req": tmp_path / "r", "png": tmp_path / "s.png"}
    monkeypatch.setattr(ringdetect, "SHOT_SERVICE", svc)

    def helper():
        while not svc["req"].exists():
            time.sleep(0.02)
        Image.fromarray(gallery(2, 2, 4)).save(svc["png"])
        svc["req"].unlink()

    threading.Thread(target=helper, daemon=True).start()
    assert ringdetect.capture(3) is not None
    monkeypatch.setattr(ringdetect, "SHOT_SERVICE", {"req": tmp_path / "r2", "png": tmp_path / "s2.png"})
    assert ringdetect.capture(0.3) is None  # helper silent -> None, request cleaned up
    assert not (tmp_path / "r2").exists()
