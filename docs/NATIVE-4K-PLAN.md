# Native 4K BBC Master mini-emulator running stipple256

A Windows `.exe` (≤ 4096 bytes, Crinkler-compressed) that embeds the existing
256-byte `stipple256.bin` and reproduces what it would draw on a real BBC
Master — by running a stripped-down 6502 core through a minimal MOS VDU
handler and blitting the framebuffer with GDI.

**Status: built and working.** Source lives in `4k-emu/`. Final size: **1874 B**
(46 % of the 4 KB budget). Built against VS toolchain v145, Crinkler 2.3.

---

## 1. Scope — what gets emulated

### 1.1 6502 CPU subset

31 opcodes (of 151). Inventoried from stipple256.asm:

- Loads/stores: `LDA` (#imm, zp, abs,X, abs,Y), `LDX #imm`, `LDY #imm`,
  `STA zp`, `STY zp,X`
- Transfers: `TAX, TAY, TXA, TYA`
- Shifts/rotates: `ASL A, ASL zp, LSR A, ROL A, ROL zp`
- Arithmetic/logic: `ADC` (#imm, zp), `SBC #imm`, `AND #imm`, `ORA zp`,
  `CLC`
- Memory: `INC zp, DEC zp, DEX, DEY`
- Branches: `BEQ, BNE, BMI, BPL, BCC`
- Control: `JSR abs, RTS`

Flag model: N, Z, C only. V and D are never branched on or set by
stipple256, so they're omitted.

`B9` (LDA abs,Y) was missed in the initial inventory — caught on the first
Debug run when the emulator hit `emit6` and bailed with `UNKNOWN opcode B9
at PC=$1999`. Added; that's the full set.

### 1.2 MOS VDU command vocabulary

Every command byte stipple256 sends to OSWRCH:

| VDU | Params | Behaviour we implement |
|---|---|---|
| 22  | n            | MODE n — no-op (we're hardwired to MODE 0) |
| 17  | n            | Text colour — no-op (VDU 5 means we don't render text) |
| 18  | mode, c      | GCOL — no-op (stipple256 only emits GCOL 0,0) |
| 12  | —            | CLS — clear fb1 to white (0) via `__stosd` |
| 5   | —            | VDU 5 — no-op |
| 29  | xL,xH,yL,yH  | Set graphics origin |
| 25  | k,xL,xH,yL,yH | PLOT k at absolute (x, y) — handles k=4 (MOVE) and k=157 (filled circle, fg) |

PLOT 157 is the only primitive that draws anything. Radii used (logical
units): {4, 12, 20} — one per source-pixel cell value {1, 2, 3}.

### 1.3 Screen

- Internal framebuffer: 640 × 256, **1 bpp**, MSB-leftmost packing,
  stride 80 bytes (`fb1[80 * 256]` = 20 KB BSS)
- 1bpp DIB with a 2-entry palette: index 0 = white, index 1 = black
- Presentation: 640 × 480 (4:3 aspect — the actual proportions a real BBC
  TV shows). This Y-stretch of 1.875× also makes ANISOTROPIC source ellipses
  look round on screen — see § 3.

### 1.4 Run model

Execute 6502 until the `beq hang` self-loop is detected (PC unchanged after
a step). Then enter the Win32 message pump showing the final framebuffer
until ESC. No real-time timing, no animation.

---

## 2. Final size budget

Tracked through each integration step:

| Stage | Size | Δ |
|---|---:|---:|
| Crinkler stub (`ExitProcess(0)`) | 423 B | — |
| + Window + GDI + msg pump (640×320 RGBA) | 594 B | +171 |
| + Embedded 241 B ROM blob | 828 B | +234 |
| + 6502 core (31 opcodes) | 1526 B | +698 |
| + VDU dispatcher + 1bpp DIB + filled-circle rasteriser | 1888 B | +362 |
| Crinkler `/COMPMODE:SLOW /HASHTRIES:1000 /ORDERTRIES:1000` | 1874 B | −14 |
| **Final** | **1874 B** | **46 % of 4096** |

The ROM blob (241 B) is essentially incompressible — already byte-tuned
and ~80 % high-entropy image data — and forms an unavoidable floor.

Crinkler flags landed on: `/CRINKLER /COMPMODE:SLOW /UNSAFEIMPORT
/UNALIGNCODE /HASHTRIES:1000 /ORDERTRIES:1000 /REPORT:out.html`.
`VERYSLOW` did nothing on top of `SLOW`.

---

## 3. Rasteriser design — the aspect dance

This is the part that took the most thinking through. There are three
non-square aspect ratios to reconcile:

1. **Logical → pixel** in MODE 0: logical_x → pixel = /2, logical_y → /4.
   So adjacent pixels are 2 logical X but 4 logical Y. stipple256 emits
   `gx = px * 4` and `gy = py * 4`, meaning per-iteration adjacent cells
   land **2 source-pixels apart in X and 1 source-pixel apart in Y**.
2. **GXR PLOT 157**: computes radius as the pixel-space distance between
   the MOVE and PLOT coords, then draws an isotropic circle in pixel
   space. With non-square pixels, those circles appear vertically
   stretched on a real BBC TV.
3. **TV aspect**: a real BBC MODE 0 shows a 640×256 framebuffer on a 4:3
   monitor — pixels are about **1.875 × taller than wide**.

What we want: **circular-looking dots on a typical PC display**. We
deviate from byte-exact GXR fidelity and pre-compensate.

The rasteriser draws an **anisotropic ellipse** in source pixels with
half-axes:

```
a = rlog >> 1                ; X half-axis (pixels) = 2× r_asm
b = rlog >> 2                ; Y half-axis (pixels) = 1× r_asm
```

(`rlog = end_x − cx`, always in {4, 12, 20}.)

Inclusion test:

```
dx² · b² + dy² · a² ≤ a² · b² + a · b
```

The `+ a·b` bias is the ellipse analogue of Bresenham's filled-disc
`+ r` bias. Without it, the tiny (a=2, b=1) ellipse for cell=1 reduces
to a 5×1 horizontal bar with two stray pixels above and below — looks
spiky. With the bias it fills out to a 5×3 octagonal blob with rounded
ends.

For each r_asm value, the source ellipse and on-screen result:

| r_asm | rlog | a,b (src px) | Bounding (src px) | Displayed (640×480) |
|---|---|---|---|---|
| 1 | 4  | 2, 1 |  5 ×  3 |  5 ×  5.6 |
| 3 | 12 | 6, 3 | 13 ×  7 | 13 × 13.1 |
| 5 | 20 | 10, 5 | 21 × 11 | 21 × 20.6 |

All three round to roughly circular on the 4:3 display.

**Why this also fixes the cluster shape**: stipple256's source-grid step
is (2, 1) pixels — closer vertically than horizontally. On a 1:1 display
this makes overlapping dot clusters look like tall streaks. But the
display steps are (2, 1) × (1, 1.875) = (2, 1.875) — almost square. So
clusters of overlapping ellipses also read isotropic on screen.

(Pure GXR fidelity — isotropic source pixel circles — gives perfectly
round individual dots in the source framebuffer, verified by a direct
single-circle test, but produces vertical-streak clusters and a tall
appearance on a 4:3 display. The anisotropic approach trades pixel-exact
GXR-match for what a person actually expects to see.)

---

## 4. Build pipeline

```
4k-emu/
  4k-emu.sln                ; VS solution, x86 only
  4k-emu.vcxproj            ; Debug + Release configs
  main.cpp                  ; everything lives here
  bin_to_h.py               ; bbc/stipple256.asm -> stipple256_bin.h via beebasm
  stipple256_bin.h          ; generated 241 B ROM as a C array (committed)
  link.exe                  ; Crinkler stub (copied from Blossom)
  .gitignore                ; ignores Debug/, Release/, screenshots, dumps...
```

Entry point: `WinMainCRTStartup` (Release, `/SUBSYSTEM:WINDOWS`,
`/IgnoreAllDefaultLibraries`, no CRT). Imports: kernel32 (`ExitProcess`,
`CreateFileA`, `GetStdHandle` in Debug-only), user32 (`CreateWindowExA`,
`PeekMessageA`, `DispatchMessageA`, `GetDC`, `GetAsyncKeyState`,
`SetWindowTextA` in Debug-only), gdi32 (`StretchDIBits`).

Build:

```sh
# Debug -- console subsystem, prints UNKNOWN opcode diagnostics, shows
# CPU state in the window title.
msbuild 4k-emu.sln /p:Configuration=Debug   /p:Platform=x86

# Release -- WinMainCRTStartup, Crinkler, 1874 B.
msbuild 4k-emu.sln /p:Configuration=Release /p:Platform=x86

# Re-generate the embedded ROM from bbc/stipple256.asm (only when the asm
# changes; needs tools/beebasm.exe in the parent repo):
python 4k-emu/bin_to_h.py
```

Output: `4k-emu/Release/4k-emu.exe`.

---

## 5. Verification

### 5.1 Compute-side

- 6502 reaches the `beq hang` self-loop ($1993) in **266,275** steps.
- Stack balanced (S = $FD at exit, matches initial).
- OSWRCH fires exactly **24,590** bytes = `14 (vinit table) + 2048 dots × 12
  (MOVE + PLOT)`. Every dot iteration emits (mona_lisa has no zero-cell
  pixels — verified by inspecting the 64 B image data, all 2-bit pairs
  ≥ 01).

### 5.2 Rasteriser

A direct unit test (calling `plot_filled_circle` with known inputs into a
cleared fb1) confirms the source ellipses come out at exactly the
expected octagonal shapes:

```
r_asm=1 (a=2,b=1): 5×3 octagonal
r_asm=3 (a=6,b=3): 13×7 octagonal
r_asm=5 (a=10,b=5): 21×11 octagonal
```

(See git history for the test scaffold — it was instrumented in Debug
during P5 and removed once the rasteriser was settled.)

### 5.3 Visual

Compared against jsbeeb running the same `stipple256.ssd` (via the
jsbeeb MCP screenshot tool). jsbeeb renders authentic GXR (isotropic
pixel circles, tall on a square-pixel display); ours renders 4:3-corrected
ellipses (round on a square-pixel display). Both reproduce the Mona Lisa
portrait clearly with the same overall texture and tonal mapping.

---

## 6. Notable gotchas hit during the build

- **Crinkler crashes on near-empty programs.** The stub (~130 B of code
  with one ExitProcess call) crashes Crinkler at "Optimizing hash table
  size" with `/COMPMODE:FAST` and above. Worked around by using
  `/COMPMODE:INSTANT` for the stub baseline only — the crash clears up
  once real code lands.
- **MSVC emits `_memset` for trivial zero-init.** `BMI1BPP bmi = {};`
  and naive `for (i=0; i<N; i++) buf[i] = 0;` loops both link-fail
  without CRT (`error LNK: Cannot find symbol '_memset'`). Fix: move
  the struct to static storage (BSS = automatic zero) and use the
  `__stosd` intrinsic for the CLS clear (inline `rep stosd`).
- **MS toolchain v145 in VS18 is incomplete.** `v180` appears under
  MSBuild but has no real toolset behind it. Use `v145` like Blossom
  does — it maps to MSVC 14.50 at runtime and works fine.
- **B9 opcode was missing** from the initial opcode inventory. Caught
  via a Debug-only `UNKNOWN opcode XX at PC=YYYY` print before
  `ExitProcess(op)`. Adding LDA abs,Y fixed it.

---

## 7. Stretch goals (not pursued, room to spare)

We're using 1874 / 4096 B. The remaining ~2200 B could support:

- **Animation**: render N dots per frame instead of run-to-completion.
  Hook the dot loop's `dec cnt_lo` and yield to the message pump every
  M iterations. ~50 B.
- **ESC-to-restart**: re-init 6502 state and CLS, re-run. ~20 B.
- **Configurable image**: bake in 2–3 ROM variants, pick at startup or
  cycle on keypress. Each extra 241 B ROM is ~241 B in the binary
  (incompressible).

None of these were asked for; the plan goal of "256-byte intro, embedded
emulator and all, under 4 KB" is met with comfortable headroom.
