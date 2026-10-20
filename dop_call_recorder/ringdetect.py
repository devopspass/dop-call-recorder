"""Active-speaker detection from the screen (X2): Teams draws a blue/periwinkle ring around the speaking participant's tile,
which is NOT exposed in the accessibility tree. We find ring rectangles in a screenshot and map each to a tile index by walking
the tile grid (gap-coloured lines between tiles), then to a name through the AX tile order (row-major = AX menu-item order).
Everything is fail-safe: any doubt returns no speaker. Callers must cross-check the result against the AX mute state."""
from __future__ import annotations

import os
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np

RING_RGB = (128, 133, 238)
GAP_RGB = (31, 31, 31)
SCREENCAPTURE = "/usr/sbin/screencapture"


def _near(a: np.ndarray, rgb, tol: int) -> np.ndarray:
    return (np.abs(a.astype(np.int16) - np.array(rgb, dtype=np.int16)) <= tol).all(axis=-1)


def find_rings(a: np.ndarray, tol: int = 14) -> list[tuple[int, int, int, int]]:
    """Bounding boxes (x0, y0, x1, y1) of hollow ring-coloured rectangles that look like participant tiles."""
    from scipy import ndimage
    H, W = a.shape[:2]
    mask = _near(a, RING_RGB, tol)
    if not mask.any():
        return []
    lab, n = ndimage.label(mask)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        h, w = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if w < W * 0.06 or h < W * 0.04:
            continue
        cnt = int((lab[sl] == i).sum())
        if cnt > 0.06 * w * h or cnt < 1.2 * (w + h):  # hollow outline, not a filled blob / stray pixels
            continue
        out.append((sl[1].start, sl[0].start, sl[1].stop - 1, sl[0].stop - 1))
    return out


def _gap(a, x, y, tol=1) -> bool:
    H, W = a.shape[:2]
    return 0 <= x < W and 0 <= y < H and bool((np.abs(a[y, x].astype(int) - GAP_RGB) <= tol).all())


def _run(a, x, y, dx, dy, limit) -> int:
    n = 0
    while n < limit and _gap(a, x + dx * n, y + dy * n):
        n += 1
    return n


