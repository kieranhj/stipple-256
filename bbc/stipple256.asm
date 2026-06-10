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
buf     = &70           ; 6 bytes: [25, k, xL, xH, yL, yH]
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

    lda #25 : sta buf+0

    lda #8
    sta cnt_hi                       ; 16-bit counter = $0800 = 2048 iters

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

    ; --- gx = px*4 -> buf+2..3 ---
    lda xa+1
    asl A
    sta buf+2
    lda #0
    rol A
    sta buf+3
    asl buf+2 : rol buf+3

    ; --- gy = py*4 -> buf+4..5 ---
    lda ya+1
    asl A
    sta buf+4
    lda #0
    rol A
    sta buf+5
    asl buf+4 : rol buf+5

    ; --- emit MOVE (k=4) ---
    lda #4 : sta buf+1
    jsr emit6

    ; --- mutate for PLOT (k=157, x += r*4) ---
    lda #157 : sta buf+1
    txa                              ; A = r (was stashed in X above)
    asl A : asl A                    ; r*4; carry clear since r<=7
    adc buf+2 : sta buf+2
    bcc emit_plot
    inc buf+3
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
    bne loop                         ; Z=0 here (from dec cnt_lo via bne, or
                                     ; dec cnt_hi via beq-not-taken). 1 B
                                     ; shorter than jmp loop.

.emit6
    ldy #0
.e6_lp
    lda buf,y
    jsr oswrch
    iny
    cpy #6
    bne e6_lp
    rts

; --- VDU init ---
; bytes stored in REVERSE; init loop reads them with X decreasing so they
; reach OSWRCH in the intended order: 22,4, 17,129, 12, 18,0,0
;   22,4    MODE 4
;   17,129  text bg = logical 1 (white)
;   12      CLS
;   18,0,0  GCOL 0,0 (plot logical 0 = black)
.vdutab
    EQUB 0, 0, 18                    ; GCOL 0,0 (read last)
    EQUB 5                           ; VDU 5 (text at graphics cursor -> hides
                                     ; the flashing text cursor). MUST come
                                     ; after CLS, otherwise CLS sees text-emit-
                                     ; goes-to-graphics and behaves differently.
    EQUB 12                          ; CLS (clears text window to bg = white)
    EQUB 129, 17                     ; COLOUR 129 (text bg = logical 1 = white)
    EQUB 4, 22                       ; MODE 4 (read first)
vdulen = * - vdutab

.image
    INCBIN "data/mona16.bin"

.prog_end

SAVE "STIP256", start, prog_end, start
