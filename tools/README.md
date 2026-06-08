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
| `--black-point`/`--white-point` | 0/1 | clamp the tonal window before stippling. |
| `--iters` | 30 | Lloyd relaxation passes. |

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

## Status

Phase 1 (this tool) is functional. Next: lock the binary data format
(radius buckets, scanline-delta) and validate compressed size, then write the
retro plot code + depacker for the target machine.
