#!/usr/bin/env bash
# Convert a source image -> bbc/data/mona16.bin, assemble stipple256.asm,
# report binary size, and optionally open the resulting .ssd.
#
# Run from anywhere; the script cd's to its own directory (bbc/).
#
# Usage:
#   ./build_256.sh                                       convert pics/mona_lisa_square.png, assemble
#   ./build_256.sh <image>                               convert <image>, assemble
#                                                        (absolute, repo-relative, or pics/<name>)
#   ./build_256.sh --run                                 default image, assemble, open .ssd
#   ./build_256.sh <image> --run                         custom image, assemble, open .ssd
#   ./build_256.sh [<image>] [--run] [extra args ...]    extra args after the image are
#                                                        passed straight through to
#                                                        tools/stipple.py --mode256, e.g.
#                                                          ./build_256.sh face.png --mode256-size 24
#                                                          ./build_256.sh --mode256-levels 8
#                                                        Later overrides earlier, so a passthrough
#                                                        flag beats our default size/levels.
set -euo pipefail

BBC_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$BBC_DIR/.." && pwd)"

# --- parse args ---
# Convention: if the first arg is positional (no leading dash) we treat it as
# the source image. Everything else is sorted into either --run or
# stipple.py-passthrough.
RUN=
IMG=
PASSTHROUGH=()

if [ $# -gt 0 ] && [[ "${1:-}" != -* ]]; then
    IMG="$1"
    shift
fi
while [ $# -gt 0 ]; do
    if [ "$1" = "--run" ]; then
        RUN=1
    else
        PASSTHROUGH+=("$1")
    fi
    shift
done

IMG="${IMG:-$REPO/pics/mona_lisa_square.png}"

# resolve image path: absolute, then repo-relative, then pics/-relative
if [ ! -f "$IMG" ]; then
    if [ -f "$REPO/$IMG" ]; then
        IMG="$REPO/$IMG"
    elif [ -f "$REPO/pics/$IMG" ]; then
        IMG="$REPO/pics/$IMG"
    else
        echo "image not found: $IMG" >&2
        exit 1
    fi
fi

# --- 1. convert image -> bbc/data/mona16.bin ---
STEM=$(basename "$IMG" | sed 's/\.[^.]*$//')
TMP="$BBC_DIR/data/.build_tmp"
mkdir -p "$TMP"
echo ">> converting $IMG"
if [ ${#PASSTHROUGH[@]} -gt 0 ]; then
    echo "   extra stipple.py args: ${PASSTHROUGH[*]}"
fi
python "$REPO/tools/stipple.py" --mode256 "$IMG" --stem "$STEM" \
    --mode256-size 16 --mode256-levels 4 \
    -o "$TMP" -q \
    ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
cp "$TMP/${STEM}_mode256_src.bin" "$BBC_DIR/data/mona16.bin"
echo "   wrote bbc/data/mona16.bin (from $STEM)"

# --- 2. assemble ---
cd "$BBC_DIR"
out=$(../tools/beebasm.exe -i stipple256.asm -do stipple256.ssd -boot STIP256 -v 2>&1)
echo "$out"

# Size report: code runs from $1900 to whatever address precedes `.image`,
# then the actual INCBIN'd image bytes follow.
last_code_addr=$(echo "$out" | grep -B1 -E "^\.image$" | head -1 | awk '{print $1}')
image_bytes=$(wc -c < "$BBC_DIR/data/mona16.bin")
if [ -n "$last_code_addr" ]; then
    code_bytes=$(( 0x${last_code_addr} - 0x1900 + 1 ))
    total=$(( code_bytes + image_bytes ))
    spare=$(( 256 - total ))
    echo
    echo "wrote: stipple256.ssd"
    echo "  code ends at \$${last_code_addr}"
    if [ "$spare" -lt 0 ]; then
        echo "  ${code_bytes} B code + ${image_bytes} B image = ${total} / 256  (OVER by $(( -spare )) B!)"
        echo "  (the asm INCBIN's the whole .bin; image grew past 64 B — "
        echo "   pass --mode256-size 16 to stay on budget, or shrink levels.)"
    else
        echo "  ${code_bytes} B code + ${image_bytes} B image = ${total} / 256  (spare: ${spare})"
    fi
else
    echo
    echo "wrote: stipple256.ssd  (size report skipped — couldn't parse listing)"
fi

# --- 3. optional --run ---
if [ -n "$RUN" ]; then
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
