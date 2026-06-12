// 4k-emu - tiny BBC Master emulator that runs stipple256 as a 4K Win32 .exe.
//
// Phases 0-3:
//   - Crinkler-linked WS_POPUP window, GDI present at 640x512 (Y-doubled).
//   - 6502 core: ~31 opcodes, N/Z/C flags. PC-trap at $FFEE -> vdu().
//   - VDU dispatcher: param-byte state machine, handles 5/12/17/18/22/25/29.
//   - 1bpp 640x256 framebuffer presented through a 2-colour DIB palette.
//   - Filled-ellipse rasteriser approximating Master GXR PLOT 157 (pixel-
//     space half-axes derived from logical radius via MODE 0's 2:4 scale).
//
// Run model: execute 6502 until PC pins (the `beq hang` self-loop), then
// the message pump shows the final framebuffer until ESC.

#include <Windows.h>
#include <intrin.h>
#include "stipple256_bin.h"

static unsigned char ram[65536];
static unsigned char fb1[80 * 256];        // 640x256, 1bpp, MSB-leftmost

// --- 6502 ---
static unsigned char A, X, Y, S;
static unsigned short PC;
static unsigned char fN, fZ, fC;

static int g_steps;
static int g_oswrch_bytes;

static __forceinline void setNZ(unsigned char v) { fZ = (v == 0); fN = (v >> 7) & 1; }
static __forceinline unsigned char fetch() { return ram[PC++]; }
static __forceinline unsigned short fetch16() { unsigned short lo = fetch(); return lo | (fetch() << 8); }

// --- VDU state ---
static unsigned char vdu_cmd;
static unsigned char vdu_pidx;             // params received so far
static unsigned char vdu_need;             // params still wanted
static unsigned char vdu_params[5];
static int g_originX, g_originY;
static int g_gx, g_gy;                     // graphics cursor (logical, pre-origin)

static int vdu_needed(unsigned char cmd) {
    // Number of param bytes after `cmd`. Anything else: zero (treat as no-op).
    switch (cmd) {
    case 17: case 22:        return 1;
    case 18:                 return 2;
    case 29:                 return 4;
    case 25:                 return 5;
    }
    return 0;
}

static inline void set_pixel(int px, int py) {
    if ((unsigned)px < 640 && (unsigned)py < 256)
        fb1[py * 80 + (px >> 3)] |= 0x80 >> (px & 7);
}

static void plot_filled_circle(int cx_log, int cy_log, int end_x_log) {
    // PLOT 157 (filled circle, abs, fg). Centre = (cx, cy) from preceding
    // MOVE; circumference point = (end_x, cy) (stipple256 always emits at the
    // same Y as the centre). The logical radius `rlog = end_x - cx` is in
    // {4, 12, 20} for r_asm in {1, 3, 5}.
    //
    // We draw an ANISOTROPIC ellipse in source pixels with half-axes
    // (a, b) = (rlog/2, rlog/4). Reasoning: MODE 0 logical X scales 2:1 to
    // pixels but logical Y scales 4:1 — so a circle in logical units lays
    // down a 2:1 horizontally-wide ellipse in pixel space. The 4:3 present
    // (640x480 from 640x256) then stretches Y by 1.875×, restoring near-
    // square dots on screen. AND stipple256's per-iteration coord step is
    // 2 source-px in X vs 1 source-px in Y, so neighboring dots cluster
    // more tightly vertically — the wide source ellipse counteracts that
    // so clusters don't end up looking like vertical streaks.
    //
    // Inclusion: `dx²·b² + dy²·a² ≤ a²·b² + a·b` — the `+ a·b` bias is the
    // ellipse analogue of Bresenham's `+ r` filled-disc bias and rounds out
    // the perimeter (otherwise small (2,1) ellipses look spiky).
    int rlog = end_x_log - cx_log;
    if (rlog < 0) rlog = -rlog;
    int a = rlog >> 1;                              // X half-axis (pixels)
    int b = rlog >> 2;                              // Y half-axis (pixels)
    int cpx = (cx_log + g_originX) >> 1;
    int cpy = 255 - ((cy_log + g_originY) >> 2);    // Y-flip
    int a2 = a * a;
    int b2 = b * b;
    int bound = a2 * b2 + a * b;
    for (int dy = -b; dy <= b; ++dy) {
        int dy2a2 = dy * dy * a2;
        for (int dx = -a; dx <= a; ++dx) {
            if (dx * dx * b2 + dy2a2 <= bound)
                set_pixel(cpx + dx, cpy + dy);
        }
    }
}

