OUTPUT_ARCH(arm)
ENTRY(_start)
MEMORY {
    rom   : ORIGIN = 0x08000000, LENGTH = 32M
    iwram : ORIGIN = 0x03000000, LENGTH = 31K
    ewram : ORIGIN = 0x02000000, LENGTH = 256K
}
SECTIONS {
    .header : { KEEP(*(.header)) } > rom
    .config 0x08000400 : { KEEP(*(.vbconfig)) } > rom
    .text : { *(.text .text.*) *(.rodata .rodata.*) . = ALIGN(4); } > rom
    .iwram : {
        __iwram_start = .;
        *(.iwram .iwram.*) *(.data .data.*)
        . = ALIGN(4);
        __iwram_end = .;
    } > iwram AT > rom
    __iwram_lma = LOADADDR(.iwram);
    .bss (NOLOAD) : { __bss_start = .; *(.bss .bss.* COMMON) . = ALIGN(4); __bss_end = .; } > iwram
    __rom_end = LOADADDR(.iwram) + SIZEOF(.iwram);
    /DISCARD/ : { *(.ARM.exidx*) *(.ARM.attributes) *(.comment) }
}
