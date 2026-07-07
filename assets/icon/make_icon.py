#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Generate VibeVoice icon candidates: teal squircle + waveform.

Scar sess.9161: the squircle must live INSIDE the canvas with a transparent
margin (~10%), never edge-to-edge, or macOS wraps it in a grey rounded rect.

Usage: python3 make_icon.py --variant 1 --out candidate1.png
"""
from __future__ import annotations

import argparse

from PIL import Image, ImageDraw

S = 1024
M = int(S * 0.10)                    # transparent margin (the scar)
R = int((S - 2 * M) * 0.225)         # macOS squircle-ish corner radius

PALETTES = {1: ((13, 148, 136), (4, 47, 46)),    # teal → deep teal (AI Accelerator family)
            2: ((45, 212, 191), (15, 118, 110)), # bright aqua → teal
            3: ((20, 184, 166), (2, 26, 25))}    # teal → near-black

BARS = {1: [.30, .55, .90, .65, 1.0, .70, .45, .25],
        2: [.20, .45, .75, 1.0, .75, .45, .20],
        3: [.35, .70, 1.0, .55, .85, .40]}


def build(variant: int) -> Image.Image:
    top, bot = PALETTES[variant]
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad = Image.new("RGBA", (S, S))
    for y in range(S):
        t = y / S
        grad.paste(tuple(int(a + (b - a) * t) for a, b in zip(top, bot)) + (255,), (0, y, S, y + 1))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([M, M, S - M, S - M], radius=R, fill=255)
    img.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(img)
    bars = BARS[variant]
    bw = int((S - 2 * M) * 0.055)
    gap = int((S - 2 * M) * 0.045)
    total = len(bars) * bw + (len(bars) - 1) * gap
    x = (S - total) // 2
    max_h = (S - 2 * M) * 0.52
    for h in bars:
        bh = int(max_h * h)
        d.rounded_rectangle([x, (S - bh) // 2, x + bw, (S + bh) // 2], radius=bw // 2, fill=(255, 255, 255, 235))
        x += bw + gap
    return img


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", type=int, default=1, choices=sorted(PALETTES))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    build(a.variant).save(a.out)
    print(f"wrote {a.out}")
