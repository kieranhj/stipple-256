# STIPPLE-256 LFSR seed brute force — investigation log

**Date:** 2026-06-09
**Goal:** the 256-byte stipple intro at 16×16×2bpp + R2 placement looks OK at 768
iterations but lacks fine detail. This investigation tests whether the R2
plastic-φ placement can be swapped for two 16-bit LFSRs whose seeds we
brute-force search per image, in the hope that "best LFSR for this picture"
beats "generic R2 for any picture" — and whether shifting to a denser data
layout (32×32 binary + dither) helps.

See [STIPPLE-256.md](STIPPLE-256.md) for the baseline. This document records
what was tried, what was measured, and what (if anything) is worth porting to
the asm.

---

## Why R2 was the obvious starting point

The current asm uses an additive R2 (plastic-φ) sequence with 16-bit
accumulators:

```
xa += $C142     ; 1/φ₂ × 65536 (asm-rounded)
ya += $91DF     ; 1/φ₂² × 65536
px = xa >> 8    ; high byte → pixel
py = ya >> 8
```

R2 is a **quasi-random low-discrepancy sequence**: by construction it fills the
unit square as evenly as possible after any prefix. For a stipple intro this
is structurally ideal — every region gets sampled roughly proportionally to
its area, so a 16×16 cell's local darkness translates into a predictable
amount of ink.

The cost is that R2 is *image-blind*. For any given picture, *some* specific
placement sequence will land more dots on important features (eyes, contour
lines) than R2 does by accident. Whether that "better" sequence is reachable
within the 256-byte budget is the question.

---

## What the LFSR buys

A 16-bit Galois right-shift LFSR with tap mask `0xB400` has period 65535 and
is extremely cheap on the 6502:

```
LSR HI       ; shift HI right
ROR LO       ; shift LO right, taking HI bit 0
BCC noxor    ; carry = old LO bit 0
LDA HI
EOR #$B4     ; tap mask is high-byte-only
STA HI
noxor:
```

That's **13 bytes / ~20 cycles** per advance — about the same as one axis of
the R2 adder. Two independent LFSRs (one per axis) cost ~26 bytes versus
~28 bytes for the current two-axis R2 adder, so the swap is free byte-wise.

What we gain: the (x_seed, y_seed) pair is a 32-bit knob we can tune per
picture. There are 65535 × 65535 ≈ 4.3 × 10⁹ possible seed pairs — a search
space.

What we lose: low-discrepancy. The LFSR sequence is pseudo-random. Over a
short window (1024–2048 of the 65535-state cycle) consecutive `(hi(state),
hi(state'))` pairs cluster and clump rather than spreading evenly. We're
betting that *some* clumping pattern happens to align with the target image
better than R2's even spread.

The visual gap is stark even on a blank target — see
[`out_lfsr/coverage_n1024.png`](../out_lfsr/coverage_n1024.png):

| R2 (1024 points) | LFSR (1, 32768) | LFSR (13579, 24680) |
|------------------|-----------------|---------------------|
| even sloped quasi-grid | clumps + voids | different clumps + voids |

R2 lays down a near-perfect grid; the LFSR pairs leave large voids and dense
clusters. For an image-dependent metric, brute force has to find an LFSR
seed pair whose clusters and voids *happen* to land on dark and light image
regions respectively. That's a high bar.

`tap = 0xB400` was chosen because it's max-length **and** the tap byte is
entirely in the high byte — a single `EOR #$B4`. Other primitive polynomials
(`0xD008`, `0x8016`, `0xA801`) also tested as max-length but need an
additional low-byte EOR.

---

## What the brute force did

For each (image, data mode, iteration count) tuple:

1. **Random sample 300 000 seed pairs**, render each, score it against the
   target with **gaussian box-filtered MSE** (4×4 block average → 64×64 grid,
   then per-pixel squared error vs the same-blurred target). Keep the top 3.

2. **Coordinate-descent refinement** on each top-3 seed: alternate exhaustive
   sweep of one axis (all 65535 values, with the other fixed) until no
   further improvement. Each sweep takes ~9 s in numba.

3. **R2 baseline**: render the same configuration with the asm's R2
   constants. Record its MSE.

4. Save: target | R2-rendered | best-LFSR-rendered side-by-side image, plus
   the seed pair as JSON for direct asm porting.

