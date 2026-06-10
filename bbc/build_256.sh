#!/usr/bin/env bash
# Assemble stipple256.asm -> stipple256.ssd and report size.
# Run from anywhere; the script cd's to its own directory (bbc/).
# Usage:  ./build_256.sh           (build only)
#         ./build_256.sh --run     (build, then open the SSD in the default app)
set -euo pipefail

cd "$(dirname "$0")"

out=$(../tools/beebasm.exe -i stipple256.asm -do stipple256.ssd -boot STIP256 -v 2>&1)
echo "$out"

# Size report: code runs from $1900 to whatever address precedes `.image`,
# then 64 B of image data follow.
last_code_addr=$(echo "$out" | grep -B1 -E "^\.image$" | head -1 | awk '{print $1}')
if [ -n "$last_code_addr" ]; then
    code_bytes=$(( 0x${last_code_addr} - 0x1900 + 1 ))
    total=$(( code_bytes + 64 ))
    spare=$(( 256 - total ))
    echo
    echo "wrote: stipple256.ssd"
    echo "  code ends at \$${last_code_addr}"
    echo "  ${code_bytes} B code + 64 B image = ${total} / 256  (spare: ${spare})"
else
    echo
    echo "wrote: stipple256.ssd  (size report skipped — couldn't parse listing)"
fi

if [ "${1:-}" = "--run" ]; then
    # Open the SSD with whatever the OS associates with .ssd (e.g. BeebEm, B-em).
    # Falls back gracefully if `start` isn't available (non-Windows shell).
    if command -v cmd.exe >/dev/null 2>&1; then
        cmd.exe /c start "" "stipple256.ssd"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open stipple256.ssd
    elif command -v open >/dev/null 2>&1; then
        open stipple256.ssd
    else
        echo "no opener found; load bbc/stipple256.ssd manually"
    fi
fi