static void vdu_dispatch() {
    unsigned char* p = vdu_params;
    switch (vdu_cmd) {
    case 12:                              // CLS -- clear to background (white = 0)
        __stosd((unsigned long*)fb1, 0, (80 * 256) / 4);     // inline rep stosd
        break;
    case 29:                              // set origin
        g_originX = (short)(p[0] | (p[1] << 8));
        g_originY = (short)(p[2] | (p[3] << 8));
        break;
    case 25: {                            // PLOT k, x, y
        unsigned char k = p[0];
        int x = (short)(p[1] | (p[2] << 8));
        int y = (short)(p[3] | (p[4] << 8));
        if (k == 4) {                     // MOVE absolute
            g_gx = x; g_gy = y;
        } else if (k == 157) {            // filled circle, abs, fg
            plot_filled_circle(g_gx, g_gy, x);
        }
    } break;
    // 5, 17, 18, 22 -- intentional no-ops (cosmetic state we don't model).
    }
}

static void vdu(unsigned char a) {
    g_oswrch_bytes++;
    if (vdu_need) {
        vdu_params[vdu_pidx++] = a;
        if (--vdu_need == 0) vdu_dispatch();
        return;
    }
    vdu_cmd = a;
    vdu_pidx = 0;
    vdu_need = (unsigned char)vdu_needed(a);
    if (vdu_need == 0) vdu_dispatch();
}

static void cpu_step() {
    unsigned char op = fetch();
    switch (op) {
    case 0xA9: A = fetch(); setNZ(A); break;
    case 0xA5: A = ram[fetch()]; setNZ(A); break;
    case 0xBD: { unsigned short a = fetch16(); A = ram[(unsigned short)(a + X)]; setNZ(A); } break;
    case 0xB9: { unsigned short a = fetch16(); A = ram[(unsigned short)(a + Y)]; setNZ(A); } break;
    case 0x85: ram[fetch()] = A; break;
    case 0xA2: X = fetch(); setNZ(X); break;
    case 0xA0: Y = fetch(); setNZ(Y); break;
    case 0x94: ram[(unsigned char)(fetch() + X)] = Y; break;
    case 0xAA: X = A; setNZ(X); break;
    case 0xA8: Y = A; setNZ(Y); break;
    case 0x8A: A = X; setNZ(A); break;
    case 0x98: A = Y; setNZ(A); break;
    case 0x0A: fC = A >> 7; A <<= 1; setNZ(A); break;
    case 0x06: { unsigned char a = fetch(); fC = ram[a] >> 7; ram[a] <<= 1; setNZ(ram[a]); } break;
    case 0x4A: fC = A & 1; A >>= 1; setNZ(A); break;
    case 0x2A: { unsigned char c0 = fC; fC = A >> 7; A = (A << 1) | c0; setNZ(A); } break;
    case 0x26: { unsigned char a = fetch(); unsigned char c0 = fC; fC = ram[a] >> 7; ram[a] = (ram[a] << 1) | c0; setNZ(ram[a]); } break;
    case 0x69: { unsigned int r = A + fetch() + fC; fC = (r >> 8) & 1; A = (unsigned char)r; setNZ(A); } break;
    case 0x65: { unsigned int r = A + ram[fetch()] + fC; fC = (r >> 8) & 1; A = (unsigned char)r; setNZ(A); } break;
    case 0xE9: { unsigned int r = A + (fetch() ^ 0xFF) + fC; fC = (r >> 8) & 1; A = (unsigned char)r; setNZ(A); } break;
    case 0x29: A &= fetch(); setNZ(A); break;
    case 0x05: A |= ram[fetch()]; setNZ(A); break;
    case 0x18: fC = 0; break;
    case 0xE6: { unsigned char a = fetch(); ++ram[a]; setNZ(ram[a]); } break;
    case 0xC6: { unsigned char a = fetch(); --ram[a]; setNZ(ram[a]); } break;
    case 0xCA: --X; setNZ(X); break;
    case 0x88: --Y; setNZ(Y); break;
    case 0xF0: { signed char d = (signed char)fetch(); if ( fZ) PC = (unsigned short)(PC + d); } break;
    case 0xD0: { signed char d = (signed char)fetch(); if (!fZ) PC = (unsigned short)(PC + d); } break;
    case 0x30: { signed char d = (signed char)fetch(); if ( fN) PC = (unsigned short)(PC + d); } break;
    case 0x10: { signed char d = (signed char)fetch(); if (!fN) PC = (unsigned short)(PC + d); } break;
    case 0x90: { signed char d = (signed char)fetch(); if (!fC) PC = (unsigned short)(PC + d); } break;
    case 0x20: { unsigned short a = fetch16(); unsigned short r = (unsigned short)(PC - 1); ram[0x100 + S] = r >> 8; ram[0x100 + (unsigned char)(S - 1)] = r & 0xFF; S -= 2; PC = a; } break;
    case 0x60: { unsigned char lo = ram[0x100 + (unsigned char)(S + 1)]; unsigned char hi = ram[0x100 + (unsigned char)(S + 2)]; S += 2; PC = (unsigned short)(((hi << 8) | lo) + 1); } break;
    // 0x12 (KIL/JAM, undocumented) is our OSWRCH trap byte. We plant `12 60`
    // at $FFEE so `jsr $FFEE` lands here -> we run vdu(A), then the next
    // fetch picks up the 0x60 (RTS) and returns to caller. Saves the
    // PC=$FFEE check from the top of cpu_step.
    case 0x12: vdu(A); break;
    default: {
#if !defined(RELEASE)
        char msg[128];
        wsprintfA(msg, "UNKNOWN opcode %02X at PC=%04X (step %d)\n", op, (unsigned short)(PC - 1), g_steps);
        DWORD wr; WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), msg, lstrlenA(msg), &wr, NULL);
#endif
        ExitProcess((unsigned)op);
    }}
    g_steps++;
}

