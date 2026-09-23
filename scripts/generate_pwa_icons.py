"""One-off dev script to generate the PWA app icons (static/icons/). Pillow is a dev-only tool
here -- it's not a runtime dependency of the deployed app, only the generated PNGs are used.
Draws a simple lightning-bolt mark matching the app's existing sidebar brand icon (the "⚡" used
in index.html), in the app's own color palette (static/style.css :root), full-bleed so the same
files work as maskable icons too (content kept inside the ~80% safe zone Android's adaptive-icon
mask uses)."""
from PIL import Image, ImageDraw

BG = (11, 11, 17)  # --bg
BOLT = (139, 124, 246)  # --blue / accent

OUT_DIR = "static/icons"


def _bolt_points(size: int) -> list:
    # A simple lightning-bolt polygon, hand-tuned in a 100x100 design grid, kept within the
    # center ~72 units so it sits comfortably inside a maskable icon's safe zone.
    design = [
        (58, 14), (30, 56), (46, 56), (42, 86),
        (70, 44), (54, 44), (58, 14),
    ]
    scale = size / 100
    return [(x * scale, y * scale) for x, y in design]


def make_icon(size: int) -> Image.Image:
    img = Image.new("RGB", (size, size), BG)
    draw = ImageDraw.Draw(img)
    draw.polygon(_bolt_points(size), fill=BOLT)
    return img


def main():
    import os

    os.makedirs(OUT_DIR, exist_ok=True)
    for size in (192, 512):
        img = make_icon(size)
        path = f"{OUT_DIR}/icon-{size}.png"
        img.save(path, "PNG")
        print(f"wrote {path}")

    # Standalone favicon
    make_icon(32).save(f"{OUT_DIR}/favicon-32.png", "PNG")
    print(f"wrote {OUT_DIR}/favicon-32.png")


if __name__ == "__main__":
    main()
