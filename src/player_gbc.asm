; =====================================================================
;  VIDEO BOY COLOR  –  Video player for the Game Boy Color (CGB only)
;
;  * Double speed (8 MHz): 16 bytes per scanline are copied into VRAM
;    during HBlank/OAM scan, 32 bytes per VBlank line.
;  * Two full-screen buffers spread over both VRAM banks, switched with a
;    single LCDC write in VBlank -> no tearing.
;  * Colour: 8 BG palettes x 4 colours per frame, palette per 8x8 tile
;    (attribute maps in VRAM bank 1).
;  * Sound: 2 samples per scanline = 18396 Hz, 3-bit stereo through NR50.
;    Sample 1 at HBlank start, sample 2 half a line later via timer.
;  * Stream format per packet (MBC5, one packet never crosses a bank):
;      [audio 308*N][palettes 64][runs ...][end]
;      run = [chunks (16 byte)] [dst lo] [dst hi | VRAM bank << 6] data
;      end = 0: next packet follows,  $FF: next packet at start of next bank
;
;  Controls: START = pause, SELECT = restart
; =====================================================================

DEF rP1    EQU $FF00
DEF rTIMA  EQU $FF05
DEF rTMA   EQU $FF06
DEF rTAC   EQU $FF07
DEF rIF    EQU $FF0F
DEF rNR30  EQU $FF1A
DEF rNR32  EQU $FF1C
DEF rNR33  EQU $FF1D
DEF rNR34  EQU $FF1E
DEF rNR50  EQU $FF24
DEF rNR51  EQU $FF25
DEF rNR52  EQU $FF26
DEF rLCDC  EQU $FF40
DEF rSTAT  EQU $FF41
DEF rSCY   EQU $FF42
DEF rSCX   EQU $FF43
DEF rLY    EQU $FF44
DEF rKEY1  EQU $FF4D
DEF rVBK   EQU $FF4F
DEF rBCPS  EQU $FF68
DEF rBCPD  EQU $FF69
DEF rIE    EQU $FFFF

DEF LCDC_A EQU %10010001     ; tiles $8000 mode, map $9800
DEF LCDC_B EQU %10001001     ; tiles $8800 mode, map $9C00

DEF HALF_TICKS EQU 23        ; timer ticks from arming to the mid-line sample

DEF CFG        EQU $2F00
DEF CFG_N      EQU CFG+0      ; LCD frames per video frame
DEF CFG_AUDLEN EQU CFG+1      ; 16 bit: audio bytes per packet (=308*N)
DEF CFG_SCX    EQU CFG+3
DEF CFG_SCY    EQU CFG+4
DEF CFG_NPKT   EQU CFG+5      ; 16 bit: number of packets
DEF MAPS       EQU $3000      ; $3000 tile maps (bank 0), $3800 attribute maps (bank 1)

SECTION "hram", HRAM
hLine:      db
hPeriod:    db
hPktLo:     db
hPktHi:     db
hBankLo:    db
hBankHi:    db
hRemLo:     db
hRemHi:     db
hJoyPrev:   db
hRun:       db
hDone:      db
hPalLo:     db
hPalHi:     db
hPalOK:     db

SECTION "header", ROM0[$100]
    nop
    jp Start
    ds $150 - @, 0

; ---------------------------------------------------------------------
MACRO COPY16
    REPT 16
        ld a, [hl+]
        ld [de], a
        inc e
    ENDR
    jr nz, .nc\@
    inc d
.nc\@:
ENDM

MACRO SAMPLE
    ld a, [bc]
    inc bc
    ldh [rNR50], a
ENDM

; clear pending interrupts and start the half-line timer
MACRO ARM
    xor a
    ldh [rIF], a
    ld a, 256 - HALF_TICKS
    ldh [rTIMA], a
ENDM

; wait for the timer, then the mid-line sample
MACRO HALF
    ldh a, [rIF]
    bit 2, a
    jr nz, .late\@
    halt
.late\@:
    SAMPLE
ENDM

; one transfer slot without HALT (VBlank)
MACRO VSLOT
    ldh a, [hRun]
    or a
    jr z, .h\@
    dec a
    ldh [hRun], a
    COPY16
    jr .e\@
.h\@:
    call ParseHdr
.e\@:
ENDM

; VBlank line: sample, slot, mid-line sample, slot
MACRO VBL_LINE
.w\@:
    ldh a, [rLY]
    cp \1
    jr c, .w\@
    SAMPLE
    ARM
    VSLOT
    HALF
    VSLOT
ENDM

SECTION "main", ROM0[$150]
Start:
    cp $11                  ; A = $11 on Game Boy Color
    jr z, .cgb
    di
.nocgb:
    jr .nocgb
.cgb:
    di
    ld sp, $E000

