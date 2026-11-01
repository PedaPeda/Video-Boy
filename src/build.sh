#!/bin/sh
# Rebuilds both player ROMs (DMG + Game Boy Color) and embeds them into ../videoboy.py
# (requires RGBDS in PATH)
set -e
cd "$(dirname "$0")"
rgbasm -o player.o player.asm
rgblink -o player.gb player.o
rgbfix -v -m MBC5 -t VIDEOBOY -p 0 player.gb
rgbasm -o player_gbc.o player_gbc.asm
rgblink -o player_gbc.gb player_gbc.o
rgbfix -v -C -m MBC5 -t VIDEOBOYC -p 0 player_gbc.gb
python3 - <<'PY'
import base64, re, zlib
def enc(f):
    return base64.b64encode(zlib.compress(open(f, 'rb').read()[:0x4000], 9)).decode()
s = open('../videoboy.py').read()
s = re.sub(r'PLAYER_B64 = "[^"]*"', 'PLAYER_B64 = "' + enc('player.gb') + '"', s)
s = re.sub(r'PLAYER_GBC_B64 = "[^"]*"', 'PLAYER_GBC_B64 = "' + enc('player_gbc.gb') + '"', s)
open('../videoboy.py', 'w').write(s)
PY
rm -f player.o player.gb player_gbc.o player_gbc.gb
echo "videoboy.py updated."
