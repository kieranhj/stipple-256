#!/usr/bin/env python3
"""
lfsr_brute.py — find good 16-bit LFSR seeds for the 256-byte stipple intro.

What this does
--------------
Replaces the R2 plastic-phi placement with two independent 16-bit Galois LFSRs
(one per axis), then brute-forces the (x_seed, y_seed) pair against a target
image to minimise blurred-pixel MSE.

LFSR
----
Galois right-shift, tap mask 0xB400:
    state >>= 1; if old bit 0 was 1: state ^= 0xB400
Tap is high-byte-only (0xB4) -> cheapest possible 6502:
    LSR HI : ROR LO : BCC + LDA HI : EOR #$B4 : STA HI
Period = 65535 (max-length, verified). High byte of state = pixel coord 0..255.

Data modes
----------
  cell16x16x2bpp   current scheme. 64 B data. r in {0,2,4,6}.
  cell32x32x1bpp   32x32 binary cells + Floyd-Steinberg dither. 128 B data.
                   draw / skip at fixed radius.

Search
------
Per (image, mode, n_iters): random sample S seed pairs, then coord-descent
refine the top K. Each refine pass exhaustively sweeps one axis (65535 candidates)
with the other fixed. Repeat until no improvement.

Metric
------
Gaussian-blurred MSE (sigma=4 px) of rendered ink vs target tone, both at 256x256.

Usage
-----
  python tools/lfsr_brute.py --image pics/mona_list_square.png --mode 16x2 \
      --iters 1024 --random 200000 --refine 3 --out out_lfsr/
  python tools/lfsr_brute.py --runall              # all images x modes x iters

Output
------
  out/<stem>_<mode>_<iters>_lfsr.png     best rendered ink (256x256)
  out/<stem>_<mode>_<iters>_compare.png  target | r2_baseline | best_lfsr
  out/<stem>_<mode>_<iters>_result.json  metrics, best seeds, config
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

import numba
from numba import njit, prange


# -----------------------------------------------------------------------------
# LFSR
# -----------------------------------------------------------------------------

LFSR_TAP = 0xB400   # high-byte tap 0xB4, max-length verified


def build_lfsr_cycle(tap: int = LFSR_TAP) -> np.ndarray:
    """Return the full 65535-state cycle of the Galois right-shift LFSR,
    starting from state 1. cycle[i] is the i'th state."""
    cycle = np.empty(65535, dtype=np.uint16)
    s = 1
    for i in range(65535):
        cycle[i] = s
        out = s & 1
        s = s >> 1
        if out:
            s ^= tap
    assert s == 1, "LFSR did not return to seed — not max-length"
    return cycle


# -----------------------------------------------------------------------------
# Image / cell preparation
# -----------------------------------------------------------------------------

def load_image_256(path: str) -> np.ndarray:
    """Load image, fit to 256x256 (cover), return darkness float [0,1]."""
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img).convert("L")
    else:
        img = img.convert("L")
    sw, sh = img.size
    s = max(256 / sw, 256 / sh)
    nw, nh = max(1, round(sw * s)), max(1, round(sh * s))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - 256) // 2
    top = (nh - 256) // 2
    img = img.crop((left, top, left + 256, top + 256))
    lum = np.asarray(img, dtype=np.float64) / 255.0
    return 1.0 - lum


def cells_16x16x2bpp(dark: np.ndarray) -> np.ndarray:
    """Downsample darkness to 16x16, quantise to 4 levels 0..3 (LUMINANCE
    direction: 0 = light/skip, 3 = dark/max radius). Flip vertically to match
    BBC y-up convention. Returns uint8 grid (rows top-of-screen first when
    sampled with `cells[ys, xs]`)."""
    img = Image.fromarray(((1.0 - dark) * 255).astype(np.uint8), "L")
    small = img.resize((16, 16), Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float64) / 255.0       # luminance
    lum = np.clip(np.round(arr * 3).astype(np.uint8), 0, 3)
    # invert to darkness ordering (0=skip, 3=biggest dot)
    cells_dark = 3 - lum
    return cells_dark.astype(np.uint8)


