#!/usr/bin/env python3
"""Builds web/index.html (single-file browser app) from web/src/ and the player ROMs
embedded in ../videoboy.py. Run after changing the players or the web sources."""
import base64, os, re, zlib
here = os.path.dirname(os.path.abspath(__file__))
src = os.path.join(here, "src")
vb = open(os.path.join(here, "..", "videoboy.py")).read()
def player(name):
    raw = zlib.decompress(base64.b64decode(re.search(name + r' = "([^"]*)"', vb).group(1)))[:0x4000]
    return base64.b64encode(raw.rstrip(b"\x00")).decode()
t = open(os.path.join(src, "template.html")).read()
eng = open(os.path.join(src, "engine.js")).read()
t = t.replace("/*@@ENGINE@@*/", eng).replace("/*@@ENGINE_MAIN@@*/", eng)
t = t.replace("@@PLAYER_DMG@@", player("PLAYER_B64")).replace("@@PLAYER_GBC@@", player("PLAYER_GBC_B64"))
open(os.path.join(here, "index.html"), "w").write(t)
print("web/index.html built,", len(t), "bytes")
