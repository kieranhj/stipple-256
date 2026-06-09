# 256-byte stipple intro (BBC Master) — design notes

Follow-on from `docs/STIPPLE.md`. Target: a stipple "photo" intro that fits the
**256-byte** sizecoding category on the **BBC Master** (MOS 3.x, GXR circle
PLOTs built in). MODE 4 (320×256, 1bpp), white background, black dots of
varying size, irregular placement, render in < 30 s.

The 4 KB MODE 4 player (`bbc/stipple.asm`) stays as the "big" version. This is a
separate, much smaller target with a different architecture.

---

## The core realization

At 256 bytes you **cannot store dot coordinates**. Our most-compressed image was
~2 KB of near-entropy delta data — already close to the information floor, so no
packer reaches 256. The architecture has to flip:

> **Stop storing positions. Synthesize them on the device each frame, and sample
> a tiny (or zero-byte) brightness source to choose each dot's size.**

The irregular placement we wanted now comes *for free* from the generator. This
is the single change that takes us from 4 KB to 256 B.

A faithful *arbitrary* photo at 256 B is physically impossible (the information
isn't there). The realistic sweet spots are a **procedural subject** (most
impressive, ~zero data) or a **brutally downsampled stored image** re-organicized
by the stipple (keeps the "photo" feel).

---

## Plot code — use the Master's GXR circle PLOTs (no plotter code at all)

A filled dot is two VDU PLOT bursts; MOS does the span-fill:

```
VDU 25, 4,   cx; cy;        ; k=4   MOVE absolute to centre
VDU 25, 153, cx+r; cy;      ; k=153 FILLED circle, radius = distance to point
```

- `153` (&99) = filled circle absolute; `145` (&91) = outline. **Verify these
  exact codes in the emulator** — from memory they're right, and they are
  Master-only (GXR), which is why the Master is the target.
- Coordinates are graphics units: x 0..1279, y 0..1023, 16-bit little-endian in
  the VDU stream. From a pixel `(px,py)`: `gx = px*4`, `gy = (255-py)*4` (or skip
  the flip — a vertically mirrored stipple is fine). Radius `R = r_pixels * 4`.
- Dots are generated in a loop, so the VDU bytes are emitted by code, not
  stored. Cost is runtime (we have 30 s), not size.

### White background / black dots — ~8 bytes of init

```
VDU 22,4        ; MODE 4
VDU 17,135      ; text background = white (128+7)
VDU 12          ; clear screen to white
VDU 18,0,0      ; GCOL 0,0 -> plot colour black
```

Emit via a small data table + an `OSWRCH` (&FFEE) loop.

---

## Placement — R2 low-discrepancy sequence (~12 bytes, no tables, no multiply)

The **R2 additive sequence** gives blue-noise-ish even coverage from two 16-bit
accumulators:

```
xacc += XINC ; yacc += YINC      ; each iteration
x = hi(xacc) ; y = hi(yacc)
```

with `XINC = round(0.7548776662 * 65536)`, `YINC = round(0.5698402909 * 65536)`
(plane-filling phi constants; tune to taste). Read the high bytes as the
coordinate. Alternative: an LFSR over a cell index for guaranteed no-repeat full
coverage — similar size. The number of dots is just a loop counter, bounded by
time/overdraw, not by size.

---

## The three options (by how "photo" you want it)

**1. Procedural source — recommended for the compo. ~100–150 B, no data.**
`brightness = f(x,y)` from a formula (shaded sphere/torus, simple SDF face,
plasma, metaballs, Mandelbrot); `radius = (255 - brightness) >> 5` (→ 0..7,
0 = skip). Best wow-per-byte; the stipple+circle look carries it. Not a photo.

**2. Tiny stored source — keeps the "photo" feel. ~140–200 B.**
Store a brutally downsampled, posterized image — e.g. **24×24 @ 2 bpp ≈ 144 B**
(optionally RLE/zx02 to fit 32×32). On device: R2 placement → nearest-cell
lookup → size the dot by that cell's level. The stipple re-organicizes the
blocky source so a 24×24 face reads as a portrait. Most on-theme with the
project; leaves ~100 B for code.

**3. Hybrid silhouette — ~80–160 B.**
Store a 1-bpp mask (very RLE-friendly) that *gates* where dots go; size them
from a procedural gradient. Cheap data, strong subject recognition.

### Rough byte budget (procedural, option 1)

| part | bytes |
|------|-------|
| VDU init (table + OSWRCH loop) | ~16 |
| R2 placement | ~14 |
| brightness eval (formula) | ~20–40 |
| radius map | ~6 |
| emit MOVE + filled-circle VDU | ~30–50 |
| loop control | ~10 |
| **total** | **~100–150** |

Option 2 swaps the brightness formula for a table lookup (~15 B) + the stored
image (64–150 B).

---

## Radius / tone mapping

White bg, black dots, darker = bigger. `darkness = 255 - brightness`,
`r = darkness >> 5` (0..7), skip `r == 0`. The GXR circle codes give us any
radius free, so we are not limited to a sprite set as in the 4 KB version. Total
ink still roughly tracks tone because more+bigger dots land in dark regions.

---

## Recommended next steps (command-line workflow)

1. **Add a `--mode256` preview to `tools/stipple.py`** that emulates the exact
   device algorithm — R2 (or LFSR) placement, sample either a procedural
   function (`--procedural NAME`) or a downsampled+posterized source (option 2),
   round radius to the integer dot sizes — and renders the result + prints a
   byte-budget estimate (code guess + data size). This lets us tune the look on
   the laptop before writing any 6502.
   - For option 2 it should also emit the tiny source bytes (and an RLE/zx02
     size) so we know the data cost exactly.
2. Tune downsample resolution / posterize levels / dot count against the test
   images (lena and mona_lisa tend to survive extreme downsampling best; parrot
   is the running example).
3. Write the ~150-byte 6502 (`bbc/stipple256.asm`), assemble with BeebAsm, and
   verify in an emulator. Reuse the OSWRCH/VDU init approach from
   `bbc/stipple.asm`.
4. Confirm the filled/outline circle PLOT codes (153 / 145) and the graphics-unit
   scaling on a real Master / emulator.

## Open questions for the morning

- **Option 1 (procedural) or 2 (tiny photo)?** Lean: build the preview for
  option 2 first (keeps the photo concept), with a `--procedural` switch to also
  try option 1.
- **Subject**: keep the parrot, or use a portrait (lena / mona_lisa) that
  downsamples better?
- Placement: R2 sequence (simplest, blue-noise-ish) vs LFSR permutation
  (guaranteed coverage). R2 is the default recommendation.

## Status / caveats

- Nothing for the 256 B target is built yet — this is the plan only.
- PLOT code numbers and graphics-unit scaling are from memory; verify on the
  Master/emulator before relying on them.
- The 4 KB MODE 4 player in `bbc/` is unaffected and still the "full quality"
  path.
