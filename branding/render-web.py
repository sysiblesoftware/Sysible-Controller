#!/usr/bin/env python3
"""Render Sysible Controller web branding from the canonical mark.

Single source: branding/sysible-controller-mark.svg (the dark green-ring tile with
the hub-and-nodes topology, matching the Sysible Linux / SysTerm family).
Regenerates the web app logo, favicon, and README badges in Sora. Run:

    python3 branding/render-web.py
"""
import io
import os

import cairosvg
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARK = os.path.join(ROOT, "branding/sysible-controller-mark.svg")
# Vendored Sora (SIL OFL) — the brand display face. See branding/fonts/OFL.txt.
FONT = os.path.join(ROOT, "branding/fonts/Sora.ttf")
FONT_WEIGHT = "SemiBold"   # display weight for the wordmark

INK = (20, 33, 58)        # "Sysible" — dark ink on the light login card
SUB = (90, 107, 140)      # "CONTROLLER"
TAG = (139, 152, 176)     # tagline
GREEN = (109, 219, 115)   # #6ddb73 — the brand accent (wordmark underline)
FG_DARK = (233, 240, 247)  # wordmark on dark (README dark badge)


def _font(size, weight=FONT_WEIGHT):
    """Load Sora at a given pixel size and variation weight (Sora is a variable
    font; fall back gracefully if a named instance is unavailable)."""
    f = ImageFont.truetype(FONT, size)
    for nm in (weight, "SemiBold", "Regular"):
        try:
            f.set_variation_by_name(nm)
            break
        except Exception:
            continue
    return f


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
        font = _font(size)
        tr = size * tracking_ratio
        w = sum(d.textlength(c, font=font) for c in text) + tr * (len(text) - 1)
        if w <= max_w:
            return font, tr
        size -= 2
    return _font(8), 0


def vertical_lockup(W, H, bg):
    """Mark over 'Sysible' / 'CONTROLLER' / tagline — matches the prior composition."""
    canvas = Image.new("RGBA", (W, H), bg)
    ms = int(H * 0.38)
    m = mark(ms)
    canvas.alpha_composite(m, ((W - ms) // 2, int(H * 0.13)))
    d = ImageDraw.Draw(canvas)
    mx = W * 0.86
    word_top = int(H * 0.55)
    f1, t1 = _fit("Sysible", mx, int(H * 0.135), 0.006)
    w1 = _tracked(d, "Sysible", f1, W / 2, word_top, INK, t1)
    # Thin green accent underline, seated just below the wordmark baseline
    # (a short centred rule — the brand signature, not a strikethrough).
    asc, desc = f1.getmetrics()
    uy = word_top + asc + int(H * 0.012)
    uw = min(w1 * 0.5, W * 0.20)
    uh = max(3, int(H * 0.007))
    d.rectangle([W / 2 - uw / 2, uy, W / 2 + uw / 2, uy + uh], fill=GREEN)
    f2, t2 = _fit("CONTROLLER", mx, int(H * 0.072), 0.028)
    _tracked(d, "CONTROLLER", f2, W / 2, int(H * 0.74), SUB, t2)
    f3, t3 = _fit("IT INFRASTRUCTURE MANAGEMENT SOFTWARE", mx, int(H * 0.028), 0.012)
    _tracked(d, "IT INFRASTRUCTURE MANAGEMENT SOFTWARE", f3, W / 2, int(H * 0.83), TAG, t3)
    return canvas


def horizontal_badge(W, H, fg):
    """README badge: mark + 'SYSIBLE CONTROLLER', transparent."""
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    s = int(H * 0.80)
    m = mark(s)
    d = ImageDraw.Draw(canvas)
    txt = "SYSIBLE CONTROLLER"
    gap = int(H * 0.14)
    tracking = int(H * 0.03)
    # Shrink the wordmark to fit the canvas width (Sora is wider than the prior
    # face; a fixed size overran the badge). Leave a small side margin.
    size = int(H * 0.26)
    while size > 10:
        f = _font(size)
        tw = sum(d.textlength(c, font=f) for c in txt) + tracking * (len(txt) - 1)
        if s + gap + tw <= W * 0.96:
            break
        size -= 2
    x0 = (W - (s + gap + tw)) / 2
    canvas.alpha_composite(m, (int(x0), (H - s) // 2))
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
    save(horizontal_badge(980, 260, FG_DARK), ".github/sysible-logo-dark.png")
    save(horizontal_badge(980, 260, INK), ".github/sysible-logo-light.png")


if __name__ == "__main__":
    main()
