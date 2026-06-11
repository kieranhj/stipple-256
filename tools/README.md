# tools/stipple.py — photo → variable-size stipple data

Host-side (Python) converter + visualiser for the 4KB stipple-graphics
experiment. It turns a photo into an irregular set of black dots on white,
where each dot's **radius encodes local darkness** (hybrid size-led
stippling), renders previews to tune the look by eye, and reports how well
the `(x, y, radius)` data compresses against the 4KB budget.

The retro *plot* code (precomputed disc sprites + bucketed blit loop +
matching depacker) is a separate, later artifact — this tool only produces
and validates the data.

## Setup

```
pip install numpy scipy pillow
```

## Usage

```
# real photo, defaults (320x256, 2200 dots, 8 radius levels, size-led)
python3 tools/stipple.py photo.jpg -o stipple_out

# kick the tyres with no asset
python3 tools/stipple.py --make-test-image /tmp/test.png -o /tmp/out
```

Outputs in `<outdir>/`: `<stem>_stipple.png` (1x), `_stipple_3x.png`,
`_compare.png` (source | stipple), `_dots.csv` (x,y,r list). The run also
prints a radius histogram, coverage vs. target tone, a tonal-error figure,
and compressed-size estimates (naive vs. bucketed+delta, zlib & lzma).

## Key knobs

| flag | default | effect |
|------|---------|--------|
| `-n/--dots` | 2200 | dot count. Fewer dots ⇒ bigger dots, wider radius range, smaller data, coarser highlights. |
| `--density-exponent` | 0.0 | placement weighting. **0 = even spacing, tone carried by SIZE** (size-led). Higher clusters dots in shadows, toward classic Secord stippling (size becomes constant). |
| `-l/--levels` | 8 | discrete radius levels (8 ⇒ 3 bits). |
| `--radius-scale` | 1.0 | global dot-size multiplier; >1 increases overlap so darkest tones go solid black. |
| `--gamma` | 1.0 | darkness gamma; >1 lightens midtones. |
| `--brightness` | 0.0 | additive luminance lift, −1..1. |
| `--contrast` | 1.0 | contrast about mid-grey; >1 punchier. |
| `--posterize` | 0 | quantize luminance to N bands (0=off; try 4–6). |
| `--black-point`/`--white-point` | 0/1 | clamp the tonal window before stippling. |
| `--fit` | cover | `cover`=fill+crop, `contain`=whole image letterboxed (portraits). |
| `--iters` | 30 | Lloyd relaxation passes. |
| `--bbc` | off | also export the BBC delta stream and ZX02-compress it (see below). |

## BBC Micro export

`--bbc` writes `<stem>.bbc.bin` (the raw delta stream) and, if a `zx02`
compressor is found, `<stem>.bbc.zx02` (what ships on disc), self-checking
both round-trips. Build zx02 from `github.com/dmsc/zx02` and pass `--zx02 PATH`.

```
python3 tools/stipple.py photo.png -o out --fit contain --gamma 1.4 -n 1700 \
    --bbc --zx02 /path/to/zx02
python3 tools/verify_bbc.py out/photo.bbc.bin   # prove the BBC plotter logic
```

`verify_bbc.py` replays the stream through the exact MODE 4 address arithmetic
used by `bbc/stipple.asm` and diffs against the renderer (expects 0 diffs).

See **`docs/STIPPLE.md`** for the data format, the 6502 player, and status.

## How it works

1. Fit image to canvas (centre crop), grayscale, tone prep → `darkness ∈ [0,1]`.
2. Seed N points by darkness-weighted rejection sampling.
3. Lloyd relaxation (weighted centroidal Voronoi) with weight
   `darkness**density_exponent`.
4. Radius per dot from the **tone mass** of its Voronoi cell
   (`r = sqrt(Σ darkness / π) · radius_scale`) — this conserves total ink, so
   stipple coverage automatically tracks image tone. Quantize to `levels`
   integer radii; radius-0 dots (highlights) drop out.
5. Render with the same integer-radius disc masks the retro target will use,
   so the preview is faithful.

The radius formula degrades gracefully across `--density-exponent`: at 0 the
cells are equal-area so radius ∝ √darkness (size-led); raise it and cells
shrink in shadows so radius trends constant (density-led / classic stipple).

## `--mode256` (256-byte intro source generator)

`stipple.py --mode256 <img>` produces the 64-byte 16×16 @ 2bpp source that
`bbc/stipple256.asm` reads via INCBIN, plus a preview PNG of how it'll look
on-device. Knobs: `--mode256-size N`, `--mode256-levels L`, `--mode256-dots N`.
End-to-end (convert + assemble + size report) lives in `bbc/build_256.sh`.

`stipple_ui.py` is a Gradio app for interactive tuning of radius mappings,
dither, dot-script-vs-cell-lookup data modes, and BBC aspect / `gx*5` stretch
preview. Run with `python tools/stipple_ui.py`.

See [`docs/STIPPLE-256.md`](../docs/STIPPLE-256.md) for the 256-byte design
notes and [`docs/STIPPLE-256-LFSR.md`](../docs/STIPPLE-256-LFSR.md) for the
LFSR-vs-R2 brute-force investigation.

## Status

- **4 KB MODE 4 player tooling** (this script + `verify_bbc.py`) — functional;
  data format locked; 6502 player assembles but is unverified on emulator
  (see `docs/STIPPLE.md`).
- **256-byte MODE 0 intro tooling** (`--mode256`, `stipple_ui.py`,
  `lfsr_brute.py`) — shipped; 6502 intro verified on jsbeeb at 251/256 bytes.
