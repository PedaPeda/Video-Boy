#!/bin/sh
# Builds player_gba.gba (needs clang + lld, and gbafix from devkitPro's gba-tools)
set -e
# Output: player_gba.gba (copy it to ../../web/players/ and run python3 ../../web/build.py)
cd "$(dirname "$0")"
GBAFIX=${GBAFIX:-gbafix}
CFLAGS="--target=armv4t-none-eabi -mcpu=arm7tdmi -marm -O2 -ffreestanding -fno-builtin -nostdlib -fno-pic -fno-exceptions -fno-unwind-tables -Wall"
clang $CFLAGS -c crt0.s -o crt0.o
clang $CFLAGS -c player.c -o player.o
ld.lld -T gba.ld crt0.o player.o -o player.elf
llvm-objcopy -O binary player.elf player_gba.gba
$GBAFIX player_gba.gba -tVIDEOBOY -cAVBE -m00 >/dev/null
rm -f crt0.o player.o
ls -l player_gba.gba
