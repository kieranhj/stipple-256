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

def load_darkness(path, W, H, opts, fit="cover"):
    """Load an image, fit it to WxH, apply tone preprocessing, return darkness.

    Preprocessing order (all operate on luminance in [0,1]):
      1. black/white point   - clamp/stretch the input tonal window
      2. brightness          - additive lift/drop
      3. contrast            - scale around mid-grey (0.5)
      4. posterize           - quantize to N luminance bands (0 = off)
    then darkness = 1 - luminance, and finally:
      5. gamma               - darkness ** gamma (>1 lightens midtones)
    """
    img = Image.open(path)
    if img.mode in ("RGBA", "LA", "P"):
        # flatten any transparency onto white so it reads as background
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("L")
    img = _fit(img, W, H, fit)
    lum = np.asarray(img, dtype=np.float64) / 255.0

    lo, hi = opts.black_point, opts.white_point
    lum = np.clip((lum - lo) / max(hi - lo, 1e-6), 0.0, 1.0)
    lum = lum + opts.brightness
    lum = (lum - 0.5) * opts.contrast + 0.5
    lum = np.clip(lum, 0.0, 1.0)
    if opts.posterize and opts.posterize >= 2:
        n = opts.posterize
        lum = np.round(lum * (n - 1)) / (n - 1)

    dark = 1.0 - lum
    dark = np.power(np.clip(dark, 0.0, 1.0), opts.gamma)
    return dark


