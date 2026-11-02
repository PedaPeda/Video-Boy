// VIDEO BOY DS - video player for the Nintendo DS, DS Lite, DSi and 3DS (via TWiLight Menu++
// or nds-bootstrap) and DS flash carts.
//
// Picture: top screen, 8 bpp bitmap BG3 (256 colours from 32768), two pages flipped in VBlank,
//          scaled to 256x192 by the affine hardware.
// Data:    video.bin inside the ROM's NitroFS, streamed with fread (works from SD via DLDI /
//          DSi SD and from a cartridge); a queue of read-ahead records absorbs SD hiccups.
// Sound:   8-bit stereo PCM, 355 samples per frame (21238 Hz), two looping channels on a ring
//          buffer in main RAM that is refilled ahead of the playback position.
// Keys:    START = pause, SELECT = restart.
#include <nds.h>
#include <filesystem.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SPF 355                     // samples per LCD frame (sound timer 789)
#define RINGF 48                    // ring buffer length in LCD frames
#define RING (SPF * RINGF)
#define QUEUE 24                    // records read ahead

typedef struct {
    u32 magic, version, n, frames, w, h;
    s32 pa, pd, x, y;
    u32 channels, spf;
} Header;

typedef struct { u8 *data; u32 size; } Record;

static Header hdr;
static FILE *vfile;
static long firstRecord;

static Record queue[QUEUE];
static u32 qHead, qCount;           // queue holds records qHead .. qHead+qCount-1 (frame numbers)
static u32 framesRead;

static s8 ringL[RING] ALIGN(32), ringR[RING] ALIGN(32);
static int chL = -1, chR = -1;
static u32 audioWritten;            // LCD frames whose samples are in the ring

static volatile u32 vcount, paused, flipPending, flipAt, flipFrame, shown;
static volatile u16 *flipPal;
static u16 palette[256];
static int pageShown;               // 0 or 1

static void set_page(int p) {
    REG_BG3CNT = BG_BMP8_256x256 | BG_BMP_BASE(p ? 4 : 0) | BG_PRIORITY(0);
    pageShown = p;
}

static void vblank(void) {
    if (!paused) vcount++;
    if (flipPending && (s32)(vcount - flipAt) >= 0) {
        if (flipPal) dmaCopy((const void *)flipPal, BG_PALETTE, 512);
        set_page(pageShown ^ 1);
        shown = flipFrame;
        flipPending = 0;
    }
}

static u8 *page_ptr(int p) { return (u8 *)(p ? BG_BMP_RAM(4) : BG_BMP_RAM(0)); }

// run header: u16 offset, u8 width (blocks of 8 px), u8 rows; rows == 0 ends the packet
static const u8 *decode(const u8 *p, u8 *page) {
    for (;;) {
        u32 h = *(const u32 *)p;
        p += 4;
        u32 rows = h >> 24;
        if (!rows) break;
        u32 words = ((h >> 16) & 0xFF) * 2;
        vu32 *dst = (vu32 *)(page + (h & 0xFFFF));
        const u32 *src = (const u32 *)p;
        for (u32 r = 0; r < rows; r++) {
            for (u32 i = 0; i < words; i++) dst[i] = src[i];
            src += words;
            dst += 64;
        }
        p = (const u8 *)src;
    }
    return p;
}

static Record *rec(u32 k) { return &queue[(k - qHead + (qHead % QUEUE)) % QUEUE]; }

static void read_ahead(void) {
    while (qCount < QUEUE && framesRead < hdr.frames) {
        u32 size;
        if (fread(&size, 4, 1, vfile) != 1) { framesRead = hdr.frames; break; }
        u8 *d = malloc(size);
        if (!d || fread(d, 1, size, vfile) != size) { free(d); framesRead = hdr.frames; break; }
        Record *r = &queue[(qHead + qCount) % QUEUE];
        r->data = d; r->size = size;
        qCount++;
        framesRead++;
    }
}