def cells_32x32x1bpp(dark: np.ndarray, method: str = "floyd") -> np.ndarray:
    """Downsample darkness to 32x32 then dither to binary. Returns uint8 0/1."""
    img = Image.fromarray(((1.0 - dark) * 255).astype(np.uint8), "L")
    small = img.resize((32, 32), Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float64) / 255.0   # luminance 0..1

    if method == "floyd":
        out = arr.copy()
        for y in range(32):
            for x in range(32):
                old = out[y, x]
                new = 1.0 if old >= 0.5 else 0.0
                out[y, x] = new
                err = old - new
                if x + 1 < 32:
                    out[y, x + 1] += err * 7 / 16
                if y + 1 < 32:
                    if x > 0:
                        out[y + 1, x - 1] += err * 3 / 16
                    out[y + 1, x] += err * 5 / 16
                    if x + 1 < 32:
                        out[y + 1, x + 1] += err * 1 / 16
        binary_lum = (out >= 0.5).astype(np.uint8)
    elif method == "bayer":
        # 4x4 Bayer matrix, normalised to threshold values in [0,1)
        b = np.array([
            [ 0,  8,  2, 10],
            [12,  4, 14,  6],
            [ 3, 11,  1,  9],
            [15,  7, 13,  5],
        ], dtype=np.float64) / 16.0
        thr = np.tile(b, (8, 8))
        binary_lum = (arr > thr).astype(np.uint8)
    else:
        raise ValueError(method)

    # Dark = 1 means "ink/dot here", light = 0 = skip
    return (1 - binary_lum).astype(np.uint8)


# -----------------------------------------------------------------------------
# Renderer (numba)
# -----------------------------------------------------------------------------

# pre-flatten disc masks at radii 0..7 into a single array indexed by start/len.
def _build_disc_table(max_r: int = 7):
    starts = np.zeros(max_r + 1, dtype=np.int32)
    lens = np.zeros(max_r + 1, dtype=np.int32)
    dxs = []
    dys = []
    for r in range(max_r + 1):
        starts[r] = len(dxs)
        if r == 0:
            dxs.append(0); dys.append(0)
        else:
            # BBC MOS Bresenham-like: x^2 + y^2 <= r^2 + r (matches emulator)
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if dx * dx + dy * dy <= r * r + r:
                        dxs.append(dx); dys.append(dy)
        lens[r] = len(dxs) - starts[r]
    return (np.array(dxs, dtype=np.int32),
            np.array(dys, dtype=np.int32),
            starts, lens)

DISC_DX, DISC_DY, DISC_START, DISC_LEN = _build_disc_table(7)


@njit(cache=True, fastmath=True)
def _render_16x2(seq_hi, x_off, y_off, n, cells, dxs, dys, starts, lens, canvas):
    """Render mode 16x16x2bpp. cells is (16,16) uint8 darkness 0..3.
    radius = cell * 2 -> {0,2,4,6}. Skip if r==0."""
    canvas[:] = 0
    L = 65535
    for i in range(n):
        x = seq_hi[(x_off + i) % L]
        y = seq_hi[(y_off + i) % L]
        # BBC y is bottom-up; mirror so preview's image is right way up
        y_pix = 255 - y
        cx = x >> 4
        cy = y_pix >> 4
        cell = cells[cy, cx]
        r = cell * 2
        if r == 0:
            continue
        s = starts[r]; nm = lens[r]
        for k in range(nm):
            px = x + dxs[s + k]
            py = y_pix + dys[s + k]
            if 0 <= px < 256 and 0 <= py < 256:
                canvas[py, px] = 1


