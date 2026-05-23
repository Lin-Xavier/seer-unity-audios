#!/usr/bin/env python3
"""
make_icon.py
============
Generate `app_icon.ico` — a multi-resolution Windows icon for the
Seer BGM Extractor. The design is a rounded blue square with a white
eighth note in the center, with a small download arrow underneath to
hint at the extraction theme.

Requirements:  pip install Pillow

Run once after editing the design; the resulting `app_icon.ico` is
picked up by build.bat (--icon flag) and by the GUI at runtime
(iconbitmap call).
"""

from PIL import Image, ImageDraw

# Standard Windows icon sizes — Explorer/Taskbar pick the best fit
SIZES = [16, 24, 32, 48, 64, 128, 256]

# Theme colors
BG_COLOR = (52, 120, 220, 255)        # blue
BG_GRADIENT_TO = (88, 156, 252, 255)  # lighter blue for subtle highlight
NOTE_COLOR = (255, 255, 255, 255)
ARROW_COLOR = (255, 255, 255, 230)


def draw_icon(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background: rounded square. Margin scales with size so we don't lose
    # pixels at 16x16. Corner radius is ~18% of the side.
    margin = max(1, size // 24)
    radius = max(2, int(size * 0.18))
    draw.rounded_rectangle(
        [margin, margin, size - margin - 1, size - margin - 1],
        radius=radius,
        fill=BG_COLOR,
    )

    # Eighth note in the upper-middle of the icon.
    # Coordinates expressed as fractions of size, then rounded.
    f = lambda v: int(round(v * size))

    # Note head: tilted ellipse, drawn as a regular ellipse for simplicity
    # at small sizes (tilt isn't visible below ~32px anyway).
    head_left   = f(0.28)
    head_right  = f(0.56)
    head_top    = f(0.58)
    head_bottom = f(0.78)
    draw.ellipse([head_left, head_top, head_right, head_bottom],
                 fill=NOTE_COLOR)

    # Stem: vertical bar on the right side of the head, rising upward
    stem_x_left  = head_right - max(1, size // 28)
    stem_x_right = head_right
    stem_top     = f(0.18)
    stem_bottom  = head_top + (head_bottom - head_top) // 2
    draw.rectangle([stem_x_left, stem_top, stem_x_right, stem_bottom],
                   fill=NOTE_COLOR)

    # Flag: triangle/curve off the top of the stem
    flag_width = f(0.20)
    flag_height = f(0.14)
    draw.polygon([
        (stem_x_right, stem_top),
        (stem_x_right + flag_width, stem_top + flag_height // 2),
        (stem_x_right + flag_width // 2, stem_top + flag_height),
        (stem_x_right, stem_top + flag_height + max(1, size // 40)),
    ], fill=NOTE_COLOR)

    # Download arrow at the bottom, smaller and slightly transparent,
    # hinting at the "extract" theme. Skip on very small sizes where it
    # would just look like noise.
    if size >= 32:
        arrow_cx = f(0.5)
        arrow_top = f(0.82)
        arrow_bottom = f(0.90)
        arrow_half_width = f(0.08)
        shaft_half_width = max(1, size // 40)
        # Arrow shaft
        draw.rectangle([
            arrow_cx - shaft_half_width, arrow_top,
            arrow_cx + shaft_half_width, arrow_bottom - max(2, size // 24),
        ], fill=ARROW_COLOR)
        # Arrowhead (downward triangle)
        draw.polygon([
            (arrow_cx - arrow_half_width, arrow_bottom - max(2, size // 16)),
            (arrow_cx + arrow_half_width, arrow_bottom - max(2, size // 16)),
            (arrow_cx, arrow_bottom),
        ], fill=ARROW_COLOR)

    return img


def main() -> None:
    images = [draw_icon(s) for s in SIZES]
    # Pillow's ICO writer takes the largest image and resizes for the
    # sizes list — but it preserves the per-size renders we pass via
    # `append_images` for better fidelity at small sizes.
    images[-1].save(
        "app_icon.ico",
        format="ICO",
        sizes=[(s, s) for s in SIZES],
        append_images=images[:-1],
    )
    print(f"Wrote app_icon.ico with {len(SIZES)} resolutions: {SIZES}")


if __name__ == "__main__":
    main()