// 1bpp BITMAPINFO with a 2-entry palette. Windows.h's BITMAPINFO has a
// 1-entry trailing palette, so we declare our own.
struct BMI1BPP {
    BITMAPINFOHEADER h;
    RGBQUAD pal[2];
};

// Global, statically initialised. Goes into .data and is correct at PE
// load time -- no runtime init needed. Crinkler compresses .data tightly
// (mostly zeros).
static BMI1BPP gBMI = {
    { sizeof(BITMAPINFOHEADER), 640, -256, 1, 1, BI_RGB, 0, 0, 0, 0, 0 },
    { { 0xFF, 0xFF, 0xFF, 0 }, { 0, 0, 0, 0 } },
};

#if defined(RELEASE)
int WinMainCRTStartup()
#else
int main()
#endif
{
    HWND hwnd = CreateWindowExA(
        0, (LPCSTR)0xC018, "", WS_POPUP | WS_VISIBLE,
        50, 50, 640, 480, NULL, NULL, NULL, NULL);
    HDC hdc = GetDC(hwnd);

    // Load stipple256 ROM into emulated RAM at $1900 (inline rep movsb).
    __movsb(ram + kStipple256LoadAddr, kStipple256Bin, kStipple256BinLen);

    // OSWRCH trap stub at $FFEE: opcode 0x12 (vdu(A)), then 0x60 (RTS).
    ram[0xFFEE] = 0x12;
    ram[0xFFEF] = 0x60;

    // Run the 6502 to completion (PC pinned by `beq hang`).
    // A,X,Y,S,fN,fZ,fC are static BSS = already zero; we just need PC.
    // S=0xFD is the BBC reset convention but stipple256 doesn't depend on it
    // (only JSR/RTS use the stack, and they balance regardless of init).
    PC = kStipple256LoadAddr;
    for (;;) {
        unsigned short before = PC;
        cpu_step();
        if (PC == before) break;
    }

#if !defined(RELEASE)
    {
        char buf[128];
        wsprintfA(buf, "steps=%d oswrch=%d  A=%02X X=%02X Y=%02X PC=%04X",
                  g_steps, g_oswrch_bytes, A, X, Y, PC);
        SetWindowTextA(hwnd, buf);
    }
#endif

    // Draw the (already-final) framebuffer once. Note: if the window is
    // ever obscured by another window, it'll go blank on uncover because
    // we don't handle WM_PAINT. For a one-shot intro that's acceptable.
    StretchDIBits(hdc, 0, 0, 640, 480, 0, 0, 640, 256,
                  fb1, (BITMAPINFO*)&gBMI, DIB_RGB_COLORS, SRCCOPY);
    while (!(GetAsyncKeyState(VK_ESCAPE) & 0x8000));
    ExitProcess(0);
    return 0;
}