@njit(cache=True, fastmath=True)
def _render_32x1(seq_hi, x_off, y_off, n, cells, dxs, dys, starts, lens,
                 canvas, fixed_r):
    """Render mode 32x32x1bpp+dither. cells is (32,32) uint8 binary draw/skip."""
    canvas[:] = 0
    L = 65535
    s = starts[fixed_r]; nm = lens[fixed_r]
    for i in range(n):
        x = seq_hi[(x_off + i) % L]
        y = seq_hi[(y_off + i) % L]
        y_pix = 255 - y
        cx = x >> 3
        cy = y_pix >> 3
        if cells[cy, cx] == 0:
            continue
        for k in range(nm):
            px = x + dxs[s + k]
            py = y_pix + dys[s + k]
            if 0 <= px < 256 and 0 <= py < 256:
                canvas[py, px] = 1


@njit(cache=True, parallel=False, fastmath=True)
def _blur_box_64(canvas, out64):
    """Cheap perceptual low-pass: average over 4x4 blocks -> 64x64.
    Faster than per-candidate gaussian for the inner search."""
    for by in range(64):
        for bx in range(64):
            s = 0
            for dy in range(4):
                for dx in range(4):
                    s += canvas[by * 4 + dy, bx * 4 + dx]
            out64[by, bx] = s / 16.0


@njit(cache=True, fastmath=True)
def _mse(a64, target64):
    s = 0.0
    for y in range(64):
        for x in range(64):
            d = a64[y, x] - target64[y, x]
            s += d * d
    return s / (64.0 * 64.0)


@njit(cache=True, fastmath=True)
def score_16x2(seq_hi, x_off, y_off, n, cells, dxs, dys, starts, lens,
               canvas, blur64, target64):
    _render_16x2(seq_hi, x_off, y_off, n, cells, dxs, dys, starts, lens, canvas)
    _blur_box_64(canvas, blur64)
    return _mse(blur64, target64)


@njit(cache=True, fastmath=True)
def score_32x1(seq_hi, x_off, y_off, n, cells, dxs, dys, starts, lens,
               canvas, blur64, target64, fixed_r):
    _render_32x1(seq_hi, x_off, y_off, n, cells, dxs, dys, starts, lens,
                 canvas, fixed_r)
    _blur_box_64(canvas, blur64)
    return _mse(blur64, target64)


@njit(cache=True, fastmath=True)
def sweep_axis_16x2(seq_hi, axis, fixed, fixed_other, n, cells,
                    dxs, dys, starts, lens, canvas, blur64, target64):
    """Exhaustive sweep of one axis 0..65534 with the other fixed.
    axis=0 sweeps X (fixed_other is Y), axis=1 sweeps Y (fixed_other is X).
    Returns (best_val, best_offset)."""
    best = 1e30
    best_off = 0
    for v in range(65535):
        if axis == 0:
            m = score_16x2(seq_hi, v, fixed_other, n, cells,
                           dxs, dys, starts, lens, canvas, blur64, target64)
        else:
            m = score_16x2(seq_hi, fixed_other, v, n, cells,
                           dxs, dys, starts, lens, canvas, blur64, target64)
        if m < best:
            best = m
            best_off = v
    return best, best_off


@njit(cache=True, fastmath=True)
def sweep_axis_32x1(seq_hi, axis, fixed_other, n, cells,
                    dxs, dys, starts, lens, canvas, blur64, target64, fixed_r):
    best = 1e30
    best_off = 0
    for v in range(65535):
        if axis == 0:
            m = score_32x1(seq_hi, v, fixed_other, n, cells,
                           dxs, dys, starts, lens, canvas, blur64, target64,
                           fixed_r)
        else:
            m = score_32x1(seq_hi, fixed_other, v, n, cells,
                           dxs, dys, starts, lens, canvas, blur64, target64,
                           fixed_r)
        if m < best:
            best = m
            best_off = v
    return best, best_off


