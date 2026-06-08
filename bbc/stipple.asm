; stipple.asm - BBC Micro MODE 4 stipple-image player (Phase 3, first pass)
; ---------------------------------------------------------------------------
; Decompresses a ZX02 dot stream and plots variable-size black dots on a white
; MODE 4 (320x256, 1bpp) screen. Built with BeebAsm.
;
;   beebasm -i stipple.asm -do stipple.ssd -boot STIPPLE -v
;
; Data is produced by tools/stipple.py --bbc (see docs/STIPPLE.md for the
; stream format). Dot centres are pre-clamped so discs never cross the screen
; edge, so the plotter needs no clipping.
;
; STATUS: assembles clean; NOT yet run on hardware/emulator. See docs/STIPPLE.md.
; ---------------------------------------------------------------------------

oswrch  = &FFEE

; --- RAM buffers (not part of the saved file) ---
rowlo    = &4000        ; 256-entry screen row address tables, built at runtime
rowhi    = &4100
out_addr = &4400        ; ZX02 depack target (the dot stream)

SCREEN   = &5800        ; MODE 4 screen base

; --- zero page for the plotter ($70-$83, all within the free &70-&8F block).
;     The ZX02 decompressor uses $80-$89 but only *before* plotting starts, so
;     the tail of this block reuses that space harmlessly. ---
STRM  = &70             ; 2  stream read pointer
SPAN  = &72             ; 2  current radius span-table base
SPANI = &74             ; 2  working span pointer (per dot)
SRC   = &76             ; 2  screen write pointer
Xpos  = &78             ; 2  current dot x (0..319)
Xs    = &7A             ; 2  span start x (Xpos + dx)
Ypos  = &7C             ; 1  current dot y
MASK  = &7D             ; 1  current pixel bit mask
LEN   = &7E             ; 1  pixels left in span
dxt   = &7F             ; 1  span dx (signed)
dyt   = &80             ; 1  span dy (signed)
nbk   = &81             ; 1  buckets remaining
nln   = &82             ; 1  lines remaining in bucket
ndot  = &83             ; 1  dots remaining on line

ORG &1900
GUARD SCREEN

.start
    ; --- send the VDU init sequence ---
    ldx #0
.vinit
    lda vdutab,x
    jsr oswrch
    inx
    cpx #(vdutab_end - vdutab)
    bne vinit

    jsr build_rowbase
    jsr full_decomp                 ; depack comp_data -> out_addr

    lda #LO(out_addr) : sta STRM
    lda #HI(out_addr) : sta STRM+1
    jsr plot_all

.hang
    jmp hang

.vdutab
    EQUB 22, 4                      ; MODE 4 (320x256, 2 colour)
    EQUB 23, 1, 0, 0, 0, 0, 0, 0, 0, 0   ; cursor off
    EQUB 19, 0, 7, 0, 0, 0         ; logical colour 0 -> white  (background)
    EQUB 19, 1, 0, 0, 0, 0         ; logical colour 1 -> black  (dots)
.vdutab_end

; ---------------------------------------------------------------------------
; getstrm - read one byte from the dot stream, advance pointer. Returns A.
; ---------------------------------------------------------------------------
.getstrm
    ldy #0
    lda (STRM),y
    inc STRM
    bne gs_done
    inc STRM+1
.gs_done
    rts

; ---------------------------------------------------------------------------
; plot_all - parse the decompressed stream and plot every dot.
; ---------------------------------------------------------------------------
.plot_all
    jsr getstrm
    sta nbk                         ; number of bucket-entries

.pa_bucket
    jsr getstrm                     ; radius
    tax
    lda spanptr_lo,x : sta SPAN
    lda spanptr_hi,x : sta SPAN+1
    jsr getstrm
    sta nln                         ; lines in this bucket

.pa_line
    jsr getstrm
    sta Ypos                        ; absolute y
    jsr getstrm
    sta ndot                        ; dots on this line
    lda #0
    sta Xpos
    sta Xpos+1

.pa_dot
    ; accumulate delta-x into Xpos (0xFF = continue / escape)