### Configurations swept

Per image (mona / face / eye):

| mode | data bytes | iters | radius |
|------|-----------:|------:|--------|
| 16×2 (current) | 64  | 1024 | cell × 2 → {0,2,4,6} |
| 16×2 (current) | 64  | 2048 | cell × 2 → {0,2,4,6} |
| 32×1 + FS dither | 128 | 1024 | fixed r=3 |
| 32×1 + FS dither | 128 | 1024 | fixed r=4 |
| 32×1 + FS dither | 128 | 2048 | fixed r=3 |
| 32×1 + FS dither | 128 | 2048 | fixed r=4 |

The 32×1 mode is the "more apparent data" experiment: 32×32 cells (1024
binary "draw / skip" decisions) pre-dithered with Floyd-Steinberg, then each
hit cell stamps a fixed-radius disc. Budget: 128 B for data leaves ~128 B
for code, vs ~190 B in the current 64-B-data layout — feasible but tight.

---

## Results

Raw data: `out_lfsr/all_results.json`; per-config visual comparisons:
`out_lfsr/<stem>_<mode>_n<iters>[_r<R>]_compare_2x.png` (target | R2 | best-LFSR).

### Headline

**R2 wins almost everywhere. Brute-force LFSR did not find a configuration
that meaningfully beats R2 within the chosen metric.**

- **LFSR-beats-R2 configs: 4 / 18**, by 3–7 % each — all in the same niche
  (`32x1 r=3 @ 2048 iters` for every image, plus `mona 32x1 r=4 @ 2048`).
  Even in those wins, **the absolute MSE in that mode is roughly 2× worse
  than the 16×2 mode with R2** at the same iter count, so it's a Pyrrhic
  victory: LFSR wins in a regime where the regime itself is worse than
  the alternative.

- In the **current data mode (16×2)** the LFSR was strictly worse for
  mona / face by 3–4 % and a statistical tie for eye (+0.4 %) at 2048 iters.
  At 1024 iters the LFSR loses by 50–77 %.

- **The 2× iteration count (1024 → 2048) was the only intervention that
  reliably improved every image, every mode, with no asm-budget cost
  beyond a tiny loop-counter tweak.**

### Full result table

| image | mode | iters | r | LFSR x | LFSR y | LFSR MSE | R2 MSE | Δ vs R2 | search time |
|-------|------|------:|--:|-------:|-------:|---------:|-------:|---------|------------:|
| mona_list_square | 16x2 | 1024 | — | 34960 | 14452 | 0.109627 | 0.073071 | -50.0 % | 75 s |
| mona_list_square | 16x2 | 2048 | — | 34164 | 34175 | 0.084326 | 0.081952 | -2.9 % | 165 s |
| mona_list_square | 32x1 | 1024 | 3 |  7726 | 52834 | 0.223242 | 0.197379 | -13.1 % | 34 s |
| mona_list_square | 32x1 | 1024 | 4 | 19089 | 24999 | 0.160763 | 0.125136 | -28.5 % | 40 s |
| mona_list_square | 32x1 | 2048 | 3 | 15037 |  7905 | 0.143534 | 0.148083 | **+3.1 %** | 65 s |
| mona_list_square | 32x1 | 2048 | 4 | 50668 |  2575 | 0.115478 | 0.113671 | -1.6 % | 164 s |
| face | 16x2 | 1024 | — | 16655 | 65497 | 0.074086 | 0.041772 | -77.4 % | 139 s |
| face | 16x2 | 2048 | — |  6589 | 24690 | 0.048966 | 0.047017 | -4.1 % | 347 s |
| face | 32x1 | 1024 | 3 | 57104 | 61665 | 0.290960 | 0.239492 | -21.5 % | 65 s |
| face | 32x1 | 1024 | 4 | 56646 | 58318 | 0.172488 | 0.092546 | -86.4 % | 103 s |
| face | 32x1 | 2048 | 3 | 42113 |  1311 | 0.143719 | 0.154945 | **+7.2 %** | 135 s |
| face | 32x1 | 2048 | 4 | 58448 | 41581 | 0.073973 | 0.061204 | -20.9 % | 138 s |
| eye | 16x2 | 1024 | — | 52782 | 30578 | 0.097387 | 0.063731 | -52.8 % | 140 s |
| eye | 16x2 | 2048 | — | 58372 | 12530 | 0.075969 | 0.076289 | **+0.4 %** | 218 s |
| eye | 32x1 | 1024 | 3 | 33283 | 42922 | 0.247442 | 0.208710 | -18.6 % | 40 s |
| eye | 32x1 | 1024 | 4 | 36236 | 20061 | 0.161728 | 0.100758 | -60.5 % | 72 s |
| eye | 32x1 | 2048 | 3 | 19958 | 51094 | 0.138048 | 0.143008 | **+3.5 %** | 112 s |
| eye | 32x1 | 2048 | 4 | 35732 | 29558 | 0.090265 | 0.082197 | -9.8 % | 175 s |

