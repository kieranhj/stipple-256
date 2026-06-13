# Stipple graphics for retro hardware — progress & plan

> **Historical note (2026-06-11):** this document describes the **4 KB MODE 4
> player** (`bbc/stipple.asm`) — the original target. Active development
> shifted to the **256-byte MODE 0 intro** (`bbc/stipple256.asm`); see
> [STIPPLE-256.md](STIPPLE-256.md) and
> [STIPPLE-256-LFSR.md](STIPPLE-256-LFSR.md). The 4 KB player still assembles
> but **has not been run on an emulator** — item 1 of the "Next steps" list
> below remains the open task.

Status as of this session. Goal: turn a photo into an irregular pattern of
black dots of varying size on a white background, encode the dots as a small,
well-compressing `(x, y, radius)` list, and plot them on a BBC Micro in MODE 4
(320×256, 1bpp) in well under 30 seconds.

**TL;DR — where we are:**

- ✅ **Phase 1** host tool (`tools/stipple.py`): photo → variable-size stipple,
  with previews and tuning knobs (incl. gamma/brightness/contrast/posterize).
  Tested on 12 images.
- ✅ **Phase 2** data format locked, packer implemented, ZX02 compression wired
  and round-trip verified. ~2 KB compressed per image — comfortably small.
- ✅ **Phase 3** BBC Micro 6502 code (`bbc/`): MODE 4 player with runtime
  address tables, ZX02 depacker, and a dot plotter. **Assembles clean with
  BeebAsm.** The MODE 4 addressing + plotting logic is proven correct against
  the renderer (0-pixel-diff, see Validation). **Not yet run on an
  emulator/hardware** — that's the top of the next-steps list.

---

## 1. The approach

Variable-dot stippling ("irregular halftoning"). Tone is carried mostly by dot
**size**; placement is irregular (no visible grid). Pipeline:

1. Fit image to 320×256, grayscale, tone preprocessing → `darkness ∈ [0,1]`.
2. Seed N points by darkness-weighted sampling.
3. **Lloyd relaxation** (weighted centroidal Voronoi) for organic, grid-free
   placement. Placement weight is `darkness ** density_exponent`:
   `0` = even spacing (size-led, default) … higher = points cluster in
   shadows (toward classic Secord stippling).
4. **Radius from cell tone-mass** (ink conservation):
   `r = sqrt(Σ darkness over the dot's Voronoi cell / π) · radius_scale`,
   quantized to 8 integer levels (3 bits). Radius-0 dots (highlights) drop out.
   This single formula spans the whole look: at `density_exponent 0` cells are
   equal-area so `r ∝ √darkness` (size-led); raise it and cells shrink in
   shadows so `r` trends constant (density-led). Total ink automatically tracks
   image tone, so brightness comes out right with no manual balancing.

---

## 2. Phase 1 — the converter/visualiser  (`tools/stipple.py`)

Host-side Python (numpy / scipy / Pillow). Renders previews using the *same*
integer-radius disc masks the BBC uses, so the preview is faithful.

```
python3 tools/stipple.py photo.png -o out --fit contain --gamma 1.4 -n 1700
```

Outputs in `out/`: `<stem>_stipple.png` (1×), `_stipple_3x.png`, `_compare.png`
(source | stipple), `_dots.csv`. Prints a radius histogram, coverage vs target
tone, a tonal-error figure, and compressed-size estimates.

### Knobs

| flag | default | effect |
|------|---------|--------|
| `-n/--dots` | 2200 | dot count. Fewer ⇒ bigger dots, wider radius range, smaller data, coarser highlights. |
| `--density-exponent` | 0.0 | 0 = even spacing / size-led; higher clusters dots in shadows. |
| `-l/--levels` | 8 | discrete radius levels (3 bits). |
| `--radius-scale` | 1.0 | global dot-size multiplier (>1 ⇒ darks merge to solid black). |
| `--gamma` | 1.0 | darkness gamma (>1 lightens midtones). |
| `--brightness` | 0.0 | additive luminance lift (−1..1). |
| `--contrast` | 1.0 | contrast about mid-grey. |
| `--posterize` | 0 | quantize luminance to N bands (0=off; try 4–6 for a poster look). |
| `--black-point`/`--white-point` | 0/1 | input tonal-window clamp. |
| `--fit` | cover | `cover` = fill+crop; `contain` = whole image letterboxed (use for portraits). |
| `--bbc` | off | also write the BBC delta-stream + ZX02-compress it (needs `--zx02 PATH`). |