@njit(cache=True, fastmath=True)
def random_search_16x2(seq_hi, rand_x, rand_y, n_iters, cells,
                       dxs, dys, starts, lens, canvas, blur64, target64,
                       topk_mse, topk_x, topk_y):
    """Random sample loop in numba. Updates a pre-sized top-K array (sorted asc).
    Returns (best_mse, best_x, best_y)."""
    K = topk_mse.shape[0]
    best = 1e30; bx = 0; by = 0
    for i in range(rand_x.shape[0]):
        xo = rand_x[i]; yo = rand_y[i]
        m = score_16x2(seq_hi, xo, yo, n_iters, cells,
                       dxs, dys, starts, lens, canvas, blur64, target64)
        if m < best:
            best = m; bx = xo; by = yo
        # insertion-sort into topk (small K, cheap)
        if m < topk_mse[K - 1]:
            j = K - 1
            while j > 0 and topk_mse[j - 1] > m:
                topk_mse[j] = topk_mse[j - 1]
                topk_x[j] = topk_x[j - 1]
                topk_y[j] = topk_y[j - 1]
                j -= 1
            topk_mse[j] = m
            topk_x[j] = xo
            topk_y[j] = yo
    return best, bx, by


@njit(cache=True, fastmath=True)
def random_search_32x1(seq_hi, rand_x, rand_y, n_iters, cells,
                       dxs, dys, starts, lens, canvas, blur64, target64,
                       fixed_r, topk_mse, topk_x, topk_y):
    K = topk_mse.shape[0]
    best = 1e30; bx = 0; by = 0
    for i in range(rand_x.shape[0]):
        xo = rand_x[i]; yo = rand_y[i]
        m = score_32x1(seq_hi, xo, yo, n_iters, cells,
                       dxs, dys, starts, lens, canvas, blur64, target64,
                       fixed_r)
        if m < best:
            best = m; bx = xo; by = yo
        if m < topk_mse[K - 1]:
            j = K - 1
            while j > 0 and topk_mse[j - 1] > m:
                topk_mse[j] = topk_mse[j - 1]
                topk_x[j] = topk_x[j - 1]
                topk_y[j] = topk_y[j - 1]
                j -= 1
            topk_mse[j] = m
            topk_x[j] = xo
            topk_y[j] = yo
    return best, bx, by


# -----------------------------------------------------------------------------
# Target preparation
# -----------------------------------------------------------------------------

def build_target64(dark: np.ndarray) -> np.ndarray:
    """Low-pass the target to 64x64 box averages — matches the renderer metric."""
    out = np.empty((64, 64), dtype=np.float64)
    for by in range(64):
        for bx in range(64):
            out[by, bx] = dark[by * 4:by * 4 + 4, bx * 4:bx * 4 + 4].mean()
    return out


# -----------------------------------------------------------------------------
# Search
# -----------------------------------------------------------------------------

@dataclass
class SearchResult:
    image: str
    mode: str
    n_iters: int
    fixed_r: int           # only meaningful for 32x1
    best_x: int
    best_y: int
    best_mse: float
    r2_mse: float
    n_random: int
    n_refine_passes: int
    refine_improvements: int
    elapsed_s: float


def r2_seq_hi(n: int):
    """R2 placement (current asm constants), returning hi bytes only."""
    XINC = 0xC142
    YINC = 0x91DF
    xacc = 0; yacc = 0
    xs = np.empty(n, dtype=np.uint8)
    ys = np.empty(n, dtype=np.uint8)
    for i in range(n):
        xacc = (xacc + XINC) & 0xFFFF
        yacc = (yacc + YINC) & 0xFFFF
        xs[i] = xacc >> 8
        ys[i] = yacc >> 8
    return xs, ys


