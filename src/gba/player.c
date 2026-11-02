// VIDEO BOY ADVANCE - video player for the Game Boy Advance (also GBA SP, Game Boy Micro,
// Nintendo DS/DS Lite in GBA mode, 3DS via open_agb_firm).
//
// Picture: mode 4 (8 bpp, 256 colours from 32768), two pages flipped in VBlank, the
//          picture is scaled to the screen by the BG2 affine hardware.
// Stream:  packets of changed 8x8 blocks, read directly from the cartridge.
// Sound:   8-bit stereo at 18157 Hz (304 samples per frame) through Direct Sound A/B,
//          fed by DMA straight from ROM; timer 0 keeps it locked to the video.
// Keys:    START = pause, SELECT = restart.
#include <stdint.h>

typedef volatile uint16_t vu16;
typedef volatile uint32_t vu32;
#define REG16(a) (*(vu16 *)(a))
#define REG32(a) (*(vu32 *)(a))

#define DISPCNT   REG16(0x04000000)
#define DISPSTAT  REG16(0x04000004)
#define VCOUNT    REG16(0x04000006)
#define BG2PA     (*(volatile int16_t *)0x04000020)
#define BG2PB     (*(volatile int16_t *)0x04000022)
#define BG2PC     (*(volatile int16_t *)0x04000024)
#define BG2PD     (*(volatile int16_t *)0x04000026)
#define BG2X      (*(volatile int32_t *)0x04000028)
#define BG2Y      (*(volatile int32_t *)0x0400002C)
#define SOUNDCNT_L REG16(0x04000080)
#define SOUNDCNT_H REG16(0x04000082)
#define SOUNDCNT_X REG16(0x04000084)
#define FIFO_A    0x040000A0
#define FIFO_B    0x040000A4
#define DMA1SAD   REG32(0x040000BC)
#define DMA1DAD   REG32(0x040000C0)
#define DMA1CNT   REG32(0x040000C4)
#define DMA2SAD   REG32(0x040000C8)
#define DMA2DAD   REG32(0x040000CC)
#define DMA2CNT   REG32(0x040000D0)
#define DMA3SAD   REG32(0x040000D4)
#define DMA3DAD   REG32(0x040000D8)
#define DMA3CNT   REG32(0x040000DC)
#define TM0CNT_L  REG16(0x04000100)
#define TM0CNT_H  REG16(0x04000102)
#define KEYINPUT  REG16(0x04000130)
#define IE        REG16(0x04000200)
#define IF        REG16(0x04000202)
#define WAITCNT   REG16(0x04000204)
#define IME       REG16(0x04000208)
#define BIOS_IF   REG16(0x03007FF8)
#define IRQ_VEC   (*(void (*volatile *)(void))0x03007FFC)

#define PAL   ((vu16 *)0x05000000)
#define VRAM  0x06000000u
#define PAGE1 0x0600A000u

#define TIMER_RELOAD (65536 - 924)      // 16777216 / 924 = 18157 Hz, 304 samples per frame

typedef struct {
    uint32_t magic;       // 'VBA1'
    uint32_t n;           // LCD frames per video frame
    uint32_t frames;      // video frames
    uint32_t video;       // address of the first packet
    uint32_t audioL;      // address of the left (or mono) sample stream, 0 = no sound
    uint32_t audioR;      // address of the right stream (== audioL for mono)
    int32_t  pa, pd;      // BG2 scale (8.8)
    int32_t  x, y;        // BG2 reference point (20.8)
} Config;

__attribute__((section(".vbconfig"), used))
const Config vb_config = { 0x31414256, 3, 0, 0, 0, 0, 256, 256, 0, 0 };
#define CFG ((const volatile Config *)0x08000400)

// ------------------------------------------------------------------ IRQ state
static volatile uint32_t vcount;       // VBlanks since the start of the video (not counted while paused)
static volatile uint32_t paused;
static volatile uint32_t flipPending, flipAt, flipFrame, shown, startAudio;
static volatile uint32_t flipPal;      // address of 512 palette bytes or 0

static void audio_stop(void) {
    TM0CNT_H = 0;
    DMA1CNT = 0;
    DMA2CNT = 0;
}

static void audio_start(void) {
    if (!CFG->audioL) return;
    SOUNDCNT_H = (1 << 2) | (1 << 3) | (1 << 11) | (1 << 15) |
                 (CFG->audioR != CFG->audioL ? ((1 << 9) | (1 << 12)) : ((1 << 8) | (1 << 9)));
    DMA1SAD = CFG->audioL;
    DMA1DAD = FIFO_A;
    DMA1CNT = 0xB6400000;               // enable, FIFO timing, repeat, 32 bit, dest fixed
    if (CFG->audioR != CFG->audioL) {
        DMA2SAD = CFG->audioR;
        DMA2DAD = FIFO_B;
        DMA2CNT = 0xB6400000;
    }
    TM0CNT_L = TIMER_RELOAD;
    TM0CNT_H = 0x80;
}