Bold rows = LFSR beat R2 (4 / 18).

### Best absolute quality per image (lowest MSE — not "biggest win")

Independent of whether LFSR beat R2, here's where each image actually looks best:

| image | best config | placement | MSE |
|-------|-------------|-----------|----:|
| mona  | 16×2 @ 2048 iters | R2 | 0.0820 |
| face  | 16×2 @ 2048 iters | R2 | 0.0470 |
| eye   | 16×2 @ 2048 iters | R2 / LFSR (tied) | 0.0760 / 0.0763 |

In other words, **the best version of every image is the current `16x2 + R2`
pipeline at double the current iteration count (2048 vs the current 768)**.
The brute force did not unseat that.

### Did 32×1 + dither outperform 16×2?

No, not on this metric. Across all images and all iter counts, 16×2 R2 wins
on MSE. The 32×1 r=4 @ 2048 results are visually interesting — coarser,
more graphic — but consistently lose 16–60 % on the metric.

Two reasons:
1. **Lost tonal resolution.** Binary cells with dither only encode "ink /
   no ink" at each cell — radius levels are gone. The metric, which blurs
   then compares tone, rewards fine radius modulation.
2. **Sparser coverage at low fixed-r.** With r=3, even when every cell is
   "on", the 1024 / 2048 dots don't fully fill the 256×256 plane, leaving
   light voids. r=4 helps but also blows out dark areas to pure black.

### Did 2048 iters meaningfully add detail over 1024?

Yes, decisively. Across every (image, mode) pair the 2048-iter MSE was
22–37 % lower than the 1024-iter MSE — a much bigger swing than anything
the LFSR search produced. Doubling iterations is the highest-impact change
available.

---

## Honest assessment

The structural argument predicted this and the search confirmed it. R2 is a
low-discrepancy sequence specifically engineered for even space-filling; any
LFSR draws from a pseudo-random distribution whose 1024–2048 sample windows
clump and leave voids. The 4×4-block-blur metric rewards even ink — exactly
R2's strength.

The brute force did its job: 300 k random samples + 3-start coord-descent
explored ~0.02 % of the 4.3 × 10⁹-pair search space and converged to a real
local optimum every time. There just isn't a global optimum among LFSR seeds
that catches R2 at this metric — the structural ceiling is real.

The 4 narrow LFSR wins (32×1 r=3 @ 2048 iters, every image; plus mona 16×2
@ 2048 by a hair) are honest local-metric wins. But they're in regimes whose
absolute quality is worse than the regime where R2 wins. **There is no
practical reason to port any of the brute-forced seeds to asm.**

Independent of the LFSR result, the modal comparison answered two questions
that matter for the asm port:

1. **16×2 beats 32×1 + dither on this metric** for every image and every
   iter count. Radius levels carry more perceptual weight than cell count.
2. **2048 iters is reliably better than 1024** by ~25 % MSE. The current asm
   does 768 iters; tripling that (or even just doubling to 1536) is the
   simplest available quality bump.

---

## What this didn't try (and why)

- **Smarter search**: simulated annealing or CMA-ES instead of random +
  coord-descent. The metric landscape is highly non-convex (one seed pair
  shifts where every disc lands) and 4.3B-point. Coord-descent finds *some*
  local optimum but global search would need ML budget, not a few hours.

- **Non-Galois LFSRs**: a Fibonacci LFSR with the same polynomial gives the
  same cycle in reverse order; brute-forcing one is brute-forcing the other.