def search_one(image_path: str, mode: str, n_iters: int,
               n_random: int, n_refine_top: int, fixed_r: int,
               seq_hi: np.ndarray, log=print, rng=None) -> tuple[SearchResult, np.ndarray]:
    """Run random search + coord-descent refine. Returns (result, best_canvas)."""
    if rng is None:
        rng = np.random.default_rng(42)
    t0 = time.time()

    dark = load_image_256(image_path)
    target64 = build_target64(dark)

    if mode == "16x2":
        cells = cells_16x16x2bpp(dark)
    elif mode == "32x1":
        cells = cells_32x32x1bpp(dark, method="floyd")
    else:
        raise ValueError(mode)

    canvas = np.zeros((256, 256), dtype=np.uint8)
    blur64 = np.zeros((64, 64), dtype=np.float64)

    # --- random phase (numba inner loop) ---
    log(f"  random search: {n_random} candidates")
    K = max(1, n_refine_top)
    topk_mse = np.full(K, 1e30, dtype=np.float64)
    topk_x = np.zeros(K, dtype=np.int64)
    topk_y = np.zeros(K, dtype=np.int64)

    # do random in chunks so we can log progress
    chunk = max(1, n_random // 10)
    best_mse = float("inf"); best_x = 0; best_y = 0
    t_rand = time.time()
    done = 0
    while done < n_random:
        c = min(chunk, n_random - done)
        rand_x = rng.integers(0, 65535, size=c, dtype=np.int64)
        rand_y = rng.integers(0, 65535, size=c, dtype=np.int64)
        if mode == "16x2":
            bm, bx, by = random_search_16x2(
                seq_hi, rand_x, rand_y, n_iters, cells,
                DISC_DX, DISC_DY, DISC_START, DISC_LEN,
                canvas, blur64, target64,
                topk_mse, topk_x, topk_y,
            )
        else:
            bm, bx, by = random_search_32x1(
                seq_hi, rand_x, rand_y, n_iters, cells,
                DISC_DX, DISC_DY, DISC_START, DISC_LEN,
                canvas, blur64, target64, fixed_r,
                topk_mse, topk_x, topk_y,
            )
        if bm < best_mse:
            best_mse = bm; best_x = int(bx); best_y = int(by)
        done += c
        log(f"    {done:>7}/{n_random}  best MSE {best_mse:.6f}  "
            f"({done/(time.time()-t_rand+1e-9):.0f}/s)")
    log(f"  random done: best MSE {best_mse:.6f}  elapsed {time.time()-t_rand:.1f}s")

    # --- coord-descent refinement on top-K ---
    refine_improvements = 0
    refine_passes = 0
    K_refine = min(K, int(np.sum(topk_mse < 1e30)))
    for ki in range(K_refine):
        m0 = float(topk_mse[ki]); x0 = int(topk_x[ki]); y0 = int(topk_y[ki])
        log(f"  refining top-{ki+1} (start MSE {m0:.6f}, x={x0}, y={y0})")
        cur_mse = m0; cur_x = x0; cur_y = y0
        while True:
            refine_passes += 1
            t_pass = time.time()
            # sweep X for fixed Y
            if mode == "16x2":
                bm, bx = sweep_axis_16x2(
                    seq_hi, 0, 0, cur_y, n_iters, cells,
                    DISC_DX, DISC_DY, DISC_START, DISC_LEN,
                    canvas, blur64, target64,
                )
            else:
                bm, bx = sweep_axis_32x1(
                    seq_hi, 0, cur_y, n_iters, cells,
                    DISC_DX, DISC_DY, DISC_START, DISC_LEN,
                    canvas, blur64, target64, fixed_r,
                )
            improved = False
            if bm < cur_mse - 1e-12:
                cur_mse = float(bm); cur_x = int(bx); improved = True
            # sweep Y for fixed X
            if mode == "16x2":
                bm, by = sweep_axis_16x2(
                    seq_hi, 1, 0, cur_x, n_iters, cells,
                    DISC_DX, DISC_DY, DISC_START, DISC_LEN,
                    canvas, blur64, target64,
                )
            else:
                bm, by = sweep_axis_32x1(
                    seq_hi, 1, cur_x, n_iters, cells,
                    DISC_DX, DISC_DY, DISC_START, DISC_LEN,
                    canvas, blur64, target64, fixed_r,
                )
            if bm < cur_mse - 1e-12:
                cur_mse = float(bm); cur_y = int(by); improved = True
            log(f"    pass {refine_passes}: MSE {cur_mse:.6f}  "
                f"(x={cur_x}, y={cur_y})  {time.time()-t_pass:.1f}s")
            if not improved:
                break
            refine_improvements += 1
        if cur_mse < best_mse:
            best_mse = cur_mse; best_x = cur_x; best_y = cur_y
            log(f"  new global best: MSE {best_mse:.6f}  "
                f"(x={best_x}, y={best_y})")

    # --- R2 baseline ---
    xs_r2, ys_r2 = r2_seq_hi(n_iters)
    r2_canvas = np.zeros((256, 256), dtype=np.uint8)
    r2_blur = np.zeros((64, 64), dtype=np.float64)
    # custom render for R2 (it doesn't use LFSR seq)
    if mode == "16x2":
        for i in range(n_iters):
            x = int(xs_r2[i]); y = int(ys_r2[i])
            y_pix = 255 - y
            cell = cells[y_pix >> 4, x >> 4]
            r = cell * 2
            if r == 0:
                continue
            s = DISC_START[r]; nm = DISC_LEN[r]
            for k in range(nm):
                px = x + int(DISC_DX[s + k])
                py = y_pix + int(DISC_DY[s + k])
                if 0 <= px < 256 and 0 <= py < 256:
                    r2_canvas[py, px] = 1
    else:
        s = DISC_START[fixed_r]; nm = DISC_LEN[fixed_r]
        for i in range(n_iters):
            x = int(xs_r2[i]); y = int(ys_r2[i])
            y_pix = 255 - y
            if cells[y_pix >> 3, x >> 3] == 0:
                continue
            for k in range(nm):
                px = x + int(DISC_DX[s + k])
                py = y_pix + int(DISC_DY[s + k])
                if 0 <= px < 256 and 0 <= py < 256:
                    r2_canvas[py, px] = 1
    _blur_box_64(r2_canvas, r2_blur)
    r2_mse = _mse(r2_blur, target64)
    log(f"  R2 baseline MSE: {r2_mse:.6f}")

    # render best one more time for output
    if mode == "16x2":
        _render_16x2(seq_hi, best_x, best_y, n_iters, cells,
                     DISC_DX, DISC_DY, DISC_START, DISC_LEN, canvas)
    else:
        _render_32x1(seq_hi, best_x, best_y, n_iters, cells,
                     DISC_DX, DISC_DY, DISC_START, DISC_LEN, canvas, fixed_r)

    elapsed = time.time() - t0
    log(f"  elapsed {elapsed:.1f}s  best MSE {best_mse:.6f} "
        f"vs R2 {r2_mse:.6f}  ({100*(r2_mse-best_mse)/max(r2_mse,1e-9):+.1f}%)")

    res = SearchResult(
        image=image_path, mode=mode, n_iters=n_iters, fixed_r=fixed_r,
        best_x=best_x, best_y=best_y, best_mse=best_mse, r2_mse=r2_mse,
        n_random=n_random, n_refine_passes=refine_passes,
        refine_improvements=refine_improvements, elapsed_s=elapsed,
    )
    # also return r2 canvas for compare image
    return res, canvas.copy(), r2_canvas.copy(), cells


# -----------------------------------------------------------------------------
# Output
# -----------------------------------------------------------------------------

def save_outputs(res: SearchResult, lfsr_canvas: np.ndarray,
                 r2_canvas: np.ndarray, cells: np.ndarray,
                 dark: np.ndarray, out_dir: Path, stem: str):
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"{stem}_{res.mode}_n{res.n_iters}"
    if res.mode == "32x1":
        suffix += f"_r{res.fixed_r}"

    # invert: 1=ink -> black on white
    lfsr_img = np.where(lfsr_canvas > 0, 0, 255).astype(np.uint8)
    r2_img = np.where(r2_canvas > 0, 0, 255).astype(np.uint8)
    tgt_img = ((1.0 - dark) * 255).astype(np.uint8)

    Image.fromarray(lfsr_img, "L").save(out_dir / f"{suffix}_lfsr.png")
    Image.fromarray(r2_img, "L").save(out_dir / f"{suffix}_r2.png")

    # 3-up compare: target | r2 | lfsr
    pad = 8
    H, W = 256, 256
    comp = np.full((H, W * 3 + 2 * pad), 255, dtype=np.uint8)
    comp[:, :W] = tgt_img
    comp[:, W + pad:2 * W + pad] = r2_img
    comp[:, 2 * W + 2 * pad:] = lfsr_img
    Image.fromarray(comp, "L").save(out_dir / f"{suffix}_compare.png")

    # also 2x upscaled compare
    Image.fromarray(comp, "L").resize(
        (comp.shape[1] * 2, comp.shape[0] * 2), Image.NEAREST
    ).save(out_dir / f"{suffix}_compare_2x.png")

    with open(out_dir / f"{suffix}_result.json", "w") as f:
        json.dump(asdict(res), f, indent=2)


# -----------------------------------------------------------------------------
# Driver
# -----------------------------------------------------------------------------

def parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", default=None)
    ap.add_argument("--mode", choices=["16x2", "32x1"], default="16x2")
    ap.add_argument("--iters", type=int, default=1024)
    ap.add_argument("--random", type=int, default=200_000)
    ap.add_argument("--refine-top", type=int, default=3,
                    help="number of top candidates from random phase to refine")
    ap.add_argument("--fixed-r", type=int, default=3,
                    help="32x1 mode: fixed disc radius")
    ap.add_argument("--out", default="out_lfsr")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--runall", action="store_true",
                    help="run all images x modes x iters; ignore --image/--mode/...")
    return ap.parse_args()


