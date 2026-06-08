#!/usr/bin/env python3
"""
stipple.py - convert a photo into variable-size black-dot stipple data.

Phase 1 tool for the 4KB stipple-graphics experiment: it turns a source image
into an irregular set of black dots on a white background, where each dot's
RADIUS encodes local image darkness (hybrid size-led stippling). It renders
previews so the look can be tuned by eye, and reports how well the resulting
(x, y, radius) data compresses - i.e. whether it will fit a 4KB budget.

Pipeline
--------
1. Load image, fit to the target canvas (default 320x256, 5:4), grayscale.
2. Tone prep: gamma + black/white point clamp. darkness = 1 - luminance.
3. Seed N points by darkness-weighted rejection sampling.
4. Lloyd relaxation (weighted centroidal Voronoi) over the darkness map.
   The placement weight is darkness**density_exponent:
     density_exponent = 0  -> even spacing, tone carried purely by dot SIZE
     larger             -> dots cluster in shadows (toward classic stippling)
5. Radius per dot from the tone MASS of its Voronoi cell (ink conservation):
   r = sqrt(sum(darkness over cell) / pi) * radius_scale, then quantized to
   one of `levels` integer radii (default 8 -> 3 bits). Radius-0 dots drop out.
6. Render previews (1x, 3x, side-by-side) and export (x,y,r) data.
7. Report dot count, radius histogram, tonal error, and compressed data size.

This is a HOST-side tool. The retro plot code is a separate later artifact.
"""

import argparse
import io
import sys
import zlib
import lzma
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree
from scipy.ndimage import gaussian_filter
from PIL import Image


# --------------------------------------------------------------------------
# image / tone prep
# --------------------------------------------------------------------------