__attribute__((section(".iwram"), noinline))
void irq_handler(void) {
    uint16_t f = IF;
    if (f & 1) {
        if (!paused) vcount++;
        if (flipPending && (int32_t)(vcount - flipAt) >= 0) {
            if (flipPal) {
                DMA3SAD = flipPal;
                DMA3DAD = 0x05000000;
                DMA3CNT = 0x84000000 | 128;          // 32 bit, 128 words
            }
            DISPCNT = (DISPCNT ^ 0x10) & ~0x80;      // flip, leave forced blank
            shown = flipFrame;
            flipPending = 0;
            if (startAudio) { startAudio = 0; vcount = 0; audio_start(); }
        }
    }
    IF = f;
    BIOS_IF |= f;
}

// ------------------------------------------------------------------ decoding
// Copies the runs of one packet into a page. Run header: u16 offset, u8 width (blocks), u8 rows.
__attribute__((section(".iwram"), noinline))
uint32_t decode_runs(uint32_t p, uint32_t page) {
    for (;;) {
        uint32_t h = *(const uint32_t *)p;
        p += 4;
        uint32_t rows = h >> 24;
        if (!rows) break;
        uint32_t words = ((h >> 16) & 0xFF) * 2;
        vu32 *dst = (vu32 *)(page + (h & 0xFFFF));
        const uint32_t *src = (const uint32_t *)p;
        for (uint32_t r = 0; r < rows; r++) {
            for (uint32_t i = 0; i < words; i++) dst[i] = src[i];
            src += words;
            dst += 60;
        }
        p = (uint32_t)src;
    }
    return p;
}
// pointer to function in IWRAM (long call from ROM)
static uint32_t (*const decode)(uint32_t, uint32_t) = decode_runs;

static void vsync(void) { __asm__ volatile("swi 0x050000" ::: "r0", "r1", "r2", "r3", "memory"); }

static uint16_t keysPrev;
static uint16_t keys_new(void) {
    uint16_t k = ~KEYINPUT & 0x3FF;
    uint16_t n = k & ~keysPrev;
    keysPrev = k;
    return n;
}

// returns 1 when SELECT asked for a restart
static int poll_keys(void) {
    uint16_t n = keys_new();
    if (n & 4) return 1;
    if (n & 8) {
        paused = 1;
        TM0CNT_H = 0;
        for (;;) {
            vsync();
            n = keys_new();
            if (n & 4) { paused = 0; return 1; }
            if (n & 8) break;
        }
        paused = 0;
        if (CFG->audioL) TM0CNT_H = 0x80;
    }
    return 0;
}

static void clear_vram(void) {
    for (vu32 *v = (vu32 *)VRAM; v < (vu32 *)(VRAM + 0x14000); v++) *v = 0;
    for (int i = 0; i < 256; i++) PAL[i] = 0;
}

int main(void) {
    WAITCNT = 0x4317;                        // ROM 3/1 wait states, prefetch
    DISPCNT = 0x80;                          // forced blank while setting up
    clear_vram();
    BG2PA = CFG->pa; BG2PB = 0; BG2PC = 0; BG2PD = CFG->pd;
    BG2X = CFG->x;   BG2Y = CFG->y;
    SOUNDCNT_X = 0x80;
    SOUNDCNT_L = 0;
    IRQ_VEC = irq_handler;
    DISPSTAT = 1 << 3;
    IE = 1;
    IME = 1;
    DISPCNT = 0x0404 | 0x80;                 // mode 4, BG2, still blank until the first frame

    const uint32_t n = CFG->n, frames = CFG->frames;
    for (;;) {                               // one pass = the whole video
        audio_stop();
        flipPending = 0;
        uint32_t base = ((DISPCNT >> 4) & 1) ^ 1;    // frame 0 goes to the hidden page
        uint32_t p = CFG->video;
        int restart = 0;
        for (uint32_t k = 0; k < frames && !restart; k++) {
            if (k > 0) {                     // the page of frame k is free once frame k-1 is shown
                while (flipPending || shown != k - 1) { vsync(); if (poll_keys()) { restart = 1; break; } }
                if (restart) break;
            }
            uint32_t flags = *(const uint32_t *)p;
            p += 4;
            uint32_t pal = 0;
            if (flags & 1) { pal = p; p += 512; }
            uint32_t page = ((k + base) & 1) ? PAGE1 : VRAM;
            p = decode(p, page);
            IME = 0;
            flipPal = pal;
            flipFrame = k;
            if (k == 0) { flipAt = vcount + 1; startAudio = 1; }
            else flipAt = k * n;
            flipPending = 1;
            IME = 1;
            if (poll_keys()) restart = 1;
        }
        // show the last frame for its full time
        while (!restart && (flipPending || (int32_t)(vcount - frames * n) < 0)) { vsync(); if (poll_keys()) restart = 1; }
        IME = 0; flipPending = 0; startAudio = 0; IME = 1;
        paused = 0;
    }
}

// the compiler may call these
void *memcpy(void *d, const void *s, unsigned n) { uint8_t *a = d; const uint8_t *b = s; while (n--) *a++ = *b++; return d; }
void *memset(void *d, int c, unsigned n) { uint8_t *a = d; while (n--) *a++ = (uint8_t)c; return d; }
