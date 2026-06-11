; stipple256.asm - 256-byte stipple intro for the BBC Master.
; ---------------------------------------------------------------------------
; 16x16 @ 2bpp stored image (64 B). R2 placement (plastic-phi additive),
; per-dot cell lookup, r = cell*2-1 -> {0,1,3,5} pixel radii (0 = skip).
; GXR PLOT 157 (filled circle absolute, fg). Buffer-loop OSWRCH: a 6-byte
; VDU template is mutated and emitted twice per dot (MOVE then PLOT).
; 16-bit zero-page counter for the 2048-iteration dot loop (X is clobbered by
; the image lookup so it can't be the counter).
;
; Build:  ..\tools\beebasm.exe -i stipple256.asm -do stipple256.ssd -boot STIP256 -v
; Boot:   the SSD autoboots via *RUN STIP256.
;
; Source bytes encode darkness (0=light -> r=0 skip; 3=dark -> r=6 max),
; LSB-first packed, vertically flipped so the BBC y=0-at-bottom convention
; lines up with the source image's natural top-down orientation. All
; produced by `python tools/stipple.py --mode256 <img> ...`.
; ---------------------------------------------------------------------------

oswrch  = &FFEE

XINC    = &C142
YINC    = &91DF

; --- zero page ---
; xa/ya/cnt_lo MUST stay contiguous at $7C..$80 — the zero-init loop relies
; on it. r lives in X (no ZP slot) — see "tax" after the asl/sbc.
buf     = &70           ; 6 bytes: [yH, yL, xH, xL, k, 25]  (reversed; emit6
                        ; counts Y down from 5 so buf[5]=25 is sent first).
xa      = &7C
ya      = &7E
cnt_lo  = &80
cnt_hi  = &81
tmp     = &82

ORG &1900
GUARD &7C00

.start
    ; --- VDU init, sent in reverse via dex/bpl (saves 2 bytes vs forward) ---
    ldx #vdulen-1
    stx cnt_hi                       ; 16-bit iter counter = vdulen-1 in hi byte.
                                     ; With current vdutab (14 bytes incl. VDU 29
                                     ; origin centring), vdulen-1 = 13, so the
                                     ; counter starts at $0D00 = 3328 iters.
                                     ; Stashing X here is free (2 B); the
                                     ; alternative `lda # / sta cnt_hi` is 4 B.
                                     ; Iter count is whatever vdulen-1 happens
                                     ; to be — if you ever want to lock it to
                                     ; a specific value, add explicit init here.
.vinit
    lda vdutab,x
    jsr oswrch
    dex
    bpl vinit

    ; --- A = 0 free here (vdutab[0] = 0 was the last byte read by vinit). ---
    ; Zero xa, xa+1, ya, ya+1, cnt_lo in one indexed loop (5 contiguous ZP).
    ldx #4
.zerolp
    sta xa,x                         ; sta &7C,x  (zp,x mode)
    dex
    bpl zerolp

    lda #25 : sta buf+5              ; emitted first (Y=5)

.loop
    ; --- R2 --- (ya first so A = xa+1 falls through into the cell lookup)
    clc
    lda ya   : adc #LO(YINC) : sta ya
    lda ya+1 : adc #HI(YINC) : sta ya+1
    clc
    lda xa   : adc #LO(XINC) : sta xa
    lda xa+1 : adc #HI(XINC) : sta xa+1

    ; --- image lookup (16x16 @ 2bpp LSB-first); A = xa+1 already ---
    lsr A : lsr A : lsr A : lsr A
    sta tmp
    lda ya+1
    and #&F0
    ora tmp
    tay                              ; Y = cell_idx (kept for shift count)
    lsr A : lsr A
    tax                              ; X = byte offset (CLOBBERS LOOP X — fine,
                                     ;                    loop counter is in cnt_*)
    tya
    and #3
    asl A
    tay                              ; Y = shift count (0,2,4,6)
    lda image,x
    dey
    bmi sh_done
.shloop
    lsr A
    dey
    bpl shloop
.sh_done
    and #3
    beq skip_dot
    asl A
    sbc #0                           ; r = cell*2 - 1 -> {1, 3, 5} (odd radii;
    tax                              ; carry is clear after asl since cell<=3)
                                     ; r lives in X across emit6 (OSWRCH and
                                     ; emit6 both preserve X).

    ; --- gx = px*4 -> buf+3 (lo), buf+2 (hi) (reversed layout)
    ; Picture sits left-aligned on screen (no horizontal stretch). Spans
    ; 0..1020 logical units of MODE 0's 1280 width — i.e. left 80% of screen,
    ; right ~260 units empty. Centring would cost ~11 B back (see commit log);
    ; we're spending the saved bytes elsewhere.
    lda xa+1
    asl A
    sta buf+3
    lda #0
    rol A
    sta buf+2
    asl buf+3 : rol buf+2            ; buf+3:buf+2 = px*4

    ; --- gy = py*4 -> buf+1 (lo), buf+0 (hi) ---
    lda ya+1
    asl A
    sta buf+1
    lda #0
    rol A
    sta buf+0
    asl buf+1 : rol buf+0

    ; --- emit MOVE (k=4) ---
    lda #4 : sta buf+4
    jsr emit6

    ; --- mutate for PLOT (k=157, x += r*4) ---
    lda #157 : sta buf+4
    txa                              ; A = r (was stashed in X above)
    asl A : asl A                    ; r*4; carry clear since r<=7
    adc buf+3 : sta buf+3
    bcc emit_plot
    inc buf+2
