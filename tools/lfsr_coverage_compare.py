#!/usr/bin/env python3
"""Plot point-pattern coverage of R2 vs LFSR over 1024 / 2048 iterations on a
blank 256x256 plane. The structural reason R2 wins on stipple-MSE is that it's
a low-discrepancy sequence — this visualises that.

Writes out_lfsr/coverage_compare.png (target | R2 | LFSR-seed=1 | LFSR-seed=best).
"""

import sys
from pathlib import Path
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lfsr_brute as L


def stamp(canvas, x, y):
    canvas[y, x] = 255


def render_points(xs, ys, label):
    canvas = np.zeros((256, 256), dtype=np.uint8)
    for x, y in zip(xs, ys):
        if 0 <= x < 256 and 0 <= y < 256:
            stamp(canvas, int(x), int(y))
    return 255 - canvas  # black points on white


def main():
    out = Path("out_lfsr")
    out.mkdir(parents=True, exist_ok=True)
    cycle = L.build_lfsr_cycle()
    seq_hi = (cycle >> 8).astype(np.uint8)

    for n in (1024, 2048):
        # R2
        xs_r2, ys_r2 = L.r2_seq_hi(n)
        r2 = render_points(xs_r2, ys_r2, f"R2 n={n}")

        # LFSR seed = (1, 1)  — arbitrary deterministic
        def lfsr_pair(sx, sy, n):
            xs = np.array([seq_hi[(sx + i) % 65535] for i in range(n)])
            ys = np.array([seq_hi[(sy + i) % 65535] for i in range(n)])
            return xs, ys

        # Generic LFSR seed pair (no image-fit)
        xs_l1, ys_l1 = lfsr_pair(1, 32768, n)
        lfsr1 = render_points(xs_l1, ys_l1, f"LFSR (1,32768) n={n}")

        # Another generic LFSR seed pair
        xs_l2, ys_l2 = lfsr_pair(13579, 24680, n)
        lfsr2 = render_points(xs_l2, ys_l2, f"LFSR (13579,24680) n={n}")

        pad = 8
        H = 256
        comp = np.full((H, 256 * 3 + 2 * pad), 255, dtype=np.uint8)
        comp[:, :256] = r2
        comp[:, 256 + pad:512 + pad] = lfsr1
        comp[:, 512 + 2 * pad:] = lfsr2
        Image.fromarray(comp, "L").save(out / f"coverage_n{n}.png")
        Image.fromarray(comp, "L").resize(
            (comp.shape[1] * 2, comp.shape[0] * 2), Image.NEAREST
        ).save(out / f"coverage_n{n}_2x.png")
        print(f"wrote out_lfsr/coverage_n{n}.png  (R2 | LFSR(1,1) | LFSR(1,32768))")


if __name__ == "__main__":
    main()
