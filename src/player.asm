; =====================================================================
;  VIDEO BOY  –  für den originalen Game Boy (DMG-01)
;  läuft auch auf Pocket, Color, Advance (im DMG-Modus)
;
;  Prinzip:
;   * Zwei Bildpuffer im VRAM (je max. 191 Kacheln), Umschalten per LCDC
;     -> kein Tearing.
;   * Kacheldaten werden während der HBlank-/OAM-Phase jeder Zeile
;     (8 Bytes) und im VBlank (16 Bytes pro Zeile) aus dem ROM gestreamt.
;   * Ton: 1 Sample pro Bildschirmzeile (9198 Hz), 8 Stufen über die
;     Master-Lautstärke NR50, Kanal 3 liefert einen konstanten Pegel.
;   * MBC5, bis zu 8 MB ROM.
;
;  Steuerung:  START = Pause   SELECT = Neustart
; =====================================================================

DEF rP1    EQU $FF00
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
DEF rBGP   EQU $FF47
DEF rIE    EQU $FFFF

DEF LCDC_A EQU %10010001     ; LCD an, Kacheln $8000, Map $9800, BG an
DEF LCDC_B EQU %10001001     ; LCD an, Kacheln $8800, Map $9C00, BG an

; ---- Konfiguration, wird vom Konverter in Bank 0 eingetragen --------
DEF CFG        EQU $2F00
DEF CFG_N      EQU CFG+0      ; LCD-Frames pro Videoframe
DEF CFG_AUDLEN EQU CFG+1      ; 16 bit: Audiobytes pro Paket (=154*N)
DEF CFG_CHUNKS EQU CFG+3      ; 16 bit: 8-Byte-Blöcke pro Bild (=Kacheln*2)
DEF CFG_PKTSZ  EQU CFG+5      ; 16 bit: Paketgröße
DEF CFG_PPB    EQU CFG+7      ; Pakete pro 16-KB-Bank
DEF CFG_NPKT   EQU CFG+8      ; 16 bit: Anzahl Pakete
DEF CFG_SCX    EQU CFG+10     ; Feinscroll zum Zentrieren
DEF CFG_SCY    EQU CFG+11
DEF CFG_MODE   EQU CFG+12     ; 0 = normal, 1 = komprimiert
DEF MAPS       EQU $3000      ; 2 x 1024 Byte Tilemaps

SECTION "hram", HRAM
hChkLo:     db
hChkHi:     db
hLine:      db
hPeriod:    db
hPktLo:     db
hPktHi:     db
hPktInBank: db
hBankLo:    db
hBankHi:    db
hRemLo:     db
hRemHi:     db
hJoyPrev:   db
hPaused:    db
hRun:       db      ; Kompression: restliche 8-Byte-Blöcke im aktuellen Lauf
hDone:      db      ; Kompression: Bild vollständig
hBase:      db      ; Kompression: High-Byte des Zielpuffers

SECTION "header", ROM0[$100]
    nop
    jp Start
    ds $150 - @, 0            ; Platz für Header (rgbfix)

; ---------------------------------------------------------------------
; Makros
; ---------------------------------------------------------------------

; Einen 8-Byte-Block reservieren; springt nach \1, wenn das Bild fertig ist
MACRO TAKE_CHUNK
    ldh a, [hChkLo]
    sub 1
    ldh a, [hChkHi]
    jr nc, .lo\@
    or a
    jr z, \1
    dec a
    ldh [hChkHi], a
.lo\@:
    ldh a, [hChkLo]
    dec a
    ldh [hChkLo], a
ENDM

; 8 Bytes [HL+] -> [DE] kopieren (DE 8-Byte-ausgerichtet)
MACRO COPY8
    REPT 8
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

; VBlank-Zeile, langsamer Pfad: warten bis LY >= \1, Sample, max. 8 Bytes
MACRO VBL_SLOW
.w\@:
    ldh a, [rLY]
    cp \1
    jr c, .w\@
    SAMPLE
    TAKE_CHUNK .s\@
    COPY8
.s\@:
ENDM

; VBlank-Zeile, schneller Pfad (Blöcke schon reserviert): Sample + 16 Bytes
MACRO VBL_FAST
.w\@:
    ldh a, [rLY]
    cp \1
    jr c, .w\@
    SAMPLE
    COPY8
    COPY8
ENDM

SECTION "main", ROM0[$150]
Start:
    di
    ld sp, $E000

    ; auf VBlank warten, LCD aus
.wv:
    ldh a, [rLY]
    cp 144
    jr c, .wv
    xor a
    ldh [rLCDC], a

    ; VRAM-Kacheln schwarz füllen
    ld hl, $8000
    ld bc, $1800