// samples of video frame k start after its video packet; returns pointer to the left stream
static const s8 *audio_of(Record *r, const u8 *afterVideo) { (void)r; return (const s8 *)afterVideo; }

static u32 videoEnd[QUEUE];          // offset of the audio within each queued record
static u8 videoKnown[QUEUE];

static u32 video_size(Record *r) {   // walk the packet to find where the audio begins
    const u8 *p = r->data;
    u32 flags = *(const u32 *)p;
    p += 4;
    if (flags & 1) p += 512;
    for (;;) {
        u32 h = *(const u32 *)p;
        p += 4;
        u32 rows = h >> 24;
        if (!rows) break;
        p += rows * ((h >> 16) & 0xFF) * 8;
    }
    return p - r->data;
}

// fill the ring with audio of queued records, never more than RINGF-2 frames ahead of playback
static void write_audio(void) {
    if (!hdr.channels) return;
    u32 n = hdr.n;
    for (;;) {
        u32 vf = audioWritten / n;               // video frame whose audio is next
        if (vf >= hdr.frames) return;
        if (vf < qHead || vf >= qHead + qCount) return;
        if ((s32)(audioWritten + n - vcount) > RINGF - 2) return;
        Record *r = rec(vf);
        u32 slot = (vf - qHead + (qHead % QUEUE)) % QUEUE;
        if (!videoKnown[slot]) { videoEnd[slot] = video_size(r); videoKnown[slot] = 1; }
        const s8 *L = audio_of(r, r->data + videoEnd[slot]);
        const s8 *R = hdr.channels == 2 ? L + n * SPF : L;
        for (u32 f = 0; f < n; f++) {
            u32 o = ((audioWritten + f) % RINGF) * SPF;
            memcpy(ringL + o, L + f * SPF, SPF);
            if (hdr.channels == 2) memcpy(ringR + o, R + f * SPF, SPF);
            DC_FlushRange(ringL + o, SPF);
            if (hdr.channels == 2) DC_FlushRange(ringR + o, SPF);
        }
        audioWritten += n;
    }
}

static void drop_front(void) {
    u32 slot = qHead % QUEUE;
    free(queue[slot].data);
    queue[slot].data = NULL;
    videoKnown[slot] = 0;
    qHead++;
    qCount--;
}

static void audio_stop(void) {
    if (chL >= 0) soundKill(chL);
    if (chR >= 0) soundKill(chR);
    chL = chR = -1;
}

static void audio_start(void) {
    if (!hdr.channels) return;
    // 21250 makes libnds program the sound timer with 789 -> 355 samples per frame
    if (hdr.channels == 2) {
        chL = soundPlaySample(ringL, SoundFormat_8Bit, RING, 21250, 127, 0, true, 0);
        chR = soundPlaySample(ringR, SoundFormat_8Bit, RING, 21250, 127, 127, true, 0);
    } else {
        chL = soundPlaySample(ringL, SoundFormat_8Bit, RING, 21250, 127, 64, true, 0);
    }
}

static u32 keysPrev;
static u32 keys_new(void) {
    scanKeys();
    u32 k = keysHeld();
    u32 n = k & ~keysPrev;
    keysPrev = k;
    return n;
}

// returns 1 if SELECT asked for a restart
static int poll_keys(void) {
    u32 n = keys_new();
    if (n & KEY_SELECT) return 1;
    if (n & KEY_START) {
        paused = 1;
        if (chL >= 0) soundPause(chL);
        if (chR >= 0) soundPause(chR);
        for (;;) {
            swiWaitForVBlank();
            n = keys_new();
            if (n & KEY_SELECT) { paused = 0; return 1; }
            if (n & KEY_START) break;
        }
        if (chL >= 0) soundResume(chL);
        if (chR >= 0) soundResume(chR);
        paused = 0;
    }
    return 0;
}

static void fail(const char *msg) {
    printf("\n\n  %s\n", msg);
    for (;;) swiWaitForVBlank();
}