.wv:
    ldh a, [rLY]
    cp 144
    jr c, .wv
    xor a
    ldh [rLCDC], a

    ; ---- double speed ----
    xor a
    ldh [rIE], a
    ldh [rIF], a
    ld a, $30
    ldh [rP1], a
    ld a, 1
    ldh [rKEY1], a
    stop

    ; ---- clear both VRAM banks ----
    ld a, 1
    ldh [rVBK], a
    call ClearVRAM
    xor a
    ldh [rVBK], a
    call ClearVRAM

    ; ---- tile maps (bank 0) and attribute maps (bank 1) ----
    ld hl, MAPS
    ld de, $9800
    call Copy2K
    ld a, 1
    ldh [rVBK], a
    ld hl, MAPS + $800
    ld de, $9800
    call Copy2K

    ; ---- all palettes black ----
    ld a, $80
    ldh [rBCPS], a
    ld b, 64
    xor a
.pal:
    ldh [rBCPD], a
    dec b
    jr nz, .pal

    ld a, [CFG_SCX]
    ldh [rSCX], a
    ld a, [CFG_SCY]
    ldh [rSCY], a

    ; ---- sound: channel 3 as DC source, NR50 = PCM ----
    ld a, $80
    ldh [rNR52], a
    xor a
    ldh [rNR30], a
    ld hl, $FF30
    ld a, $FF
    ld b, 16
.wave:
    ld [hl+], a
    dec b
    jr nz, .wave
    ld a, $80
    ldh [rNR30], a
    ld a, $20
    ldh [rNR32], a
    xor a
    ldh [rNR33], a
    ld a, $87
    ldh [rNR34], a
    ld a, $44
    ldh [rNR51], a
    ldh [rNR50], a

    ; ---- timer (524288 Hz in double speed) + HBlank interrupt as wake-up ----
    xor a
    ldh [rTMA], a
    ld a, %101
    ldh [rTAC], a
    ld a, %00001000
    ldh [rSTAT], a
    ld a, %00000110         ; STAT + timer (IME stays 0, only wakes HALT)
    ldh [rIE], a

    xor a
    ldh [hJoyPrev], a
    ldh [hRun], a
    ldh [hPalOK], a
    ldh [hRemLo], a         ; 0 -> reset at the first boundary
    ldh [hRemHi], a
    inc a
    ldh [hDone], a
    ldh [hPeriod], a
    ld bc, Silence
    ld a, LCDC_B
    ldh [rLCDC], a

; =====================================================================
; Main loop: one pass = one LCD frame
;   BC = audio, HL = stream, DE = VRAM destination
; =====================================================================
MainLoop:
.wvb:
    ldh a, [rLY]
    cp 144
    jr c, .wvb
    SAMPLE
    ARM
    ldh a, [hPeriod]
    dec a
    ldh [hPeriod], a
    jp z, .boundary

    VSLOT
    HALF
    VSLOT
    VBL_LINE 145
    VBL_LINE 146
    jp .l147

.boundary:                  ; palettes of the finished frame, then switch
    call PalTask0
    HALF
    call PalTask
.w145:
    ldh a, [rLY]
    cp 145
    jr c, .w145
    SAMPLE
    ARM
    call PalTask
    HALF
    call PalTask
.w146:
    ldh a, [rLY]
    cp 146
    jr c, .w146
    SAMPLE
    ARM
    call Setup
    HALF
    VSLOT

.l147:
    VBL_LINE 147
    VBL_LINE 148
    VBL_LINE 149
    VBL_LINE 150
    VBL_LINE 151
    VBL_LINE 152

    ; ---- line 153 (LY already reads 0) ----
.l153:
    ldh a, [rLY]
    cp 144
    jr nc, .l153
    SAMPLE
    ARM
    VSLOT
    HALF
    call Joypad

    ; ---- visible lines ----
    ld a, 144
    ldh [hLine], a
.vis:
    ldh a, [hRun]
    or a
    jr z, .hdr
    dec a
    ldh [hRun], a
    xor a
    ldh [rIF], a
    halt                    ; wakes at HBlank start
    SAMPLE
    ARM
    COPY16                  ; finishes before mode 3 of the next line
    jr .half
.hdr:
    xor a
    ldh [rIF], a
    halt
    SAMPLE
    ARM
    call ParseHdr
.half:
    HALF
    ldh a, [hLine]
    dec a
    ldh [hLine], a
    jp nz, .vis
    jp MainLoop

; ---------------------------------------------------------------------
; Read the next run header (no VRAM access)
; ---------------------------------------------------------------------
ParseHdr:
    ldh a, [hDone]
    or a
    ret nz
    ld a, [hl+]
    or a
    jr z, .endSame
    inc a
    jr z, .endBank
    dec a
    ldh [hRun], a
    ld a, [hl+]
    ld e, a
    ld a, [hl+]
    ld d, a
    and $40
    rlca
    rlca
    ldh [rVBK], a
    res 6, d
    ret
