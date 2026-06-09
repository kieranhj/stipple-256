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

- The 4 KB MODE 4 player in `bbc/stipple.asm` is unaffected and still the
  "full quality" path.

---

## What got built

A working **256-byte** Master intro: [`bbc/stipple256.asm`](../bbc/stipple256.asm)
plus the data file [`bbc/data/mona16.bin`](../bbc/data/mona16.bin) (16×16 @
2bpp = 64 B). Boots from `bbc/stipple256.ssd`. Verified end-to-end on jsbeeb
Master.

### Final layout (255 bytes of 256)

| section | bytes | what |
|---|---|---|
| code | 183 | R2 advance, image lookup, gx/gy compute, MOVE+PLOT emit, loop ctrl |
| VDU init table | 9 | MODE 4 + palette setup + cursor-hide |
| image data | 64 | 16×16 @ 2bpp, LSB-first, darkness-encoded, vertically flipped |
| **total** | **255 / 256** | 1 byte spare |

### Algorithm (matching the on-device implementation)

1. **VDU init** (sent in reverse from a 9-byte table via `dex/bpl`, -2 B vs
   forward):
   - `22, 4`  — MODE 4 (320×256, 1bpp).
   - `17, 129` — text bg = logical 1 (white in default palette).
   - `12` — CLS (clears to bg = white).
   - `5` — VDU 5: text-at-graphics-cursor; suppresses the flashing text
     cursor (it must come *after* CLS — in VDU 5 mode CLS doesn't clear
     the way we want).
   - `18, 0, 0` — GCOL 0,0: plot in logical 0 (black).

2. **Placement** — R2 plastic-φ additive sequence in two 16-bit zero-page
   accumulators (`XINC = $C142`, `YINC = $91DF`). Read the high byte of each
   accumulator as `(px, py)` in 0..255. Cheap, no tables.

3. **Cell lookup** — `cell_idx = (py & $F0) | (px >> 4)`; byte_offset =
   `cell_idx >> 2`; shift_count = `(cell_idx & 3) * 2`. Load source byte,
   shift right by shift_count, `AND #3` to extract the 2-bit cell. Source
   bytes are **LSB-first packed** so this needs no EOR/complement.

4. **Radius** — `r = cell * 2` → `{0, 2, 4, 6}` pixel radii. `r == 0`
   skips the dot. Graphics-unit radius is `r * 4`.

5. **Emit** — build a 6-byte buffer `[25, k, xL, xH, yL, yH]` in zero page,
   `JSR emit6` (a small loop that walks the buffer through OSWRCH). Twice
   per dot: `k=4` (MOVE absolute) at `(px*4, py*4)`, then `k=157` (filled
   circle absolute) at `(px*4 + r*4, py*4)`. The MOS computes the circle
   radius as the distance from the last MOVE to this perimeter point.