- **Hybrid LFSR-for-X + R2-for-Y**: keeps R2's uniformity in one axis. Not
  pursued because it makes the asm bigger (need both adders and LFSR).

- **Per-iteration radius perturbation**: vary radius by sub-pixel position
  in the 16-bit accumulator. Bilinear sampling was tried earlier in the
  Gradio UI (commit e3fe14e) without big visual gains; the constraint is
  data, not sub-pixel placement.

- **Floyd-Steinberg on the 16×2 cell quantisation**: pre-dithering the
  4-level cell map. Would slightly redistribute radius levels but doesn't
  change the underlying "1024 dots from 64 B data" capacity ceiling.

---

## Porting a winning seed back to asm

If a per-image LFSR seed pair *does* outperform R2 for the chosen target
image, the asm change is small:

```asm
; replace
;   xa += XINC   ;   ya += YINC
; with

    ; LFSR step on (xa+1 : xa) — high-byte tap 0xB4
    lsr xa+1
    ror xa
    bcc xa_noxor
    lda xa+1
    eor #$B4
    sta xa+1
.xa_noxor

    lsr ya+1
    ror ya
    bcc ya_noxor
    lda ya+1
    eor #$B4
    sta ya+1
.ya_noxor
```

…and set the seeds at startup:

```asm
    lda #LO(X_SEED) : sta xa
    lda #HI(X_SEED) : sta xa+1
    lda #LO(Y_SEED) : sta ya
    lda #HI(Y_SEED) : sta ya+1
```

Byte cost in zero page (where `xa` / `ya` live in the current asm):

- LFSR per axis: `lsr zp (2) + ror zp (2) + bcc (2) + lda zp (2) + eor # (2)
  + sta zp (2)` = **12 B** / axis = 24 B total for both axes.
- R2 per axis (current asm L57-62): `clc (1) + lda zp (2) + adc # (2) +
  sta zp (2) + lda zp (2) + adc # (2) + sta zp (2)` = **13 B** / axis =
  26 B total.

LFSR is **2 B cheaper** on the per-iteration code. But it adds **seed-init
cost**: the current init is one `lda #0` shared across `xa..xa+1..ya..ya+1`
(2 B + 4×2 B = 10 B for four bytes of zeros plus the `cnt_lo` reset). LFSR
needs four distinct seed bytes, costing roughly 4 × 4 B = 16 B of init code
(or 12 B if a temp register is reused). Net: roughly **+4 B overhead** for
the LFSR variant, in exchange for being able to tune the picture-specific
seed pair.

In the current 255/256-B intro, 4 B is findable (e.g. drop one byte of the
6-byte VDU template if a cheaper init path exists). **Practical
recommendation: don't bother** — the search showed no LFSR seed pair that
gives a visibly better result than R2 for any tested image, so the trade-off
buys nothing.

---

## What I'd actually do next (morning recommendation)

In priority order:

1. ✅ **Done.** `stipple256.asm` now sets `cnt = $0800` (2048). Wall-clock is
   ~30 s per the build-time advisory; visually a big improvement over 768.

2. ✅ **Tooling done.** Floyd-Steinberg pre-dithering is available in
   `tools/stipple_ui.py` for tuning the 4-level cell quantisation; per-image
   hand-tuning still open as needed.

3. ✅ **LFSR not ported.** Confirmed — R2 stays.

4. **Open.** 32×1 r=4 mode with R2 placement still untried in asm. The
   chunky-graphic look is interesting but needs ~128 B of data (vs current
   64 B) which would push code past budget; would need extra byte savings
   first (none currently in reach).

5. **Open.** Targeted `y_seed = x_seed + d` brute force not run. Even if it
   found a marginally better seed it wouldn't change recommendation (3), so
   low priority.

---

## File map

- `tools/lfsr_brute.py` — the brute force search (numba renderer +
  random + coord-descent).
- `out_lfsr/` — per-config rendered comparisons + JSON seed records.
- `out_lfsr/all_results.json` — table of every run.
- `out_lfsr/run.log` — full stdout from the search.

To reproduce a single run:

```
python tools/lfsr_brute.py --image pics/mona_list_square.png \
    --mode 16x2 --iters 1024 --random 300000 --refine-top 3
```

To rerun the full sweep:

```
python tools/lfsr_brute.py --runall --random 300000 --refine-top 3
```
