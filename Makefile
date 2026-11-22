# Wrapper so that `make` works from PowerShell / Windows Terminal.
# GNU make runs recipes with cmd.exe here (no sh on PATH), so the bash build
# scripts run under Git for Windows' bash. Override with BASH=... if needed.
BASH ?= "C:\Program Files\Git\bin\bash.exe"

.PHONY: all build player help
all: build

# 256-byte intro: pics/mona_lisa_crop.png -> bbc/data/mona16.bin -> bbc/stipple256.ssd
build:
	$(BASH) bbc/build_256.sh

# The 4 KB player (set BEEBASM/ZX02/IMAGE as CLAUDE.md says)
player:
	$(BASH) bbc/build.sh

help:
	@echo make         - build the 256-byte intro bbc/stipple256.ssd (bbc/build_256.sh)
	@echo make player  - build the 4 KB player (bbc/build.sh)
	@echo make help    - this list
	@echo Needs Python 3 with pillow and tools/beebasm.exe. See START_HERE.md
