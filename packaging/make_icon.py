"""Draw the app icon (rising bars and an arrow on a navy tile) and build packaging/icon.icns."""

import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent
S = 1024


def draw() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile = Image.new("RGBA", (S, S))
    top, bottom = (22, 52, 92), (8, 22, 44)                      # navy gradient
    px = ImageDraw.Draw(tile)
    for y in range(S):
        t = y / (S - 1)
        px.line([(0, y), (S, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([100, 100, S - 100, S - 100], radius=185, fill=255)   # macOS grid
    img.paste(tile, (0, 0), mask)

    d = ImageDraw.Draw(img)
    base, width, gap, left = 760, 120, 50, 250
    for i, h in enumerate((170, 290, 420)):                      # rising bars
        x = left + i * (width + gap)
        d.rounded_rectangle([x, base - h, x + width, base], radius=22, fill=(92, 200, 170))
    d.line([(225, 610), (410, 470), (540, 520), (770, 290)], fill=(255, 255, 255), width=38, joint="curve")
    d.polygon([(800, 255), (690, 285), (770, 365)], fill=(255, 255, 255))   # arrow head
    return img


def main():
    img = draw()
    img.save(HERE / "icon.png")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            img.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
            img.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(HERE / "icon.icns")], check=True)
    print(f"Wrote {HERE / 'icon.icns'}")


if __name__ == "__main__":
    main()
