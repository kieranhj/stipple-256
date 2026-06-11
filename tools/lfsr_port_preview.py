#!/usr/bin/env python3
"""Side-by-side preview of R2 vs a specific LFSR seed at the as-shipped
asm pipeline parameters (MODE 0, 320x256 preview, gx*5 stretch, radii
{0,1,3,5}, 2048 iters, 5:4 aspect-correct preprocess).

The brute force in lfsr_brute.py rendered at the *old* asm pipeline
(MODE 4, radii {0,2,4,6}, no gx*5). This script re-renders the most
interesting LFSR find — seed (58372, 12530) on eye.png — under the
current pipeline to see if the diagonal-weave aesthetic survives.

Usage:
    python tools/lfsr_port_preview.py
    python tools/lfsr_port_preview.py --image pics/face.png --seed 58372 12530
"""
import argparse
from pathlib import Path

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parent.parent

XINC = 0xC142
YINC = 0x91DF
LFSR_TAP = 0xB400  # high-byte tap 0xB4, max-length

ITERS = 2048
RADIUS_LUT = (0, 1, 3, 5)  # cell value -> pixel radius
CELL_SIZE = 16             # 16x16 cells
LEVELS = 4                 # 4 darkness levels (2 bpp)
PREVIEW_W = 320            # MODE 0 in physical 5:4 aspect terms
PREVIEW_H = 256
LEGACY = [False]           # mutated by --legacy


def r2_sequence(n):
    xacc, yacc = 0, 0
    xs = np.empty(n, dtype=np.uint8)
    ys = np.empty(n, dtype=np.uint8)
    for i in range(n):
        xacc = (xacc + XINC) & 0xFFFF
        yacc = (yacc + YINC) & 0xFFFF
        xs[i] = xacc >> 8
        ys[i] = yacc >> 8
    return xs, ys


def build_lfsr_cycle(tap=LFSR_TAP):
    """Return the full 65535-long hi-byte cycle of the Galois LFSR (state=1 start)."""
    cyc = np.empty(65535, dtype=np.uint8)
    s = 1
    for i in range(65535):
        cyc[i] = s >> 8
        out = s & 1
        s >>= 1
        if out:
            s ^= tap
    return cyc


def lfsr_sequence(n, x_off, y_off, cycle=None):
    """One shared LFSR cycle read at two phase offsets.

    This is how the brute force works (not two independent LFSRs!): a single
    65535-long sequence of hi-bytes is sampled with x_off and y_off as start
    indices, so x[i] = cycle[(x_off + i) % L], y[i] = cycle[(y_off + i) % L].
    The phase difference (x_off - y_off) determines the 2D path shape — this
    is what produces the diagonal-weave pattern.
    """
    if cycle is None:
        cycle = build_lfsr_cycle()
    L = len(cycle)
    idx = np.arange(n)
    xs = cycle[(x_off + idx) % L]
    ys = cycle[(y_off + idx) % L]
    return xs, ys


def load_darkness(img_path):
    """Legacy path: square 256x256 fit (no aspect correction)."""
    img = Image.open(img_path)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img = img.convert("L").resize((256, 256), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.float64) / 255.0
    return 1.0 - arr


def load_darkness_5_4(img_path):
    """Load and fit to 256x256 with 5:4 aspect-correct preprocess.

    The asm reads cells at integer 16x16 grid positions but plots with gx*5
    so the on-screen picture spans the full MODE 0 width. To make the dot
    pattern proportional, we must SQUASH the source horizontally by 4/5
    before downsampling to 16x16. Equivalent: fit to 320x256 then bin to
    16x16 (each cell averages 20x16 source pixels, not 16x16).
    """
    img = Image.open(img_path)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img = img.convert("L")
    img = img.resize((PREVIEW_W, PREVIEW_H), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.float64) / 255.0
    return 1.0 - arr  # darkness


def build_cells(dark):
    """Downsample dark map to CELL_SIZE x CELL_SIZE and quantise to LEVELS.

    Returns an int8 array, value 0..LEVELS-1 where 0=light, LEVELS-1=dark
    (i.e. r=0 skip, r=5 biggest).
    """
    H, W = dark.shape
    by = H // CELL_SIZE
    bx = W // CELL_SIZE
    binned = dark[:by * CELL_SIZE, :bx * CELL_SIZE].reshape(
        CELL_SIZE, by, CELL_SIZE, bx).mean(axis=(1, 3))
    q = np.clip(np.round(binned * (LEVELS - 1)), 0, LEVELS - 1).astype(np.int32)
    return q


