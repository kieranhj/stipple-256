; zx02_decompress.asm
; ---------------------------------------------------------------------------
; BeebAsm port of the "zx02-small" 6502 decompressor (121 bytes) from
;   https://github.com/dmsc/zx02  (c) 2022 DMSC, MIT license (see LICENSE.zx0).
; ZX02 is a 6502-tuned variant of Einar Saukas' ZX0. Format is NOT ZX0-compatible.
;
; Faithful translation of 6502/zx02-small.asm into BeebAsm syntax. The X
; register doubles as a pointer selector ($FE = literal source ZX0_src,
; $00 = match source 'pntr'), which is the size trick that keeps it tiny.
;
; Calling convention:
;   - set the words at zx0_ini_block (out address + compressed address), then
;     JSR full_decomp. Data is written to out_addr until the stream ends.
;   - comp_data / out_addr are provided by the includer (see stipple.asm).
; ---------------------------------------------------------------------------

ZX0ZP   = &80
offset  = ZX0ZP+0       ; 2
bitr    = ZX0ZP+2       ; 1
ZX0_dst = ZX0ZP+3       ; 2
ZX0_src = ZX0ZP+5       ; 2
pntr    = ZX0ZP+7       ; 2
setx    = ZX0ZP+9       ; 1

.zx0_ini_block
    EQUB LO(0), HI(0)               ; initial offset-1 (see zx02 README)
    EQUB &80                        ; initial bit reservoir - do not change
    EQUB LO(out_addr),  HI(out_addr)
    EQUB LO(comp_data), HI(comp_data)

.full_decomp
    ldx #6
.copy_init
    lda zx0_ini_block,x
    sta offset,x
    dex
    bpl copy_init
    dex                             ; X = -2 ($FE)

.decode_literal
    ldy #1
    jsr get_elias
    jsr put_byte
    bcs dzx0s_new_offset

    iny
    jsr get_elias
.dzx0s_copy
    ; C=0 from get_elias
.sbc1
    lda ZX0_dst+2,x
    sbc offset+2,x
    sta pntr+2,x
    inx
    bne sbc1

    jsr put_byte
    bcc decode_literal

.dzx0s_new_offset
    iny
    jsr get_elias
    beq zx0_exit                    ; read a 0 -> end of stream
    dey
    tya
    lsr A
    sta offset+1
    jsr get_byte
    ror A
    sta offset
    ldy #1
    jsr elias_skip1
    iny
    bcc dzx0s_copy

.elias_loop
    asl bitr
    rol A
    tay
.get_elias
    asl bitr
    bne elias_skip1
    jsr get_byte
    rol A
    sta bitr
.elias_skip1
    tya
    bcs elias_loop
    rts

.get_byte
    lda (ZX0_src+2,x)
    inc ZX0_src+2,x
    bne zx0_exit
    inc ZX0_src+3,x
.zx0_exit
    rts

.put_byte
    stx setx
.ploop
    ldx setx
    jsr get_byte
    ldx #&FE
    sta (ZX0_dst+2,x)               ; X=$FE -> (ZX0_dst)
    inc ZX0_dst
    bne pb_skip
    inc ZX0_dst+1
.pb_skip
    dey
    bne ploop
    asl bitr
    rts
