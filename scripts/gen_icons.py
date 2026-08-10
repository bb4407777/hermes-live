"""Generate Hermes-live icons from a 640x640+ RGBA source PNG.

Outputs:
  assets/icon-source.png            1024x1024 RGBA (master source)
  assets/icon-macos-1024.png        1024x1024 RGBA
  assets/icon-ios-1024.png          1024x1024 RGB (no alpha; iOS App Store rule)
  assets/AppIcon.iconset/icon_*.png 10 macOS iconset sizes (RGBA)
  ios/Assets.xcassets/AppIcon.appiconset/icon-1024.png  1024x1024 RGB

Usage: .venv/bin/python scripts/gen_icons.py <source.png>
"""
from pathlib import Path
import sys

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path(sys.argv[1]).expanduser()

ICONSET_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def flatten_on_white(rgba: Image.Image) -> Image.Image:
    """iOS App Store forbids alpha in 1024x1024 icon. Composite onto white."""
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.split()[3])
    return bg


def main() -> None:
    img = Image.open(SOURCE).convert("RGBA")
    print(f"source: {SOURCE}  size={img.size}  mode={img.mode}")

    master = img.resize((1024, 1024), Image.LANCZOS)

    # master source (RGBA)
    out = ROOT / "assets/icon-source.png"
    master.save(out, "PNG", optimize=True)
    print(f"  -> {out.relative_to(ROOT)}")

    # macOS 1024 (RGBA)
    out = ROOT / "assets/icon-macos-1024.png"
    master.save(out, "PNG", optimize=True)
    print(f"  -> {out.relative_to(ROOT)}")

    # iOS 1024 (RGB, no alpha)
    out = ROOT / "assets/icon-ios-1024.png"
    flatten_on_white(master).save(out, "PNG", optimize=True)
    print(f"  -> {out.relative_to(ROOT)}")

    # ios xcassets 1024 (RGB, no alpha)
    out = ROOT / "ios/Assets.xcassets/AppIcon.appiconset/icon-1024.png"
    flatten_on_white(master).save(out, "PNG", optimize=True)
    print(f"  -> {out.relative_to(ROOT)}")

    # AppIcon.iconset (RGBA, all sizes)
    for name, size in ICONSET_SIZES:
        out = ROOT / "assets/AppIcon.iconset" / name
        master.resize((size, size), Image.LANCZOS).save(out, "PNG", optimize=True)
        print(f"  -> {out.relative_to(ROOT)}")

    print("done.")


if __name__ == "__main__":
    main()