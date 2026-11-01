# VIDEO BOY: Video Player for the Original Game Boy (DMG)

`videoboy.py` turns any video file (mp4, mkv, mov, webm …) into a Game Boy ROM with
**picture and stereo sound**. The ROM runs on the original **DMG-01** as well as on the
Game Boy Pocket, Color and Advance.

![VIDEO BOY](preview.gif)

*Captured from the cycle-accurate emulator SameBoy (DMG model). Footage: "Big Buck Bunny", see credits below.*

## Requirements

* Python 3.8+ with `numpy` (`pip install numpy`)
* `ffmpeg` and `ffprobe` in your PATH (on Windows, placing `ffmpeg.exe` and `ffprobe.exe`
  next to `videoboy.py` works too)
* For real hardware: a flash cart with **MBC5** support, e.g. EverDrive GB X series, EZ-Flash Junior
  or an 8 MB MBC5 repro flash cart. Long videos need a cart that takes 8 MB ROMs.

## Usage

```sh
python3 videoboy.py movie.mp4                         # -> movie.gb
python3 videoboy.py movie.mp4 -o clip.gb --start 1:20 --duration 45
python3 videoboy.py movie.mp4 --fps 30                # smoother, but a smaller picture
python3 videoboy.py movie.mp4 --compress              # compression mode: much more video per ROM
python3 videoboy.py movie.mp4 --preview preview.mp4   # see and hear the result before flashing
```

| Option | Meaning |
|---|---|
| `--fps auto/60/30/20/15/12/10` | Frame rate. `auto` = 20 fps, lowered automatically for long videos |
| `--size 144x80` | Picture size in pixels (multiples of 8, max. 191 tiles) |
| `--fit` | Show the whole picture with black borders instead of cropping |
| `--start`, `--duration` | Convert only a part of the video |
| `--gamma 0.8` | Brighter (< 1) or darker (> 1) |
| `--no-contrast`, `--no-dither` | Disable automatic contrast or dithering |
| `--volume 1.5` | Louder (with soft limiting) |
| `--mono` | Mono instead of stereo sound |
| `--no-audio` | No sound |
| `--title NAME` | ROM title in the cartridge header |
| `--preview file.mp4` | Preview video in green DMG look, with sound |
| `--compress` | **Compression mode**: much longer videos per ROM (see below) |
| `--tolerance 2` | Only with `--compress`: how many pixels per 8×8 tile may differ before the tile is resent (0 = lossless) |

The command line messages of the tool are in German.

**On the Game Boy:** START = pause, SELECT = restart. At the end, the video loops.

`demo.gb` is a ready-to-play demo ROM (Game of Life animation with a melody).

## What the Game Boy can do

Normal mode, 16:9 video:

| fps | Picture size | Max. length in 8 MB |
|---|---|---|
| 60 | 96×48 | ~1:40 min |
| 30 | 128×80 | ~1:25 min |
| 20 | 152×80 | ~1:40 min |
| 15 | 152×80 | ~2:15 min |
| 12 | 152×80 | ~2:50 min |
| 10 | 152×80 | ~3:25 min |

If the video is too long, `--fps auto` lowers the frame rate down to 10 fps. If it still doesn't fit,
the video is cut (with a notice). Use `--compress`, a lower `--fps` or `--start/--duration` for longer material.

The Game Boy cannot read the flash cart's SD card while a ROM is running, so a longer film has to be split
into several ROMs that you start one after another from the cart's menu.

## Compression mode (`--compress`)

```sh
python3 videoboy.py movie.mp4 --compress
python3 videoboy.py movie.mp4 --compress --tolerance 4 --fps 15   # even longer
```

Instead of storing every frame completely, the ROM only contains the 8×8 tiles that changed since the
last frame in the same buffer. The player writes just these tiles to VRAM; everything else stays on screen.

* **How much more fits?** That depends on the material. Calm scenes (dialogue, landscapes, cartoons with a still
  background) compress very well, fast cuts and camera pans hardly at all.
  Measured with "Big Buck Bunny": **2.4×** less memory at 20 fps; a 6-minute sequence with many cuts at 10 fps
  needed 1.6× less, i.e. over 5 minutes in 8 MB instead of 3:25.
* **`--tolerance`** (default 2): a tile in which at most this many pixels changed is not resent. This saves a
  lot, because dithering makes single pixels flip all the time in real video. Errors do not accumulate, because the
  converter tracks exactly what the Game Boy is showing. `0` is lossless; higher values (4–6) give more length but
  occasional "stuck" pixels.
* With `--fps auto`, the converter compresses first and only lowers the frame rate if the result does not fit.
* Sound is not compressed (9198 Hz, 3-bit stereo), so it takes up a larger share of the ROM in this mode.
* The maximum picture size at 30 and 60 fps is slightly smaller than in normal mode (150 and 75 tiles).
* `--preview` shows exactly the compressed result.

## How it works

* **Picture:** 4 shades of grey with ordered dithering (Bayer 4×4). The steady pattern does not flicker in moving
  scenes. The picture is stored directly as tile data in the ROM.
* **Streaming:** In every scanline the CPU copies 8 bytes into VRAM, exactly during the HBlank/OAM phase in which
  VRAM is writable (woken up by `HALT` on the STAT interrupt). During VBlank it copies 16 bytes per line.
  That is about 1.25 KB per LCD frame.
* **Double buffering:** Two picture buffers (up to 191 tiles each) live in the two tile data areas, each with its
  own tile map. A single LCDC write during VBlank switches between them, so there is no tearing.
* **Sound:** Channel 3 plays a constant waveform, and the master volume register NR50 is rewritten once per
  scanline. This gives **stereo** PCM with 3 bits per side (bits 6–4 left, 2–0 right) at 9198 Hz, in sync with
  the picture.
* **Audio processing (taken from the [MP3 BOY](https://github.com/PedaPeda/MP3-Boy) project):** high-pass against
  rumble, band limiting below the Nyquist frequency, less boom, more presence, loudness levelling, compressor,
  compensation for the treble loss caused by the Game Boy's sample-and-hold, limiter. After that, symmetric
  quantisation to 8 levels per side with noise shaping, and silence sits exactly on one level, so quiet passages
  don't hiss. The filters are adapted to 9198 Hz. Noise shaping uses strength 0.3 instead of MP3 BOY's 0.8,
  because that measured cleanest at this sample rate. MP3 BOY's 16384 Hz is not possible here, because the CPU
  needs every scanline for streaming the picture.
* **ROM:** MBC5, up to 8 MB. Each packet (sound for N frames + one picture) sits entirely inside one 16 KB bank.
* **Compression mode:** In each scanline the player either copies an 8-byte block or reads the header of the next
  run of tiles (start address + length, without touching VRAM). Packets have variable length; an end marker says
  whether the next packet follows directly or starts at the beginning of the next bank.

Tested in the cycle-accurate emulator SameBoy (DMG model), in both modes: every frame arrives pixel-perfect at an
exactly even frame rate, picture and sound stay in sync, including 8 MB ROMs across all 511 banks and stress
videos that use almost the full per-frame capacity. Pause and restart work. Not yet tested on real hardware.

Simple emulators that don't emulate the NR50 master volume register (e.g. PyBoy) play no sound.

## Building the player yourself (optional)

`videoboy.py` already contains the finished player. If you want to change `src/player.asm`, you need
[RGBDS](https://rgbds.gbdev.io):

```sh
sh src/build.sh      # builds the player and embeds it into videoboy.py
```

## Credits

Preview footage: "Big Buck Bunny" © Blender Foundation, [peach.blender.org](https://peach.blender.org),
licensed under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).