.clr:
    ld a, $FF
    ld [hl+], a
    dec bc
    ld a, b
    or c
    jr nz, .clr

    ; Tilemaps kopieren
    ld hl, MAPS
    ld de, $9800
    ld bc, $0800
.map:
    ld a, [hl+]
    ld [de], a
    inc de
    dec bc
    ld a, b
    or c
    jr nz, .map

    ld a, [CFG_SCX]
    ldh [rSCX], a
    ld a, [CFG_SCY]
    ldh [rSCY], a
    xor a
    ldh [hJoyPrev], a
    ldh [hPaused], a
    ld a, %11100100
    ldh [rBGP], a

    ; ---- Ton: Kanal 3 als Gleichspannungsquelle ----
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
    ld a, $20               ; 100 %
    ldh [rNR32], a
    xor a
    ldh [rNR33], a
    ld a, $87               ; Trigger
    ldh [rNR34], a
    ld a, $44               ; Kanal 3 links+rechts
    ldh [rNR51], a
    ld a, $44
    ldh [rNR50], a

    ; ---- HBlank-Interrupt (nur zum Aufwecken aus HALT, IME bleibt 0) ----
    ld a, %00001000
    ldh [rSTAT], a
    ld a, %00000010
    ldh [rIE], a
    xor a
    ldh [rIF], a

    call ResetVideo
    xor a
    ldh [hChkLo], a
    ldh [hChkHi], a
    ld bc, Silence
    ld a, 1
    ldh [hPeriod], a
    ld a, LCDC_A
    ldh [rLCDC], a
    ld a, [CFG_MODE]
    or a
    jp nz, MainLoopZ

; =====================================================================
; Hauptschleife – ein Durchlauf = ein LCD-Frame (154 Zeilen)
;   BC = Audiozeiger, HL = Kachelquelle, DE = VRAM-Ziel
; =====================================================================
MainLoop:
.wvb:
    ldh a, [rLY]
    cp 144
    jr c, .wvb

    ; --- Zeile 144: Sample (altes Paket), ggf. Puffer tauschen ---
    SAMPLE
    ldh a, [hPeriod]
    dec a
    ldh [hPeriod], a
    call z, NewPacket

    ; mind. 16 Blöcke übrig? -> schneller Pfad
    ldh a, [hChkHi]
    or a
    jp nz, .fast
    ldh a, [hChkLo]
    cp 16
    jp nc, .fast
    VBL_SLOW 145
    VBL_SLOW 146
    VBL_SLOW 147
    VBL_SLOW 148
    VBL_SLOW 149
    VBL_SLOW 150
    VBL_SLOW 151
    VBL_SLOW 152
    jp .l153
.fast:
    ldh a, [hChkLo]
    sub 16
    ldh [hChkLo], a
    ldh a, [hChkHi]
    sbc 0
    ldh [hChkHi], a
    VBL_FAST 145
    VBL_FAST 146
    VBL_FAST 147
    VBL_FAST 148
    VBL_FAST 149
    VBL_FAST 150
    VBL_FAST 151
    VBL_FAST 152

    ; --- Zeile 153 (LY liest hier bereits 0) ---
.l153:
    ldh a, [rLY]
    cp 144
    jr nc, .l153
    SAMPLE
    call Joypad

    ; --- sichtbare Zeilen 0..143 ---
    ld a, 144
    ldh [hLine], a
.vis:
    TAKE_CHUNK .audioOnly
    xor a
    ldh [rIF], a
    halt                    ; wacht zu Beginn von HBlank auf
    SAMPLE
    COPY8                   ; endet sicher vor Mode 3 der nächsten Zeile
    jr .next
.audioOnly:
    xor a
    ldh [rIF], a
    halt
    SAMPLE
.next:
    ldh a, [hLine]
    dec a
    ldh [hLine], a
    jr nz, .vis
    jp MainLoop

; ---------------------------------------------------------------------
; Neues Paket: Anzeige umschalten, Zeiger auf nächstes Bild/Audio
; ---------------------------------------------------------------------
NewPacket:
    ldh a, [rLCDC]
    xor LCDC_A ^ LCDC_B
    ldh [rLCDC], a
    bit 4, a
    ld de, $8C00            ; zeigt A -> schreibe in B
    jr nz, .dst
    ld de, $8000            ; zeigt B -> schreibe in A