def main():
    args = parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"building LFSR cycle (tap 0x{LFSR_TAP:04X})...")
    cycle = build_lfsr_cycle()
    seq_hi = (cycle >> 8).astype(np.uint8)
    assert seq_hi.shape == (65535,)

    rng = np.random.default_rng(args.seed)

    if args.runall:
        images = [
            "pics/mona_list_square.png",
            "pics/face.png",
            "pics/eye.png",
        ]
        configs = [
            # mode, n_iters, fixed_r
            ("16x2", 1024, 0),
            ("16x2", 2048, 0),
            ("32x1", 1024, 3),
            ("32x1", 1024, 4),
            ("32x1", 2048, 3),
            ("32x1", 2048, 4),
        ]
        all_results = []
        for img in images:
            stem = Path(img).stem
            for mode, n_iters, fixed_r in configs:
                print(f"\n=== {stem} | mode {mode} | iters {n_iters}"
                      f"{' r='+str(fixed_r) if mode=='32x1' else ''} ===")
                res, lfsr_c, r2_c, cells = search_one(
                    img, mode, n_iters, args.random, args.refine_top,
                    fixed_r, seq_hi, rng=rng,
                )
                dark = load_image_256(img)
                save_outputs(res, lfsr_c, r2_c, cells, dark, out_dir, stem)
                all_results.append(asdict(res))
        with open(out_dir / "all_results.json", "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"\nall done. results -> {out_dir}")
    else:
        if not args.image:
            sys.exit("--image required (or use --runall)")
        stem = Path(args.image).stem
        print(f"=== {stem} | mode {args.mode} | iters {args.iters} ===")
        res, lfsr_c, r2_c, cells = search_one(
            args.image, args.mode, args.iters,
            args.random, args.refine_top, args.fixed_r,
            seq_hi, rng=rng,
        )
        dark = load_image_256(args.image)
        save_outputs(res, lfsr_c, r2_c, cells, dark, out_dir, stem)
        print(f"\ndone. results -> {out_dir}")


if __name__ == "__main__":
    main()
