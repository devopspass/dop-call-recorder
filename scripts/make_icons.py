"""Generates app icon (.icns) and menu-bar state icons from icons/base.png (DevOps Pass pinwheel).
Usage: .venv/bin/python scripts/make_icons.py   -> icons/AppIcon.icns, icons/menubar/<state>{,@2x}.png"""
import subprocess, shutil
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent / "icons"
base = Image.open(ROOT / "base.png").convert("RGBA")
base = base.crop(base.getbbox())
RED, AMBER, GREY, BLUE = (230, 50, 50, 255), (245, 170, 20, 255), (110, 110, 120, 255), (40, 110, 230, 255)

def squircle_mask(n, r=0.2237):
    m = Image.new("L", (n * 4, n * 4), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, n * 4 - 1, n * 4 - 1], radius=int(n * 4 * 0.2237 * 1.0), fill=255)
    return m.resize((n, n), Image.LANCZOS)

def paste_center(canvas, img, scale, dx=0, dy=0):
    n = canvas.width
    s = int(n * scale)
    im = img.copy(); im.thumbnail((s, s), Image.LANCZOS)
    canvas.alpha_composite(im, ((n - im.width) // 2 + dx, (n - im.height) // 2 + dy))

def glyph(d, kind, cx, cy, r, fg=(255, 255, 255, 255)):
    w = max(1, int(r * 0.16))
    if kind == "mic":
        d.rounded_rectangle([cx - r*.22, cy - r*.55, cx + r*.22, cy + r*.1], radius=r*.22, fill=fg)
        d.arc([cx - r*.42, cy - r*.3, cx + r*.42, cy + r*.38], 0, 180, fill=fg, width=w)
        d.line([cx, cy + r*.38, cx, cy + r*.62], fill=fg, width=w)
        d.line([cx - r*.25, cy + r*.62, cx + r*.25, cy + r*.62], fill=fg, width=w)
    elif kind == "clock":
        d.ellipse([cx - r*.5, cy - r*.5, cx + r*.5, cy + r*.5], outline=fg, width=w)
        d.line([cx, cy, cx, cy - r*.32], fill=fg, width=w); d.line([cx, cy, cx + r*.24, cy + r*.1], fill=fg, width=w)
    elif kind == "pause":
        for sx in (-.2, .2):
            d.rectangle([cx + r*sx - r*.09, cy - r*.4, cx + r*sx + r*.09, cy + r*.4], fill=fg)
    elif kind == "warn":
        d.rectangle([cx - r*.07, cy - r*.45, cx + r*.07, cy + r*.1], fill=fg)
        d.ellipse([cx - r*.09, cy + r*.25, cx + r*.09, cy + r*.43], fill=fg)

def badge(canvas, color, kind, frac=0.40, ring=True):
    n = canvas.width; S = 4
    layer = Image.new("RGBA", (n * S, n * S), (0, 0, 0, 0)); d = ImageDraw.Draw(layer)
    r = n * S * frac / 2; cx = n * S - r - n * S * 0.02; cy = cx
    if ring: d.ellipse([cx - r - n*S*.02, cy - r - n*S*.02, cx + r + n*S*.02, cy + r + n*S*.02], fill=(255, 255, 255, 255))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    if kind: glyph(d, kind, cx, cy, r)
    canvas.alpha_composite(layer.resize((n, n), Image.LANCZOS))

def app_icon(n=1024):
    c = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    tile = int(n * 0.8125); off = (n - tile) // 2
    sh = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    sh.paste((0, 0, 0, 90), (off, off + n // 60), squircle_mask(tile))
    c.alpha_composite(sh.filter(ImageFilter.GaussianBlur(n // 50)))
    t = Image.new("RGBA", (tile, tile), (255, 255, 255, 255)); t.putalpha(squircle_mask(tile))
    pin = Image.new("RGBA", (tile, tile), (0, 0, 0, 0)); paste_center(pin, base, 0.62, -tile // 24, -tile // 24)
    t.alpha_composite(pin)
    c.alpha_composite(t, (off, off))
    sub = c.crop((off, off, off + tile, off + tile)); bl = Image.new("RGBA", sub.size, (0, 0, 0, 0))
    badge(bl, BLUE, "mic", frac=0.34)
    bl.putalpha(Image.composite(bl.getchannel("A"), Image.new("L", bl.size, 0), squircle_mask(tile)))
    c.alpha_composite(bl, (off, off))
    return c

def menubar(state, n):
    c = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    img = base
    if state == "stopped":
        g = img.convert("LA").convert("RGBA"); a = g.getchannel("A").point(lambda v: int(v * .45)); g.putalpha(a); img = g
    paste_center(c, img, 0.86, -n // 14, -n // 14)
    spec = {"recording": (RED, None), "transcribing": (AMBER, "clock"), "paused": (GREY, "pause"), "error": (AMBER, "warn")}
    if state in spec: badge(c, *spec[state], frac=0.52, ring=False)
    return c

def main():
    iconset = ROOT / "AppIcon.iconset"; shutil.rmtree(iconset, ignore_errors=True); iconset.mkdir()
    big = app_icon(1024); big.save(ROOT / "AppIcon-1024.png")
    for s in (16, 32, 128, 256, 512):
        big.resize((s, s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
        big.resize((s * 2, s * 2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ROOT / "AppIcon.icns")], check=True)
    shutil.rmtree(iconset)
    mb = ROOT / "menubar"; mb.mkdir(exist_ok=True)
    for st in ("idle", "recording", "transcribing", "paused", "error", "stopped"):
        menubar(st, 72).resize((18, 18), Image.LANCZOS).save(mb / f"{st}.png")
        menubar(st, 144).resize((36, 36), Image.LANCZOS).save(mb / f"{st}@2x.png")
    # contact sheet for preview
    sheet = Image.new("RGBA", (6 * 120 + 20, 360), (235, 235, 240, 255))
    for i, st in enumerate(("idle", "recording", "transcribing", "paused", "error", "stopped")):
        sheet.alpha_composite(menubar(st, 144).resize((108, 108), Image.LANCZOS), (10 + i * 120, 240))
    sheet.alpha_composite(big.resize((220, 220), Image.LANCZOS), (10, 10))
    sheet.save(ROOT / "preview.png")

main()
