#!/bin/sh
# Rebuilds the player ROM and embeds it into ../videoboy.py (requires RGBDS in PATH)
set -e
cd "$(dirname "$0")"
rgbasm -o player.o player.asm
rgblink -o player.gb player.o
rgbfix -v -m MBC5 -t VIDEOBOY -p 0 player.gb
python3 - <<'PY'
import base64, re, zlib
b = open('player.gb', 'rb').read()[:0x4000]
enc = base64.b64encode(zlib.compress(b, 9)).decode()
s = open('../videoboy.py').read()
s = re.sub(r'PLAYER_B64 = "[^"]*"', 'PLAYER_B64 = "' + enc + '"', s)
open('../videoboy.py', 'w').write(s)
PY
rm -f player.o player.gb
echo "videoboy.py updated."
