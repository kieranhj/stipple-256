# stipple-256

A 256-byte stipple "photo" intro for the BBC Master, plus the Python tooling
used to design the dot pattern and downsample source images into the on-device
data block.

The intro renders a stippled portrait by emitting filled-circle GXR PLOTs at
positions drawn from a low-discrepancy R2 (plastic-phi) sequence. A 16×16 ×
2bpp downsampled image (64 bytes) is read per dot to pick one of four radii
({0, 1, 3, 5} pixels — 0 skips). Total binary: 256 bytes.

```
bbc/stipple256.asm          the 256-byte intro
bbc/build_256.sh            convert image -> .bin, assemble -> .ssd
tools/stipple.py            offline image -> mode256 binary
tools/stipple_ui.py         Gradio preview tool for tuning
docs/STIPPLE-256.md         design notes
docs/STIPPLE-256-LFSR.md    LFSR vs R2 sequence experiments
```

There's also a larger 4 KB MODE 4 stipple player (`bbc/stipple.asm`,
`docs/STIPPLE.md`) — the earlier version this 256-byte target was carved out
of.

## Build

The 256-byte intro:

```sh
cd bbc
./build_256.sh                          # default image: pics/mona_lisa_crop.png
./build_256.sh face.png                 # use pics/face.png
./build_256.sh face.png --mode256-size 16 --mode256-levels 4
./build_256.sh --run                    # also open the resulting .ssd
```

Output: `bbc/stipple256.ssd` (autoboots via `*RUN STIP256`).

Requires `tools/beebasm.exe` (not committed — download from
<https://github.com/stardot/beebasm>) and a Python 3 install with `pillow`,
`numpy`, and (for the UI) `gradio`.

## Preview UI

```sh
python tools/stipple_ui.py
```

A Gradio app for tuning radius mappings, iteration counts, and aspect handling
against the BBC's MODE 0 / MODE 4 dot geometry before committing the asm
parameters.

## License

MIT — see [LICENSE](LICENSE).