def disc_mask(r):
    if r <= 0:
        return np.ones((1, 1), dtype=bool)
    yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
    return (xx * xx + yy * yy) <= r * r + r


def stamp(xs, ys, radii, W, H):
    max_r = int(radii.max()) if len(radii) else 0
    masks = {r: disc_mask(r) for r in range(max_r + 1)}
    ink = np.zeros((H, W), dtype=bool)
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


def render(xs, ys, cells):
    """Apply the on-device algorithm: cell lookup -> radius -> stamp with gx*5."""
    # BBC y=0 at bottom; the asm reads cell from py (untransformed) but plots
    # at flipped y. Mirror for preview to match emulator orientation.
    ys_flipped = (255 - ys.astype(np.int32)).astype(np.uint8)
    iy = (ys_flipped.astype(np.int32) * CELL_SIZE) // 256
    ix = (xs.astype(np.int32) * CELL_SIZE) // 256
    cell_vals = cells[iy, ix]
    lut = np.asarray(RADIUS_LUT, dtype=np.int32)
    radii = lut[np.clip(cell_vals, 0, len(lut) - 1)]
    if LEGACY[0]:
        # old asm: no gx stretch; render at 256x256 (matches brute force output).
        return stamp(xs, ys_flipped, radii, 256, 256)
    gx_pixels = (xs.astype(np.int32) * 5) // 4
    return stamp(gx_pixels, ys_flipped, radii, PREVIEW_W, PREVIEW_H)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=str(REPO / "pics/eye.png"))
    ap.add_argument("--seed", nargs=2, type=int, default=[58372, 12530],
                    metavar=("XOFF", "YOFF"),
                    help="phase offsets into the shared 65535-long LFSR cycle. "
                         "(Brute force calls these 'seeds' but they're "
                         "actually offsets — see lfsr_sequence().)")
    ap.add_argument("--out", default=str(REPO / "out_lfsr_port"))
    ap.add_argument("--legacy", action="store_true",
                    help="use old asm params (radii {0,2,4,6}, no gx*5) for "
                         "a direct comparison with the brute-force-era output.")
    args = ap.parse_args()

    if args.legacy:
        global RADIUS_LUT
        RADIUS_LUT = (0, 2, 4, 6)
        LEGACY[0] = True

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(args.image).stem

    print(f">> loading {args.image}  (legacy={args.legacy})")
    dark = load_darkness(args.image) if args.legacy else load_darkness_5_4(args.image)
    cells = build_cells(dark)
    print(f"   cells (16x16, 0=light, {LEVELS-1}=dark):")
    for row in cells:
        print("     " + " ".join(str(v) for v in row))

    print(f">> rendering R2 (XINC={XINC:04X}, YINC={YINC:04X}) x {ITERS} iters")
    r2_xs, r2_ys = r2_sequence(ITERS)
    r2_ink, r2_plot = render(r2_xs, r2_ys, cells)
    print(f"   plotted {r2_plot} / {ITERS} dots (rest were cell=0 skips)")

    xoff, yoff = args.seed
    print(f">> rendering LFSR (tap {LFSR_TAP:04X}, offsets x={xoff}, y={yoff},"
          f" phase diff {(xoff - yoff) % 65535}) x {ITERS} iters")
    lfsr_xs, lfsr_ys = lfsr_sequence(ITERS, xoff, yoff)
    lfsr_ink, lfsr_plot = render(lfsr_xs, lfsr_ys, cells)
    print(f"   plotted {lfsr_plot} / {ITERS} dots")

    def to_img(ink):
        return np.where(ink, 0, 255).astype(np.uint8)

    r2_img = to_img(r2_ink)
    lfsr_img = to_img(lfsr_ink)
    H, W = r2_img.shape
    pad = 8
    comp = np.full((H, 2 * W + pad), 255, dtype=np.uint8)
    comp[:, :W] = r2_img
    comp[:, W + pad:] = lfsr_img

    Image.fromarray(r2_img, "L").save(out_dir / f"{stem}_r2.png")
    Image.fromarray(lfsr_img, "L").save(out_dir / f"{stem}_lfsr.png")
    Image.fromarray(comp, "L").save(out_dir / f"{stem}_compare.png")
    print(f">> wrote {out_dir}/{stem}_r2.png")
    print(f">> wrote {out_dir}/{stem}_lfsr.png")
    print(f">> wrote {out_dir}/{stem}_compare.png  (R2 | LFSR)")


if __name__ == "__main__":
    main()
