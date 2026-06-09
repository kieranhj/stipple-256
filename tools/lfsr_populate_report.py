#!/usr/bin/env python3
"""Read out_lfsr/all_results.json and emit markdown table fragments for
docs/STIPPLE-256-LFSR.md. Prints to stdout for manual paste-in."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RES = ROOT / "out_lfsr" / "all_results.json"

if not RES.exists():
    sys.exit(f"{RES} not found; run --runall first")

results = json.loads(RES.read_text())

def by(image, mode, iters, fixed_r=None):
    """Return result dict for the given key, or None."""
    for r in results:
        if r["mode"] != mode or r["n_iters"] != iters:
            continue
        if Path(r["image"]).stem != image:
            continue
        if fixed_r is not None and r["fixed_r"] != fixed_r:
            continue
        return r
    return None


def pct(lfsr, r2):
    if r2 <= 0:
        return "n/a"
    delta = (r2 - lfsr) / r2 * 100
    return f"{delta:+.1f}%"


print("\n## Headline numbers (auto-generated)\n")
print(f"Total configs run: {len(results)}")
n_wins = sum(1 for r in results if r["best_mse"] < r["r2_mse"])
print(f"LFSR-beats-R2 configs: {n_wins} / {len(results)}\n")

print("\n### Full result table\n")
print("| image | mode | iters | r | LFSR x | LFSR y | LFSR MSE | R2 MSE | Δ vs R2 | search time |")
print("|-------|------|------:|--:|-------:|-------:|---------:|-------:|---------|------------:|")
for r in results:
    img = Path(r["image"]).stem
    fr = r["fixed_r"] if r["mode"] == "32x1" else "—"
    print(f"| {img} | {r['mode']} | {r['n_iters']} | {fr} | "
          f"{r['best_x']:5d} | {r['best_y']:5d} | "
          f"{r['best_mse']:.6f} | {r['r2_mse']:.6f} | "
          f"{pct(r['best_mse'], r['r2_mse'])} | {r['elapsed_s']:.0f}s |")

print("\n### Per-image best (16x2 mode, 1024 iters)\n")
print("| image | LFSR x_seed | LFSR y_seed | LFSR MSE | R2 MSE | Δ |")
print("|-------|------------:|------------:|---------:|-------:|---|")
for img in ["mona_list_square", "face", "eye"]:
    r = by(img, "16x2", 1024)
    if r is None:
        continue
    print(f"| {img} | {r['best_x']} (0x{r['best_x']:04X}) | "
          f"{r['best_y']} (0x{r['best_y']:04X}) | "
          f"{r['best_mse']:.6f} | {r['r2_mse']:.6f} | "
          f"{pct(r['best_mse'], r['r2_mse'])} |")

print("\n### Mode comparison @ 1024 iters per image (LFSR MSE / R2 MSE)\n")
print("| image | 16x2 LFSR | 16x2 R2 | 32x1 r=3 LFSR | 32x1 r=3 R2 | 32x1 r=4 LFSR | 32x1 r=4 R2 |")
print("|-------|----------:|--------:|--------------:|------------:|--------------:|------------:|")
for img in ["mona_list_square", "face", "eye"]:
    row = [img]
    for mode, fr in [("16x2", None), ("32x1", 3), ("32x1", 4)]:
        r = by(img, mode, 1024, fr)
        if r is None:
            row += ["—", "—"]
        else:
            row += [f"{r['best_mse']:.4f}", f"{r['r2_mse']:.4f}"]
    print("| " + " | ".join(row) + " |")

print("\n### Mode comparison @ 2048 iters per image\n")
print("| image | 16x2 LFSR | 16x2 R2 | 32x1 r=3 LFSR | 32x1 r=3 R2 | 32x1 r=4 LFSR | 32x1 r=4 R2 |")
print("|-------|----------:|--------:|--------------:|------------:|--------------:|------------:|")
for img in ["mona_list_square", "face", "eye"]:
    row = [img]
    for mode, fr in [("16x2", None), ("32x1", 3), ("32x1", 4)]:
        r = by(img, mode, 2048, fr)
        if r is None:
            row += ["—", "—"]
        else:
            row += [f"{r['best_mse']:.4f}", f"{r['r2_mse']:.4f}"]
    print("| " + " | ".join(row) + " |")

# pick the "headline" claim
best_by_image = {}
for r in results:
    img = Path(r["image"]).stem
    delta = (r["r2_mse"] - r["best_mse"]) / max(r["r2_mse"], 1e-9)
    if img not in best_by_image or delta > best_by_image[img][0]:
        best_by_image[img] = (delta, r)

print("\n### Best LFSR-vs-R2 result for each image (any config)\n")
for img, (delta, r) in best_by_image.items():
    print(f"- **{img}**: best at mode {r['mode']}, "
          f"iters {r['n_iters']}"
          f"{', r='+str(r['fixed_r']) if r['mode']=='32x1' else ''}: "
          f"LFSR MSE {r['best_mse']:.4f} vs R2 {r['r2_mse']:.4f} "
          f"({pct(r['best_mse'], r['r2_mse'])})")
