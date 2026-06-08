#!/usr/bin/env python3
"""
verify_bbc.py - validate the BBC plotter logic without a 6502 emulator.

Decodes a packed dot stream (the *.bbc.bin produced by stipple.py --bbc) and
plots it into a simulated BBC MODE 4 screen using the EXACT address arithmetic
that bbc/stipple.asm uses (rowbase table, Xs & $FFF8 byte column, mask table,
+8 horizontal byte step across 8x8 char cells). It then compares the result,
pixel for pixel, against stipple.py's own disc renderer.

A MATCH proves the data format + MODE 4 addressing + span tables are correct;
only the literal 6502 transcription and the ZX02 port then remain to be tested
on a real CPU / emulator.

    python3 tools/verify_bbc.py stipple_out/parrot.bbc.bin
"""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stipple as S

W, H = 320, 256
MASKTAB = [0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01]


def disc_spans(r):
    """Same disc formula as spans.asm / stipple.disc_mask."""
    out = []
    for dy in range(-r, r + 1):
        half = math.isqrt(r * r - dy * dy)
        out.append((dy, -half, 2 * half + 1))
    return out


def plot_mode4(dots):
    """Plot dots into a 10KB MODE 4 buffer exactly as bbc/stipple.asm does."""
    scr = bytearray(40 * 8 * 32)                       # 10240 bytes
    rowbase = [(y >> 3) * 320 + (y & 7) for y in range(256)]
    sp = {r: disc_spans(r) for r in range(1, 8)}
    for x, y, r in dots:
        for dy, dx, ln in sp[r]:
            src = rowbase[(y + dy) & 0xFF]
            xs = x + dx
            mask = MASKTAB[xs & 7]
            src += xs & 0xFFF8
            for _ in range(ln):
                scr[src] |= mask
                if mask == 1:
                    mask = 0x80
                    src += 8
                else:
                    mask >>= 1
    return scr


def mode4_to_image(scr):
    img = np.zeros((H, W), np.uint8)
    for y in range(H):
        rb = (y >> 3) * 320 + (y & 7)
        for cx in range(40):
            b = scr[rb + cx * 8]
            for bit in range(8):
                if b & (0x80 >> bit):
                    img[y, cx * 8 + bit] = 1
    return img


def reference_render(dots):
    ink = np.zeros((H, W), bool)
    masks = {r: S.disc_mask(r) for r in range(8)}
    for x, y, r in dots:
        m = masks[r]
        x0, y0 = x - r, y - r
        sx0, sy0 = max(0, -x0), max(0, -y0)
        dx0, dy0 = max(0, x0), max(0, y0)
        ex, ey = min(W, x0 + m.shape[1]), min(H, y0 + m.shape[0])
        ink[dy0:ey, dx0:ex] |= m[sy0:sy0 + (ey - dy0), sx0:sx0 + (ex - dx0)]
    return ink


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: verify_bbc.py <stream.bbc.bin> [out.png]")
    raw = Path(sys.argv[1]).read_bytes()
    dots = S.unpack_bbc(raw)
    scr = plot_mode4(dots)
    img = mode4_to_image(scr)
    ref = reference_render(dots)
    diff = int(np.sum((img > 0) != ref))
    print(f"dots={len(dots)}  black px MODE4={int(img.sum())}  "
          f"reference={int(ref.sum())}  differences={diff}  "
          f"{'MATCH' if diff == 0 else 'MISMATCH'}")
    if len(sys.argv) > 2:
        from PIL import Image
        out = np.where(img > 0, 0, 255).astype(np.uint8)
        Image.fromarray(out, "L").resize((W * 3, H * 3), Image.NEAREST).save(sys.argv[2])
        print("wrote", sys.argv[2])
    sys.exit(0 if diff == 0 else 1)


if __name__ == "__main__":
    main()