.pa_dx
    jsr getstrm
    pha
    clc
    adc Xpos
    sta Xpos
    bcc pa_dx_nc
    inc Xpos+1
.pa_dx_nc
    pla
    cmp #255
    beq pa_dx

    jsr plotdot

    dec ndot
    bne pa_dot
    dec nln
    bne pa_line
    dec nbk
    bne pa_bucket
    rts

; ---------------------------------------------------------------------------
; plotdot - plot the dot at (Xpos,Ypos) using the span list at SPAN.
; ---------------------------------------------------------------------------
.plotdot
    lda SPAN   : sta SPANI
    lda SPAN+1 : sta SPANI+1
.pd_loop
    ldy #0
    lda (SPANI),y : sta dyt
    iny
    lda (SPANI),y : sta dxt
    iny
    lda (SPANI),y : sta LEN
    bne pd_go
    rts                             ; len==0 terminates the span list
.pd_go
    clc
    lda SPANI : adc #3 : sta SPANI
    bcc pd_a
    inc SPANI+1
.pd_a
    ; row pointer = rowtab[Ypos + dyt]   (signed dyt, wraps mod 256, in range)
    lda Ypos
    clc
    adc dyt
    tay
    lda rowlo,y : sta SRC
    lda rowhi,y : sta SRC+1
    ; Xs = Xpos + sign_extend(dxt)
    lda Xpos
    clc
    adc dxt
    sta Xs
    lda Xpos+1
    bit dxt                         ; N = bit7 of dxt (does not touch carry/A)
    bpl pd_pos
    adc #&FF                        ; dxt negative -> add $FF (+carry)
    jmp pd_hi
.pd_pos
    adc #0                          ; dxt positive -> add carry only
.pd_hi
    sta Xs+1
    ; MASK = maskTab[Xs & 7]
    lda Xs
    and #7
    tax
    lda maskTab,x
    sta MASK
    ; SRC += (Xs & $FFF8)   (== (Xs>>3)*8, the byte column on this row)
    lda Xs
    and #&F8
    clc
    adc SRC
    sta SRC
    lda Xs+1
    adc SRC+1
    sta SRC+1
    jsr plotrun
    jmp pd_loop

; ---------------------------------------------------------------------------
; plotrun - OR LEN pixels rightward starting at SRC / MASK. In MODE 4 the
; horizontally-next screen byte is +8 (8 bytes per 8x8 char cell).
; ---------------------------------------------------------------------------
.plotrun
    ldy #0
.pr1
    lda (SRC),y
    ora MASK
    sta (SRC),y
    lsr MASK
    bcc pr_dec
    lda #&80
    sta MASK
    lda SRC
    clc
    adc #8
    sta SRC
    bcc pr_dec
    inc SRC+1
.pr_dec
    dec LEN
    bne pr1
    rts

; ---------------------------------------------------------------------------
; build_rowbase - fill rowlo/rowhi with the MODE 4 byte address of x=0 for
; each y. address(y) = SCREEN + (y>>3)*320 + (y&7); +1 per line, +313 when
; stepping from row 7 of a char cell into row 0 of the next.
; ---------------------------------------------------------------------------
.build_rowbase
    lda #LO(SCREEN) : sta SRC
    lda #HI(SCREEN) : sta SRC+1
    ldx #0
.brl
    lda SRC   : sta rowlo,x
    lda SRC+1 : sta rowhi,x
    txa
    and #7
    cmp #7
    beq brcross
    inc SRC
    bne brnext
    inc SRC+1
    jmp brnext
.brcross
    clc
    lda SRC   : adc #LO(313) : sta SRC
    lda SRC+1 : adc #HI(313) : sta SRC+1
.brnext
    inx
    bne brl
    rts

.maskTab
    EQUB &80, &40, &20, &10, &08, &04, &02, &01

INCLUDE "zx02_decompress.asm"
INCLUDE "spans.asm"

.comp_data
INCBIN "data/parrot.zx02"
.comp_data_end

.prog_end

SAVE "STIPPLE", start, prog_end, start