def load_darkness(path, W, H, gamma, black_point, white_point):
    """Load an image, fit it to WxH (centre crop), return darkness in [0,1]."""
    img = Image.open(path).convert("L")
    img = _fit_crop(img, W, H)
    lum = np.asarray(img, dtype=np.float64) / 255.0

    # clamp the tonal window, then gamma. white_point maps to white (0 dark),
    # black_point maps to black (1 dark).
    lo, hi = black_point, white_point
    lum = np.clip((lum - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    dark = 1.0 - lum
    dark = np.power(dark, gamma)
    return dark


def _fit_crop(img, W, H):
    """Scale to cover WxH preserving aspect, then centre-crop to WxH."""
    sw, sh = img.size
    scale = max(W / sw, H / sh)
    nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - W) // 2
    top = (nh - H) // 2
    return img.crop((left, top, left + W, top + H))


def make_test_image(path, W, H):
    """Write a synthetic test image so the tool can be exercised without an asset."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
    # horizontal luminance gradient
    grad = xx / (W - 1)
    # a couple of soft dark blobs + a bright disc, to exercise tone + edges
    def blob(cx, cy, rad):
        return np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * rad * rad)))
    img = grad
    img -= 0.7 * blob(W * 0.32, H * 0.45, W * 0.13)
    img -= 0.5 * blob(W * 0.68, H * 0.62, W * 0.10)
    img += 0.6 * blob(W * 0.78, H * 0.30, W * 0.07)
    img = np.clip(img, 0, 1)
    Image.fromarray((img * 255).astype(np.uint8), "L").save(path)


# --------------------------------------------------------------------------
# stippling
# --------------------------------------------------------------------------

def seed_points(dark, n, weight_exp, rng):
    """Rejection-sample n initial points with probability ~ darkness**weight_exp."""
    H, W = dark.shape
    w = np.power(np.clip(dark, 0, 1), weight_exp).ravel()
    s = w.sum()
    if s <= 0:                       # fully white image -> uniform
        w = np.ones_like(w)
        s = w.sum()
    p = w / s
    idx = rng.choice(W * H, size=n, replace=True, p=p)
    ys, xs = np.divmod(idx, W)
    # jitter inside the pixel so coincident seeds separate during relaxation
    pts = np.stack([xs + rng.random(n), ys + rng.random(n)], axis=1)
    return pts


def lloyd(pts, coords, weight, N, W, H, iters, rng, log):
    """Weighted Lloyd relaxation. Returns relaxed points and final cell index."""
    wflat = weight
    wx = coords[:, 0]
    wy = coords[:, 1]
    idx = None
    for it in range(iters):
        tree = cKDTree(pts)
        _, idx = tree.query(coords, workers=-1)
        wsum = np.bincount(idx, weights=wflat, minlength=N)
        cx = np.bincount(idx, weights=wflat * wx, minlength=N)
        cy = np.bincount(idx, weights=wflat * wy, minlength=N)
        nz = wsum > 0
        moved = np.linalg.norm(
            np.stack([cx[nz] / wsum[nz], cy[nz] / wsum[nz]], 1) - pts[nz], axis=1
        )
        pts = pts.copy()
        pts[nz, 0] = cx[nz] / wsum[nz]
        pts[nz, 1] = cy[nz] / wsum[nz]
        # cells that captured no weight (all-white): scatter them back into the
        # image so they get a chance to find tone next pass.
        dead = ~nz
        if dead.any():
            pts[dead, 0] = rng.random(dead.sum()) * W
            pts[dead, 1] = rng.random(dead.sum()) * H
        if log:
            print(f"  lloyd iter {it+1:2d}/{iters}  mean move {moved.mean():.3f}px",
                  file=sys.stderr)
    # final assignment for radius computation
    tree = cKDTree(pts)
    _, idx = tree.query(coords, workers=-1)
    return pts, idx


def compute_radii(dark, idx, N, levels, radius_scale):
    """Radius per dot from cell tone-mass (ink conservation), quantized."""
    dflat = dark.ravel()
    tonemass = np.bincount(idx, weights=dflat, minlength=N)
    r = np.sqrt(tonemass / np.pi) * radius_scale
    # quantize to integer radii 0..(levels-1); 0 means "dropped" (too light)
    rq = np.clip(np.round(r).astype(int), 0, levels - 1)
    return rq


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def disc_mask(r):
    """Boolean disc of integer radius r. r=0 -> single pixel."""
    if r <= 0:
        return np.ones((1, 1), dtype=bool)
    yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= r * r


def render(points, radii, W, H, levels):
    """Stamp filled discs (OR) onto a white canvas. Returns bool ink map."""
    ink = np.zeros((H, W), dtype=bool)
    masks = {r: disc_mask(r) for r in range(levels)}
    for (x, y), r in zip(points, radii):
        if r <= 0:
            continue
        m = masks[r]
        ix, iy = int(round(x)), int(round(y))
        x0, y0 = ix - r, iy - r
        # clip to canvas
        sx0 = max(0, -x0); sy0 = max(0, -y0)
        dx0 = max(0, x0);  dy0 = max(0, y0)
        ex = min(W, x0 + m.shape[1]); ey = min(H, y0 + m.shape[0])
        if ex <= dx0 or ey <= dy0:
            continue
        sub = m[sy0:sy0 + (ey - dy0), sx0:sx0 + (ex - dx0)]
        ink[dy0:ey, dx0:ex] |= sub
    return ink


def save_previews(dark, ink, outdir, stem, scale3=3):
    """Write 1x stipple, 3x stipple, and a source|stipple comparison."""
    H, W = ink.shape
    stip = np.where(ink, 0, 255).astype(np.uint8)        # black dots on white
    src = ((1.0 - dark) * 255).astype(np.uint8)

    Image.fromarray(stip, "L").save(outdir / f"{stem}_stipple.png")
    Image.fromarray(stip, "L").resize(
        (W * scale3, H * scale3), Image.NEAREST).save(outdir / f"{stem}_stipple_3x.png")

    pad = 8
    comp = np.full((H, W * 2 + pad), 255, np.uint8)
    comp[:, :W] = src
    comp[:, W + pad:] = stip
    Image.fromarray(comp, "L").resize(
        ((W * 2 + pad) * 2, H * 2), Image.NEAREST).save(outdir / f"{stem}_compare.png")


# --------------------------------------------------------------------------
# data export + compression report
# --------------------------------------------------------------------------

def export_and_report(points, radii, W, H, levels, outdir, stem):
    """Write a (x,y,r) list and estimate compressed data size several ways."""
    keep = radii > 0
    xs = np.clip(np.round(points[keep, 0]).astype(int), 0, W - 1)
    ys = np.clip(np.round(points[keep, 1]).astype(int), 0, H - 1)
    rs = radii[keep].astype(int)
    n = len(rs)

    # human-readable list, sorted scanline for readability
    order = np.lexsort((xs, ys))
    with open(outdir / f"{stem}_dots.csv", "w") as f:
        f.write("x,y,r\n")
        for i in order:
            f.write(f"{xs[i]},{ys[i]},{rs[i]}\n")

    # --- packing estimate A: naive 3 bytes/dot (x, y, r) ------------------
    naive = bytearray()
    for i in range(n):
        naive += bytes((xs[i] & 0xFF, ys[i] & 0xFF, rs[i] & 0xFF))
    # --- packing estimate B: bucket by radius, scanline-sort, delta x ------
    packed = bytearray()
    for r in range(1, levels):
        sel = np.where(rs == r)[0]
        if len(sel) == 0:
            packed += bytes((0, 0))
            continue
        bx, by = xs[sel], ys[sel]
        o = np.lexsort((bx, by))           # sort by y then x
        bx, by = bx[o], by[o]
        packed += bytes((len(sel) & 0xFF, (len(sel) >> 8) & 0xFF))
        py = -1
        for j in range(len(sel)):
            if by[j] != py:                # new scanline: emit absolute y marker
                packed += bytes((255, by[j] & 0xFF))
                py = by[j]
                prevx = 0
            dx = bx[j] - prevx
            packed += bytes((min(dx, 254) & 0xFF,))
            prevx = bx[j]

    def comp(b):
        return len(zlib.compress(bytes(b), 9)), len(lzma.compress(bytes(b)))

    naive_z, naive_x = comp(naive)
    pack_z, pack_x = comp(packed)
    bits = n * (np.ceil(np.log2(W)) + np.ceil(np.log2(H)) + np.log2(levels))

    print("\n=== data / compression report ===")
    print(f"dots kept                : {n}")
    print(f"theoretical raw          : {bits/8:8.0f} B  "
          f"({(np.ceil(np.log2(W))+np.ceil(np.log2(H))+np.log2(levels)):.1f} bits/dot)")
    print(f"naive 3B/dot raw         : {len(naive):8d} B")
    print(f"  zlib / lzma            : {naive_z:8d} B / {naive_x:d} B")
    print(f"bucketed+delta raw       : {len(packed):8d} B")
    print(f"  zlib / lzma            : {pack_z:8d} B / {pack_x:d} B   <-- best estimate")
    print(f"4KB budget               : {4096} B")
    return n


def tonal_error(dark, ink, sigma):
    """Mean absolute difference between blurred target tone and dot coverage."""
    a = gaussian_filter(dark, sigma)
    b = gaussian_filter(ink.astype(np.float64), sigma)
    return float(np.mean(np.abs(a - b)))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", nargs="?", help="source image (omit with --make-test-image)")
    ap.add_argument("-W", "--width", type=int, default=320)
    ap.add_argument("-H", "--height", type=int, default=256)
    ap.add_argument("-n", "--dots", type=int, default=2200, help="target dot count")
    ap.add_argument("-l", "--levels", type=int, default=8, help="radius levels (3 bits=8)")
    ap.add_argument("--density-exponent", type=float, default=0.0,
                    help="placement weight exp: 0=even spacing(size-led, default), higher=cluster in shadows (toward classic stippling)")
    ap.add_argument("--radius-scale", type=float, default=1.0, help="global dot-size multiplier")
    ap.add_argument("--iters", type=int, default=30, help="Lloyd relaxation iterations")
    ap.add_argument("--gamma", type=float, default=1.0, help="darkness gamma (>1 lightens mids)")
    ap.add_argument("--black-point", type=float, default=0.0)
    ap.add_argument("--white-point", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-o", "--outdir", default="stipple_out")
    ap.add_argument("--stem", default=None, help="output filename stem")
    ap.add_argument("--make-test-image", metavar="PATH", default=None,
                    help="write a synthetic test image to PATH and use it")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    W, H = args.width, args.height
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    if args.make_test_image:
        make_test_image(args.make_test_image, W, H)
        args.image = args.make_test_image
    if not args.image:
        ap.error("provide an image, or use --make-test-image PATH")

    stem = args.stem or Path(args.image).stem
    rng = np.random.default_rng(args.seed)

    dark = load_darkness(args.image, W, H, args.gamma, args.black_point, args.white_point)

    # precompute the pixel-centre grid and placement weights once
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
    coords = np.stack([xx.ravel() + 0.5, yy.ravel() + 0.5], axis=1)
    weight = np.power(np.clip(dark, 0, 1), args.density_exponent).ravel()

    if not args.quiet:
        print(f"image {W}x{H}  mean darkness {dark.mean():.3f}  "
              f"dots {args.dots}  density_exp {args.density_exponent}  "
              f"radius_scale {args.radius_scale}", file=sys.stderr)

    pts = seed_points(dark, args.dots, args.density_exponent, rng)
    pts, idx = lloyd(pts, coords, weight, args.dots, W, H, args.iters, rng,
                     log=not args.quiet)
    radii = compute_radii(dark, idx, args.dots, args.levels, args.radius_scale)

    ink = render(pts, radii, W, H, args.levels)
    save_previews(dark, ink, outdir, stem)
    n = export_and_report(pts, radii, W, H, args.levels, outdir, stem)

    # radius histogram + fidelity
    hist = np.bincount(radii, minlength=args.levels)
    print("\n=== radius histogram (level: count) ===")
    for r in range(args.levels):
        bar = "#" * int(40 * hist[r] / max(hist.max(), 1))
        tag = " (dropped)" if r == 0 else ""
        print(f"  r={r}: {hist[r]:5d} {bar}{tag}")
    print(f"\ncoverage (black px)      : {ink.sum()} / {W*H} "
          f"({100*ink.sum()/(W*H):.1f}%)  target tone {100*dark.mean():.1f}%")
    print(f"tonal error (sigma=3)    : {tonal_error(dark, ink, 3.0):.4f}")
    print(f"\noutputs in {outdir}/  ({stem}_stipple.png, _stipple_3x.png, "
          f"_compare.png, _dots.csv)")


if __name__ == "__main__":
    main()