.endSame:
    ld a, l
    ldh [hPktLo], a
    ld a, h
    ldh [hPktHi], a
    jr .done
.endBank:
    xor a
    ldh [hPktLo], a
    ld a, $40
    ldh [hPktHi], a
    ldh a, [hBankLo]
    inc a
    ldh [hBankLo], a
    jr nz, .done
    ldh a, [hBankHi]
    inc a
    ldh [hBankHi], a
.done:
    ld a, 1
    ldh [hDone], a
    ret

; ---------------------------------------------------------------------
; Palette upload in 4 parts of 16 bytes (old bank is still mapped)
; ---------------------------------------------------------------------
PalTask0:
    ldh a, [hPalLo]
    ld l, a
    ldh a, [hPalHi]
    ld h, a
    ld a, $80
    ldh [rBCPS], a
PalTask:
    ldh a, [hPalOK]
    or a
    ret z
    push bc
    ld c, LOW(rBCPD)
    REPT 16
        ld a, [hl+]
        ldh [c], a
    ENDR
    pop bc
    ret

; ---------------------------------------------------------------------
; Switch to the next packet
; ---------------------------------------------------------------------
Setup:
    ldh a, [hRemLo]
    ld l, a
    ldh a, [hRemHi]
    or l
    jr nz, .toggle
    call ResetVideo
    ld a, LCDC_B            ; packet 0 is written into buffer A
    jr .setl
.toggle:
    ldh a, [rLCDC]
    xor LCDC_A ^ LCDC_B
.setl:
    ldh [rLCDC], a

    ldh a, [hBankLo]
    ld [$2000], a
    ldh a, [hBankHi]
    ld [$3000], a

    ldh a, [hPktLo]
    ld c, a
    ldh a, [hPktHi]
    ld b, a
    ld a, [CFG_AUDLEN]
    add c
    ld l, a
    ld a, [CFG_AUDLEN+1]
    adc b
    ld h, a
    ld a, l
    ldh [hPalLo], a
    ld a, h
    ldh [hPalHi], a
    ld a, 1
    ldh [hPalOK], a
    ld de, 64
    add hl, de
    xor a
    ldh [hRun], a
    ldh [hDone], a

    ldh a, [hRemLo]
    sub 1
    ldh [hRemLo], a
    ldh a, [hRemHi]
    sbc 0
    ldh [hRemHi], a
    ld a, [CFG_N]
    ldh [hPeriod], a
    ret

ResetVideo:                 ; only changes A
    ld a, 1
    ldh [hBankLo], a
    xor a
    ldh [hBankHi], a
    ldh [hPktLo], a
    ld a, $40
    ldh [hPktHi], a
    ld a, [CFG_NPKT]
    ldh [hRemLo], a
    ld a, [CFG_NPKT+1]
    ldh [hRemHi], a
    ret

; ---------------------------------------------------------------------
ClearVRAM:
    ld hl, $8000
    ld de, $2000
.c:
    xor a
    ld [hl+], a
    dec de
    ld a, d
    or e
    jr nz, .c
    ret

Copy2K:
    ld bc, $0800
.c:
    ld a, [hl+]
    ld [de], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .c
    ret

; ---------------------------------------------------------------------
; Buttons (once per frame in line 153)
; ---------------------------------------------------------------------
ReadNew:
    push hl
    ld a, $10
    ldh [rP1], a
    ldh a, [rP1]
    ldh a, [rP1]
    ldh a, [rP1]
    cpl
    and $0F
    ld l, a
    ld a, $30
    ldh [rP1], a
    ldh a, [hJoyPrev]
    cpl
    and l
    ld h, a
    ld a, l
    ldh [hJoyPrev], a
    ld a, h
    pop hl
    ret

Joypad:
    call ReadNew
    bit 2, a
    jr z, .noSel
    xor a                   ; restart at the next frame change
    ldh [hRemLo], a
    ldh [hRemHi], a
    inc a
    ldh [hPeriod], a
    ret
.noSel:
    bit 3, a
    ret z
.pause:
.p1:
    ldh a, [rLY]
    cp 144
    jr z, .p1
.p2:
    ldh a, [rLY]
    cp 144
    jr nz, .p2
    call ReadNew
    bit 3, a
    jr z, .pause
.p3:                        ; resume at the end of line 153
    ldh a, [rLY]
    cp 152
    jr nz, .p3
.p4:
    ldh a, [rLY]
    cp 144
    jr nc, .p4
    ret

Silence:
    db $44

SECTION "cfg", ROM0[CFG]
    db 1
    dw 308
    db 0, 0
    dw 0

SECTION "maps", ROM0[MAPS]
    ds $1000, 0
