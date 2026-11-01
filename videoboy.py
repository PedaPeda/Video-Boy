#!/usr/bin/env python3
"""
videoboy.py (VIDEO BOY) – macht aus einer beliebigen Videodatei ein Game-Boy-ROM (DMG-01).

    python3 videoboy.py film.mp4                 -> film.gb
    python3 videoboy.py film.mp4 -o clip.gb --start 0:30 --duration 60 --fps 30
    python3 videoboy.py film.mp4 --gbc --compress     -> film.gbc (Game Boy Color, Farbe)

Benötigt: Python 3.8+, numpy, ffmpeg/ffprobe im PATH.
Ausgabe: MBC5-ROM bis 8 MB, läuft auf echter Hardware per Flashcart
(EverDrive, EZ-Flash Jr., GB Operator-Flashcarts …) und in Emulatoren.

Steuerung am Game Boy:  START = Pause,  SELECT = von vorne.
"""
import argparse, base64, json, math, os, shutil, subprocess, sys, tempfile, zlib
import numpy as np

# ---- Konstanten der Hardware -------------------------------------------------
CPU_HZ       = 4194304
FRAME_CYCLES = 70224                      # Takte pro LCD-Frame
LCD_FPS      = CPU_HZ / FRAME_CYCLES      # 59.7275 Hz
AUDIO_HZ     = CPU_HZ / 456               # 1 Sample pro Zeile = 9198 Hz
LINES        = 154
CHUNKS_PER_LCD_FRAME = 144 + 16           # 8-Byte-Blöcke pro LCD-Frame
MAX_TILES    = 191                        # pro Bildpuffer (1 Kachel = Rand)
BORDER_TILE  = 191
SILENCE      = 0x44                       # Stille: Stufe 4 links und rechts
BANK         = 0x4000
MAX_BANKS    = 512                        # MBC5: 8 MB

CFG_ADDR  = 0x2F00
MAPS_ADDR = 0x3000

PLAYER_B64 = "eNrt1j9ME1EcB/B3FI6WQvnnUAnqFZBcSMzVuBjTvlSsIU44SZwYTGASBhPjAMmVAv3Df/lXWGRxvjiYhhCDwmJD6+BAYqTmRyIMRMxLXMzFgK+t1QYN0uvY36fta/rufa/Xd797V0KK3OZd4d1hT0+8ilhNj4ifVJIqYq69FAwR2068bz+Z/LC0ttb7oM/2JZ7cWXq2tnqN3rvjvd3Z3nk/Zy/n0+3DVfLtKgHmPZq+rmvgcRBVIHZ63GR98kL67iDOWhIRiLmtrj7doVcocEu3KtCuwRiM08/QQVVo1aDB4Tymx2JNU5n0g/c0UAkaNbhAA3CReuEyf7VQM9ykJXCsQXViRdRABZ9gK6cC+OkMePRKJbpRXZ45kAoTtDC/G/zxVpH5ohvrJqYe1bznb96jmV+b1W2B+ZxiVG52g4+pblDb6hr/+ZSEczz31GBu1mBuzmBu3mBuwWBu0WAuYiS3uVXGAzWgMt8nwof/Pp2nZM50Wg3nZwvMzxWYny8wv1BgfrHAfKTA/LQznU98FOk0DP2ppivZauLLxeP/fIPdnB3EhtwwJK1uJgXm+WoHz1ZvLRmXTLVEZaF+Fn4ZXxFZ4IBILHhAnGy4k4106ILi69dLlGDvnm5SQNVLFfDpZalOkXeyUbdUxwIuCEhlLOiCoIN49HIFRgdheABGWGhbgBAL82oO60QB/26Mr2gBDYIwTD0wkh6qmxUI6RYFwrE9WgOEpR/KfnU/dfJPY8rz3kEYG9iNJV6JW12yRYMQhF3gj211v01NkaynWklPbe6WD1OzLumZuYt5T66YNyw5i+REVJbcMHHa3JkSpZac5fHsidm8E3N5J+bzTizknVjMOxHJO3GiynlQ5sGzlLY1Oyi1o3R1v9nkt8nJ6Ou2qGxzyfWpA+hmU6PNXbFMSdrrtUzt5VYtL8rJWPaaoONSCb9JT53hmtBgAib/KvIYQQghhBBCCCGEEEIIIYQQQgghhBBCCCGUN2GZN8tEKNbfv44QQgihooP/ABFCCKHi8xNmP7oy"

BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]], np.float32)


def die(msg):
    print("Fehler: " + msg, file=sys.stderr)
    sys.exit(1)


