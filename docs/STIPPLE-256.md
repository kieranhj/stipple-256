# 256-byte stipple intro (BBC Master) — design notes

Follow-on from `docs/STIPPLE.md`. Target: a stipple "photo" intro that fits the
**256-byte** sizecoding category on the **BBC Master** (MOS 3.x, GXR circle
PLOTs built in). MODE 0 (640×256, 1bpp), white background, black dots of
varying size, irregular placement, render in < 30 s.

The 4 KB MODE 4 player (`bbc/stipple.asm`) stays as the "big" version. This is a
separate, much smaller target with a different architecture.

> **Status:** shipped at **251 / 256 bytes** (5 spare). See
> [What got built](#what-got-built) for the final layout. The pre-build sketch
> below (architecture options, byte-budget estimate, "open questions") is
> preserved as a record of the design decisions; the as-built numbers differ.

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

- ~~`153` (&99) = filled circle absolute~~ — **wrong**. `153` is the *relative*
  filled-circle code; the absolute-fg code is **`157`** (= 152+5). PLOT codes
  are sub-classed by `k mod 8` and the mistake produced screen-filling discs
  before being caught. `145` (&91) = outline. Both are Master-only (GXR),
  which is why the Master is the target.
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

*(All of the below was done — preserved as a record of the original plan.)*

1. ✅ **`--mode256` preview in `tools/stipple.py`** — emulates the device
   algorithm (R2 placement, cell lookup, integer radii), prints byte-budget
   estimate, emits the tiny source bytes. Also exposed via `tools/stipple_ui.py`
   (Gradio).
2. ✅ Downsample / posterize / dot-count tuning done across the test images.
   `mona_list_square` chosen as default subject.
3. ✅ `bbc/stipple256.asm` written, assembled with BeebAsm, verified on jsbeeb
   Master.
4. ✅ PLOT codes confirmed: **`157`** (not 153) for absolute-foreground filled
   circle. See bug note in [What got built](#what-got-built).

## Open questions for the morning

- **Option 1 (procedural) vs 2 (tiny photo)?** → **Option 2** chosen and shipped.
- **Subject?** → `mona_list_square` (cropped Mona Lisa) is the default;
  `monarch` and `mandarin-duck-1` also read well at 16×16.
- **Placement: R2 vs LFSR?** → **R2** confirmed (see
  [STIPPLE-256-LFSR.md](STIPPLE-256-LFSR.md) — brute-forced LFSR seeds did not
  beat R2 within the budget).

## Status / caveats

- The 4 KB MODE 4 player in `bbc/stipple.asm` is unaffected and still the
  "full quality" path.

---

## What got built

A working **256-byte** Master intro: [`bbc/stipple256.asm`](../bbc/stipple256.asm)
plus the data file [`bbc/data/mona16.bin`](../bbc/data/mona16.bin) (16×16 @
2bpp = 64 B). Boots from `bbc/stipple256.ssd`. Verified end-to-end on jsbeeb
Master.

### Final layout (251 bytes of 256)

| section | bytes | what |
|---|---|---|
| code | 178 | R2 advance, image lookup, gx*5/gy*4 compute, MOVE+PLOT emit, loop ctrl |
| VDU init table | 9 | MODE 0 + palette setup + cursor-hide |
| image data | 64 | 16×16 @ 2bpp, LSB-first, darkness-encoded, vertically flipped |
| **total** | **251 / 256** | 5 bytes spare |

### Algorithm (matching the on-device implementation)

1. **VDU init** (sent in reverse from a 9-byte table via `dex/bpl`, -2 B vs
   forward):
   - `22, 0`  — MODE 0 (640×256, 1bpp). MOS VDU logical coords (0..1279 ×
     0..1023) are resolution-independent so dots stay round despite the 5:4
     physical aspect.
   - `17, 129` — text bg = logical 1 (white in default palette).
   - `12` — CLS (clears to bg = white).
   - `5` — VDU 5: text-at-graphics-cursor; suppresses the flashing text
     cursor (it must come *after* CLS — in VDU 5 mode CLS doesn't clear
     the way we want).
   - `18, 0, 0` — GCOL 0,0: plot in logical 0 (black).

2. **Placement** — R2 plastic-φ additive sequence in two 16-bit zero-page
   accumulators (`XINC = $C142`, `YINC = $91DF`). Read the high byte of each
   accumulator as `(px, py)` in 0..255. Cheap, no tables. The `ya` add is
   emitted *before* `xa` so the carry-out of `xa+1` lands in A ready for the
   image lookup — no reload.

3. **Cell lookup** — `cell_idx = (py & $F0) | (px >> 4)`; byte_offset =
   `cell_idx >> 2`; shift_count = `(cell_idx & 3) * 2`. Load source byte,
   shift right by shift_count, `AND #3` to extract the 2-bit cell. Source
   bytes are **LSB-first packed** so this needs no EOR/complement.

4. **Radius** — `r = cell * 2 - 1` via `asl A : sbc #0` (carry is clear after
   the asl since cell ≤ 3) → `{1, 3, 5}` pixel radii for cells 1..3; cell 0
   short-circuits earlier and skips the dot. `r` is held in X across `emit6`
   (no zero-page slot needed — OSWRCH preserves X). Graphics-unit radius is
   `r * 4`.

5. **Emit** — build a 6-byte buffer `[yH, yL, xH, xL, k, 25]` in zero page
   (reversed layout — `emit6` counts Y from 5 down to 0 with `dey/bpl`, saving
   2 B vs `iny/cpy #6/bne`). Twice per dot: `k=4` (MOVE absolute) at
   `(px*5, py*4)`, then `k=157` (filled circle absolute) at `(px*5 + r*4,
   py*4)`. The MOS computes the circle radius as the distance from the last
   MOVE to this perimeter point. **`px*5`** (instead of px*4) stretches the
   256-pixel dot field across MODE 0's full 640-pixel screen width; an
   offline 5:4 fit in the Python preprocessor compensates so the picture
   isn't horizontally squashed.

6. **Halt** — 16-bit zero-page counter (`$0800 = 2048` initial). Each
   iteration: `dec cnt_lo`; on roll, `dec cnt_hi`; when `cnt_hi` hits 0 the
   following `.hang beq hang` traps. The `.hang` label sits *on* the `beq`
   so a single instruction serves double duty (Z-set ⇒ infinite self-loop;
   Z-clear ⇒ falls through, no extra byte for a dedicated halt). The
   back-edge to `.loop` is `jmp` not `bne` — the loop body grew past the
   -128 branch limit when `gx*5` was added.

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
- **MODE 0 vs MODE 4.** Earlier builds used MODE 4 (320×256); the current
  build is MODE 0 (640×256) for smoother circles — the GXR circle is
  rasterised at the *physical* pixel grid, so doubling horizontal pixel
  density makes small circles look noticeably less blocky. MOS VDU
  coordinates are resolution-independent (0..1279 wide regardless of mode),
  so PLOT 157 produces physically round circles in both modes.
- **R2 banding.** At this density and aspect, the R2 step lands many
  consecutive dots at nearly the same y, producing visible horizontal
  bands of merged ovals. Stylistic feature at this scale — looks like
  scan-line halftone.

### Workflow

End-to-end via `bbc/build_256.sh` (converts image + assembles + size report):

```
cd bbc
./build_256.sh                                  # default: pics/mona_list_square.png
./build_256.sh face.png                         # use pics/face.png
./build_256.sh face.png --mode256-levels 8      # extra args pass through to stipple.py
./build_256.sh --run                            # also open the resulting .ssd
```

Manual path (preview/tune first, then drop the bin in):

```
# preview / choose a source (16x16 @ 2bpp)
python tools/stipple.py --mode256 pics/<image>.png --stem <stem> \
    --mode256-size 16 --mode256-levels 4 --mode256-dots 2048 \
    -o stipple_out -q

cp stipple_out/<stem>_mode256_src.bin bbc/data/mona16.bin
cd bbc && ../tools/beebasm.exe -i stipple256.asm -do stipple256.ssd -boot STIP256 -v
```

Interactive tuning lives in `tools/stipple_ui.py` (Gradio). The Python preview
is faithful enough to the on-device behaviour to pick a subject and tone-tune
(`--gamma`, `--contrast`, `--black-point`) without rebooting the emulator each
iteration.

### Subjects tested (2048 dots @ 16×16 @ 2bpp)

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
denser (24×24 = 144 B at 2 bpp) doesn't fit alongside the existing ~178 B of
code, and the cell-index trick `(py & $F0) | (px >> 4)` only works because the
grid is exactly 16 wide — a 24-wide grid needs a `row * 24` multiply (~10 extra
bytes) on top of the data overflow. So we either *make 16×16×4 read better* or
**change the architecture** so the same byte budget conveys more.

**1. Squeeze 16×16×4 harder (no code change).** ✅ *Largely done in tooling.*
   Aggressive tone prep, sharpen-before-downsample, and Floyd–Steinberg / blue-noise
   dither into the 4-level quantization are all available in
   `tools/stipple_ui.py` (see the dither + radius-preset controls). The remaining
   win is *hand-tuning the 64 B* per subject — still open per-image.

**2. Re-interpret the 64 B as a dot script.** ✅ *Prototyped in the UI.*
   `tools/stipple_ui.py` exposes a "dot-script" data mode that reads the bytes
   as `radius_for_iteration[i]` with bilinear sampling. Conclusion: the visual
   gain over cell-lookup was marginal at this resolution, and the offline
   complexity isn't worth shipping a second asm path. Kept as a UI option only.

**3. Two-pass multi-frequency.** Not pursued. ~6–10 B of code was the estimate,
   but the budget was spent elsewhere (gx*5 stretch, MODE 0 switch). Still on
   the table as a clean win if the radius preset ever changes.

**4. Brute-forced LFSR seed (no image data).** ✖ *Dismissed.* See
   [STIPPLE-256-LFSR.md](STIPPLE-256-LFSR.md): 300 k random seeds + coord-descent
   on three target images, R2 won 14 of 18 configurations and in the 4 LFSR
   wins the absolute MSE was worse than R2 in the better regime. No seed pair
   ports to asm.

### Ideas not pursued (and what changed)

- **PRNG-driven radii + brute-forced seed.** ✖ Dismissed — see (4) above and
  the LFSR doc. Confirmed structurally: 2048 dots × 3 bits ≈ 6 kbit of entropy;
  no 16/32-bit seed encodes a recognisable image.
- **~~px×5 instead of px×4~~** ✅ **Implemented.** `gx*5` is in the asm (cost:
  10 B net after `bne loop` had to become `jmp loop`), with offline 5:4 aspect
  compensation in `tools/stipple_ui.py` / `tools/stipple.py`.
- **~~4-byte radius LUT~~** ✅ **Implemented for free** via the `asl A : sbc #0`
  trick — gives `{1, 3, 5}` for cells {1, 2, 3} in 4 bytes of code, no LUT
  needed. The original "costs ~7 B" estimate assumed a table-based mapping.
- **LFSR placement** instead of R2. ✖ Dismissed — see LFSR doc. Confirmed
  worse than R2 visually *and* on MSE, despite being byte-comparable.
