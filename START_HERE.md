# stipple-256: start here

A 256-byte stipple "photo" intro for the BBC Master (sizecoding: GXR filled-circle PLOTs at R2
sequence positions, radius from a 16x16 2bpp image), plus the Python tools that design the dot
pattern. Also an older 4 KB MODE 4 stipple player it was carved out of. Submitted to NOVA 2026.

| | |
|---|---|
| GitHub | https://github.com/kieranhj/stipple-256 (public) |
| Local path | D:\beeb\mine\stipple-256 (moved from OneDrive\BEEB\Repos\stipple-256 on 2026-11-22) |
| Branch | main; others: 4k-emu, stash/4k-emu-2026-06-13, wip/2026-11-tidy |
| Last commit | 2026-06-13: NOVA submission. |

## What's in here
- `bbc/stipple256.asm` the 256-byte intro; `bbc/build_256.sh` its build; `bbc/data/mona16.bin` the image
- `bbc/stipple.asm` (+ spans.asm, zx02_decompress.asm) and `bbc/build.sh`: the legacy 4 KB player
- `tools/stipple.py` image -> binary, `tools/stipple_ui.py` Gradio tuner, `tools/lfsr_*.py` LFSR experiments
- `tools/beebasm.exe` (gitignored, present), `submission/` the NOVA entry (ssd, png, zip, readme)
- `docs/STIPPLE-256.md`, `STIPPLE-256-LFSR.md`, `STIPPLE.md` design notes; `out_lfsr_*/` (gitignored) experiment output

## Build and run
- `make`: runs `bbc/build_256.sh` under Git for Windows' bash (`C:\Program Files\Git\bin\bash.exe`;
  override with `make BASH=...`): converts `pics/mona_lisa_crop.png` -> `bbc/data/mona16.bin`,
  assembles -> `bbc/stipple256.ssd` and prints the byte budget. `make player` builds the 4 KB
  player (`bbc/build.sh`), `make help`. Makefile added 2026-11-22; **not yet run**, because the
  build rewrites `bbc/data/mona16.bin` and `bbc/stipple256.ssd`, which have uncommitted changes.
- Needs Python 3 with pillow (and numpy) and `tools/beebasm.exe`. `build_256.sh --run` opens the
  .ssd with the Windows file association (an emulator).

## Local changes
- Uncommitted: `bbc/data/mona16.bin`, `bbc/stipple256.ssd` modified; `CLAUDE.md` untracked.
- One stash (`stash@{0}`: WIP on 4k-emu), also pushed as branch `stash/4k-emu-2026-06-13`.
- `wip/2026-11-tidy` (pushed 2026-11-21): snapshot of the uncommitted work on main (the two files
  above plus CLAUDE.md).
- `4k-emu`: 3 commits not on main - a native Win32 BBC Master mini-emulator that runs stipple256
  (code-golfed to 1545 B).

## Status and next steps
- The intro was submitted (2026-06-13); 241 of 256 bytes used (15 spare) at the iteration lock.
- Open: whether to commit CLAUDE.md and the changed image/ssd, and what to do with 4k-emu.

## See also
README.md, CLAUDE.md, docs/, submission/readme.txt.