### Results (uniform settings, `--fit contain --gamma 1.3 -n 2000`)

All 12 `image2mode1` test images convert recognisably. Dot counts 1262–2000,
ZX02-compressed data 2.3–3.2 KB, tonal error 0.04–0.07. (Busy/dark images want
a touch more `--brightness`/`--gamma` per image.) The parrot tuned to ~1 KB of
dots (1056) at 2.1 KB compressed.

---

## 3. Phase 2 — the data format

Bucketed by radius, scanline-sorted, delta-x encoded; the whole stream is then
ZX02-compressed for disc and depacked into RAM at boot.

### Stream layout (decompressed in RAM)

```
nbuckets : 1 byte
repeat nbuckets:
    radius : 1 byte                 (1..7)
    nlines : 1 byte                 (scanlines with >=1 dot of this radius; 1..255)
    repeat nlines:
        y  : 1 byte                 (absolute, 0..255, ascending)
        n  : 1 byte                 (dots on this line, 1..255)
        repeat n:
            dx : delta-x from the previous dot on the line (prevx resets to 0
                 each line). Escape coding: 0xFF means "add 255 and read the
                 next byte too"; a byte < 255 ends the value. So the real
                 delta = sum of bytes read until one is < 255.
```

Notes / rationale:

- `x` spans 0..319 (>8 bits); delta+escape keeps everything byte-sized and very
  compressible while staying trivial to decode on a 6502 (a running 16-bit add).
- A radius bucket with >255 scanlines is emitted as multiple bucket-entries with
  the same radius (`nbuckets` counts entries). A line never has >255 dots at
  320px wide, so `n` always fits a byte.
- **Dot centres are pre-clamped to `[r, W-1-r] × [r, H-1-r]`** so discs never
  cross the screen edge — the 6502 plotter then needs **no clipping**.

The packer is `pack_bbc()`; a byte-exact reference decoder is `unpack_bbc()`
(this mirrors the 6502 logic and is used to self-check every export).

### Compression finding (ZX02)

We use **ZX02** (`github.com/dmsc/zx02`), a 6502-tuned ZX0 variant: tiny, fast
decompressor (~121 bytes for the "small" build) intended for ≤16 KB payloads.

Key result: on this data **ZX02 only shaves ~3 %** off the raw delta stream
(parrot: 2161 → 2089 B). The delta-coding already removes almost all the
redundancy, so a general LZ has little left to find. Implications:

- The delta stream itself is the real compressor here; ZX02 is a small bonus.
- If the budget is tight, **reducing dot count** is the biggest lever, not the
  packer. (zlib/lzma land in the same ballpark — confirmed in the tool report.)
- ZX02 is still worth keeping: small decompressor, and it never *expands*.

ZX02 round-trips are verified automatically (`zx02` compress → `dzx02`
decompress == original) when `--bbc` runs with the tools available.

---

## 4. Phase 3 — the BBC Micro 6502 player (`bbc/`)

```
bbc/stipple.asm            main: MODE 4 setup, row tables, depack, plot loop
bbc/zx02_decompress.asm    BeebAsm port of dmsc's zx02-small decompressor
bbc/spans.asm              GENERATED per-radius disc span tables (r=1..7)
bbc/data/parrot.zx02       example compressed dot stream (the parrot)
bbc/build.sh               regenerate data (optional) + assemble to stipple.ssd
```

### MODE 4 facts used

- 320×256, 1bpp, screen base `&5800`, 10 KB. Laid out as 40×32 char cells of
  8 bytes each (one byte = 8 horizontal pixels, **bit 7 = leftmost pixel**).
- Byte address of pixel `(x,y)` = `&5800 + (y>>3)*320 + (y&7) + (x>>3)*8`;
  bit = `7-(x&7)`. **Horizontally adjacent screen bytes are +8 apart** (next
  char cell), which the plotter exploits.
- Palette is remapped so logical 0 = white (background, the cleared screen) and
  logical 1 = black (dots), so plotting is pure OR into a clear screen.