def need(tool):
    if not shutil.which(tool):
        die(f"'{tool}' nicht gefunden – bitte ffmpeg installieren (https://ffmpeg.org).")


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                         capture_output=True, text=True)
    if out.returncode:
        die("ffprobe konnte die Datei nicht lesen:\n" + out.stderr)
    info = json.loads(out.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        die("keine Videospur gefunden.")
    w, h = int(v["width"]), int(v["height"])
    sar = v.get("sample_aspect_ratio", "1:1")
    try:
        sn, sd = map(int, sar.split(":"))
        if sn and sd:
            w = w * sn / sd
    except ValueError:
        pass
    dur = float(info["format"].get("duration", v.get("duration", 0)) or 0)
    return w / h, dur, a is not None


def choose_layout(aspect, n, size_arg, limit=None):
    if size_arg:
        try:
            pw, ph = map(int, size_arg.lower().split("x"))
        except ValueError:
            die("--size erwartet z. B. 144x80")
        if pw % 8 or ph % 8:
            die("--size: Breite und Höhe müssen Vielfache von 8 sein")
        tw, th = pw // 8, ph // 8
        if tw > 20 or th > 18:
            die("--size: maximal 160x144")
        return tw, th
    if limit is None:
        limit = min(MAX_TILES, CHUNKS_PER_LCD_FRAME * n // 2)
    best = None
    for th in range(1, 19):
        for tw in range(1, 21):
            if tw * th > limit:
                continue
            err = abs(math.log((tw / th) / aspect))
            if err > 0.12:
                continue
            key = (tw * th, -err)
            if best is None or key > best[0]:
                best = (key, tw, th)
    if best is None:
        return min(20, limit), 1
    return best[1], best[2]


def read_video(path, tw, th, n, ss, dur, fill):
    W, H = tw * 8, th * 8
    fps = f"{CPU_HZ}/{FRAME_CYCLES * n}"
    if fill:
        vf = (f"scale=iw*sar:ih,fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase:flags=area,"
              f"crop={W}:{H},format=gray")
    else:
        vf = (f"scale=iw*sar:ih,fps={fps},scale={W}:{H}:force_original_aspect_ratio=decrease:flags=area,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,format=gray")
    cmd = ["ffmpeg", "-v", "error"] + ss + ["-i", path] + dur + ["-an", "-vf", vf, "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True)
    if raw.returncode:
        die("ffmpeg (Video) fehlgeschlagen:\n" + raw.stderr.decode(errors="replace"))
    frames = np.frombuffer(raw.stdout, np.uint8)
    return frames[: len(frames) // (W * H) * W * H].reshape(-1, H, W)


def read_audio(path, ss, dur, mono=False, rate=9198):
    """Ton dekodieren und aufbereiten (Filterkette aus MP3 BOY, angepasst an die Abtastrate):
    Trittschall weg, Bandbegrenzung unter Nyquist, weniger Dröhnen, mehr Präsenz,
    Lautheitsausgleich, Kompressor, Ausgleich der Höhendämpfung durch das
    Sample-and-Hold des Game Boy, Limiter."""
    if rate > 12000:                               # GBC: 18396 Hz (Werte wie MP3 BOY bei 16384 Hz)
        lp, shelf = 8000, 5000
    else:
        lp, shelf = 4200, 2800
    af = (f"highpass=f=90,lowpass=f={lp},"
          "equalizer=f=250:t=q:w=1:g=-2,"            # weniger Dröhnen
          f"equalizer=f={2500 if rate < 12000 else 3000}:t=q:w=1:g=2,"   # mehr Präsenz
          "dynaudnorm=f=75:g=7:p=0.9:m=40:s=12,"
          "acompressor=threshold=0.05:ratio=8:attack=2:release=60:makeup=4,"
          f"highshelf=f={shelf}:g=2.5:t=q:w=0.7,"   # Halteglied-Ausgleich
          "alimiter=limit=0.98:attack=1:release=20")
    ch = "1" if mono else "2"
    for resample in (f",aresample={rate}:resampler=soxr", ""):
        cmd = ["ffmpeg", "-v", "error"] + ss + ["-i", path] + dur + \
              ["-vn", "-af", af + resample, "-ac", ch, "-ar", str(rate), "-f", "f32le", "-"]
        raw = subprocess.run(cmd, capture_output=True)
        if raw.returncode == 0:
            break
    else:
        die("ffmpeg (Audio) fehlgeschlagen:\n" + raw.stderr.decode(errors="replace"))
    a = np.frombuffer(raw.stdout, np.float32).astype(np.float64).reshape(-1, int(ch))
    if mono:
        a = np.repeat(a, 2, axis=1)
    return a


def quantize_video(frames, gamma, contrast, dither):
    f = frames.astype(np.float32) / 255.0
    if contrast:
        lo, hi = np.percentile(f[:: max(1, len(f) // 200)], [1, 99])
        if hi - lo > 0.05:
            f = np.clip((f - lo) / (hi - lo), 0, 1)
    if gamma != 1.0:
        f = f ** gamma
    v = f * 3.0
    if dither:
        H, W = f.shape[1:]
        th = (BAYER4 + 0.5) / 16.0 - 0.5
        tile = np.tile(th, (H // 4 + 1, W // 4 + 1))[:H, :W]
        v = v + tile
    lvl = np.clip(np.rint(v), 0, 3).astype(np.uint8)
    return (3 - lvl)                       # Farbindex: 0 = weiß, 3 = schwarz


def encode_tiles(idx):
    """(F,H,W) Farbindizes -> (F, tiles*16) Game-Boy-2bpp, zeilenweise Kacheln"""
    F, H, W = idx.shape
    th, tw = H // 8, W // 8
    t = idx.reshape(F, th, 8, tw, 8).transpose(0, 1, 3, 2, 4)      # F,th,tw,row,px
    weights = (1 << np.arange(7, -1, -1)).astype(np.uint16)
    lo = ((t & 1) * weights).sum(-1).astype(np.uint8)
    hi = (((t >> 1) & 1) * weights).sum(-1).astype(np.uint8)
    out = np.stack([lo, hi], -1)                                     # F,th,tw,row,2
    return out.reshape(F, th * tw * 16)


def _quantize_channel(ch, gate=0.03, shape=0.3, rate=9198):
    """Float [-1,1] -> Stufen 0..7 (aus MP3 BOY):
    symmetrisch um 3.5, Rauschformung schiebt das Quantisierungsrauschen in tiefe
    Frequenzen, wo es von der Musik verdeckt wird; Stille liegt exakt auf Stufe 4.
    Formungsstärke 0.3 statt 0.8: bei 9198 Hz gemessen am besten (A-bewertet)."""
    w = rate // 50
    env = np.convolve(np.abs(ch), np.ones(w) / w, "same")
    silent = env < gate
    k = int(rate * 0.06)
    glide = np.convolve(silent.astype(np.float64), np.ones(k) / k, "same")
    y = (3.5 + 0.5 * glide + np.clip(ch, -1, 1) * 4.0).tolist()
    sil = silent.tolist()
    out = bytearray(len(y))
    e = 0.0
    for i in range(len(y)):
        if sil[i]:
            out[i] = 4
            e = 0.0
            continue
        v = y[i] - shape * e            # NTF = 1 + shape*z^-1
        q = int(v + 0.5) if v > -0.5 else 0
        if q > 7:
            q = 7
        e = v - q
        if e > 1.0:
            e = 1.0
        elif e < -1.0:
            e = -1.0
        out[i] = q
    return np.frombuffer(bytes(out), dtype=np.uint8)


def quantize_audio(a, gain=1.0, shape=0.3, rate=9198):
    """(N,2) Float -> NR50-Bytes: Bits 6-4 links, 2-0 rechts (Stereo)."""
    if len(a) == 0:
        return np.zeros(0, np.uint8)
    peak = np.percentile(np.abs(a), 99.5)
    if peak < 1e-4:
        return np.full(len(a), SILENCE, np.uint8)
    y = a / peak
    y = np.tanh(y * 1.1 * gain) / np.tanh(1.1)
    l = _quantize_channel(y[:, 0], shape=shape, rate=rate)
    r = _quantize_channel(y[:, 1], shape=shape, rate=rate)
    return ((l << 4) | r).astype(np.uint8)


def build_maps(tw, th):
    x0, y0 = (20 - tw) // 2, (18 - th) // 2
    m0 = np.full((32, 32), BORDER_TILE, np.uint8)
    m1 = np.full((32, 32), BORDER_TILE, np.uint8)
    i = np.arange(tw * th).reshape(th, tw)
    m0[y0:y0 + th, x0:x0 + tw] = i
    m1[y0:y0 + th, x0:x0 + tw] = (192 + i) & 0xFF
    return m0.tobytes() + m1.tobytes()


def build_rom(tiles, audio, n, tw, th, title):
    F = len(tiles)
    aud_len = LINES * n
    vid_len = tw * th * 16
    pkt = aud_len + vid_len
    ppb = BANK // pkt
    npk = F + 1                                   # +1: letztes Bild braucht noch eine Anzeigeperiode
    need = npk * aud_len
    if len(audio) < need:
        audio = np.concatenate([audio, np.full(need - len(audio), SILENCE, np.uint8)])
    silence = np.full(aud_len, SILENCE, np.uint8)
    black = np.zeros(vid_len, np.uint8)
    nbanks = (npk + ppb - 1) // ppb
    data = bytearray(nbanks * BANK)
    for k in range(npk):
        a = silence if k == 0 else audio[(k - 1) * aud_len: k * aud_len]
        v = tiles[k] if k < F else black
        off = (k // ppb) * BANK + (k % ppb) * pkt
        data[off: off + aud_len] = a.tobytes()
        data[off + aud_len: off + pkt] = v.tobytes()

    cfg = bytes([n]) + aud_len.to_bytes(2, "little") + (tw * th * 2).to_bytes(2, "little") + \
          pkt.to_bytes(2, "little") + bytes([ppb]) + npk.to_bytes(2, "little") + \
          bytes([(-4 * (tw % 2)) & 0xFF, (-4 * (th % 2)) & 0xFF])
    return finalize(cfg, tw, th, data, nbanks, title), nbanks


def finalize(cfg, tw, th, data, nbanks, title):
    bank0 = bytearray(zlib.decompress(base64.b64decode(PLAYER_B64))[:BANK])
    bank0[CFG_ADDR: CFG_ADDR + len(cfg)] = cfg
    bank0[MAPS_ADDR: MAPS_ADDR + 2048] = build_maps(tw, th)

    total = 1 + nbanks
    size_code = 0
    while (2 << size_code) < total:
        size_code += 1
    rom = bank0 + data
    rom += bytes([0xFF]) * ((2 << size_code) * BANK - len(rom))
    rom = bytearray(rom)
    t = title.upper().encode("ascii", "replace")[:15]
    rom[0x134:0x144] = t + bytes(16 - len(t))
    rom[0x143] = 0x00
    rom[0x147] = 0x19                              # MBC5
    rom[0x148] = size_code
    rom[0x149] = 0x00
    c = 0
    for b in rom[0x134:0x14D]:
        c = (c - b - 1) & 0xFF
    rom[0x14D] = c
    rom[0x14E] = rom[0x14F] = 0
    g = sum(rom) & 0xFFFF
    rom[0x14E], rom[0x14F] = g >> 8, g & 0xFF
    return bytes(rom)


# ---- Kompressionsmodus ---------------------------------------------------------
RUN_MAX = 127                                     # Kacheln pro Lauf (Blöcke <= 254)
SLOTS_PER_LCD_FRAME = 144 + 8


def z_max_tiles(n):
    cap = SLOTS_PER_LCD_FRAME * n
    t = MAX_TILES
    while 2 * t + -(-t // RUN_MAX) + 1 > cap:
        t -= 1
    return t


def z_runs(changed):
    """Indizes geänderter Kacheln -> Liste (start, länge)"""
    runs = []
    for i in changed:
        if runs and runs[-1][0] + runs[-1][1] == i and runs[-1][1] < RUN_MAX:
            runs[-1][1] += 1
        else:
            runs.append([i, 1])
    return runs


def z_packets(tiles, idx, audio, n, tw, th, tol):
    """Delta-Kodierung: Paket k überträgt nur die Kacheln, die sich gegenüber dem
    Inhalt des Zielpuffers (Bild k-2 inkl. Toleranz) geändert haben."""
    F = len(tiles)
    T = tw * th
    aud_len = LINES * n
    cap = SLOTS_PER_LCD_FRAME * n
    tdat = tiles.reshape(F, T, 16)
    pix = idx.reshape(F, th, 8, tw, 8).transpose(0, 1, 3, 2, 4).reshape(F, T, 64)
    silence = np.full(aud_len, SILENCE, np.uint8)
    buf = [None, None]
    pkts, sent = [], []
    for k in range(F):
        b = k & 1
        if buf[b] is None:
            changed = np.arange(T)
            buf[b] = pix[k].copy()
        else:
            changed = np.nonzero((buf[b] != pix[k]).sum(1) > tol)[0]
            runs = z_runs(changed)
            if 2 * len(changed) + len(runs) + 1 > cap:
                changed = np.arange(T)
            buf[b][changed] = pix[k][changed]
        p = bytearray(silence.tobytes() if k == 0 else audio[(k - 1) * aud_len: k * aud_len].tobytes())
        for st, ln in z_runs(changed):
            off = st * 16
            p += bytes([ln * 2, off & 0xFF, off >> 8])
            p += tdat[k, st:st + ln].tobytes()
        p += b"\x00"
        pkts.append(bytes(p))
        sent.append(len(changed))
    return pkts, sent


def z_fit(pkts, aud_len, max_banks=MAX_BANKS):
    """Wie viele Bilder passen (inkl. Abschluss-Paket) in max_banks?"""
    limit = (max_banks - 1) * BANK
    pos, best = 0, 0
    extra = aud_len + 1
    for k, p in enumerate(pkts):
        if pos % BANK + len(p) > BANK:
            pos += BANK - pos % BANK
        pos += len(p)
        if pos > limit:
            break
        e = pos + (BANK - pos % BANK if pos % BANK + extra > BANK else 0) + extra
        if e <= limit:
            best = k + 1
    return best


def build_rom_z(pkts, audio, n, tw, th, title, F):
    aud_len = LINES * n
    T = tw * th
    tail = audio[(F - 1) * aud_len: F * aud_len].tobytes()
    tail += bytes([SILENCE]) * (aud_len - len(tail)) + b"\x00"   # Abschluss: Ton des letzten Bildes
    data = bytearray()
    last_end = None
    for p in list(pkts[:F]) + [tail]:
        if data and len(data) % BANK == 0:
            data[last_end] = 0xFF                 # Paket endete exakt am Bankende
        elif len(data) % BANK + len(p) > BANK:
            data[last_end] = 0xFF                 # nächstes Paket am Anfang der nächsten Bank
            data += bytes([0xFF]) * (BANK - len(data) % BANK)
        data += p
        last_end = len(data) - 1
    nbanks = -(-len(data) // BANK)
    data += bytes([0xFF]) * (nbanks * BANK - len(data))
    cfg = bytes([n]) + aud_len.to_bytes(2, "little") + (T * 2).to_bytes(2, "little") + \
          (0).to_bytes(2, "little") + bytes([1]) + (F + 1).to_bytes(2, "little") + \
          bytes([(-4 * (tw % 2)) & 0xFF, (-4 * (th % 2)) & 0xFF, 1])
    return finalize(cfg, tw, th, bytes(data), nbanks, title), nbanks


def write_preview(path, idx, audio, n, sound):
    """Vorschau-Video: so sieht/klingt es auf dem Game Boy (grünes DMG-Display, 4x)."""
    pal = np.array([[0x9B, 0xBC, 0x0F], [0x8B, 0xAC, 0x0F], [0x30, 0x62, 0x30], [0x0F, 0x38, 0x0F]], np.uint8)
    F, H, W = idx.shape
    scr = np.full((F, 144, 160), 3, np.uint8)
    y0, x0 = (144 - H) // 2, (160 - W) // 2
    scr[:, y0:y0 + H, x0:x0 + W] = idx
    rgb = pal[scr]
    tmpa = path + ".tmp.raw"
    args = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "160x144",
            "-r", f"{CPU_HZ}/{FRAME_CYCLES * n}", "-i", "-"]
    if sound:
        st = np.stack([(audio >> 4) & 7, audio & 7], 1).astype(np.float32) / 3.5 - 1.0
        st.astype(np.float32).tofile(tmpa)
        args += ["-f", "f32le", "-ar", "9198", "-ac", "2", "-i", tmpa, "-af", "highpass=f=20",
                 "-c:a", "aac", "-ar", "44100"]
    args += ["-vf", "scale=640:576:flags=neighbor", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-crf", "18", "-shortest", path]
    p = subprocess.run(args, input=rgb.tobytes(), capture_output=True)
    if os.path.exists(tmpa):
        os.remove(tmpa)
    if p.returncode:
        print("Warnung: Vorschau fehlgeschlagen:\n" + p.stderr.decode(errors="replace"), file=sys.stderr)


# =============================================================================
#  GAME BOY COLOR (--gbc)
# =============================================================================
PLAYER_GBC_B64 = "eNrt1k9o01AcB/AXu/7J5tZ189DNqemYUnpYKnrYoXvUrTI8SD1ZPO0gbG6H9SAOkQ3a9X+3wfy/nTx58hBUpMgu0yoYuwheRGGVn4KDDScPdnGZtCath22C4D8I+vukebQv6e998xJ4IeQ/lz/FvVgbGFisJ3Wm8yRGdpN6YnMcSGdIw9LiyHKx+GZufn7w7EjDx8Xi0tyt+YdH6OkTgePBnuCZ3mqFBW1vqXwbfpIqOdymdWdp/TABFijNdKkS+CUog516gVAOTjbqbVDJ81KldRGvg8wqL3m910W6vv2IwDmL9r8hs7CpmkToVWtE6NF6D0nQ6vKWadnS2G4Wvmg9rVSANgn20RTspwE4CB0SWKgZrNQGx6gFyhKkIQPTkISUD7IQ5TpraQb81XS1Jv18O10BM4t2Q/T5axPL5Nxd3ZDxNLX91Efg9jhNyoqN2QshgRvVK/92qUDp8vaMhsp2xajZ8sM1Sge/paDSyX83l3rfzjO2X5ES4v/shF018M28ZuBs1w2c7YaBs900cLZZA2eb8f6lbMomT2cgolfs0ypqA4xuGehXBmjaUWPHXES6IfL4gi2/wbFsbsGTczf73E79Wvo9oWW/1QrBwj15HCbGIOZslmCC+iHG4j6IC2aW8EFCW5WzMpsMs6lBfU1m07lnTxuGPO9/vD2SWTLMUg8Eq3KHpxlnDfN/cmpLbnyVCCyxSrxsIshifSonRsPqLjE9OA6TYzClDTbt8JMWSVuvsyz5ioMkS70lkFKJCFFZOxyXIFENqZpFSKoWEVKyi0QcRJDa9166L2zIHLF5mprrLt4VPssfaCMQVtnEZXtYfwVhafG2Nlx67J2sFPlCyK29h1TeDKJyof+Zfu/dqt4Kqn64372mP6qCWn0o5ABBCCGEEEIIIYQQQgghhBBCCCGEEEIIIaPgjnI4CQghhBBCCCGEEPqnfQXa3cXx"
GBC_AUDIO_HZ = CPU_HZ / 228                       # 2 Samples pro Zeile = 18396 Hz
GBC_SAMPLES_PER_FRAME = 2 * LINES                 # 308
GBC_MAX_TILES = 360
GBC_SPLIT = 232                                   # Kacheln im ersten VRAM-Abschnitt
GBC_BORDER = 232                                  # Rand-Kachel (Bank 0, $8E80)
GBC_CFG = 0x2F00
GBC_MAPS = 0x3000


def gbc_cap(n):
    """Transfer-Slots pro Paket: 144 sichtbare + 19 VBlank-Slots pro LCD-Frame,
    minus 5 für Palettenwechsel und Umschalten."""
    return 163 * n - 5


def gbc_full_slots(tw, th):
    t = tw * th
    runs = 1 + (1 if t > 0 else 0) + (1 if t > GBC_SPLIT else 0)
    return t + 2 * th + runs + 1


def choose_layout_gbc(aspect, n, size_arg):
    cap = gbc_cap(n)
    if size_arg:
        try:
            pw, ph = map(int, size_arg.lower().split("x"))
        except ValueError:
            die("--size erwartet z. B. 160x88")
        if pw % 8 or ph % 8 or pw > 160 or ph > 144:
            die("--size: Vielfache von 8, maximal 160x144")
        return pw // 8, ph // 8
    best = None
    for th in range(1, 19):
        for tw in range(1, 21):
            if gbc_full_slots(tw, th) > cap:
                continue
            err = abs(math.log((tw / th) / aspect))
            if err > 0.12:
                continue
            key = (tw * th, -err)
            if best is None or key > best[0]:
                best = (key, tw, th)
    if best is None:
        return 20, 1
    return best[1], best[2]


def gbc_tile_loc(t, b):
    """Kachel t im Puffer b -> (VRAM-Bank, Adresse, Index in der Tilemap)"""
    if b == 0:                                    # Puffer A, Adressierung $8000
        if t < GBC_SPLIT:
            return 0, 0x8000 + 16 * t, t
        return 1, 0x8000 + 16 * (t - GBC_SPLIT), t - GBC_SPLIT
    if t < GBC_SPLIT:                             # Puffer B, Adressierung $8800
        return 1, 0x8980 + 16 * t, (152 + t) & 0xFF
    return 0, 0x9000 + 16 * (t - GBC_SPLIT), t - GBC_SPLIT


def gbc_maps(tw, th):
    """Startinhalt: Tilemaps A/B (Bank 0) und Attribut-Maps A/B (Bank 1)"""
    x0, y0 = (20 - tw) // 2, (18 - th) // 2
    idx = [np.full((32, 32), GBC_BORDER, np.uint8) for _ in range(2)]
    att = [np.zeros((32, 32), np.uint8) for _ in range(2)]
    for b in range(2):
        for t in range(tw * th):
            bank, _, ix = gbc_tile_loc(t, b)
            idx[b][y0 + t // tw, x0 + t % tw] = ix
            att[b][y0 + t // tw, x0 + t % tw] = bank << 3
    return idx[0].tobytes() + idx[1].tobytes() + att[0].tobytes() + att[1].tobytes()


def read_video_rgb_stream(path, tw, th, n, ss, dur, fill):
    W, H = tw * 8, th * 8
    fps = f"{CPU_HZ}/{FRAME_CYCLES * n}"
    if fill:
        vf = (f"scale=iw*sar:ih,fps={fps},scale={W}:{H}:force_original_aspect_ratio=increase:flags=area,"
              f"crop={W}:{H},format=rgb24")
    else:
        vf = (f"scale=iw*sar:ih,fps={fps},scale={W}:{H}:force_original_aspect_ratio=decrease:flags=area,"
              f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,format=rgb24")
    cmd = ["ffmpeg", "-v", "error"] + ss + ["-i", path] + dur + ["-an", "-vf", vf, "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = W * H * 3
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield np.frombuffer(buf, np.uint8).reshape(H, W, 3)
    p.wait()


class GBCQuantizer:
    """Pro Bild 8 Paletten x 4 Farben (RGB555) und eine Palette pro Kachel.
    k-means über Kacheln und Pixel, warm gestartet mit den Paletten des vorigen
    Bildes; bleiben die alten Paletten fast so gut, werden sie unverändert übernommen
    (ruhiges Bild, bessere Kompression)."""

    def __init__(self, tw, th, dither, border, gamma):
        self.tw, self.th = tw, th
        self.dither, self.border, self.gamma = dither, border, gamma
        self.prev = None
        self.prev_err = None
        self.count = 0
        thr = (BAYER4 + 0.5) / 16.0
        self.thr = np.tile(thr, (2, 2)).reshape(64)          # 8x8 Kachel

    def _tiles(self, img):
        th, tw = self.th, self.tw
        return img.reshape(th, 8, tw, 8, 3).transpose(0, 2, 1, 3, 4).reshape(th * tw, 64, 3)

    @staticmethod
    def _dist(X, P):
        # X (T,64,3), P (8,4,3) -> (T,64,8,4)  (quadratische Abstände per Matrixprodukt)
        T = len(X)
        Xf = X.reshape(-1, 3).astype(np.float32)
        Pf = P.reshape(-1, 3).astype(np.float32)
        d = (Xf * Xf).sum(1)[:, None] - 2.0 * (Xf @ Pf.T) + (Pf * Pf).sum(1)[None]
        return np.maximum(d, 0).reshape(T, 64, 8, 4)

    def _tile_err(self, X, P):
        """(T,8): Fehler jeder Kachel mit jeder Palette"""
        d = self._dist(X, P)
        m = np.minimum(np.minimum(d[..., 0], d[..., 1]), np.minimum(d[..., 2], d[..., 3]))
        return m.sum(1)

    def _assign(self, X, P):
        d = self._tile_err(X, P)                           # (T,8)
        a = d.argmin(1)
        return a, d[np.arange(len(a)), a].sum()

    def _fix(self, P):
        P = np.clip(np.rint(P), 0, 31)
        if self.border:
            P[0, 0] = 0
        return P

    def _refine(self, X, P, a, iters=2):
        P = P.copy()
        for p in range(8):
            m = X[a == p].reshape(-1, 3)
            if len(m) == 0:
                continue
            for _ in range(iters):
                lab = ((m[:, None, :] - P[p][None]) ** 2).sum(-1).argmin(1)
                for c in range(4):
                    sel = m[lab == c]
                    if len(sel):
                        P[p, c] = sel.mean(0)
        return P

    def _cold(self, X):
        T = len(X)
        means = X.mean(1)
        lum = means @ np.array([0.3, 0.59, 0.11])
        order = np.argsort(lum)
        cent = means[order[np.linspace(0, T - 1, 8).astype(int)]].copy()
        for _ in range(6):
            lab = ((means[:, None] - cent[None]) ** 2).sum(-1).argmin(1)
            for k in range(8):
                if (lab == k).any():
                    cent[k] = means[lab == k].mean(0)
        P = np.zeros((8, 4, 3))
        for k in range(8):
            m = X[lab == k].reshape(-1, 3) if (lab == k).any() else X[order[(k * T) // 8]]
            l = m @ np.array([0.3, 0.59, 0.11])
            o = np.argsort(l)
            for c in range(4):
                P[k, c] = m[o[min(len(o) - 1, int((c + 0.5) / 4 * len(o)))]]
        a, _ = self._assign(X, P)
        for _ in range(3):
            P = self._refine(X, P, a, 2)
            a, _ = self._assign(X, P)
        return P

    def _reseed_empty(self, X, P, a):
        used = np.bincount(a, minlength=8)
        if used.min() > 0:
            return P
        d = self._tile_err(X, P)[np.arange(len(a)), a]
        worst = np.argsort(d)[::-1]
        j = 0
        for p in np.nonzero(used == 0)[0]:
            t = worst[j % len(worst)]; j += 1
            m = X[t]
            o = np.argsort(m @ np.array([0.3, 0.59, 0.11]))
            for c in range(4):
                P[p, c] = m[o[min(63, int((c + 0.5) * 16))]]
        return P

    def quantize(self, frame):
        img = frame.astype(np.float64) / 255.0
        if self.gamma != 1.0:
            img = img ** self.gamma
        img = img * 31.0
        X = self._tiles(img)
        cands = []
        if self.prev is not None:
            a, e = self._assign(X, self.prev)
            cands.append((e * 0.92, self.prev, "prev"))     # Hysterese: alte Paletten bevorzugen
            Pw = self.prev.copy()
            for _ in range(2):
                Pw = self._reseed_empty(X, Pw, a)
                Pw = self._fix(self._refine(X, Pw, a, 2))
                a, e = self._assign(X, Pw)
            cands.append((e, Pw, "warm"))
            scene = self.prev_err is not None and e > 1.6 * self.prev_err + 1
        else:
            scene = True
        if scene or self.count % 48 == 0:
            Pc = self._fix(self._cold(X))
            a, e = self._assign(X, Pc)
            cands.append((e, Pc, "cold"))
        e, P, _ = min(cands, key=lambda c: c[0])
        P = self._fix(P)
        a, err = self._assign(X, P)
        self.prev, self.prev_err = P, err
        self.count += 1
        # Pixel -> Farbindex (geordnetes Dithering zwischen den zwei nächsten Farben)
        Pt = P[a]                                            # (T,4,3)
        d = ((X[:, :, None, :] - Pt[:, None, :, :]) ** 2).sum(-1)   # (T,64,4)
        if not self.dither:
            return P.astype(np.uint8), a.astype(np.uint8), d.argmin(-1).astype(np.uint8)
        o = np.argsort(d, -1)
        c1, c2 = o[..., 0], o[..., 1]
        A = Pt[np.arange(len(a))[:, None], c1]
        B = Pt[np.arange(len(a))[:, None], c2]
        v = B - A
        t = ((X - A) * v).sum(-1) / np.maximum((v * v).sum(-1), 1e-9)
        idx = np.where(t > self.thr[None, :], c2, c1)
        return P.astype(np.uint8), a.astype(np.uint8), idx.astype(np.uint8)


def gbc_tile_bytes(idx):
    """(T,64) Farbindizes -> (T,16) 2bpp"""
    t = idx.reshape(-1, 8, 8)
    w = (1 << np.arange(7, -1, -1)).astype(np.uint16)
    lo = ((t & 1) * w).sum(-1).astype(np.uint8)
    hi = (((t >> 1) & 1) * w).sum(-1).astype(np.uint8)
    return np.stack([lo, hi], -1).reshape(-1, 16)


def gbc_pal_bytes(P):
    v = P[..., 0].astype(np.uint16) | (P[..., 1].astype(np.uint16) << 5) | (P[..., 2].astype(np.uint16) << 10)
    return v.astype("<u2").tobytes()


class GBCEncoder:
    """Hält nach, was in den beiden Bildpuffern auf dem Game Boy steht, und baut
    daraus die Pakete (voll oder nur Änderungen)."""

    def __init__(self, tw, th, n, compress, tol):
        self.tw, self.th, self.n = tw, th, n
        self.T = tw * th
        self.compress, self.tol = compress, tol
        self.cap = gbc_cap(n)
        self.x0, self.y0 = (20 - tw) // 2, (18 - th) // 2
        self.buf_idx = [None, None]
        self.buf_pal = [None, None]
        self.locs = [[gbc_tile_loc(t, b) for t in range(self.T)] for b in range(2)]
        self.k = 0
        self.sent = 0

    def _attr_chunks(self, b, pal, rows):
        base = 0x9800 if b == 0 else 0x9C00
        out = []
        for r in rows:
            row = np.zeros(32, np.uint8)
            for tx in range(self.tw):
                t = r * self.tw + tx
                row[self.x0 + tx] = (self.locs[b][t][0] << 3) | pal[t]
            addr = base + (self.y0 + r) * 32
            out.append((1, addr, row[:16].tobytes()))
            out.append((1, addr + 16, row[16:].tobytes()))
        return out

    def packet(self, audio_bytes, P, pal, idx, tiles):
        b = self.k & 1
        T = self.T
        full = (not self.compress) or self.buf_idx[b] is None
        if not full:
            Pf = P.astype(np.int16)
            cur = Pf[self.buf_pal[b][:, None], self.buf_idx[b]]     # (T,64,3) wie es jetzt aussähe
            new = Pf[pal[:, None], idx]
            diff = np.abs(cur - new).max(-1)
            thr = 0 if self.tol == 0 else 2
            changed = np.nonzero((diff > thr).sum(1) > self.tol)[0]
            npal = self.buf_pal[b].copy()
            nidx = self.buf_idx[b].copy()
            npal[changed] = pal[changed]
            nidx[changed] = idx[changed]
            rows = sorted(set((changed // self.tw).tolist()))
            old_att = self._attr_chunks(b, self.buf_pal[b], rows)
            new_att = self._attr_chunks(b, npal, rows)
            att = [c for c, o in zip(new_att, old_att) if c[2] != o[2]]
            chunks = [(self.locs[b][t][0], self.locs[b][t][1], tiles[t].tobytes()) for t in changed] + att
            if len(chunks) + len(self._runs(chunks)) + 1 > self.cap:
                full = True
        if full:
            changed = np.arange(T)
            npal, nidx = pal.copy(), idx.copy()
            chunks = [(self.locs[b][t][0], self.locs[b][t][1], tiles[t].tobytes()) for t in range(T)] + \
                     self._attr_chunks(b, npal, range(self.th))
        self.buf_pal[b], self.buf_idx[b] = npal, nidx
        self.sent += len(changed)
        self.k += 1
        p = bytearray(audio_bytes) + gbc_pal_bytes(P)
        for bank, addr, data in self._runs(chunks):
            p += bytes([len(data) // 16, addr & 0xFF, (addr >> 8) | (bank << 6)]) + data
        p += b"\x00"
        rendered = (self.buf_pal[b], self.buf_idx[b])
        return bytes(p), rendered

    @staticmethod
    def _runs(chunks):
        runs = []
        for bank, addr, data in sorted(chunks, key=lambda c: (c[0], c[1])):
            if runs and runs[-1][0] == bank and runs[-1][1] + len(runs[-1][2]) == addr and len(runs[-1][2]) < 254 * 16:
                runs[-1][2] += data
            else:
                runs.append([bank, addr, bytearray(data)])
        return runs


def gbc_render(P, pal, idx, tw, th):
    """Bildschirm 160x144 RGB (8 bit), wie ihn der Game Boy Color zeigt."""
    col = P.astype(np.uint16)[pal[:, None], idx]                     # (T,64,3) 0..31
    col = (col * 255 // 31).astype(np.uint8)
    img = col.reshape(th, tw, 8, 8, 3).transpose(0, 2, 1, 3, 4).reshape(th * 8, tw * 8, 3)
    scr = np.zeros((144, 160, 3), np.uint8)
    y0, x0 = (144 - th * 8) // 2, (160 - tw * 8) // 2
    scr[y0:y0 + th * 8, x0:x0 + tw * 8] = img
    return scr


def gbc_place(pkts, max_banks=MAX_BANKS):
    """Pakete auf Bänke verteilen; gibt (Daten, Bänke) oder None zurück."""
    data = bytearray()
    last_end = None
    for p in pkts:
        if data and len(data) % BANK == 0:
            data[last_end] = 0xFF
        elif len(data) % BANK + len(p) > BANK:
            data[last_end] = 0xFF
            data += bytes([0xFF]) * (BANK - len(data) % BANK)
        data += p
        last_end = len(data) - 1
        if -(-len(data) // BANK) > max_banks - 1:
            return None
    nb = -(-len(data) // BANK)
    data += bytes([0xFF]) * (nb * BANK - len(data))
    return bytes(data), nb


def gbc_finalize(n, tw, th, data, nbanks, npk, title):
    bank0 = bytearray(zlib.decompress(base64.b64decode(PLAYER_GBC_B64))[:BANK])
    aud = GBC_SAMPLES_PER_FRAME * n
    cfg = bytes([n]) + aud.to_bytes(2, "little") + \
          bytes([(-4 * (tw % 2)) & 0xFF, (-4 * (th % 2)) & 0xFF]) + npk.to_bytes(2, "little")
    bank0[GBC_CFG:GBC_CFG + len(cfg)] = cfg
    bank0[GBC_MAPS:GBC_MAPS + 4096] = gbc_maps(tw, th)
    total = 1 + nbanks
    size_code = 0
    while (2 << size_code) < total:
        size_code += 1
    rom = bytearray(bank0 + data)
    rom += bytes([0xFF]) * ((2 << size_code) * BANK - len(rom))
    t = title.upper().encode("ascii", "replace")[:11]
    rom[0x134:0x143] = t + bytes(15 - len(t))
    rom[0x143] = 0xC0                              # nur Game Boy Color
    rom[0x147] = 0x19
    rom[0x148] = size_code
    rom[0x149] = 0x00
    c = 0
    for b in rom[0x134:0x14D]:
        c = (c - b - 1) & 0xFF
    rom[0x14D] = c
    rom[0x14E] = rom[0x14F] = 0
    g = sum(rom) & 0xFFFF
    rom[0x14E], rom[0x14F] = g >> 8, g & 0xFF
    return bytes(rom)


def gbc_encode(src, ss, dur, tw, th, n, args, audio, preview_path):
    """Ein Durchlauf: Video streamen, quantisieren, Pakete bauen.
    Gibt (Pakete, Bilderzahl, Anteil übertragener Kacheln) zurück."""
    aud_len = GBC_SAMPLES_PER_FRAME * n
    border = tw < 20 or th < 18
    q = GBCQuantizer(tw, th, not args.no_dither, border, args.gamma)
    enc = GBCEncoder(tw, th, n, args.compress, args.tolerance)
    pkts = []
    limit = (MAX_BANKS - 1) * BANK
    pos = 0                                        # exakte Bankbelegung wie in gbc_place
    tail = aud_len + 64 + 1
    prev = None
    pv = None
    if preview_path:
        pv = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "160x144",
                               "-r", f"{CPU_HZ}/{FRAME_CYCLES * n}", "-i", "-", "-vf", "scale=640:576:flags=neighbor",
                               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16", preview_path + ".v.mp4"],
                              stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    k = 0
    for frame in read_video_rgb_stream(src, tw, th, n, ss, dur, not args.fit):
        P, pal, idx = q.quantize(frame)
        tiles = gbc_tile_bytes(idx)
        a = audio[(k - 1) * aud_len: k * aud_len] if k else np.full(aud_len, SILENCE, np.uint8)
        a = np.concatenate([a, np.full(aud_len - len(a), SILENCE, np.uint8)]).tobytes()
        p, (rp, ri) = enc.packet(a, P, pal, idx, tiles)
        np_ = pos + (BANK - pos % BANK if pos % BANK and pos % BANK + len(p) > BANK else 0) + len(p)
        nt = np_ + (BANK - np_ % BANK if np_ % BANK and np_ % BANK + tail > BANK else 0) + tail
        if nt > limit:                             # 8 MB voll (inkl. Abschluss-Paket)
            break
        pos = np_
        pkts.append(p)
        prev = P
        if pv:
            pv.stdin.write(gbc_render(P, rp, ri, tw, th).tobytes())
        k += 1
        if k % 200 == 0:
            print(f"  {k} Bilder …", end="\r", flush=True)
    if pv:
        pv.stdin.close()
        pv.wait()
    F = len(pkts)
    if F:
        a = audio[(F - 1) * aud_len: F * aud_len]
        a = np.concatenate([a, np.full(aud_len - len(a), SILENCE, np.uint8)]).tobytes()
        pkts.append(a + gbc_pal_bytes(prev) + b"\x00")          # Abschluss: Ton des letzten Bildes
    ratio = enc.sent / max(1, enc.k * tw * th)
    return pkts, F, ratio


def main_gbc(args, aspect, dur_s, has_audio, ss, auto):
    fps_map = {"60": 1, "30": 2, "20": 3, "15": 4, "12": 5, "10": 6}
    n = 3 if auto else fps_map[args.fps]
    if auto and not args.compress:
        # ohne Kompression ist die Länge vorab berechenbar: höchste Bildrate, die passt,
        # sonst die mit der größten Spielzeit (Pakete dürfen keine Bankgrenze überschreiten)
        best = None
        for m in (3, 4, 5, 6):
            tw, th = choose_layout_gbc(aspect, m, args.size)
            if gbc_full_slots(tw, th) > gbc_cap(m):
                continue
            size = GBC_SAMPLES_PER_FRAME * m + 64 + tw * th * 16 + 2 * th * 16 + 4 * 3 + 1
            secs = ((MAX_BANKS - 1) * (BANK // size) - 1) * m / LCD_FPS
            if secs >= dur_s:
                best = (m, secs)
                break
            if best is None or secs > best[1]:
                best = (m, secs)
        if best:
            n = best[0]
    sound = not (args.no_audio or not has_audio)
    MAX_READ = 20 * 60
    read_s = min(dur_s, MAX_READ)
    dur = ["-t", f"{read_s:.3f}"] if (args.duration or read_s < dur_s) else []
    if sound:
        print("Lese Ton …")
        audio_f = read_audio(args.input, ss, dur, args.mono, rate=int(round(GBC_AUDIO_HZ)))
        audio = quantize_audio(audio_f, args.volume, shape=0.3, rate=int(round(GBC_AUDIO_HZ)))
    else:
        audio = np.zeros(0, np.uint8)
    title = args.title or os.path.splitext(os.path.basename(args.input))[0]
    src, vss, vdur = args.input, ss, dur
    tmpdir = None
    try:
        if auto:
            tw, th = choose_layout_gbc(aspect, n, args.size)
            W, H = tw * 8, th * 8
            fit = (f"force_original_aspect_ratio=decrease:flags=area,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black"
                   if args.fit else f"force_original_aspect_ratio=increase:flags=area,crop={W}:{H}")
            tmpdir = tempfile.mkdtemp(prefix="videoboy_")
            src = os.path.join(tmpdir, "zw.mkv")
            print("Dekodiere Video …")
            r = subprocess.run(["ffmpeg", "-v", "error", "-y"] + ss + ["-i", args.input] + dur +
                               ["-an", "-vf", f"scale=iw*sar:ih,scale={W}:{H}:{fit},format=rgb24",
                                "-c:v", "ffv1", src], capture_output=True)
            if r.returncode:
                die("ffmpeg (Video) fehlgeschlagen:\n" + r.stderr.decode(errors="replace"))
            vss, vdur = [], []
        while True:
            tw, th = choose_layout_gbc(aspect, n, args.size)
            if gbc_full_slots(tw, th) > gbc_cap(n):
                die(f"{tw*8}x{th*8} ist bei {LCD_FPS/n:.0f} fps zu groß für den Game Boy Color – "
                    f"--fps senken oder --size verkleinern")
            print(f"Game Boy Color: {tw*8}x{th*8} Pixel, {LCD_FPS/n:.2f} fps, 8 Paletten x 4 Farben, "
                  f"{'Kompression an (Toleranz ' + str(args.tolerance) + ')' if args.compress else 'ohne Kompression'}, "
                  f"Ton: {'18396 Hz, 3 bit ' + ('Mono' if args.mono else 'Stereo') if sound else 'aus'}")
            print("Wandle um …")
            pkts, F, ratio = gbc_encode(src, vss, vdur, tw, th, n, args, audio,
                                        args.preview if args.preview else None)
            expected = int(read_s * LCD_FPS / n) - 2
            if F >= expected or not auto or not args.compress or n >= 6:
                break
            nn = min(6, max(n + 1, math.ceil(n * expected / max(F, 1) * 0.9)))
            print(f"  passt bei {LCD_FPS/n:.0f} fps nicht in 8 MB – versuche {LCD_FPS/nn:.0f} fps")
            n = nn
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    if F == 0:
        die("keine Bilder gelesen")
    if F < int(read_s * LCD_FPS / n) - 2:
        print(f"Hinweis: passt nicht ganz in 8 MB – auf {F*n/LCD_FPS:.0f} s gekürzt.", file=sys.stderr)
    data, nbanks = gbc_place(pkts)
    rom = gbc_finalize(n, tw, th, data, nbanks, len(pkts), title)
    out = args.output or os.path.splitext(args.input)[0] + ".gbc"
    with open(out, "wb") as f:
        f.write(rom)
    print(f"Fertig: {out}  ({len(rom)//1024} KB, {F} Bilder, {F*n/LCD_FPS:.1f} s, MBC5, nur Game Boy Color)")
    if args.compress:
        print(f"  Kompression: {ratio*100:.0f} % der Kacheln übertragen")
    if args.preview:
        v = args.preview + ".v.mp4"
        if sound:
            tmpa = args.preview + ".a.raw"
            a = audio[:(F + 1) * GBC_SAMPLES_PER_FRAME * n]
            (np.stack([(a >> 4) & 7, a & 7], 1).astype(np.float32) / 3.5 - 1.0).tofile(tmpa)
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", v, "-f", "f32le", "-ar", str(int(round(GBC_AUDIO_HZ))),
                            "-ac", "2", "-i", tmpa, "-af", "highpass=f=20", "-c:v", "copy", "-c:a", "aac",
                            "-ar", "44100", "-shortest", args.preview])
            os.remove(tmpa)
            os.remove(v)
        else:
            os.replace(v, args.preview)
        print(f"Vorschau: {args.preview}")



def main():
    ap = argparse.ArgumentParser(description="Video -> Game-Boy-ROM (DMG)")
    ap.add_argument("input")
    ap.add_argument("-o", "--output", help="Ausgabe .gb (Standard: <input>.gb)")
    ap.add_argument("--fps", default="auto", help="auto | 60 | 30 | 20 | 15 | 12 | 10  (Standard: auto ≈ 20)")
    ap.add_argument("--size", help="Bildgröße in Pixeln, z. B. 144x80 (Vielfache von 8)")
    ap.add_argument("--fit", action="store_true", help="ganzes Bild mit Rand statt zuschneiden")
    ap.add_argument("--start", help="Startzeit, z. B. 1:23 oder 83.5")
    ap.add_argument("--duration", help="Länge in Sekunden")
    ap.add_argument("--gamma", type=float, default=1.0, help="Helligkeitskurve (<1 heller, >1 dunkler)")
    ap.add_argument("--no-contrast", action="store_true", help="keine automatische Kontrastanpassung")
    ap.add_argument("--no-dither", action="store_true", help="kein Dithering")
    ap.add_argument("--no-audio", action="store_true", help="ohne Ton")
    ap.add_argument("--volume", type=float, default=1.0, help="Lautstärkefaktor (Standard 1.0)")
    ap.add_argument("--mono", action="store_true", help="Ton in Mono statt Stereo")
    ap.add_argument("--title", help="ROM-Titel (max. 15 Zeichen)")
    ap.add_argument("--preview", help="zusätzlich Vorschau-MP4 schreiben")
    ap.add_argument("--gbc", action="store_true",
                    help="ROM für den Game Boy Color: Farbe, bis Vollbild, Ton mit 18396 Hz")
    ap.add_argument("--compress", action="store_true",
                    help="Kompressionsmodus: nur geänderte Bildteile speichern -> deutlich längere Videos")
    ap.add_argument("--tolerance", type=int, default=2,
                    help="Kompression: so viele Pixel pro 8x8-Kachel dürfen abweichen, bevor sie neu "
                         "übertragen wird (0 = verlustfrei, Standard 2, höher = länger aber unsauberer)")
    args = ap.parse_args()

    need("ffmpeg"); need("ffprobe")
    if not os.path.exists(args.input):
        die(f"Datei '{args.input}' nicht gefunden")
    aspect, src_dur, has_audio = probe(args.input)
    ss = ["-ss", args.start] if args.start else []
    start_s = 0.0
    if args.start:
        parts = [float(p) for p in args.start.split(":")]
        for p in parts:
            start_s = start_s * 60 + p
    dur_s = float(args.duration) if args.duration else max(0.0, src_dur - start_s)

    fps_map = {"60": 1, "30": 2, "20": 3, "15": 4, "12": 5, "10": 6}
    auto = args.fps == "auto"
    if auto:
        n = 3
    elif args.fps in fps_map:
        n = fps_map[args.fps]
    else:
        die("--fps: erlaubt sind auto, 60, 30, 20, 15, 12, 10")

    if args.gbc:
        if args.fps not in ("auto",) and args.fps not in fps_map:
            die("--fps: erlaubt sind auto, 60, 30, 20, 15, 12, 10")
        return main_gbc(args, aspect, dur_s, has_audio, ss, auto)
    if args.compress:
        return main_compressed(args, aspect, dur_s, has_audio, ss, auto, n)

    # bei Bedarf Bildrate senken, damit es in 8 MB passt
    while True:
        tw, th = choose_layout(aspect, n, args.size)
        if tw * th > MAX_TILES:
            die(f"{tw*8}x{th*8} = {tw*th} Kacheln, der Game Boy schafft max. {MAX_TILES} "
                f"(z. B. 160x72, 152x80, 128x88, 120x96)")
        if tw * th * 2 > CHUNKS_PER_LCD_FRAME * n:
            die(f"{tw*8}x{th*8} ist bei {LCD_FPS/n:.0f} fps zu groß: max. {80*n} Kacheln – "
                f"--fps senken oder --size verkleinern")
        pkt = LINES * n + tw * th * 16
        ppb = BANK // pkt
        max_s = ((MAX_BANKS - 1) * ppb - 1) * n / LCD_FPS
        if dur_s <= max_s or not auto or n >= 6:
            break
        n += 1
    dur = []
    if dur_s > max_s:
        print(f"Hinweis: Video ({dur_s:.0f} s) passt nicht in 8 MB – wird auf {max_s:.0f} s gekürzt. "
              f"(Mit --start/--duration einen Ausschnitt wählen oder --fps senken.)", file=sys.stderr)
        dur_s = max_s
        dur = ["-t", f"{max_s:.3f}"]
    elif args.duration:
        dur = ["-t", args.duration]

    print(f"Bild: {tw*8}x{th*8} Pixel, {LCD_FPS/n:.2f} fps, Ton: {'aus' if args.no_audio or not has_audio else '9198 Hz, 3 bit ' + ('Mono' if args.mono else 'Stereo')}")
    print("Lese Video …")
    frames = read_video(args.input, tw, th, n, ss, dur, not args.fit)
    if len(frames) == 0:
        die("keine Bilder gelesen")
    idx = quantize_video(frames, args.gamma, not args.no_contrast, not args.no_dither)
    tiles = encode_tiles(idx)
    F = len(tiles)
    if args.no_audio or not has_audio:
        audio = np.full(F * LINES * n, SILENCE, np.uint8)
    else:
        print("Lese Ton …")
        audio = quantize_audio(read_audio(args.input, ss, dur, args.mono), args.volume)

    title = args.title or os.path.splitext(os.path.basename(args.input))[0]
    rom, nbanks = build_rom(tiles, audio, n, tw, th, title)
    out = args.output or os.path.splitext(args.input)[0] + ".gb"
    with open(out, "wb") as f:
        f.write(rom)
    print(f"Fertig: {out}  ({len(rom)//1024} KB, {F} Bilder, {F*n/LCD_FPS:.1f} s, MBC5)")
    if args.preview:
        print("Schreibe Vorschau …")
        write_preview(args.preview, idx, audio, n, not (args.no_audio or not has_audio))
        print(f"Vorschau: {args.preview}")


def main_compressed(args, aspect, dur_s, has_audio, ss, auto, n):
    MAX_READ = 20 * 60
    sound = not (args.no_audio or not has_audio)
    read_s = dur_s
    if read_s > MAX_READ:
        print(f"Hinweis: Es werden höchstens {MAX_READ//60} Minuten gelesen – mit --start/--duration "
              f"einen Ausschnitt wählen.", file=sys.stderr)
        read_s = MAX_READ
    dur = ["-t", f"{read_s:.3f}"] if (args.duration or read_s < dur_s) else []
    audio_f = None
    if sound:
        print("Lese Ton …")
        audio_f = read_audio(args.input, ss, dur, args.mono)
    title = args.title or os.path.splitext(os.path.basename(args.input))[0]
    src, vss, vdur, fill = args.input, ss, dur, not args.fit
    tmpdir = None
    if auto:
        # Original nur einmal dekodieren: kleine verlustfreie Zwischendatei in Zielgröße
        tw, th = choose_layout(aspect, n, args.size, z_max_tiles(n))
        W, H = tw * 8, th * 8
        if fill:
            vf = f"scale=iw*sar:ih,scale={W}:{H}:force_original_aspect_ratio=increase:flags=area,crop={W}:{H},format=gray"
        else:
            vf = (f"scale=iw*sar:ih,scale={W}:{H}:force_original_aspect_ratio=decrease:flags=area,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,format=gray")
        tmpdir = tempfile.mkdtemp(prefix="videoboy_")
        src = os.path.join(tmpdir, "zw.mkv")
        print("Dekodiere Video …")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y"] + ss + ["-i", args.input] + dur +
                           ["-an", "-vf", vf, "-c:v", "ffv1", src], capture_output=True)
        if r.returncode:
            die("ffmpeg (Video) fehlgeschlagen:\n" + r.stderr.decode(errors="replace"))
        vss, vdur = [], []
    try:
        res = _compress_loop(args, aspect, n, auto, sound, audio_f, src, vss, vdur, fill, title)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return res


def _compress_loop(args, aspect, n, auto, sound, audio_f, src, ss, dur, fill, title):
    while True:
        lim = z_max_tiles(n)
        tw, th = choose_layout(aspect, n, args.size, lim)
        if tw * th > lim:
            die(f"{tw*8}x{th*8} = {tw*th} Kacheln, im Kompressionsmodus sind bei {LCD_FPS/n:.0f} fps "
                f"max. {lim} möglich – --fps senken oder --size verkleinern")
        print(f"Bild: {tw*8}x{th*8} Pixel, {LCD_FPS/n:.2f} fps, Kompression an (Toleranz {args.tolerance}), "
              f"Ton: {'9198 Hz, 3 bit ' + ('Mono' if args.mono else 'Stereo') if sound else 'aus'}")
        print("Lese Video …")
        frames = read_video(src, tw, th, n, ss, dur, fill)
        if len(frames) == 0:
            die("keine Bilder gelesen")
        idx = quantize_video(frames, args.gamma, not args.no_contrast, not args.no_dither)
        del frames
        tiles = encode_tiles(idx)
        if sound:
            audio = quantize_audio(audio_f, args.volume)
        else:
            audio = np.full((len(tiles) + 1) * LINES * n, SILENCE, np.uint8)
        print("Komprimiere …")
        need = (len(tiles) + 1) * LINES * n
        if len(audio) < need:
            audio = np.concatenate([audio, np.full(need - len(audio), SILENCE, np.uint8)])
        pkts, sent = z_packets(tiles, idx, audio, n, tw, th, args.tolerance)
        F = z_fit(pkts, LINES * n)
        if F == len(tiles) or not auto or n >= 6:
            break
        nn = min(6, max(n + 1, math.ceil(n * len(tiles) / max(F, 1) * 0.85)))
        print(f"  passt bei {LCD_FPS/n:.0f} fps nicht in 8 MB – versuche {LCD_FPS/nn:.0f} fps")
        n = nn
    if F < len(tiles):
        print(f"Hinweis: passt nicht ganz in 8 MB – auf {F*n/LCD_FPS:.0f} s gekürzt. "
              f"(--tolerance erhöhen, --fps senken oder Ausschnitt wählen.)", file=sys.stderr)
    rom, nbanks = build_rom_z(pkts, audio, n, tw, th, title, F)
    ratio = sum(sent[:F]) / (F * tw * th)
    out = args.output or os.path.splitext(args.input)[0] + ".gb"
    with open(out, "wb") as f:
        f.write(rom)
    normal_banks = -(-(F + 1) // (BANK // (LINES * n + tw * th * 16)))
    print(f"Fertig: {out}  ({len(rom)//1024} KB, {F} Bilder, {F*n/LCD_FPS:.1f} s, MBC5)")
    print(f"  Kompression: {ratio*100:.0f} % der Kacheln übertragen, {normal_banks/nbanks:.1f}x weniger Speicher als ohne")
    if args.preview:
        print("Schreibe Vorschau …")
        dec = decode_z_preview(idx[:F], tw, th, args.tolerance, n)
        write_preview(args.preview, dec, audio, n, sound)
        print(f"Vorschau: {args.preview}")


def decode_z_preview(idx, tw, th, tol, n):
    """Simuliert den Decoder (inkl. Toleranz), damit die Vorschau exakt dem Game Boy entspricht."""
    F = len(idx)
    T = tw * th
    pix = idx.reshape(F, th, 8, tw, 8).transpose(0, 1, 3, 2, 4).reshape(F, T, 64)
    cap = SLOTS_PER_LCD_FRAME * n
    buf = [None, None]
    out = np.empty_like(pix)
    for k in range(F):
        b = k & 1
        if buf[b] is None:
            buf[b] = pix[k].copy()
        else:
            ch = np.nonzero((buf[b] != pix[k]).sum(1) > tol)[0]
            if 2 * len(ch) + len(z_runs(ch)) + 1 > cap:
                ch = np.arange(T)
            buf[b][ch] = pix[k][ch]
        out[k] = buf[b]
    return out.reshape(F, th, tw, 8, 8).transpose(0, 1, 3, 2, 4).reshape(F, th * 8, tw * 8)


if __name__ == "__main__":
    main()