def tile_index(a: np.ndarray, box: tuple[int, int, int, int], n_tiles: int) -> int | None:
    """Row-major index of the ring's tile in the gallery grid, or None when the grid can't be established."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0 + 1, y1 - y0 + 1
    xm, ym = (x0 + x1) // 2, (y0 + y1) // 2
    lim = max(4, int(w * 0.05))
    # tile gap thickness = the shortest uncapped gap-run next to the ring (a long run is the window margin/toolbar area)
    gxs = [r for r in (_run(a, x0 - 1, ym, -1, 0, lim), _run(a, x1 + 1, ym, 1, 0, lim)) if r >= 2]
    gys = [r for r in (_run(a, xm, y0 - 1, 0, -1, lim), _run(a, xm, y1 + 1, 0, 1, lim)) if r >= 2]
    if not gxs or not gys:
        return None
    gx, gy = min(gxs), min(gys)
    px, py = w + gx, h + gy

    sy, sx = int(h * 0.1), int(w * 0.1)

    def run_at(x: int, y: int, horizontal: bool):
        """Gap run of the expected thickness through (x, y) (searched +-6 px along the axis) -> (start, end) or None.
        Tile backgrounds can be exactly gap-coloured, so only runs of ~gap thickness count."""
        for off in (0, 1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6):
            xx, yy = (x + off, y) if horizontal else (x, y + off)
            if not _gap(a, xx, yy):
                continue
            v = xx if horizontal else yy
            s0 = e0 = v
            while (_gap(a, s0 - 1, yy) if horizontal else _gap(a, xx, s0 - 1)) and v - s0 < 4 * gx:
                s0 -= 1
            while (_gap(a, e0 + 1, yy) if horizontal else _gap(a, xx, e0 + 1)) and e0 - v < 4 * gx:
                e0 += 1
            g = gx if horizontal else gy
            if abs((e0 - s0 + 1) - g) <= 2:
                return s0, e0
        return None

    def runs(tx: int, ty: int, dx: int, dy: int) -> int:
        """Consecutive gap lines (tile separators, plus a window margin line if drawn) in direction (dx, dy)."""
        k = 0
        while k < 12:
            if dx:
                x = (tx - gx // 2 - 1 - k * px) if dx < 0 else (tx + w + gx // 2 + k * px)
                ok = run_at(x, ty + sy, True)
            else:
                y = (ty - gy // 2 - 1 - k * py) if dy < 0 else (ty + h + gy // 2 + k * py)
                ok = run_at(tx + sx, y, False)
            if not ok:
                break
            k += 1
        return k

    for hm in (1, 0):  # horizontal window-margin line drawn (Teams: yes) or not
        for vm in (0, 1):
            left = max(runs(x0, y0, -1, 0) - hm, 0)
            right = max(runs(x0, y0, 1, 0) - hm, 0)
            up = max(runs(x0, y0, 0, -1) - vm, 0)
            down = max(runs(x0, y0, 0, 1) - vm, 0)
            rows = up + down + 1
            if rows == 1:
                cols = n_tiles
                if left + right + 1 != n_tiles:
                    continue
            else:
                top_y = y0 - up * py
                cols = 1 + max(runs(x0, top_y, -1, 0) - hm, 0) + max(runs(x0, top_y, 1, 0) - hm, 0)
            if cols >= 1 and rows * cols >= n_tiles > (rows - 1) * cols and left < cols:
                idx = up * cols + left
                if 0 <= idx < n_tiles:
                    return idx
    return None


def speakers_from_image(a: np.ndarray, tiles: list[dict]) -> list[str]:
    """Names of tiles carrying the speaker ring. `tiles` = AX tiles in order [{name, muted}]; a ringed tile that AX reports as
    muted is rejected (misaligned grid or stale frame)."""
    names = []
    for box in find_rings(a):
        i = tile_index(a, box, len(tiles))
        if i is None:
            continue
        t = tiles[i]
        if t.get("muted") and not t.get("self"):
            continue
        names.append(t["name"])
    return list(dict.fromkeys(names))


# Set by AxPoller while the AX helper app runs: the helper owns Screen Recording, the python agent usually does not.
SHOT_SERVICE: dict | None = None  # {"req": Path, "png": Path}
# One-shot helper capture (doctor / no poller running): callable(png_path, timeout) -> bool
SHOT_ONESHOT = None


def _load(p: str) -> np.ndarray | None:
    from PIL import Image
    try:
        if Path(p).stat().st_size < 1000:
            return None
        return np.asarray(Image.open(p).convert("RGB"))
    except (OSError, ValueError):
        return None


def _capture_service(svc: dict, timeout: float) -> np.ndarray | None:
    import time
    png, req = Path(svc["png"]), Path(svc["req"])
    png.unlink(missing_ok=True)
    req.touch()
    end = time.time() + timeout
    while time.time() < end:
        if png.exists() and not req.exists():
            img = _load(str(png))
            png.unlink(missing_ok=True)
            return img
        time.sleep(0.1)
    req.unlink(missing_ok=True)
    return None


def capture(timeout: float = 5.0) -> np.ndarray | None:
    """Screenshot of the main display as RGB array; None if it fails (no Screen Recording permission, etc.).
    Preferred path is the AX helper app (it holds Screen Recording); direct `screencapture` is the fallback."""
    if SHOT_SERVICE:
        return _capture_service(SHOT_SERVICE, timeout)
    if SHOT_ONESHOT:
        fd, p = tempfile.mkstemp(suffix=".png", prefix="dop-ring-")
        os.close(fd)
        try:
            return _load(p) if SHOT_ONESHOT(p, timeout) else None
        finally:
            try:
                os.unlink(p)
            except OSError:
                pass
    from PIL import Image
    fd, p = tempfile.mkstemp(suffix=".png", prefix="dop-ring-")
    os.close(fd)
    try:
        r = subprocess.run([SCREENCAPTURE, "-x", "-t", "png", p], capture_output=True, timeout=timeout)
        if r.returncode != 0 or Path(p).stat().st_size < 1000:
            return None
        return np.asarray(Image.open(p).convert("RGB"))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    finally:
        try:
            os.unlink(p)
        except OSError:
            pass
