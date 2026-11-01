#!/usr/bin/env python3
"""
videoboy.py (VIDEO BOY) – macht aus einer beliebigen Videodatei ein Game-Boy-ROM (DMG-01).

    python3 videoboy.py film.mp4                 -> film.gb
    python3 videoboy.py film.mp4 -o clip.gb --start 0:30 --duration 60 --fps 30

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


def read_audio(path, ss, dur, mono=False):
    """Ton dekodieren und aufbereiten (Filterkette aus MP3 BOY, angepasst an 9198 Hz):
    Trittschall weg, Bandbegrenzung unter Nyquist, weniger Dröhnen, mehr Präsenz,
    Lautheitsausgleich, Kompressor, Ausgleich der Höhendämpfung durch das
    Sample-and-Hold des Game Boy, Limiter."""
    af = ("highpass=f=90,lowpass=f=4200,"
          "equalizer=f=250:t=q:w=1:g=-2,"            # weniger Dröhnen
          "equalizer=f=2500:t=q:w=1:g=2,"            # mehr Präsenz
          "dynaudnorm=f=75:g=7:p=0.9:m=40:s=12,"
          "acompressor=threshold=0.05:ratio=8:attack=2:release=60:makeup=4,"
          "highshelf=f=2800:g=2.5:t=q:w=0.7,"        # Halteglied-Ausgleich
          "alimiter=limit=0.98:attack=1:release=20")
    ch = "1" if mono else "2"
    for resample in (",aresample=9198:resampler=soxr", ""):
        cmd = ["ffmpeg", "-v", "error"] + ss + ["-i", path] + dur + \
              ["-vn", "-af", af + resample, "-ac", ch, "-ar", "9198", "-f", "f32le", "-"]
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


def _quantize_channel(ch, gate=0.03, shape=0.3):
    """Float [-1,1] -> Stufen 0..7 (aus MP3 BOY):
    symmetrisch um 3.5, Rauschformung schiebt das Quantisierungsrauschen in tiefe
    Frequenzen, wo es von der Musik verdeckt wird; Stille liegt exakt auf Stufe 4.
    Formungsstärke 0.3 statt 0.8: bei 9198 Hz gemessen am besten (A-bewertet)."""
    rate = int(AUDIO_HZ)
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


def quantize_audio(a, gain=1.0):
    """(N,2) Float -> NR50-Bytes: Bits 6-4 links, 2-0 rechts (Stereo)."""
    if len(a) == 0:
        return np.zeros(0, np.uint8)
    peak = np.percentile(np.abs(a), 99.5)
    if peak < 1e-4:
        return np.full(len(a), SILENCE, np.uint8)
    y = a / peak
    y = np.tanh(y * 1.1 * gain) / np.tanh(1.1)
    l = _quantize_channel(y[:, 0])
    r = _quantize_channel(y[:, 1])
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
