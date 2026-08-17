#!/usr/bin/env python3
"""Render Sysible Controller web branding from the canonical mark.

Single source: branding/sysible-controller-mark.svg (the family hexagon body with
the hub+nodes topology, matching Sysible Linux / SysTerm). Regenerates the web
app logo, favicon, and README badges so they all share the new brand. Run:

    python3 branding/render-web.py
"""
import io
import os

import cairosvg
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = os.path.join(ROOT, "branding/sysible-controller-mark.svg")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

INK = (20, 33, 58)        # "Sysible"
SUB = (90, 107, 140)      # "CONTROLLER"
TAG = (139, 152, 176)     # tagline
FG_DARK = (233, 240, 247)  # wordmark on dark (README dark badge)


def mark(size):
    png = cairosvg.svg2png(url=MARK, output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _tracked(draw, text, font, cx, y, fill, tracking):
    widths = [draw.textlength(c, font=font) for c in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = cx - total / 2
    for c, w in zip(text, widths):
        draw.text((x, y), c, font=font, fill=fill)
        x += w + tracking
    return total


def _fit(text, max_w, cap_px, tracking_ratio):
    """Font + tracking whose tracked width fits max_w (shrinks font AND tracking)."""
    d = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    size = cap_px
    while size > 8:
        font = ImageFont.truetype(FONT, size)
        tr = size * tracking_ratio
        w = sum(d.textlength(c, font=font) for c in text) + tr * (len(text) - 1)
        if w <= max_w:
            return font, tr
        size -= 2
    return ImageFont.truetype(FONT, 8), 0


def vertical_lockup(W, H, bg):
    """Mark over 'Sysible' / 'CONTROLLER' / tagline — matches the prior composition."""
    canvas = Image.new("RGBA", (W, H), bg)
    ms = int(H * 0.38)
    m = mark(ms)
    canvas.alpha_composite(m, ((W - ms) // 2, int(H * 0.13)))
    d = ImageDraw.Draw(canvas)
    mx = W * 0.86
    f1, t1 = _fit("Sysible", mx, int(H * 0.135), 0.006)
    _tracked(d, "Sysible", f1, W / 2, int(H * 0.55), INK, t1)
    f2, t2 = _fit("CONTROLLER", mx, int(H * 0.072), 0.028)
    _tracked(d, "CONTROLLER", f2, W / 2, int(H * 0.70), SUB, t2)
    f3, t3 = _fit("IT INFRASTRUCTURE MANAGEMENT SOFTWARE", mx, int(H * 0.028), 0.012)
    _tracked(d, "IT INFRASTRUCTURE MANAGEMENT SOFTWARE", f3, W / 2, int(H * 0.80), TAG, t3)
    return canvas


def horizontal_badge(H, fg):
    """README badge: mark + 'SYSIBLE CONTROLLER', transparent.

    The canvas WIDTH is derived from the content (mark + gap + tracked wordmark +
    side margins) instead of being fixed, so the wordmark always renders at full
    size and never clips. The previous fixed-width version overflowed the 980px
    canvas and cut 'CONTROLLER' down to 'CONTROLL' on GitHub.
    """
    s = int(H * 0.80)
    gap = int(H * 0.14)
    margin = int(H * 0.10)
    f = ImageFont.truetype(FONT, int(H * 0.26))
    tracking = int(H * 0.03)
    txt = "SYSIBLE CONTROLLER"
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    tw = sum(probe.textlength(c, font=f) for c in txt) + tracking * (len(txt) - 1)
    W = int(s + gap + tw + 2 * margin)
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    m = mark(s)
    d = ImageDraw.Draw(canvas)
    x0 = margin
    canvas.alpha_composite(m, (x0, (H - s) // 2))
    asc, desc = f.getmetrics()
    _tracked(d, txt, f, x0 + s + gap + tw / 2, (H - (asc + desc)) // 2, fg, tracking)
    return canvas


def save(img, rel):
    p = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    img.convert("RGBA").save(p)
    print("wrote", rel)


def main():
    # Reuse the EXACT baked background of the existing logo so it blends into the
    # login card exactly as before (the logo is intentionally non-transparent).
    cur = os.path.join(ROOT, "webgui/frontend/public/sysible_logo.png")
    bg = Image.open(cur).convert("RGBA").getpixel((4, 4)) if os.path.exists(cur) else (245, 247, 252, 255)

    logo = vertical_lockup(1024, 1088, bg)
    fav = mark(256)
    for rel in ("webgui/frontend/public/sysible_logo.png",
                "webgui/frontend/dist/sysible_logo.png",
                "sysible_logo.png"):
        save(logo, rel)
    for rel in ("webgui/frontend/public/favicon.png",
                "webgui/frontend/dist/favicon.png"):
        save(fav, rel)
    save(horizontal_badge(260, FG_DARK), ".github/sysible-logo-dark.png")
    save(horizontal_badge(260, INK), ".github/sysible-logo-light.png")


if __name__ == "__main__":
    main()
