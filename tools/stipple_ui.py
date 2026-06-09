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
                black_point, white_point):
    """Run stipple.load_darkness equivalent on a PIL.Image; return (dark, lum_preview)."""
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
    img = st._fit(img, 256, 256, fit)
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
    "cell×2 (current BBC: 0,2,4,6)": [0, 2, 4, 6],
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


def _render_bbc(cells, size, levels, dots, radius_lut):
    """Faithful preview of the on-device algorithm with an explicit radius LUT.

    Takes pre-quantised cells (luminance encoding, 0=dark .. L-1=light) so the
    same dithered grid drives both the cells preview and this render.
    """
    darkness_grid = (levels - 1) - cells       # 0=light, levels-1=dark

    xs, ys = st.r2_sequence(dots)
    iy = (ys.astype(np.int32) * size) // 256
    ix = (xs.astype(np.int32) * size) // 256
    cell_vals = darkness_grid[iy, ix]

    lut = np.asarray(radius_lut, dtype=np.int32)
    radii = lut[np.clip(cell_vals, 0, len(lut) - 1)]

    W = H = 256
    max_r = int(radii.max()) if len(radii) else 0
    ink = np.zeros((H, W), dtype=bool)
    masks = {r: st.disc_mask(r) for r in range(max_r + 1)}
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
    return ink, radii, plotted


def render(picker, upload, fit, gamma, brightness, contrast, posterize,
           black_point, white_point, size, levels, dots,
           radius_preset, radius_text, dither):
    img = _resolve_image(picker, upload)
    if img is None:
        blank = np.full((256, 256), 255, dtype=np.uint8)
        return blank, blank, blank, "Pick or upload an image."

    dark, lum_preview = _preprocess(
        img, fit, gamma, brightness, contrast, posterize, black_point, white_point)

    size = int(size)
    levels = int(levels)
    dots = int(dots)

    cells = _downsample_dither(dark, size, levels, dither)
    bits_per = max(1, int(np.ceil(np.log2(levels))))

    cells_dev = np.flipud((levels - 1) - cells)
    src_bytes = st.pack_bits(cells_dev.ravel(), bits_per)

    cell_lum = (cells.astype(np.float64) / max(levels - 1, 1) * 255.0).astype(np.uint8)
    cells_img = _cells_preview(cell_lum)

    lut = _parse_lut(radius_text, levels)
    ink, radii, plotted = _render_bbc(cells, size, levels, dots, lut)
    stipple_img = np.where(ink, 0, 255).astype(np.uint8)

    raw_bytes = len(src_bytes)
    code_est = 191  # current asm reality (see docs/STIPPLE-256.md)
    total = code_est + raw_bytes
    over = total - 256

    max_r = max(7, int(max(lut)) if lut else 7)
    rh = np.bincount(np.clip(radii, 0, max_r), minlength=max_r + 1)
    hist = "  ".join(f"r{r}:{rh[r]}" for r in range(max_r + 1))

    lut_str = ",".join(str(v) for v in lut)
    info = (
        f"**Source**: {size}×{size} @ {bits_per}bpp = {raw_bytes} B raw "
        f"({dither} dither)  \n"
        f"**Radius LUT**: cell→radius = [{lut_str}]  \n"
        f"**Dots**: {dots} attempted, {plotted} plotted (r>0)  \n"
        f"**Radius histogram**: {hist}  \n"
        f"**Byte budget**: code≈{code_est} + data={raw_bytes} = **{total}** "
        f"({'OK, ' + str(256 - total) + ' B spare' if over <= 0 else 'OVER by ' + str(over) + ' B'})"
    )
    return lum_preview, cells_img, stipple_img, info


def reset_defaults():
    return ("cover", 1.0, 0.0, 1.0, 0, 0.0, 1.0, 16, 4, 768,
            "cell×2 (current BBC: 0,2,4,6)", "0,2,4,6", "none")


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
                gr.Markdown("### Preprocessing")
                gamma = gr.Slider(0.2, 4.0, value=1.0, step=0.05, label="gamma (>1 lightens midtones)")
                brightness = gr.Slider(-0.5, 0.5, value=0.0, step=0.01, label="brightness")
                contrast = gr.Slider(0.2, 3.0, value=1.0, step=0.05, label="contrast")
                posterize = gr.Slider(0, 8, value=0, step=1, label="posterize (0 = off)")
                black_point = gr.Slider(0.0, 1.0, value=0.0, step=0.01, label="black point")
                white_point = gr.Slider(0.0, 1.0, value=1.0, step=0.01, label="white point")
                gr.Markdown("### Device parameters")
                size = gr.Slider(8, 32, value=16, step=4, label="grid size (NxN). 16 is the size-coded target.")
                levels = gr.Slider(2, 8, value=4, step=1, label="levels (4 = 2 bpp)")
                dots = gr.Slider(64, 2048, value=768, step=32, label="R2 dot iterations")
                gr.Markdown("### Radius mapping (cell darkness → pixel radius)")
                radius_preset = gr.Radio(
                    list(RADIUS_PRESETS.keys()),
                    value="cell×2 (current BBC: 0,2,4,6)",
                    label="preset (picks one fills the textbox)",
                )
                radius_text = gr.Textbox(
                    value="0,2,4,6",
                    label="LUT (cell 0 → cell L-1, comma-separated)",
                    info="Length should match levels. r=0 skips the dot.",
                )
                gr.Markdown("### Dither (into the 4-level quantisation)")
                dither = gr.Radio(
                    DITHER_MODES, value="none", label="dither mode",
                    info="F-S diffuses quantisation error; ordered adds a Bayer threshold pattern.",
                )
                reset_btn = gr.Button("Reset defaults")
            with gr.Column(scale=2):
                with gr.Row():
                    lum_out = gr.Image(label="preprocessed luminance (256×256)",
                                       height=300, image_mode="L")
                    cells_out = gr.Image(label="stored cells, upscaled (what BBC sees)",
                                         height=300, image_mode="L")
                stipple_out = gr.Image(label="R2 stipple output (256×256, BBC orientation)",
                                       height=520, image_mode="L")
                info = gr.Markdown()

        controls = [picker, upload, fit, gamma, brightness, contrast, posterize,
                    black_point, white_point, size, levels, dots,
                    radius_preset, radius_text, dither]
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
             size, levels, dots, radius_preset, radius_text, dither],
        ).then(render, controls, outputs)

        demo.load(render, controls, outputs)

    return demo


if __name__ == "__main__":
    build_ui().launch(inbrowser=True, theme=gr.themes.Soft())