.emit_plot
    jsr emit6

.skip_dot
    ; --- 16-bit counter decrement; halt when cnt_hi rolls 1->0.
    dec cnt_lo
    bne not_yet
    dec cnt_hi
.hang
    beq hang                         ; serves double duty: first hit (cnt_hi=0)
                                     ; branches to itself = infinite loop;
                                     ; otherwise Z=0 falls through.
.not_yet
    bne loop                         ; back-edge to .loop. Z=0 here in both
                                     ; paths (bne not_yet taken with Z=0;
                                     ; or .hang fell through with Z=0), so
                                     ; bne is unconditional in practice.
                                     ; Reach: offset exactly -128 -- the
                                     ; loop body is sized to the millimetre.

.emit6
    ldy #5                           ; emit buf[5], buf[4], ..., buf[0]
.e6_lp
    lda buf,y
    jsr oswrch
    dey
    bpl e6_lp                        ; dey/bpl is 3 B vs iny/cpy #6/bne = 5 B
    rts

; --- VDU init ---
; bytes stored in REVERSE; init loop reads them with X decreasing so they
; reach OSWRCH in the intended order:
;   22, 0           MODE 0  (640x256 1bpp; 5:4 logical aspect comp by MOS)
;   17, 129         COLOUR 129 (text bg = logical 1 = white)
;   12              CLS  (clears to background = white)
;   5               VDU 5 (text at graphics cursor — hides flashing cursor).
;                   MUST come after CLS, else CLS sees text-in-graphics-mode
;                   and behaves differently.
;   18, 0, 0        GCOL 0, 0 (plot logical 0 = black)
;   29, 130, 0, 0, 0  Move origin: shifts all PLOT/MOVE coords by (+130, +0)
;                   to centre the gx*4 picture in the 1280-wide MODE 0 screen.
;                   Image spans logical 0..1020 in x; with origin 130 it lands
;                   at 130..1150 — ~130 units left and right margin.
.vdutab
    EQUB 0, 0, 0, 130, 29            ; VDU 29 — set origin (130, 0). Read last.
    EQUB 0, 0, 18                    ; GCOL 0, 0
    EQUB 5                           ; VDU 5
    EQUB 12                          ; CLS
    EQUB 129, 17                     ; COLOUR 129
    EQUB 0, 22                       ; MODE 0 (read first)
vdulen = * - vdutab

.image
    INCBIN "data/mona16.bin"

.prog_end

SAVE "STIP256", start, prog_end, start
