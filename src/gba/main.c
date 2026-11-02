// VIDEO BOY ADVANCE - video player for the Game Boy Advance (also Game Boy Micro,
// Nintendo DS / DS Lite in GBA mode, Nintendo 3DS via open_agb_firm).
//
// Picture: mode 4 (240x160, 256 colours), two pages switched in VBlank, optional
// 2x hardware scaling through the BG2 affine registers.
// Stream: packets of [palette 512][runs ...][end]; run = [row][x/8][count][0] + count*8 bytes.
// Sound: 8-bit stereo PCM at 18157 Hz (304 samples per LCD frame) streamed by DMA1/DMA2
// straight from ROM into Direct Sound A (left) and B (right), timer 0 as sample clock.
// Controls: START = pause, SELECT = restart.

typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned int u32;
typedef signed int s32;
typedef volatile u16 vu16;
typedef volatile u32 vu32;

#define REG16(a) (*(vu16*)(a))
#define REG32(a) (*(vu32*)(a))
#define DISPCNT   REG16(0x04000000)
#define DISPSTAT  REG16(0x04000004)
#define BG2PA     REG16(0x04000020)
#define BG2PB     REG16(0x04000022)
#define BG2PC     REG16(0x04000024)
#define BG2PD     REG16(0x04000026)
#define BG2X      REG32(0x04000028)
#define BG2Y      REG32(0x0400002C)
#define SOUNDCNT_L REG16(0x04000080)
#define SOUNDCNT_H REG16(0x04000082)
#define SOUNDCNT_X REG16(0x04000084)
#define SOUNDBIAS REG16(0x04000088)
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
#define BIOS_IF   (*(vu16*)0x03007FF8)
#define IRQ_VEC   (*(vu32*)0x03007FFC)
#define PALETTE   ((vu16*)0x05000000)
#define PAGE0     0x06000000
#define PAGE1     0x0600A000

#define KEY_SELECT 4
#define KEY_START  8

// Filled in by the converter (fixed address 0x080000C0)
struct Config {
	u32 magic;        // 'VBA1'
	u32 n;            // LCD frames per video frame
	u32 frames;       // number of video frames (packets)
	u32 w, h;         // stored picture size
	u32 scale;        // 1 or 2
	u32 video;        // offset of the first packet in ROM
	u32 audio_l;      // offset of the left audio track
	u32 audio_r;      // offset of the right audio track
	u32 audio_len;    // bytes per track
	u32 reserved[6];
};
__attribute__((section(".cfg"), used)) const struct Config cfg_default = { 0x31414256, 3, 0, 240, 160, 1, 0, 0, 0, 0, {0} };
// read through a volatile pointer: the converter patches these bytes after compiling
#define cfg (*(const volatile struct Config*) 0x080000C0)

static volatile u32 vbl;

__attribute__((target("arm"), noinline)) void irq_handler(void) {
	u16 f = IF;
	if (f & 1) vbl++;
	BIOS_IF |= f;
	IF = f;
}

static inline void vblank_wait(void) { __asm__ volatile("swi 0x05" ::: "r0", "r1", "r2", "r3", "memory"); }

static void dma3_copy32(const void* src, void* dst, u32 words) {
	DMA3SAD = (u32) src; DMA3DAD = (u32) dst; DMA3CNT = words | 0x84000000;
}
static void dma3_fill32(u32 value, void* dst, u32 words) {
	static volatile u32 v;
	v = value;
	DMA3SAD = (u32) &v; DMA3DAD = (u32) dst; DMA3CNT = words | 0x85000000;
}

static void audio_stop(void) {
	TM0CNT_H = 0;
	DMA1CNT = 0; DMA2CNT = 0;
	SOUNDCNT_H = 0x0004 | 0x0008 | 0x0200 | 0x1000 | 0x0800 | 0x8000; // 100 %, A left, B right, reset FIFOs
}
static void audio_start(void) {
	const u8* rom = (const u8*) 0x08000000;
	DMA1SAD = (u32) (rom + cfg.audio_l); DMA1DAD = 0x040000A0; DMA1CNT = 0xB6400000;
	DMA2SAD = (u32) (rom + cfg.audio_r); DMA2DAD = 0x040000A4; DMA2CNT = 0xB6400000;
	TM0CNT_L = 65536 - 924;   // 16777216 / 924 = 18157 Hz = 304 samples per frame
	TM0CNT_H = 0x0080;
}

// Decode one packet into a page, returns pointer to the next packet
static const u8* decode(const u8* p, u32 page, const u16** pal) {
	*pal = (const u16*) p;
	p += 512;
	for (;;) {
		u32 row = p[0];
		if (row == 0xFF) return p + 4;
		u32 x8 = p[1], count = p[2];
		p += 4;
		dma3_copy32(p, (void*) (page + row * 240 + x8 * 8), count * 2);
		p += count * 8;
	}
}

static u16 keys_prev;
static u16 keys_new(void) {
	u16 k = ~KEYINPUT & 0x03FF;
	u16 n = k & ~keys_prev;
	keys_prev = k;
	return n;
}

int main(void) {
	WAITCNT = 0x4317;                       // fast ROM access + prefetch
	IRQ_VEC = (u32) irq_handler;
	DISPSTAT = 0x0008;                      // VBlank IRQ
	IE = 0x0001;
	IME = 1;

	DISPCNT = 0x0080;                       // forced blank while clearing
	dma3_fill32(0, (void*) PAGE0, 0x14000 / 4);
	dma3_fill32(0, (void*) PALETTE, 256 / 2);

	// placement and scaling of the picture through BG2 affine parameters
	s32 s = cfg.scale ? cfg.scale : 1;
	s32 ox = (240 - (s32) cfg.w * s) / 2, oy = (160 - (s32) cfg.h * s) / 2;
	BG2PA = 256 / s; BG2PB = 0; BG2PC = 0; BG2PD = 256 / s;
	BG2X = (u32) (-ox * (256 / s)); BG2Y = (u32) (-oy * (256 / s));

	SOUNDCNT_X = 0x0080;                    // sound on
	SOUNDCNT_L = 0;
	SOUNDBIAS = 0x0200;
	keys_prev = ~KEYINPUT & 0x03FF;

	const u8* rom = (const u8*) 0x08000000;
	for (;;) {
		audio_stop();
		DISPCNT = 0x0404 | 0x0010;          // mode 4, BG2, show page 1 (black) first
		vblank_wait();
		u32 v0 = vbl;
		audio_start();
		const u8* pk = rom + cfg.video;
		const u16* pal = 0;
		int restart = 0;
		for (u32 p = 0; p <= cfg.frames && !restart; ++p) {
			const u16* next_pal = 0;
			if (p < cfg.frames) pk = decode(pk, (p & 1) ? PAGE1 : PAGE0, &next_pal);
			u32 target = v0 + (p + 1) * cfg.n;
			while ((s32) (vbl - target) < 0) vblank_wait();
			// VBlank of the next period: show the frame decoded above
			if (p < cfg.frames) {
				pal = next_pal;
				dma3_copy32(pal, (void*) PALETTE, 128);
				DISPCNT = 0x0404 | ((p & 1) ? 0x0010 : 0);
			}
			u16 k = keys_new();
			if (k & KEY_SELECT) restart = 1;
			else if (k & KEY_START) {
				TM0CNT_H = 0;                // pause: sample clock stops, DMA position is kept
				u32 t0 = vbl;
				for (;;) { vblank_wait(); if (keys_new() & KEY_START) break; }
				vblank_wait();
				v0 += vbl - t0;
				TM0CNT_H = 0x0080;
			}
		}
	}
	return 0;
}
