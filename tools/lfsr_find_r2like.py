#!/usr/bin/env python3
"""Search for the LFSR tap+phase that produces the most R2-like (uniform)
coverage. Sanity check on whether any LFSR can mimic R2.

Method:
  1. Enumerate high-byte-only Galois taps (tap = HI << 8, HI in 1..255) -- the
     6502-cheap family. Filter to max-length (period 65535).
  2. For each surviving tap, sweep a small set of phase differences. Render
     2048 unit-radius dots and score by uniformity of blurred coverage
     (variance of a box-blurred ink map; lower = more uniform = more R2-like).
  3. Compare to R2's own score with the same dot count and renderer.
  4. Save a side-by-side comparison of R2 vs best-LFSR.

Result printed: top-5 (tap, x_off, y_off, score) entries plus R2 baseline.
"""
import time
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "out_lfsr_r2like"

XINC = 0xC142
YINC = 0x91DF
N_DOTS = 2048
W = H = 256
BLUR = 8           # box blur kernel side
PHASE_DIFFS = [0, 8192, 16384, 24576, 32768, 40960, 45842, 49152, 57344]
# 45842 is the eye-image diff from brute force, kept as a reference point.


def r2_seq(n):
    xacc, yacc = 0, 0
    xs = np.empty(n, dtype=np.uint8)
    ys = np.empty(n, dtype=np.uint8)
    for i in range(n):
        xacc = (xacc + XINC) & 0xFFFF
        yacc = (yacc + YINC) & 0xFFFF
        xs[i] = xacc >> 8
        ys[i] = yacc >> 8
    return xs, ys


def lfsr_cycle(tap):
    """Build the full hi-byte cycle if the LFSR is max-length, else None."""
    cyc = np.empty(65535, dtype=np.uint8)
    s = 1
    for i in range(65535):
        cyc[i] = s >> 8
        out = s & 1
        s >>= 1
        if out:
            s ^= tap
        if s == 1 and i < 65534:
            return None  # closed early — not max-length
    return cyc if s == 1 else None


def stamp_unit(xs, ys):
    """1-pixel dots only — measures pure placement uniformity without radius noise."""
    ink = np.zeros((H, W), dtype=np.float32)
    np.add.at(ink, (ys, xs), 1.0)
    return ink


def box_blur(ink, k):
    """Mean over kxk windows via separable convolution."""
    from numpy.lib.stride_tricks import sliding_window_view
    s = sliding_window_view(ink, (k, k))
    return s.mean(axis=(-1, -2))


def score(xs, ys):
    """Uniformity: variance of a box-blurred 1-pixel-dot canvas.
    Lower = more uniform = more R2-like."""
    ink = stamp_unit(xs, ys)
    blur = box_blur(ink, BLUR)
    return float(blur.var())


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- R2 baseline ---
    t0 = time.time()
    r2_xs, r2_ys = r2_seq(N_DOTS)
    r2_score = score(r2_xs, r2_ys)
    print(f"R2 (XINC={XINC:04X}, YINC={YINC:04X}): score={r2_score:.6f}")

    # --- enumerate high-byte-only taps ---
    candidates = []
    print("\nbuilding LFSR cycles (max-length only)...")
    for hi in range(1, 256):
        tap = hi << 8
        cyc = lfsr_cycle(tap)
        if cyc is not None:
            candidates.append((tap, cyc))
    print(f"  {len(candidates)} max-length high-byte-only taps "
          f"(out of 255 candidates)")

    # --- score each ---
    print("\nscoring each tap over a sweep of phase diffs...")
    L = 65535
    results = []
    for tap, cyc in candidates:
        best = (None, None, float("inf"))
        # absolute x_off doesn't matter much for uniformity (translation only);
        # phase diff is the real dial. Fix x_off=0 and sweep y_off = -diff.
        for diff in PHASE_DIFFS:
            x_off = 0
            y_off = (-diff) % L
            idx = np.arange(N_DOTS)
            xs = cyc[(x_off + idx) % L]
            ys = cyc[(y_off + idx) % L]
            s = score(xs, ys)
            if s < best[2]:
                best = (x_off, y_off, s)
        results.append((tap, *best))

    # --- top 5 ---
    results.sort(key=lambda r: r[3])
    print(f"\nR2 baseline:              score={r2_score:.6f}")
    print("Top 5 LFSR taps (lower = more R2-like):")
    print(f"  {'tap':>8}  {'x_off':>6}  {'y_off':>6}  {'phase_diff':>10}  {'score':>10}  ratio_to_R2")
    for tap, x_off, y_off, s in results[:5]:
        diff = (x_off - y_off) % L
        ratio = s / r2_score if r2_score > 0 else float("inf")
        print(f"  0x{tap:04X}  {x_off:>6}  {y_off:>6}  {diff:>10}  {s:>10.6f}  {ratio:>6.2f}x")
    print(f"\nWorst LFSR for comparison: tap 0x{results[-1][0]:04X}  "
          f"score={results[-1][3]:.6f}  ({results[-1][3]/r2_score:.2f}x R2)")

    # --- render side-by-side: R2 vs best LFSR ---
    best_tap, best_x, best_y, best_score = results[0]
    cyc = lfsr_cycle(best_tap)
    idx = np.arange(N_DOTS)
    xs = cyc[(best_x + idx) % L]
    ys = cyc[(best_y + idx) % L]
    # Stamp small filled dots (r=2) so the result is visible
    def stamp_r(xs, ys, r=2):
        yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
        m = (xx * xx + yy * yy) <= r * r + r
        ink = np.zeros((H, W), dtype=bool)
        for x, y in zip(xs, ys):
            x0, y0 = int(x) - r, int(y) - r
            sx0 = max(0, -x0); sy0 = max(0, -y0)
            dx0 = max(0, x0);  dy0 = max(0, y0)
            ex = min(W, x0 + m.shape[1]); ey = min(H, y0 + m.shape[0])
            if ex <= dx0 or ey <= dy0:
                continue
            sub = m[sy0:sy0 + (ey - dy0), sx0:sx0 + (ex - dx0)]
            ink[dy0:ey, dx0:ex] |= sub
        return ink

    r2_img = np.where(stamp_r(r2_xs, r2_ys), 0, 255).astype(np.uint8)
    lfsr_img = np.where(stamp_r(xs, ys), 0, 255).astype(np.uint8)
    pad = 8
    comp = np.full((H, 2 * W + pad), 255, dtype=np.uint8)
    comp[:, :W] = r2_img
    comp[:, W + pad:] = lfsr_img
    Image.fromarray(r2_img, "L").save(OUT_DIR / "r2.png")
    Image.fromarray(lfsr_img, "L").save(OUT_DIR / f"best_lfsr_tap_{best_tap:04X}.png")
    Image.fromarray(comp, "L").save(OUT_DIR / f"compare_R2_vs_best_LFSR_{best_tap:04X}.png")
    print(f"\nwrote {OUT_DIR}/compare_R2_vs_best_LFSR_{best_tap:04X}.png")
    print(f"elapsed: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
