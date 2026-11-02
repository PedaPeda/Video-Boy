@ VIDEO BOY ADVANCE - start-up code
@ The cartridge header (logo, checksum) is filled in by gbafix at build time.
    .section .header, "ax"
    .arm
    .global _start
_start:
    b       reset
    .fill   156, 1, 0               @ logo (gbafix)
    .ascii  "VIDEOBOY    "          @ title, 12 bytes
    .ascii  "AVBE"                  @ game code
    .ascii  "00"                    @ maker
    .byte   0x96, 0, 0
    .fill   7, 1, 0
    .byte   0                       @ version
    .byte   0                       @ complement (gbafix)
    .byte   0, 0

reset:
    @ IRQ stack, then system mode stack
    mov     r0, #0x12
    msr     cpsr_c, r0
    ldr     sp, =0x03007FA0
    mov     r0, #0x1F
    msr     cpsr_c, r0
    ldr     sp, =0x03007F00
    @ copy .iwram (code + data) from ROM to IWRAM
    ldr     r0, =__iwram_lma
    ldr     r1, =__iwram_start
    ldr     r2, =__iwram_end
1:  cmp     r1, r2
    ldrlo   r3, [r0], #4
    strlo   r3, [r1], #4
    blo     1b
    @ clear .bss
    ldr     r1, =__bss_start
    ldr     r2, =__bss_end
    mov     r3, #0
2:  cmp     r1, r2
    strlo   r3, [r1], #4
    blo     2b
    ldr     r0, =main
    mov     lr, pc
    bx      r0
3:  b       3b
    .pool