def _fit(img, W, H, fit):
    """Fit to WxH. 'cover' = scale-to-fill + centre-crop; 'contain' = whole
    image letterboxed onto a white WxH canvas."""
    sw, sh = img.size
    if fit == "contain":
        scale = min(W / sw, H / sh)
        nw, nh = max(1, round(sw * scale)), max(1, round(sh * scale))
        small = img.resize((nw, nh), Image.LANCZOS)
        canvas = Image.new("L", (W, H), 255)        # white background
        canvas.paste(small, ((W - nw) // 2, (H - nh) // 2))
        return canvas
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
# BBC Micro data format (Phase 2)
# --------------------------------------------------------------------------
#
# Stream layout (decompressed in RAM; ZX02-compressed on disc):
#
#   nbuckets : 1 byte
#   repeat nbuckets:
#       radius : 1 byte                  (1..levels-1)
#       nlines : 1 byte                  (scanlines containing >=1 dot, 1..255)
#       repeat nlines:
#           y  : 1 byte                  (absolute, 0..255, ascending)
#           n  : 1 byte                  (dots on this line, 1..255)
#           repeat n:
#               dx : delta-x from previous dot on the line (prevx resets to 0
#                    each line). Escape coding: emit 0xFF for each whole 255,
#                    then a final byte 0..254. So real dx = sum of bytes read
#                    until a byte < 255.  (x range 0..319 needs >8 bits; this
#                    keeps everything byte-sized and very ZX02-friendly.)
#
# Dot centres are clamped to [r, W-1-r] x [r, H-1-r] so discs never cross the
# screen edge - the 6502 plotter then needs no clipping.

def pack_bbc(points, radii, W, H, levels):
    """Pack dots into the locked BBC delta-stream. Returns (bytes, kept_count)."""
    # build the list of (radius, [(y, [x,...]), ...]) bucket-entries first, so
    # nbuckets reflects any splitting of >255-line buckets.
    entries = []
    kept = 0
    for r in range(1, levels):
        sel = np.where(radii == r)[0]
        if len(sel) == 0:
            continue
        # clamp centres so the disc stays on screen (no 6502 clipping needed)
        bx = np.clip(np.round(points[sel, 0]).astype(int), r, W - 1 - r)
        by = np.clip(np.round(points[sel, 1]).astype(int), r, H - 1 - r)
        o = np.lexsort((bx, by))               # sort by y, then x
        bx, by = bx[o], by[o]
        lines = []
        i = 0
        while i < len(bx):
            y = int(by[i])
            xs_line = [int(bx[i])]
            j = i + 1
            while j < len(bx) and by[j] == y:
                xs_line.append(int(bx[j]))
                j += 1
            assert len(xs_line) <= 255, "scanline has >255 dots (impossible at 320px)"
            lines.append((y, xs_line))
            kept += len(xs_line)
            i = j
        # split into <=255-line entries that share the radius
        for s in range(0, len(lines), 255):
            entries.append((r, lines[s:s + 255]))

    out = bytearray()
    assert len(entries) <= 255, "too many bucket-entries"
    out.append(len(entries))
    for r, lines in entries:
        out.append(r)
        out.append(len(lines))
        for y, xs_line in lines:
            out.append(y)
            out.append(len(xs_line))
            prevx = 0
            for x in xs_line:
                dx = x - prevx
                while dx >= 255:
                    out.append(255)
                    dx -= 255
                out.append(dx)
                prevx = x
    return bytes(out), kept


def unpack_bbc(data):
    """Reference decoder mirroring the 6502 logic. Returns list of (x,y,r)."""
    dots = []
    p = 0
    nbuckets = data[p]; p += 1
    for _ in range(nbuckets):
        r = data[p]; p += 1
        nlines = data[p]; p += 1
        for _ in range(nlines):
            y = data[p]; p += 1
            n = data[p]; p += 1
            x = 0
            for _ in range(n):
                dx = 0
                b = data[p]; p += 1
                dx += b
                while b == 255:
                    b = data[p]; p += 1
                    dx += b
                x += dx
                dots.append((x, y, r))
    return dots


def bbc_export(points, radii, W, H, levels, outdir, stem, zx02_bin, log=True):
    """Write the raw stream + zx02-compressed data, and verify both round-trips."""
    import shutil
    import subprocess

    stream, kept = pack_bbc(points, radii, W, H, levels)
    raw_path = outdir / f"{stem}.bbc.bin"
    raw_path.write_bytes(stream)

    # verify our own format decodes back to the right dot count
    decoded = unpack_bbc(stream)
    ok = len(decoded) == kept
    if log:
        print("\n=== BBC export ===")
        print(f"raw delta stream         : {len(stream):8d} B   "
              f"({len(stream)/max(kept,1):.2f} B/dot, {kept} dots)")
        print(f"reference decode         : {'OK' if ok else 'MISMATCH'} "
              f"({len(decoded)} dots)")

    # zx02 compress (+ verify with the matching decompressor if available)
    zx02 = shutil.which(zx02_bin) or (zx02_bin if Path(zx02_bin).exists() else None)
    if zx02:
        zpath = outdir / f"{stem}.bbc.zx02"
        try:
            subprocess.run([zx02, "-f", str(raw_path), str(zpath)],
                           check=True, capture_output=True)
            zsize = zpath.stat().st_size
            if log:
                print(f"zx02 compressed          : {zsize:8d} B   "
                      f"<-- ships on disc  ({100*zsize/len(stream):.1f}% of raw)")
            # round-trip via dzx02 if it sits next to zx02
            dz = Path(zx02).with_name("dzx02")
            if dz.exists():
                rt = subprocess.run([str(dz), str(zpath)], capture_output=True)
                if rt.returncode == 0 and rt.stdout == stream:
                    if log: print("zx02 round-trip          : OK")
                elif log:
                    print("zx02 round-trip          : MISMATCH")
        except subprocess.CalledProcessError as e:
            if log: print(f"zx02 failed: {e.stderr.decode(errors='ignore')[:200]}")
    elif log:
        print(f"zx02 not found ('{zx02_bin}') - skipping compression. "
              f"Build it from github.com/dmsc/zx02 and pass --zx02 PATH.")
    return kept


# --------------------------------------------------------------------------
# mode256 — 256-byte BBC Master intro preview
# --------------------------------------------------------------------------
#
# Emulates the on-device algorithm from docs/STIPPLE-256.md:
#   - R2 low-discrepancy additive sequence in two 16-bit accumulators;
#     the high byte of each is the dot coord (0..255), matching the cheapest
#     6502 implementation.
#   - Brightness at (x,y) comes from either:
#       (a) a procedural formula evaluated on the device (zero source bytes), or
#       (b) a tiny downsampled+posterized stored image (option 2 in the plan).
#   - radius = (255 - brightness) >> 5  ->  0..7; r==0 dots are skipped.
#   - Each kept dot is a filled disc of that integer radius.
# Output canvas is 256x256 pixels; the device scales these to MODE 4 graphics
# units by *4. We work in 256x256 here so the preview math matches the bytes
# we'd actually execute.

R2_PHI2 = 1.32471795724474602596


def r2_sequence(n):
    """R2 additive low-discrepancy sequence. Returns two uint8 arrays (xs, ys).

    Mirrors the on-device math: 16-bit accumulators + two 16-bit increments;
    the dot coord is the accumulator's high byte.
    """
    XINC = round((1.0 / R2_PHI2) * 65536) & 0xFFFF             # ~0.7548 * 65536
    YINC = round((1.0 / (R2_PHI2 * R2_PHI2)) * 65536) & 0xFFFF  # ~0.5698 * 65536
    xacc = 0
    yacc = 0
    xs = np.empty(n, dtype=np.uint16)
    ys = np.empty(n, dtype=np.uint16)
    for i in range(n):
        xacc = (xacc + XINC) & 0xFFFF
        yacc = (yacc + YINC) & 0xFFFF
        xs[i] = xacc
        ys[i] = yacc
    return (xs >> 8).astype(np.uint8), (ys >> 8).astype(np.uint8)


def proc_brightness_map(name):
    """Build a 256x256 uint8 brightness map for a named procedural source."""
    yy, xx = np.mgrid[0:256, 0:256].astype(np.float64)
    fx = (xx - 128.0) / 128.0
    fy = (yy - 128.0) / 128.0
    if name == "sphere":
        d2 = fx * fx + fy * fy
        z = np.sqrt(np.clip(1.0 - d2, 0.0, 1.0))
        # light from upper-left, slight tilt
        lx, ly, lz = -0.5, -0.5, 0.707
        ln = (lx * lx + ly * ly + lz * lz) ** 0.5
        nl = np.clip((fx * lx + fy * ly + z * lz) / ln, 0.0, 1.0) ** 1.5
        # sphere reads dark on white; highlight reaches near-white, terminator
        # goes near-black, so radius maps across the full 0..7 range.
        b = np.where(d2 <= 1.0, 10.0 + 240.0 * nl, 255.0)
    elif name == "plasma":
        v = np.sin(fx * 3.1) + np.cos(fy * 2.7) + np.sin((fx + fy) * 2.3)
        b = 128.0 + 50.0 * v
    elif name == "torus":
        # off-axis torus distance proxy (cheap, not a real SDF)
        r1 = np.sqrt(fx * fx + fy * fy * 0.6) - 0.55
        d = np.sqrt(r1 * r1 + fy * fy * 0.4)
        b = np.clip(255.0 - 700.0 * np.maximum(0.0, 0.18 - d), 0.0, 255.0)
    else:
        raise ValueError(f"unknown --procedural {name!r} (try: sphere, plasma, torus)")
    return np.clip(b, 0.0, 255.0).astype(np.uint8)


def downsample_posterize(dark, size, levels):
    """Resize the darkness map to size x size and posterize to `levels`.

    Returns the cell grid (uint8, 0..levels-1) AND the 256x256 nearest-cell
    brightness map the device would see at runtime.
    """
    img = Image.fromarray(((1.0 - dark) * 255).astype(np.uint8), "L")
    small = img.resize((size, size), Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float64) / 255.0           # luminance
    cells = np.clip(np.round(arr * (levels - 1)).astype(np.uint8), 0, levels - 1)

    # nearest-cell lookup as the device would do: ix = (x * size) >> 8
    yy, xx = np.mgrid[0:256, 0:256]
    ix = (xx * size) // 256
    iy = (yy * size) // 256
    cell_lum = (cells.astype(np.float64) / max(levels - 1, 1) * 255.0).astype(np.uint8)
    bright = cell_lum[iy, ix]
    return cells, bright


def pack_bits(values, bits_per):
    """Pack small ints LSB-first into a byte stream.

    Cell 0 lands in the low bits of byte 0, cell 1 above it, etc. This is the
    cheapest layout for the 6502 lookup: shift count = (cell_idx & 3) * bits_per
    with no EOR/complement needed.
    """
    out = bytearray()
    acc = 0
    nbits = 0
    mask = (1 << bits_per) - 1
    for v in values:
        acc |= (int(v) & mask) << nbits
        nbits += bits_per
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits:
        out.append(acc & 0xFF)
    return bytes(out)


def try_zx02(data, outdir, stem, zx02_bin):
    """Compress with zx02 if available; returns size or None."""
    if not data:
        return None
    import shutil
    import subprocess
    zx02 = shutil.which(zx02_bin) or (zx02_bin if Path(zx02_bin).exists() else None)
    if not zx02:
        return None
    raw = outdir / f"{stem}_mode256_src.bin"
    raw.write_bytes(data)
    zpath = outdir / f"{stem}_mode256_src.zx02"
    try:
        subprocess.run([zx02, "-f", str(raw), str(zpath)],
                       check=True, capture_output=True)
        return zpath.stat().st_size
    except subprocess.CalledProcessError:
        return None


def mode256_render(brightmap, dots, levels=8):
    """R2-place `dots` points over the 256x256 brightmap and stamp discs."""
    W = H = 256
    xs, ys = r2_sequence(dots)
    b = brightmap[ys, xs]
    darkness = (255 - b.astype(np.int32))
    radii = (darkness >> 5).astype(np.int32)        # 0..7

    ink = np.zeros((H, W), dtype=bool)
    masks = {r: disc_mask(r) for r in range(levels)}
    plotted = 0
    for x, y, r in zip(xs, ys, radii):
        if r <= 0:
            continue
        plotted += 1
        m = masks[r]
        x0, y0 = int(x) - r, int(y) - r
        sx0 = max(0, -x0); sy0 = max(0, -y0)
        dx0 = max(0, x0);  dy0 = max(0, y0)
        ex = min(W, x0 + m.shape[1]); ey = min(H, y0 + m.shape[0])
        if ex <= dx0 or ey <= dy0:
            continue
        sub = m[sy0:sy0 + (ey - dy0), sx0:sx0 + (ex - dx0)]
        ink[dy0:ey, dx0:ex] |= sub
    return ink, radii, plotted


def save_mode256_previews(brightmap, ink, outdir, stem):
    H, W = ink.shape
    stip = np.where(ink, 0, 255).astype(np.uint8)
    Image.fromarray(stip, "L").save(outdir / f"{stem}_mode256.png")
    Image.fromarray(stip, "L").resize((W * 3, H * 3), Image.NEAREST).save(
        outdir / f"{stem}_mode256_3x.png")
    pad = 8
    comp = np.full((H, W * 2 + pad), 255, np.uint8)
    comp[:, :W] = brightmap
    comp[:, W + pad:] = stip
    Image.fromarray(comp, "L").resize(
        ((W * 2 + pad) * 2, H * 2), Image.NEAREST).save(
        outdir / f"{stem}_mode256_compare.png")


def mode256_preview(args, dark, outdir, stem):
    """Build the 256-byte-target preview and print the byte-budget estimate."""
    if args.procedural:
        bright = proc_brightness_map(args.procedural)
        src_bytes = b""
        src_descr = f"procedural '{args.procedural}' (no source data)"
        zsize = None
    else:
        size = args.mode256_size
        L = args.mode256_levels
        cells, bright = downsample_posterize(dark, size, L)
        bits_per = max(1, int(np.ceil(np.log2(L))))
        # cells from downsample_posterize encode LUMINANCE (0=dark, L-1=light)
        # but the BBC reads `r = cell * 2`, so cell 0 must mean "no dot" and
        # cell L-1 must mean "biggest dot". Invert to darkness ordering.
        # Also flip vertically: BBC graphics y=0 is at the BOTTOM of the screen.
        cells_dev = np.flipud((L - 1) - cells)
        src_bytes = pack_bits(cells_dev.ravel(), bits_per)
        src_descr = f"{size}x{size} @ {bits_per}bpp = {size*size*bits_per/8:.0f} B raw"
        zsize = try_zx02(src_bytes, outdir, stem, args.zx02)
        # always write the raw stored image too for inspection
        (outdir / f"{stem}_mode256_src.bin").write_bytes(src_bytes)

    ink, radii, plotted = mode256_render(bright, args.mode256_dots)
    save_mode256_previews(bright, ink, outdir, stem)

    # rough on-device code budget per docs/STIPPLE-256.md
    parts = {
        "VDU init (table+OSWRCH)":   16,
        "R2 placement":              14,
        "brightness eval":           (35 if args.procedural else 20),
        "radius map":                 6,
        "emit MOVE+filled circle":   40,
        "loop control":              10,
    }
    code_est = sum(parts.values())
    data_cost = zsize if zsize is not None else len(src_bytes)
    total = code_est + data_cost

    print("\n=== mode256 preview ===")
    print(f"source                   : {src_descr}")
    if src_bytes:
        print(f"source bytes (raw)       : {len(src_bytes)}")
        if zsize is not None:
            print(f"source bytes (zx02)      : {zsize}  "
                  f"({100*zsize/max(len(src_bytes),1):.0f}% of raw)  <-- ships on disc")
        else:
            print(f"source bytes (zx02)      : -- (zx02 not found)")
    print(f"dots attempted           : {args.mode256_dots}")
    print(f"dots plotted (r>0)       : {plotted}")
    rh = np.bincount(np.clip(radii, 0, 7), minlength=8)
    print("\nradius histogram (level: count)")
    for r in range(8):
        bar = "#" * int(40 * rh[r] / max(rh.max(), 1))
        tag = " (skipped)" if r == 0 else ""
        print(f"  r={r}: {rh[r]:5d} {bar}{tag}")
    print("\n=== byte budget estimate (256 B target) ===")
    for k, v in parts.items():
        print(f"  {k:30s} ~{v:3d} B")
    print(f"  {'code subtotal':30s} ~{code_est:3d} B")
    print(f"  {'data':30s}  {data_cost:3d} B")
    print(f"  {'TOTAL':30s} ~{total:3d} B   "
          f"{'OK' if total <= 256 else 'OVER ('+str(total-256)+' B)'}")
    print(f"\noutputs in {outdir}/  ({stem}_mode256.png, _mode256_3x.png, "
          f"_mode256_compare.png{', _mode256_src.bin' if src_bytes else ''})")


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
    # --- packing B: the real locked BBC delta-stream (see pack_bbc) --------
    packed, _ = pack_bbc(points, radii, W, H, levels)

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
    # --- tone preprocessing ---
    ap.add_argument("--gamma", type=float, default=1.0, help="darkness gamma (>1 lightens mids)")
    ap.add_argument("--brightness", type=float, default=0.0,
                    help="additive luminance lift, -1..1 (positive = brighter = fewer/smaller dots)")
    ap.add_argument("--contrast", type=float, default=1.0,
                    help="luminance contrast about mid-grey (1=none, >1 punchier)")
    ap.add_argument("--posterize", type=int, default=0,
                    help="quantize luminance to N bands before stippling (0=off, e.g. 4-6 for poster look)")
    ap.add_argument("--black-point", type=float, default=0.0,
                    help="input luminance mapped to full black (0..1)")
    ap.add_argument("--white-point", type=float, default=1.0,
                    help="input luminance mapped to full white (0..1)")
    ap.add_argument("--fit", choices=["cover", "contain"], default="cover",
                    help="cover=fill+crop (default), contain=whole image letterboxed")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("-o", "--outdir", default="stipple_out")
    ap.add_argument("--stem", default=None, help="output filename stem")
    ap.add_argument("--bbc", action="store_true",
                    help="also export BBC Micro delta-stream data and zx02-compress it")
    ap.add_argument("--zx02", default="zx02", help="path to the zx02 compressor binary")
    # --- 256-byte BBC Master preview (docs/STIPPLE-256.md) ---
    ap.add_argument("--mode256", action="store_true",
                    help="render the 256-byte intro preview (R2 placement, tiny "
                         "stored or procedural source) and skip the Lloyd pipeline")
    ap.add_argument("--procedural", default=None, metavar="NAME",
                    help="mode256: use a procedural brightness source instead of "
                         "the image (sphere|plasma|torus)")
    ap.add_argument("--mode256-size", type=int, default=24,
                    help="mode256: stored source resolution (NxN, default 24)")
    ap.add_argument("--mode256-levels", type=int, default=4,
                    help="mode256: posterize levels for stored source (default 4 = 2bpp)")
    ap.add_argument("--mode256-dots", type=int, default=1200,
                    help="mode256: number of R2-placed dot attempts (r==0 are skipped)")
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
    if not args.image and not args.procedural:
        ap.error("provide an image, or use --make-test-image PATH, "
                 "or --mode256 --procedural NAME")

    stem = args.stem or (Path(args.image).stem if args.image else f"proc_{args.procedural}")
    rng = np.random.default_rng(args.seed)

    if args.mode256:
        # mode256 uses 256x256 px space (matches the on-device hi-byte math)
        if args.procedural:
            dark = None
        else:
            dark = load_darkness(args.image, 256, 256, args, args.fit)
        mode256_preview(args, dark, outdir, stem)
        return

    dark = load_darkness(args.image, W, H, args, args.fit)

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
    if args.bbc:
        bbc_export(pts, radii, W, H, args.levels, outdir, stem, args.zx02,
                   log=True)

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