6. **Halt** — 16-bit zero-page counter (`$0300 = 768` initial). Each
   iteration: if `cnt_lo == 0`, decrement `cnt_hi` and check `BMI hang`;
   otherwise decrement `cnt_lo`. (Using X as the counter doesn't work
   because the image lookup `tax`'s the byte_offset into X.)

### Subtleties that took byte-shuffling to get right

- **PLOT codes are sub-classed by k mod 8.** `153` (= 152+1) is *relative*
  filled circle — passing absolute coords to it makes the MOS add the
  graphics cursor twice, producing screen-filling discs. The correct
  absolute-foreground code is **`157`** (= 152+5). See the bug-hunt
  history in commit `8d2e74f`... err, the current commit.
- **Source orientation.** BBC graphics y=0 is at the *bottom* of the
  screen, image y=0 is at the *top*. The Python packer applies
  `np.flipud` so cell row 0 of the stored bytes corresponds to the
  bottom of the source picture.
- **Cell semantics.** Cells encode *darkness* (0=light → r=0 skip;
  L-1=dark → biggest dot), not luminance. The earlier luminance encoding
  drew the image as a photographic negative.
- **MODE 4 pixel aspect.** Pixels are roughly 1:2 (wide:tall) on a CRT,
  so a "circular" PLOT 157 displays as a tall oval. Each dot covers ~2×
  the area Python's preview disc predicts, which is why we settled on
  768 iterations with r ∈ {2,4,6} instead of the naive 1024+.
- **R2 banding.** At this density and aspect, the R2 step lands many
  consecutive dots at nearly the same y, producing visible horizontal
  bands of merged ovals. Stylistic feature at this scale — looks like
  scan-line halftone.

### Workflow

```
# preview / choose a source (16x16 @ 2bpp)
python tools/stipple.py --mode256 pics/<image>.png --stem <stem> \
    --mode256-size 16 --mode256-levels 4 --mode256-dots 768 \
    -o stipple_out -q

# copy the chosen source into the data dir
cp stipple_out/<stem>_mode256_src.bin bbc/data/mona16.bin

# assemble + run on jsbeeb (or beebem etc.)
cd bbc && ../tools/beebasm.exe -i stipple256.asm -do stipple256.ssd -boot STIP256 -v
```

The Python preview is faithful enough to the on-device behaviour to pick a
subject and tone-tune (`--gamma`, `--contrast`, `--black-point`) without
rebooting the emulator each iteration.

### Subjects tested (768 dots @ 16×16 @ 2bpp)

| subject | reads as |
|---|---|
| **monarch** | butterfly — wings out, light body in centre. Clearest subject. |
| **mandarin-duck-1** | duck silhouette in left half, water/sky on right. |
| **mona_lisa_crop** | abstract portrait — diagonal body + lighter face. Default. |
| baboon | face features, two darker eye regions. |
| lena | survives badly at 16×16 — almost pure noise. |

### Where to go from here

The current build is data-starved: 16×16 × 2 bpp = 64 cells × 4 darkness levels is
near the floor of what a recognisable photographic subject can survive. Going
denser (24×24 = 144 B at 2 bpp) doesn't fit alongside the existing ~191 B of
code, and the cell-index trick `(py & $F0) | (px >> 4)` only works because the
grid is exactly 16 wide — a 24-wide grid needs a `row * 24` multiply (~10 extra
bytes) on top of the data overflow. So we either *make 16×16×4 read better* or
**change the architecture** so the same byte budget conveys more.

**1. Squeeze 16×16×4 harder (no code change).** Subjects with bold silhouettes
   against a clean field beat portraits at this resolution; tight crops where
   the subject fills the frame are mandatory. Aggressive S-curve / `--gamma`
   / `--contrast`, sharpen-before-downsample, and Floyd–Steinberg / blue-noise
   dither into the 4-level quantization (instead of nearest-level rounding) all
   help; the resulting 64 B is small enough to *hand-tune* afterwards.

**2. Re-interpret the 64 B as a dot script.** Drop the cell lookup entirely
   and read the data as `radius_for_iteration[i]` — 256 dots × 2 bits = 64 B.
   R2 still supplies (x, y); the byte stream supplies the radius. Saves ~12 B
   of cell-lookup code. The radius of any given dot is locked to its iteration
   index rather than its screen position, so the *offline* packer has to sort
   iterations such that the i-th R2 sample lands where the desired darkness
   wants its i-th biggest dot. More authoring power per byte, at the cost of
   off-device complexity.

**3. Two-pass multi-frequency.** Same 64 B, but render in two passes with
   different radius mappings (or different iteration subsets) — pass 1 places
   only the largest dots for low-frequency structure, pass 2 fills mid-tones.
   ~6–10 B of code. Better perceived structure at the same data rate.

**4. Brute-forced LFSR seed (no image data).** Throw the 64 B of source out
   entirely; ship a 2-byte seed + a fixed reconstruction rule, brute-forced
   offline to maximise SSIM against a target. Frees ~60 B for code/iterations.
   Realistically won't match a *specific* photo well, but with millions of
   candidate seeds scored against the target it can produce an "abstract
   face-like blob" that reads better than random. Pure size-coding flex.

The likely best combination on a single sitting is **(1) + (2)**: get a
bold-silhouette subject working great on the current engine first to validate
the preprocessing pipeline, then prototype the per-iteration-radius script —
same data budget but with full offline control over dot order, which is where
blue-noise / structured-dither wins really live.

### Ideas not pursued

- **PRNG-driven radii + brute-forced seed.** 768 dots × 3 bits of radius =
  ~2300 bits of entropy. No 16/24/32-bit seed encodes a recognisable
  image; you only get pleasing accidents. Useful for *abstract* intros.
- **px×5 instead of px×4** to remove the ~25% right-edge letterbox. Costs
  ~6 B; we don't have it.
- **4-byte radius LUT** for non-linear cell→radius mapping (e.g. r ∈
  {0,1,3,5}). Costs ~7 B; doesn't fit.
- **LFSR placement** instead of R2. Marginally smaller code, no
  horizontal banding, but cycles through 65535 distinct positions in
  pseudo-random order — visually noisier than R2.
