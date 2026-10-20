"""Menu app icon = variant 8A (four-colour ring + mic badge, no wave; transcribing badge violet). Writes icons/MenuAppIcon.icns and icons/menubar8/<state>{,@2x}.png.
Reuses the variant generator kept in icons/variants_gen.py."""
import subprocess, shutil, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent / "icons"
src = (ROOT / "variants_gen.py").read_text()
ns = {"__name__": "variants"}
exec(compile(src, "variants_gen.py", "exec"), ns)
Image = ns["Image"]
W = 95
big = ns["app"](W); big.save(ROOT / "MenuAppIcon-1024.png")
iset = ROOT / "MenuAppIcon.iconset"; shutil.rmtree(iset, ignore_errors=True); iset.mkdir()
for s in (16, 32, 128, 256, 512):
    big.resize((s, s), Image.LANCZOS).save(iset / f"icon_{s}x{s}.png"); big.resize((s*2, s*2), Image.LANCZOS).save(iset / f"icon_{s}x{s}@2x.png")
subprocess.run(["iconutil", "-c", "icns", str(iset), "-o", str(ROOT / "MenuAppIcon.icns")], check=True); shutil.rmtree(iset)
mb = ROOT / "menubar8"; mb.mkdir(exist_ok=True)
for st in ("idle", "recording", "transcribing", "paused", "error", "stopped", "attention"):
    im = ns["mb"](W, st if st != "stopped" else "idle", st == "stopped")
    im.resize((18, 18), Image.LANCZOS).save(mb / f"{st}.png"); im.resize((36, 36), Image.LANCZOS).save(mb / f"{st}@2x.png")
print("ok")
