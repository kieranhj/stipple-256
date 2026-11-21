# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A 256-byte stipple "photo" intro for the BBC Master (sizecoding), plus the Python
tooling that designs the dot pattern and downsamples source images into the
on-device data block. There is also an older, separate 4 KB MODE 4 stipple player
that this 256-byte target was carved out of — it still assembles but has **not**
been run on an emulator.

Two distinct deliverables share the repo; keep them separate when reasoning:

| | 256-byte intro (active) | 4 KB player (legacy) |
|---|---|---|
| asm | `bbc/stipple256.asm` | `bbc/stipple.asm` (+ `spans.asm`, `zx02_decompress.asm`) |
| build | `bbc/build_256.sh` | `bbc/build.sh` |
| mode | MODE 0, GXR circle PLOTs | MODE 4, custom span plotter |
| data | 64 B downsampled image, positions synthesized on-device | ~2 KB ZX02-compressed `(x,y,r)` list |
| design notes | `docs/STIPPLE-256.md`, `docs/STIPPLE-256-LFSR.md` | `docs/STIPPLE.md` |

## Build & run

```sh
cd bbc
./build_256.sh                       # convert pics/mona_lisa_crop.png -> data/mona16.bin, assemble
./build_256.sh face.png              # image resolved as absolute, repo-relative, or pics/<name>
./build_256.sh --run                 # also open the resulting .ssd (cmd.exe/xdg-open/open)
./build_256.sh --noboot              # don't write a !BOOT file
./build_256.sh face.png --mode256-size 16 --mode256-levels 4   # extra args pass through to stipple.py --mode256
```

`build_256.sh` prints a **byte-budget report** (code bytes + image bytes / 256,
spare or over) by parsing the BeebAsm listing — this is the primary signal when
editing the asm. Output `bbc/stipple256.ssd` autoboots via `*RUN MONASTP`.

The 4 KB player: `cd bbc && ./build.sh` (set `BEEBASM`/`ZX02`/`IMAGE` env vars;
regenerates data only when `IMAGE` is set).

There is no test suite. The closest thing is `tools/verify_bbc.py <bin>`, which
replays the 4 KB player's exact MODE 4 address arithmetic in Python and asserts a
0-pixel diff against the renderer (invoked automatically by `build.sh` when
regenerating data).

### Testing the 256-byte intro on jsbeeb (MCP)

A `jsbeeb` MCP server is configured for this project (`npx jsbeeb-mcp`; loads at
session start). Use it to actually run the intro — verified working end to end:

1. `mcp__jsbeeb__create_machine` with `model: "Master"` (GXR ROM is required for
   the filled-circle PLOT 157 — the default `B-DFS1.2` won't render dots).
2. `mcp__jsbeeb__boot_disc` with the **absolute** path to `bbc/stipple256.ssd`
   (autoboots `*RUN MONASTP`).
3. `mcp__jsbeeb__run_for_cycles` — boot + the 2048-dot MODE 0 render takes a
   while; allow ~40–50M cycles total before the picture settles.
4. `mcp__jsbeeb__screenshot` to eyeball the result (expect a centred stipple with
   white left/right margins from the VDU 29 origin), then `destroy_machine`.

`run_disc` (one-shot, `model: "Master"`) also works but its default 30 s emulated
timeout can cut the render short — prefer the granular path above for this intro.
Still validate the *look* by eye (and via the host preview tools below); jsbeeb
confirms it boots and plots, not that the tone mapping is good.

### Requirements

- `tools/beebasm.exe` — **not committed**, download from
  <https://github.com/stardot/beebasm> and place it there.
- Python 3 with `numpy`, `scipy`, `pillow`; `gradio` for the UI.
- 4 KB player also needs a `zx02` binary (<https://github.com/dmsc/zx02>).

## Host tooling (`tools/`)

- `stipple.py` — the workhorse. Default mode produces the 4 KB-style stipple
  (Lloyd-relaxed weighted Voronoi, radius encodes darkness). `--mode256` instead
  emits the tiny downsampled brightness source the intro reads on-device. The
  256-byte build path only uses `--mode256`; `build_256.sh` calls it with
  `--mode256-size 16 --mode256-levels 4` and copies `<stem>_mode256_src.bin` to
  `bbc/data/mona16.bin`.
- `stipple_ui.py` — Gradio app (`python tools/stipple_ui.py`) for tuning radius
  maps, iteration counts, and aspect handling against MODE 0/4 dot geometry
  before committing asm parameters.
- `lfsr_*.py` — an investigation (logged in `docs/STIPPLE-256-LFSR.md`) into
  swapping the R2 placement for brute-forced LFSR seeds. The conclusion was that
  R2 wins; these are analysis scripts, not part of any build.
- `verify_bbc.py` — 4 KB player validator described above.

## How the 256-byte intro works (architecture)

The core trick: at 256 bytes you cannot store dot coordinates, so positions are
**synthesized on-device** and only a 64-byte brightness source is stored.

1. **Placement** — an additive R2 (plastic-phi) low-discrepancy sequence. Two
   16-bit zero-page accumulators advance by `XINC=$C142` / `YINC=$91DF` each
   iteration; the high bytes are the pixel coords. 2048 iterations (`cnt_hi:cnt_lo`
   16-bit counter; the loop X register is clobbered by the image lookup so it
   can't be the counter).
2. **Image lookup** — `data/mona16.bin` is 16×16 @ 2bpp, LSB-first packed and
   **vertically flipped** (BBC y-up vs image top-down). Each dot reads its cell to
   get a 2-bit darkness 0..3.
3. **Radius** — `r = cell*2 - 1` → `{1,3,5}` pixels; cell 0 skips the dot (`r=0`).
4. **Plot** — GXR PLOT 157 (filled circle, absolute). A 6-byte VDU template at ZP
   `buf` ($70) is mutated and emitted twice per dot (MOVE then PLOT) by `emit6`.
   `gx = px*4`, `gy = py*4`; VDU 29 origin (+130,0) centres the picture on the
   1280-wide MODE 0 screen.

### Working in `stipple256.asm`

This file is byte-golfed to the limit (currently ~241/256). Expect dense,
non-obvious code where comments explain *why* a sequence is shaped a certain way
— read them before editing. Recurring constraints:

- Fall-through and register reuse are load-bearing (e.g. `A=xa+1` flows straight
  into the cell lookup; `r` lives in X across `emit6` because there's no spare ZP).
- Zero-page layout is contiguous on purpose: `xa/ya/cnt_lo` at `$7C..$80` so one
  init loop zeroes them; the VDU-init loop double-duties as the ZP clear.
- `vdutab` stores VDU bytes **in reverse** because the init loop reads X
  downward. CLS must precede VDU 5.
- Branch reach is exact (one `bne` back-edge is at offset −128). Adding bytes to
  the loop body can break it.
- `ORG &1900`, `GUARD &7C00`. Always re-check the build's byte-budget line after
  any change.