int main(int argc, char **argv) {
    (void)argc; (void)argv;
    powerOn(POWER_ALL_2D);
    lcdMainOnTop();
    videoSetMode(MODE_5_2D | DISPLAY_BG3_ACTIVE);
    vramSetBankA(VRAM_A_MAIN_BG_0x06000000);
    consoleDemoInit();
    BG_PALETTE[0] = 0;
    memset((void *)BG_BMP_RAM(0), 0, 0x20000);
    set_page(0);

    printf("\n  VIDEO BOY\n\n");
    if (!nitroFSInit(NULL)) fail("Cannot read the video data.\n  Start this file from a flash\n  cart or TWiLight Menu++.");
    vfile = fopen("nitro:/video.bin", "rb");
    if (vfile) setvbuf(vfile, NULL, _IOFBF, 64 * 1024);       // before the first read
    if (!vfile || fread(&hdr, sizeof hdr, 1, vfile) != 1 || hdr.magic != 0x53444256) fail("No video in this file.");
    firstRecord = ftell(vfile);

    REG_BG3PA = hdr.pa; REG_BG3PB = 0; REG_BG3PC = 0; REG_BG3PD = hdr.pd;
    REG_BG3X = hdr.x;   REG_BG3Y = hdr.y;
    soundEnable();
    irqSet(IRQ_VBLANK, vblank);
    irqEnable(IRQ_VBLANK);

    printf("  %lu frames, %lu x %lu\n\n", (unsigned long)hdr.frames, (unsigned long)hdr.w, (unsigned long)hdr.h);
    printf("  START   pause\n  SELECT  restart\n");

    for (;;) {                                   // one pass = the whole video
        audio_stop();
        while (qCount) drop_front();
        qHead = 0; framesRead = 0; audioWritten = 0;
        flipPending = 0; paused = 0;
        memset(ringL, 0, RING); memset(ringR, 0, RING);
        DC_FlushRange(ringL, RING); DC_FlushRange(ringR, RING);
        fseek(vfile, firstRecord, SEEK_SET);
        read_ahead();
        vcount = 0;
        write_audio();                           // pre-fill the ring before the start

        int restart = 0;
        u32 n = hdr.n;
        for (u32 k = 0; k < hdr.frames && !restart; k++) {
            // wait until frame k is queued and its page is free (frame k-1 on screen)
            for (;;) {
                read_ahead();
                write_audio();
                if (k >= qHead + qCount) continue;          // still reading
                if (k == 0 || (!flipPending && shown == k - 1)) break;
                swiWaitForVBlank();
                if (poll_keys()) { restart = 1; break; }
            }
            if (restart) break;
            Record *r = rec(k);
            const u8 *p = r->data;
            u32 flags = *(const u32 *)p;
            p += 4;
            u16 *pal = NULL;
            if (flags & 1) { memcpy(palette, p, 512); DC_FlushRange(palette, 512); pal = palette; p += 512; }
            decode(p, page_ptr(pageShown ^ 1));
            if (k == 0) {                        // start picture and sound together in VBlank
                swiWaitForVBlank();
                int old = enterCriticalSection();
                if (pal) dmaCopy(pal, BG_PALETTE, 512);
                set_page(pageShown ^ 1);
                shown = 0;
                vcount = 0;
                leaveCriticalSection(old);
                audio_start();
            } else {
                int old = enterCriticalSection();
                flipPal = pal;
                flipFrame = k;
                flipAt = k * n;
                flipPending = 1;
                leaveCriticalSection(old);
            }
            // records whose picture and sound are both used can go
            while (qCount && qHead < k && audioWritten / n > qHead) drop_front();
            if (poll_keys()) restart = 1;
        }
        // let the last frame play for its full time
        while (!restart && (flipPending || (s32)(vcount - hdr.frames * n) < 0)) {
            write_audio();
            swiWaitForVBlank();
            if (poll_keys()) restart = 1;
        }
    }
}
