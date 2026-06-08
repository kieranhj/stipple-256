#!/usr/bin/env bash
# Build the BBC Micro stipple demo.
#
#   1. (optional) regenerate dot data from a source image via tools/stipple.py
#   2. assemble bbc/stipple.asm with BeebAsm into bbc/stipple.ssd
#
# Requirements (not vendored - build/point at them yourself):
#   BEEBASM : path to a beebasm binary   (github.com/stardot/beebasm)
#   ZX02    : path to a zx02 binary       (github.com/dmsc/zx02)
#
# Usage:
#   ./build.sh                         # just assemble (uses existing data/parrot.zx02)
#   IMAGE=/path/to/photo.png ./build.sh   # regenerate data from IMAGE, then assemble
#
set -euo pipefail
cd "$(dirname "$0")"

BEEBASM="${BEEBASM:-beebasm}"
ZX02="${ZX02:-zx02}"
STEM="${STEM:-parrot}"

if [[ -n "${IMAGE:-}" ]]; then
    echo ">> regenerating dot data from $IMAGE"
    python3 ../tools/stipple.py "$IMAGE" -o /tmp/stipple_build --stem "$STEM" \
        --fit contain --gamma "${GAMMA:-1.4}" --contrast "${CONTRAST:-1.15}" \
        -n "${DOTS:-1700}" --bbc --zx02 "$ZX02" -q
    cp "/tmp/stipple_build/$STEM.bbc.zx02" "data/$STEM.zx02"
    # sanity-check the plotter logic against the renderer
    python3 ../tools/verify_bbc.py "/tmp/stipple_build/$STEM.bbc.bin"
fi

echo ">> assembling stipple.asm"
"$BEEBASM" -i stipple.asm -do stipple.ssd -boot STIPPLE -v | tail -3
echo ">> built bbc/stipple.ssd  (boot it: SHIFT+BREAK, or *RUN STIPPLE)"