### How it works

1. VDU init: MODE 4, cursor off, palette remap.
2. `build_rowbase` fills 256-entry `rowlo`/`rowhi` tables with the byte address
   of x=0 for each y (+1 per line, +313 stepping across a char-cell boundary).
   Done at runtime to save ~512 bytes on disc.
3. `full_decomp` (ZX02) depacks `comp_data` → `out_addr` (&4400).
4. `plot_all` walks the stream; for each dot it walks that radius's span list
   (`(dy, dx, len)` triplets) and ORs a horizontal run of `len` pixels using
   `rowbase[y+dy] + (Xs & $FFF8)` for the byte and a mask table for the start
   bit, stepping +8 bytes when the mask wraps past bit 0.

Plotting **pixel-runs per scanline** (rather than pre-shifted sprites) keeps the
code tiny and is plenty fast: ~28 K black pixels for the parrot, so a fraction
of a second at 2 MHz — the 30 s budget is not a concern; data size is.

### Memory map (load &1900, DFS-friendly)

```
&1900  program (code + spans + INCBIN'd compressed data) … ends ~&23DA
&4000  rowlo  (256)     ] runtime tables/buffers, RAM only,
&4100  rowhi  (256)     ] not part of the saved file
&4400  out_addr         depacked dot stream (~2.2 KB) … below screen
&5800  MODE 4 screen
```

### Build

```
cd bbc
BEEBASM=/path/to/beebasm ZX02=/path/to/zx02 ./build.sh           # assemble only
IMAGE=/path/to/photo.png BEEBASM=... ZX02=... ./build.sh         # regen + assemble
```
Produces `bbc/stipple.ssd` (auto-boots `MONASTP`). BeebAsm:
`github.com/stardot/beebasm`. (Note: the asm INCBINs `data/parrot.zx02`; if you
change `STEM`, update the `INCBIN` line to match.)

---

## 5. Validation done this session

- **Format**: `unpack_bbc` reference decode of every export matches the dot
  count and content; ZX02 `compress→decompress` round-trips byte-exactly.
- **Plotting/addressing** (the risky bit): `tools/verify_bbc.py` plots the
  decoded stream into a simulated MODE 4 framebuffer using the *exact* address
  arithmetic from `stipple.asm`, decodes it back to an image, and diffs against
  the tool's own disc renderer → **0 pixel differences** for the parrot.
- **Assembly**: `bbc/stipple.asm` assembles clean with BeebAsm; image fits
  &1900–~&23DA, buffers clear of the screen.

What is **not** yet verified: the literal 6502 opcode transcription and the
ZX02 port executing on a real 6502. Everything above is host-side logic that
the asm mirrors.

---

## 6. Next steps

*(For the 4 KB MODE 4 player only — see header note. The 256-byte intro is
tracked separately in [STIPPLE-256.md](STIPPLE-256.md).)*

1. **Run it.** Boot `bbc/stipple.ssd` in an emulator (b-em / jsbeeb / BeebEm)
   and confirm the depack + plot. **Still open.** Most likely bug surface:
   the ZX02 port's X-register pointer-selector trick, and the signed `dx`
   16-bit add.
2. **Per-image tuning pass** in `stipple.py` for the chosen photo(s). Open.
3. **Squeeze data** if needed: the lever is dot count (and slightly,
   delta-Y instead of absolute Y). ZX02 won't add much over the delta stream.
4. Possible polish: progressive plot (largest dots first), a fade-in, multiple
   images, or a MODE 0 (640×256) variant for finer dots. ✅ A MODE 0 variant
   exists — but as the 256-byte sister project, not as a feature of this player.
5. Decide whether to also keep a `--posterize`-driven "poster" style as an
   alternate look. Open.

## File manifest (added this session)

```
tools/stipple.py        converter/visualiser + BBC packer
tools/verify_bbc.py     6502-free plotter/addressing validation
tools/README.md         tool usage
bbc/stipple.asm         BBC MODE 4 player
bbc/zx02_decompress.asm ZX02 depacker (port)
bbc/spans.asm           generated disc span tables
bbc/data/parrot.zx02    example data
bbc/build.sh            build script
docs/STIPPLE.md         this document
```