.dst:
    ldh a, [hRemLo]
    ld l, a
    ldh a, [hRemHi]
    or l
    call z, ResetVideo      ; Ende -> von vorne

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
    push hl

    ld a, [CFG_CHUNKS]
    ldh [hChkLo], a
    ld a, [CFG_CHUNKS+1]
    ldh [hChkHi], a

    ; Paketzeiger weiterschalten
    ld a, [CFG_PKTSZ]
    add c
    ld l, a
    ld a, [CFG_PKTSZ+1]
    adc b
    ld h, a
    ldh a, [hPktInBank]
    dec a
    jr nz, .same
    ldh a, [hBankLo]
    inc a
    ldh [hBankLo], a
    jr nz, .nb
    ldh a, [hBankHi]
    inc a
    ldh [hBankHi], a
.nb:
    ld hl, $4000
    ld a, [CFG_PPB]
.same:
    ldh [hPktInBank], a
    ld a, l
    ldh [hPktLo], a
    ld a, h
    ldh [hPktHi], a

    ldh a, [hRemLo]
    sub 1
    ldh [hRemLo], a
    ldh a, [hRemHi]
    sbc 0
    ldh [hRemHi], a

    ld a, [CFG_N]
    ldh [hPeriod], a
    pop hl
    ret

; nur A verändern!
ResetVideo:
    ld a, 1
    ldh [hBankLo], a
    xor a
    ldh [hBankHi], a
    ldh [hPktLo], a
    ld a, $40
    ldh [hPktHi], a
    ld a, [CFG_PPB]
    ldh [hPktInBank], a
    ld a, [CFG_NPKT]
    ldh [hRemLo], a
    ld a, [CFG_NPKT+1]
    ldh [hRemHi], a
    ret

; ---------------------------------------------------------------------
; Tasten (einmal pro Frame in Zeile 153)
; ---------------------------------------------------------------------
ReadNew:                    ; -> A = neu gedrückte Tasten (Bit3 Start, Bit2 Select)
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
    xor a                   ; Neustart beim nächsten Bildwechsel
    ldh [hRemLo], a
    ldh [hRemHi], a
    inc a
    ldh [hPeriod], a
    ret
.noSel:
    bit 3, a
    ret z
    ; ---- Pause ----
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
    ; weiter: wieder in Zeile 153 einsteigen
.p3:
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


; =====================================================================
;  KOMPRESSIONSMODUS
;  Pro Paket: [Audio 154*N] dann Läufe: [Blöcke][Offset lo][Offset hi] + Daten,
;  Ende: 0 = nächstes Paket folgt direkt, $FF = nächstes Paket am Anfang der
;  nächsten Bank. Pro Zeile ("Slot") wird entweder ein 8-Byte-Block kopiert
;  oder ein Lauf-Kopf gelesen (ohne VRAM-Zugriff).
; =====================================================================

; Slot im VBlank (kein HALT nötig)
MACRO ZVBL
.w\@:
    ldh a, [rLY]
    cp \1
    jr c, .w\@
    SAMPLE
    ldh a, [hRun]
    or a
    jr z, .h\@
    dec a
    ldh [hRun], a
    COPY8
    jr .e\@
.h\@:
    call ParseHdr
.e\@:
ENDM

MainLoopZ:
.wvb:
    ldh a, [rLY]
    cp 144
    jr c, .wvb
    SAMPLE
    ldh a, [hPeriod]
    dec a
    ldh [hPeriod], a
    call z, NewPacketZ
    ZVBL 145
    ZVBL 146
    ZVBL 147
    ZVBL 148
    ZVBL 149
    ZVBL 150
    ZVBL 151
    ZVBL 152
.l153:
    ldh a, [rLY]
    cp 144
    jr nc, .l153
    SAMPLE
    call Joypad

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
    halt
    SAMPLE
    COPY8
    jr .next
.hdr:
    xor a
    ldh [rIF], a
    halt
    SAMPLE
    call ParseHdr
.next:
    ldh a, [hLine]
    dec a
    ldh [hLine], a
    jr nz, .vis
    jp MainLoopZ

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
    ldh a, [hBase]
    add [hl]
    inc hl
    ld d, a
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

NewPacketZ:
    ldh a, [rLCDC]
    xor LCDC_A ^ LCDC_B
    ldh [rLCDC], a
    bit 4, a
    ld a, $8C               ; zeigt A -> schreibe in B
    jr nz, .dst
    ld a, $80
.dst:
    ldh [hBase], a
    ldh a, [hRemLo]
    ld l, a
    ldh a, [hRemHi]
    or l
    call z, ResetVideo

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

SECTION "cfg", ROM0[CFG]
    db 1                    ; N
    dw 154                  ; AUDLEN
    dw 0                    ; CHUNKS
    dw 154                  ; PKTSZ
    db 1                    ; PPB
    dw 0                    ; NPKT
    db 0, 0                 ; SCX, SCY
    db 0                    ; MODE

SECTION "maps", ROM0[MAPS]
    ds 2048, 191
