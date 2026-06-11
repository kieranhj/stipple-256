#!/usr/bin/env python3
"""
stipple_ui.py - Gradio UI for tweaking the 256-byte BBC Master stipple intro.

Runs the same pipeline as `stipple.py --mode256`, but live: tweak the
preprocessing knobs and see the preprocessed luminance image, the 16x16
posterised "stored bytes" the device would actually see, and the final
R2-stippled output, all side by side.

Launch:
    python tools/stipple_ui.py
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import gradio as gr
import numpy as np
from PIL import Image

import stipple as st


REPO_ROOT = Path(__file__).resolve().parent.parent
PICS_DIR = REPO_ROOT / "pics"


# --- R2 sequence matching the asm exactly -------------------------------------
# The on-device constants in bbc/stipple256.asm are XINC=$C142, YINC=$91DF.
# stipple.r2_sequence derives its constants from the plastic-phi formula and
# rounds to $C140 / $91E1 — off by 2 from the asm. Over 768 iterations 712 of
# the dots land at different positions, hiding the R2 streak structure in the
# preview. The preview MUST use the same constants the asm actually executes,
# so we hard-code them here.
XINC_ASM = 0xC142
YINC_ASM = 0x91DF


def r2_sequence_asm(n):
    """Mirror the 6502 R2 with the exact asm constants. Returns uint8 xs, ys."""
    xacc = 0
    yacc = 0
    xs = np.empty(n, dtype=np.uint16)
    ys = np.empty(n, dtype=np.uint16)
    for i in range(n):
        xacc = (xacc + XINC_ASM) & 0xFFFF
        yacc = (yacc + YINC_ASM) & 0xFFFF
        xs[i] = xacc
        ys[i] = yacc
    return (xs >> 8).astype(np.uint8), (ys >> 8).astype(np.uint8)


def r2_sequence_asm_full(n):
    """Same R2 but return the full 16-bit accumulators as float [0, 256).

    The on-device R2 throws away the low byte and uses only the high byte for
    the pixel coordinate. But the low byte still carries information about
    the dot's *sub-pixel* position — and since the offline packer builds the
    radius script ahead of time, we can use that sub-pixel position to BILINEAR
    sample the darkness map at much finer effective resolution than the 256×256
    integer grid. The device replays the same i-th radius byte at the same
    integer position, so this costs nothing on the BBC.
    """
    xacc = 0
    yacc = 0
    xs = np.empty(n, dtype=np.float64)
    ys = np.empty(n, dtype=np.float64)
    for i in range(n):
        xacc = (xacc + XINC_ASM) & 0xFFFF
        yacc = (yacc + YINC_ASM) & 0xFFFF
        xs[i] = xacc / 256.0     # 0 .. 256, fractional part = sub-pixel
        ys[i] = yacc / 256.0
    return xs, ys


def _list_pics():
    if not PICS_DIR.exists():
        return []
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
    return sorted(p.name for p in PICS_DIR.iterdir() if p.suffix.lower() in exts)


def _resolve_image(picker_value, upload_pil):
    """Return a PIL.Image or None. Upload wins if both are set."""
    if upload_pil is not None:
        return upload_pil
    if picker_value:
        p = PICS_DIR / picker_value
        if p.exists():
            return Image.open(p)
    return None


def _preprocess(img, fit, gamma, brightness, contrast, posterize,
                black_point, white_point, bbc_aspect=True):
    """Run stipple.load_darkness equivalent on a PIL.Image; return (dark, lum_preview).

    bbc_aspect=True fits to 320x256 (the BBC's physical 5:4 screen aspect,
    same in MODE 0 and MODE 4 — MOS VDU units are resolution-independent)
    so that when the asm renders with gx*5 the on-screen image looks
    proportional. bbc_aspect=False keeps the legacy 256x256 square fit
    (image appears stretched 25% wider on the BBC).
    """
    opts = SimpleNamespace(
        gamma=float(gamma),
        brightness=float(brightness),
        contrast=float(contrast),
        posterize=int(posterize),
        black_point=float(black_point),
        white_point=float(white_point),
    )

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("L")
    W, H = (320, 256) if bbc_aspect else (256, 256)
    img = st._fit(img, W, H, fit)
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

    lum_preview = (lum * 255.0).astype(np.uint8)
    return dark, lum_preview


def _cells_preview(cells_lum):
    """Upscale the size x size luminance grid to 256x256 nearest-neighbour."""
    H, W = cells_lum.shape
    factor = 256 // max(H, 1)
    return np.kron(cells_lum, np.ones((factor, factor), dtype=np.uint8))


# --- radius-mapping presets ---------------------------------------------------
#
# The BBC code maps a 2-bit cell darkness 0..3 to a pixel radius. The current
# committed mapping is r = cell * 2  -> {0, 2, 4, 6} (the "asl A" between cell
# extraction and store-to-r). Earlier iterations used r = cell  -> {0, 1, 2, 3}
# (no asl, smaller dots). We expose this as a tunable LUT so we can A/B and
# also explore non-linear maps like {0, 2, 4, 7}.
#
# For levels > 4 we extend the presets with reasonable defaults; the text box
# is the source of truth.
RADIUS_PRESETS = {
    "odd (current BBC: 0,1,3,5)": [0, 1, 3, 5],
    "cell×2 (earlier: 0,2,4,6)": [0, 2, 4, 6],
    "cell (earlier smaller: 0,1,2,3)": [0, 1, 2, 3],
    "wide (0,2,4,7)": [0, 2, 4, 7],
    "binary (0,0,4,4)": [0, 0, 4, 4],
    "exp (0,5,13,29)": [0, 5, 13, 29],
    "custom": None,
}


def _parse_lut(text, levels):
    """Parse a comma-separated list of ints; pad/truncate to `levels`."""
    try:
        vals = [int(x.strip()) for x in text.replace(";", ",").split(",") if x.strip()]
    except ValueError:
        vals = []
    if not vals:
        vals = [0]
    while len(vals) < levels:
        vals.append(vals[-1])
    return [max(0, min(63, v)) for v in vals[:levels]]


DITHER_MODES = ["none", "floyd-steinberg", "ordered (4×4 bayer)"]


def _downsample_dither(dark, size, levels, mode):
    """Downsample dark->size×size and quantise to `levels` with optional dither.

    Returns a uint8 grid in [0, levels-1] encoding *luminance* (0=dark, L-1=light)
    — same convention as stipple.downsample_posterize, so the BBC packer's
    `cells_dev = (L-1) - cells` darkness flip still applies downstream.

    Dither is computed in luminance space (operating on the LANCZOS-downsampled
    floats), then quantised. F-S diffuses the per-cell quantisation error onto
    its 4 future neighbours; Bayer adds a fixed 4×4 threshold pattern. Both
    spread tonal information into the 16×16 grid that nearest rounding loses.
    """
    img = Image.fromarray(((1.0 - dark) * 255).astype(np.uint8), "L")
    small = img.resize((size, size), Image.LANCZOS)
    arr = np.asarray(small, dtype=np.float64) / 255.0
    H, W = arr.shape

    if mode == "floyd-steinberg":
        buf = arr.copy()
        cells = np.zeros((H, W), dtype=np.uint8)
        for y in range(H):
            for x in range(W):
                old = buf[y, x]
                lvl = int(np.clip(round(old * (levels - 1)), 0, levels - 1))
                new = lvl / max(levels - 1, 1)
                cells[y, x] = lvl
                err = old - new
                if x + 1 < W:    buf[y, x + 1]     += err * 7 / 16
                if y + 1 < H:
                    if x > 0:    buf[y + 1, x - 1] += err * 3 / 16
                    buf[y + 1, x]                  += err * 5 / 16
                    if x + 1 < W: buf[y + 1, x + 1] += err * 1 / 16
        return cells
    if mode.startswith("ordered"):
        bayer = np.array([[ 0,  8,  2, 10],
                          [12,  4, 14,  6],
                          [ 3, 11,  1,  9],
                          [15,  7, 13,  5]], dtype=np.float64) / 16.0 - 0.5
        m = bayer[np.arange(H)[:, None] % 4, np.arange(W)[None, :] % 4]
        biased = arr + m / max(levels - 1, 1)
        return np.clip(np.round(biased * (levels - 1)).astype(int),
                       0, levels - 1).astype(np.uint8)
    return np.clip(np.round(arr * (levels - 1)).astype(int),
                   0, levels - 1).astype(np.uint8)


def _disc_mask_bbc(r):
    """Filled disc matching the BBC MOS PLOT 157 rasterisation more closely.

    stipple.disc_mask uses x²+y² ≤ r², which at small radii produces sharp
    diamond/plus shapes (the corner pixels (±1,±2) at r=2 are excluded
    because 5 > 4). The BBC's Bresenham-style span-fill circle is more
    generous and includes those near-corner pixels — e.g. r=2 comes out as a
    near-square 5×5, not a 5-pixel diamond. Threshold of r²+r (= r·(r+1))
    rounds the corners in instead of flattening them: at r=2, (±1,±2) and
    (±2,±1) pass (5 ≤ 6) but (±2,±2) still fails (8 > 6).
    """
    if r <= 0:
        return np.ones((1, 1), dtype=bool)
    yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= r * r + r


def _stamp_dots(xs, ys, radii, W=256, H=256):
    """Stamp filled circular discs onto a W×H ink map."""
    max_r = int(radii.max()) if len(radii) else 0
    ink = np.zeros((H, W), dtype=bool)
    masks = {r: _disc_mask_bbc(r) for r in range(max_r + 1)}
    plotted = 0
    for x, y, r in zip(xs, ys, radii):
        r = int(r)
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
    return ink, plotted


def _render_bbc(cells, size, levels, dots, radius_lut):
    """Faithful preview of the on-device algorithm with an explicit radius LUT.

    Cell-lookup mode: takes pre-quantised cells (luminance encoding,
    0=dark .. L-1=light), and at each R2 sample reads the matching cell
    from the size×size grid.
    """
    darkness_grid = (levels - 1) - cells       # 0=light, levels-1=dark
    xs, ys = r2_sequence_asm(dots)
    # Mirror y: the BBC has y=0 at the BOTTOM of the screen, so the R2 step
    # ys=0,1,2... walks bottom-up on its display. Mirroring here makes the
    # preview's streak direction match the emulator while keeping the image
    # itself right-side up (we sample and stamp at the same mirrored y).
    ys = (255 - ys.astype(np.int32)).astype(np.uint8)
    iy = (ys.astype(np.int32) * size) // 256
    # Cell-x index uses px (xs) unchanged — the cell grid is still 16 wide,
    # the screen-fill scaling only affects WHERE on screen the dot lands.
    ix = (xs.astype(np.int32) * size) // 256
    cell_vals = darkness_grid[iy, ix]
    lut = np.asarray(radius_lut, dtype=np.int32)
    radii = lut[np.clip(cell_vals, 0, len(lut) - 1)]
    # Model the BBC asm's gx*5 screen-fill at MODE 4 resolution (320x256).
    # The asm now targets MODE 0 (640x256 pixel grid) but MOS VDU units are
    # resolution-independent: PLOT 157 produces a physically round dot at
    # the same physical size in either mode, and the screen aspect stays
    # 5:4. MODE 0 just gives finer pixel granularity along the dot edges
    # (smoother circles on hardware). Previewing at 320x256 is a faithful
    # physical-aspect view — the smoother-edge benefit only shows on
    # actual hardware / jsbeeb.
    gx_pixels = (xs.astype(np.int32) * 5) // 4
    ink, plotted = _stamp_dots(gx_pixels, ys, radii, W=320, H=256)
    return ink, radii, plotted


def _build_dot_script(dark, levels, n_dots, dither, sampling):
    """Option 2: 64 B of source = a sequence of per-iteration darkness levels.

    For each R2 iteration i ∈ [0, n_dots), sample the source darkness at the
    R2 position (no 16×16 cell grid involved), quantise to `levels`, pack at
    `bits_per` bits per dot. On device the byte stream is replayed in lockstep
    with R2: the i-th stored level supplies the radius for the i-th dot.

    `sampling` ∈ {"nearest", "bilinear"} picks the offline sampling method:
    bilinear uses the R2 sub-pixel position (the low byte of the 16-bit R2
    accumulators) to weighted-average the 4 surrounding pixels — costs zero
    on-device, since the device still plots at the integer high-byte position.
    """
    H, W = dark.shape
    if dither.startswith("ordered"):
        bayer = np.array([[ 0,  8,  2, 10],
                          [12,  4, 14,  6],
                          [ 3, 11,  1,  9],
                          [15,  7, 13,  5]], dtype=np.float64) / 16.0 - 0.5
        m = bayer[np.arange(H)[:, None] % 4, np.arange(W)[None, :] % 4]
        sample_field = np.clip(dark + m / max(levels - 1, 1), 0.0, 1.0)
    else:
        # F-S is spatially diffuse; on a sparse R2 sampling the diffusion
        # rarely reaches the actual sample point, so it's not meaningfully
        # different from 'none' here. Skip it in script mode.
        sample_field = dark

    if sampling == "bilinear":
        # Use the full 16-bit R2 accumulators (sub-pixel positions). Sample
        # the darkness map with bilinear interpolation, then take the integer
        # high byte as the actual plotting position the BBC uses.
        xf_full, yf_full = r2_sequence_asm_full(n_dots)
        # Mirror y for the BBC y-up convention (same as _render_bbc).
        yf_full = 256.0 - yf_full
        x0 = np.floor(xf_full).astype(np.int32) % W
        y0 = np.floor(yf_full).astype(np.int32) % H
        x1 = (x0 + 1) % W
        y1 = (y0 + 1) % H
        fx = xf_full - np.floor(xf_full)
        fy = yf_full - np.floor(yf_full)
        s00 = sample_field[y0, x0]
        s01 = sample_field[y0, x1]
        s10 = sample_field[y1, x0]
        s11 = sample_field[y1, x1]
        samples = ((1 - fx) * (1 - fy) * s00 + fx * (1 - fy) * s01
                   + (1 - fx) * fy * s10 + fx * fy * s11)
        xs = (x0.astype(np.int32) & 0xFF).astype(np.uint8)
        ys = (y0.astype(np.int32) & 0xFF).astype(np.uint8)
    else:
        xs, ys = r2_sequence_asm(n_dots)
        # Mirror y to match BBC y-up convention (see _render_bbc).
        ys = (255 - ys.astype(np.int32)).astype(np.uint8)
        samples = sample_field[ys.astype(np.int32), xs.astype(np.int32)]
    # quantise darkness 0..1 to integer level 0..L-1 (0=light, L-1=dark)
    dlevels = np.clip(np.round(samples * (levels - 1)).astype(np.int32),
                      0, levels - 1)
    bits_per = max(1, int(np.ceil(np.log2(levels))))
    packed = st.pack_bits(dlevels.tolist(), bits_per)
    return xs, ys, dlevels, packed


def _render_dot_script(dark, levels, n_dots, radius_lut, dither, sampling):
    """Render mode 2: R2 placement + per-iteration radius LUT lookup."""
    xs, ys, dlevels, packed = _build_dot_script(
        dark, levels, n_dots, dither, sampling)
    lut = np.asarray(radius_lut, dtype=np.int32)
    radii = lut[np.clip(dlevels, 0, len(lut) - 1)]
    ink, plotted = _stamp_dots(xs, ys, radii)
    return ink, radii, plotted, packed


def _script_radii_image(xs, ys, radii, max_r):
    """Visualisation for the 'stored bytes' panel in dot-script mode.

    Draws a 256×256 grayscale where each dot's *radius* is plotted at its
    iteration position (R2 (x,y)), normalised to 0..255. Lighter = bigger
    radius. Gives a sense of which regions the script targets and how the
    radii distribute over the canvas without obscuring the underlying tone.
    """
    img = np.full((256, 256), 230, dtype=np.uint8)   # pale background
    if max_r <= 0:
        return img
    for x, y, r in zip(xs, ys, radii):
        v = int(255 * (1.0 - r / max_r))             # bigger r -> darker
        img[int(y), int(x)] = v
    return img


DATA_MODES = [
    "cell lookup (16×16 grid)",
    "dot script (per-iteration radii)",
    "rejection sample (density)",
]


REJECT_HASH_MODES = [
    "pseudo (xa^ya mod L)",
    "pseudo (LFSR low bits)",
    "uniform (ideal reference)",
]


def _render_rejection(dark, size, levels, n_iters, fixed_r, hash_mode):
    """Option 1 prototype: probability-density rejection sampling.

    Same 64 B of data as cell-lookup mode (16×16 × log2(L) bits). At each R2
    sample, look up the cell darkness d ∈ [0, L-1] and a pseudo-random
    h ∈ [0, L-1]; plot a fixed-radius dot iff h < d. Tone is rendered by
    *dot density*, not dot size, so dark regions get a dense uniform speckle
    and light regions are sparse — no big-r overlap clumping.

    hash_mode:
      - "pseudo (xa^ya)" — what the BBC asm would compute cheaply
        (eor xa,ya ; and #(L-1)). Correlated with R2, but works.
      - "pseudo (LFSR)"  — drive a third LFSR alongside R2; take low bits.
        Better-distributed pseudo-random source, costs ~6 more asm bytes.
      - "uniform"        — numpy RNG, ideal-quality reference.
    """
    cells = _downsample_dither(dark, size, levels, "none")
    darkness = (levels - 1) - cells               # 0=light .. L-1=dark

    xs, ys = r2_sequence_asm(n_iters)
    ys = (255 - ys.astype(np.int32)).astype(np.uint8)
    ix = (xs.astype(np.int32) * size) // 256
    iy = (ys.astype(np.int32) * size) // 256
    d = darkness[iy, ix].astype(np.int32)

    if hash_mode.startswith("pseudo (xa^ya"):
        h = (xs.astype(np.int32) ^ ys.astype(np.int32)) & (levels - 1)
    elif hash_mode.startswith("pseudo (LFSR"):
        # Galois LFSR, tap 0xB400, seed 0xACE1 (arbitrary nonzero)
        state = 0xACE1
        h = np.empty(n_iters, dtype=np.int32)
        for i in range(n_iters):
            lsb = state & 1
            state >>= 1
            if lsb:
                state ^= 0xB400
            h[i] = state & (levels - 1)
    else:
        rng = np.random.default_rng(12345)
        h = rng.integers(0, levels, size=n_iters)

    accept = h < d
    keep_xs = xs[accept]
    keep_ys = ys[accept]
    radii = np.full(keep_xs.shape, fixed_r, dtype=np.int32)
    ink, plotted = _stamp_dots(keep_xs, keep_ys, radii)
    return ink, radii, plotted, cells, int(accept.sum())


def render(picker, upload, fit, gamma, brightness, contrast, posterize,
           black_point, white_point, size, levels, dots,
           radius_preset, radius_text, dither, data_mode, script_sampling,
           reject_radius, reject_hash, bbc_aspect):
    img = _resolve_image(picker, upload)
    if img is None:
        blank = np.full((256, 256), 255, dtype=np.uint8)
        return blank, blank, blank, "Pick or upload an image."

    dark, lum_preview = _preprocess(
        img, fit, gamma, brightness, contrast, posterize, black_point, white_point,
        bbc_aspect=bool(bbc_aspect))

    size = int(size)
    levels = int(levels)
    dots = int(dots)
    bits_per = max(1, int(np.ceil(np.log2(levels))))
    lut = _parse_lut(radius_text, levels)
    code_est = 191  # current asm reality (see docs/STIPPLE-256.md)
    data_budget = 64  # bytes the data section currently has room for

    if data_mode.startswith("rejection"):
        fixed_r = int(reject_radius)
        ink, radii, plotted, cells, accepted = _render_rejection(
            dark, size, levels, dots, fixed_r, reject_hash)
        stipple_img = np.where(ink, 0, 255).astype(np.uint8)
        cells_dev = np.flipud((levels - 1) - cells)
        packed = st.pack_bits(cells_dev.ravel(), bits_per)
        cell_lum = (cells.astype(np.float64) / max(levels - 1, 1) * 255.0).astype(np.uint8)
        cells_img = _cells_preview(cell_lum)
        raw_bytes = len(packed)
        # rejection asm is bigger than cell-lookup: ~+8 bytes for the
        # hash+compare+conditional, ~-3 saved by dropping the cell→radius
        # LUT (fixed r becomes an immediate). Rough estimate.
        code_est = 196
        accept_pct = (100.0 * accepted / max(dots, 1))
        mode_descr = (
            f"**Mode**: rejection sample ({reject_hash}) — "
            f"{size}×{size} @ {bits_per}bpp = {raw_bytes} B, fixed r={fixed_r}, "
            f"{dots} R2 iters → {accepted} kept ({accept_pct:.1f}% accept rate)"
        )
    elif data_mode.startswith("dot script"):
        # script mode: data is per-iteration radii. size×size = total dot
        # count, byte count = (size² × bits_per) / 8. Same semantics for size
        # as cell-lookup mode (data points per axis), just laid out as a
        # sequence instead of a grid.
        n_dots = size * size
        ink, radii, plotted, packed = _render_dot_script(
            dark, levels, n_dots, lut, dither, script_sampling)
        stipple_img = np.where(ink, 0, 255).astype(np.uint8)
        raw_bytes = len(packed)

        # cells panel: render the per-dot radii as a sparse "where the dots
        # land and how big" preview so you can compare against the source.
        xs_p, ys_p = r2_sequence_asm(n_dots)
        ys_p = (255 - ys_p.astype(np.int32)).astype(np.uint8)
        cells_img = _script_radii_image(xs_p, ys_p, radii, int(max(lut) if lut else 0))
        mode_descr = (
            f"**Mode**: dot script ({script_sampling} sampling) — "
            f"{size}×{size} = {n_dots} dots × {bits_per}b = {raw_bytes} B "
            f"({dither} dither {'(applied)' if dither.startswith('ordered') else '(F-S ignored — irrelevant for point samples)'})"
        )
    else:
        # cell-lookup mode (the current asm)
        cells = _downsample_dither(dark, size, levels, dither)
        cells_dev = np.flipud((levels - 1) - cells)
        packed = st.pack_bits(cells_dev.ravel(), bits_per)
        cell_lum = (cells.astype(np.float64) / max(levels - 1, 1) * 255.0).astype(np.uint8)
        cells_img = _cells_preview(cell_lum)
        ink, radii, plotted = _render_bbc(cells, size, levels, dots, lut)
        stipple_img = np.where(ink, 0, 255).astype(np.uint8)
        raw_bytes = len(packed)
        mode_descr = (
            f"**Mode**: cell lookup — {size}×{size} @ {bits_per}bpp = {raw_bytes} B "
            f"({dither} dither), {dots} R2 iters"
        )

    total = code_est + raw_bytes
    over = total - 256

    max_r = max(7, int(max(lut)) if lut else 7)
    rh = np.bincount(np.clip(radii, 0, max_r), minlength=max_r + 1)
    hist = "  ".join(f"r{r}:{rh[r]}" for r in range(max_r + 1))

    lut_str = ",".join(str(v) for v in lut)
    info = (
        f"{mode_descr}  \n"
        f"**Radius LUT**: cell→radius = [{lut_str}]  \n"
        f"**Dots plotted (r>0)**: {plotted}  \n"
        f"**Radius histogram**: {hist}  \n"
        f"**Byte budget**: code≈{code_est} + data={raw_bytes} = **{total}** "
        f"({'OK, ' + str(256 - total) + ' B spare' if over <= 0 else 'OVER by ' + str(over) + ' B'})"
    )
    return lum_preview, cells_img, stipple_img, info


def reset_defaults():
    return ("cover", 1.0, 0.0, 1.0, 0, 0.0, 1.0, 16, 4, 2048,
            "odd (current BBC: 0,1,3,5)", "0,1,3,5", "none",
            "cell lookup (16×16 grid)", "bilinear",
            2, "pseudo (xa^ya mod L)", True)


def apply_preset(name, current_text, levels):
    """Preset radio handler. 'custom' leaves the textbox alone."""
    if name == "custom" or name not in RADIUS_PRESETS or RADIUS_PRESETS[name] is None:
        return current_text
    vals = RADIUS_PRESETS[name]
    # pad to current `levels` so the displayed LUT matches what's used
    while len(vals) < int(levels):
        vals = vals + [vals[-1]]
    return ",".join(str(v) for v in vals[:int(levels)])


def build_ui():
    pic_choices = _list_pics()
    default_pic = "mona_lisa_crop.png" if "mona_lisa_crop.png" in pic_choices else (
        pic_choices[0] if pic_choices else None)

    with gr.Blocks(title="stipple256 tuner") as demo:
        gr.Markdown("## stipple256 tuner — preprocessing → 16×16×4 stored bytes → stipple output")
        gr.Markdown(
            "Pick a source from `pics/` (or upload), then tweak preprocessing and "
            "the device parameters. Updates live."
        )

        with gr.Row():
            with gr.Column(scale=1):
                picker = gr.Dropdown(
                    pic_choices, value=default_pic, label="pics/", interactive=True)
                upload = gr.Image(type="pil", label="...or upload (overrides picker)",
                                  height=200)
                fit = gr.Radio(["cover", "contain"], value="cover", label="fit")
                bbc_aspect = gr.Checkbox(
                    value=True,
                    label="BBC aspect (5:4 fit, compensates gx*5 stretch)",
                    info=("ON: source is fit to 320x256 (BBC physical screen "
                          "aspect, same in MODE 0 and MODE 4) so the on-screen "
                          "image looks proportional. OFF: 256x256 square fit "
                          "— image appears stretched 25% wider on the BBC."),
                )
                gr.Markdown("### Preprocessing")
                gamma = gr.Slider(0.2, 4.0, value=1.0, step=0.05, label="gamma (>1 lightens midtones)")
                brightness = gr.Slider(-0.5, 0.5, value=0.0, step=0.01, label="brightness")
                contrast = gr.Slider(0.2, 3.0, value=1.0, step=0.05, label="contrast")
                posterize = gr.Slider(0, 8, value=0, step=1, label="posterize (0 = off)")
                black_point = gr.Slider(0.0, 1.0, value=0.0, step=0.01, label="black point")
                white_point = gr.Slider(0.0, 1.0, value=1.0, step=0.01, label="white point")
                gr.Markdown("### Device parameters")
                size = gr.Slider(
                    8, 64, value=16, step=2,
                    label="grid / dot count (N×N)",
                    info=("cell-lookup mode: cell grid resolution (16 = current asm; >32 won't fit). "
                          "dot-script mode: total dot count is N×N (16×16=256 dots = 64 B at 2 bpp; "
                          "32×32=1024 dots = 256 B — exceeds budget but reveals visual ceiling)."),
                )
                levels = gr.Slider(2, 8, value=4, step=1, label="levels (4 = 2 bpp)")
                dots = gr.Slider(64, 2048, value=2048, step=32, label="R2 dot iterations")
                gr.Markdown("### Radius mapping (cell darkness → pixel radius)")
                radius_preset = gr.Radio(
                    list(RADIUS_PRESETS.keys()),
                    value="odd (current BBC: 0,1,3,5)",
                    label="preset (picks one fills the textbox)",
                )
                radius_text = gr.Textbox(
                    value="0,1,3,5",
                    label="LUT (cell 0 → cell L-1, comma-separated)",
                    info="Length should match levels. r=0 skips the dot.",
                )
                gr.Markdown("### Dither (into the 4-level quantisation)")
                dither = gr.Radio(
                    DITHER_MODES, value="none", label="dither mode",
                    info="F-S diffuses quantisation error; ordered adds a Bayer threshold pattern.",
                )
                gr.Markdown("### Data interpretation (Option 2 toggle)")
                data_mode = gr.Radio(
                    DATA_MODES, value="cell lookup (16×16 grid)",
                    label="how the 64 B of source data is read",
                    info=("'cell lookup' = current asm: 16×16×{levels} grid sampled per dot. "
                          "'dot script' = data is a sequence of per-iteration radii "
                          "(256 dots × 2 bits = 64 B at L=4). One sample per dot — "
                          "more detail at edges, fewer dots total."),
                )
                script_sampling = gr.Radio(
                    ["nearest", "bilinear"], value="bilinear",
                    label="dot-script sampling (Option 2 only)",
                    info=("'nearest' = sample dark map at the dot's integer pixel "
                          "position only. 'bilinear' = use the R2 sub-pixel position "
                          "(the low byte of the 16-bit R2 accumulators that the asm "
                          "currently throws away) to blend the 4 surrounding pixels. "
                          "Free on-device — the BBC still plots at the same integer "
                          "position; bilinear only changes the offline-baked radius."),
                )
                gr.Markdown("### Rejection sampling (Option 1 only)")
                reject_radius = gr.Slider(
                    1, 4, value=2, step=1,
                    label="fixed dot radius",
                    info="All accepted dots use this radius. Tone is rendered by density, not size.",
                )
                reject_hash = gr.Radio(
                    REJECT_HASH_MODES, value="pseudo (xa^ya mod L)",
                    label="hash source for accept/reject",
                    info=("'xa^ya' = the cheapest on-device hash (~5 bytes asm). "
                          "'LFSR' = better-distributed pseudo-random (~+6 bytes asm). "
                          "'uniform' = numpy RNG, ideal-quality reference (cannot be implemented in asm)."),
                )
                reset_btn = gr.Button("Reset defaults")
            with gr.Column(scale=2):
                with gr.Row():
                    lum_out = gr.Image(label="preprocessed luminance (320×256 BBC aspect / 256×256 square)",
                                       height=300, image_mode="L")
                    cells_out = gr.Image(label="stored cells, upscaled (what BBC sees)",
                                         height=300, image_mode="L")
                stipple_out = gr.Image(label="R2 stipple output (320×256, BBC orientation, gx*5 stretch)",
                                       height=520, image_mode="L")
                info = gr.Markdown()

        controls = [picker, upload, fit, gamma, brightness, contrast, posterize,
                    black_point, white_point, size, levels, dots,
                    radius_preset, radius_text, dither, data_mode, script_sampling,
                    reject_radius, reject_hash, bbc_aspect]
        outputs = [lum_out, cells_out, stipple_out, info]

        for c in controls:
            if c is radius_preset:
                # preset selection fills the textbox, which itself triggers render
                c.change(apply_preset, [radius_preset, radius_text, levels], radius_text)
                continue
            evt = c.release if isinstance(c, gr.Slider) else c.change
            evt(render, controls, outputs)

        reset_btn.click(
            reset_defaults, [],
            [fit, gamma, brightness, contrast, posterize, black_point, white_point,
             size, levels, dots, radius_preset, radius_text, dither, data_mode,
             script_sampling, reject_radius, reject_hash, bbc_aspect],
        ).then(render, controls, outputs)

        demo.load(render, controls, outputs)

    return demo


if __name__ == "__main__":
    build_ui().launch(inbrowser=True, theme=gr.themes.Soft())
