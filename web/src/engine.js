// VIDEO BOY engine: video frames + audio -> Game Boy / Game Boy Color ROM.
// Port of videoboy.py. Runs in a Web Worker and in Node (no DOM).
const VB = (() => {
  "use strict";
  const CPU_HZ = 4194304, FRAME_CYCLES = 70224, LCD_FPS = CPU_HZ / FRAME_CYCLES, LINES = 154;
  const BANK = 16384, MAX_BANKS = 512, SILENCE = 0x44;
  const DMG_RATE = 9198, GBC_RATE = 18396;
  const BAYER4 = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];
  const MAX_TILES = 191, CHUNKS_PER_LCD_FRAME = 160, RUN_MAX = 127, SLOTS_Z = 152;
  const GBC_SPLIT = 232, GBC_BORDER = 232;
  const LUM = [0.3, 0.59, 0.11];

  // ---------------------------------------------------------------- helpers
  function b64(s) {
    if (typeof atob === "function") { const b = atob(s); const u = new Uint8Array(b.length); for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u; }
    return new Uint8Array(Buffer.from(s, "base64"));
  }
  function rint(x) { // numpy rint: round half to even
    const f = Math.floor(x), d = x - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return (f % 2 === 0) ? f : f + 1;
  }
  function percentile(arr, q) { // numpy default (linear)
    const a = Float64Array.from(arr).sort();
    if (!a.length) return 0;
    const pos = (a.length - 1) * q / 100, lo = Math.floor(pos), hi = Math.ceil(pos);
    return a[lo] + (a[hi] - a[lo]) * (pos - lo);
  }
  class ByteBuf {
    constructor(cap = 1 << 20) { this.a = new Uint8Array(cap); this.n = 0; }
    ensure(k) { if (this.n + k > this.a.length) { let c = this.a.length * 2; while (c < this.n + k) c *= 2; const b = new Uint8Array(c); b.set(this.a.subarray(0, this.n)); this.a = b; } }
    push(x) { this.ensure(1); this.a[this.n++] = x; }
    put(u) { this.ensure(u.length); this.a.set(u, this.n); this.n += u.length; }
    fill(v, k) { this.ensure(k); this.a.fill(v, this.n, this.n + k); this.n += k; }
    bytes() { return this.a.subarray(0, this.n); }
  }

  // ---------------------------------------------------------------- layouts
  function chooseLayoutDMG(aspect, n, limit) {
    if (limit == null) limit = Math.min(MAX_TILES, Math.floor(CHUNKS_PER_LCD_FRAME * n / 2));
    let best = null;
    for (let th = 1; th <= 18; th++) for (let tw = 1; tw <= 20; tw++) {
      if (tw * th > limit) continue;
      const err = Math.abs(Math.log((tw / th) / aspect));
      if (err > 0.12) continue;
      if (!best || tw * th > best.a || (tw * th === best.a && err < best.err)) best = { a: tw * th, err, tw, th };
    }
    return best ? [best.tw, best.th] : [Math.min(20, limit), 1];
  }
  function zMaxTiles(n) { const cap = SLOTS_Z * n; let t = MAX_TILES; while (2 * t + Math.ceil(t / RUN_MAX) + 1 > cap) t--; return t; }
  function gbcCap(n) { return 163 * n - 5; }
  function gbcFullSlots(tw, th) { const t = tw * th; return t + 2 * th + 1 + (t > 0 ? 1 : 0) + (t > GBC_SPLIT ? 1 : 0) + 1; }
  function chooseLayoutGBC(aspect, n) {
    const cap = gbcCap(n);
    let best = null;
    for (let th = 1; th <= 18; th++) for (let tw = 1; tw <= 20; tw++) {
      if (gbcFullSlots(tw, th) > cap) continue;
      const err = Math.abs(Math.log((tw / th) / aspect));
      if (err > 0.12) continue;
      if (!best || tw * th > best.a || (tw * th === best.a && err < best.err)) best = { a: tw * th, err, tw, th };
    }
    return best ? [best.tw, best.th] : [20, 1];
  }
  // fixed-size checks for a user-given size
  function sizeOK(mode, compress, tw, th, n) {
    const T = tw * th;
    if (mode === "gbc") return gbcFullSlots(tw, th) <= gbcCap(n);
    if (compress) return T <= zMaxTiles(n);
    return T <= MAX_TILES && T * 2 <= CHUNKS_PER_LCD_FRAME * n;
  }

  // ---------------------------------------------------------------- audio
  // window sum like np.convolve(x, ones(w)/w, "same")
  function boxSame(x, w) {
    const N = x.length, out = new Float64Array(N), ps = new Float64Array(N + 1);
    for (let i = 0; i < N; i++) ps[i + 1] = ps[i] + x[i];
    const a = Math.floor(w / 2), b = Math.floor((w - 1) / 2);
    for (let i = 0; i < N; i++) {
      const lo = Math.max(0, i - a), hi = Math.min(N - 1, i + b);
      out[i] = (ps[hi + 1] - ps[lo]) / w;
    }
    return out;
  }
  function quantizeChannel(ch, rate, shape, gate = 0.03) {
    const N = ch.length, w = Math.floor(rate / 50), k = Math.floor(rate * 0.06);
    const ab = new Float64Array(N); for (let i = 0; i < N; i++) ab[i] = Math.abs(ch[i]);
    const env = boxSame(ab, w);
    const sil = new Float64Array(N); for (let i = 0; i < N; i++) sil[i] = env[i] < gate ? 1 : 0;
    const glide = boxSame(sil, k);
    const out = new Uint8Array(N);
    let e = 0;
    for (let i = 0; i < N; i++) {
      if (sil[i]) { out[i] = 4; e = 0; continue; }
      const c = Math.max(-1, Math.min(1, ch[i]));
      const v = (3.5 + 0.5 * glide[i] + c * 4.0) - shape * e;
      let q = v > -0.5 ? Math.floor(v + 0.5) : 0;
      if (q > 7) q = 7;
      e = v - q;
      if (e > 1) e = 1; else if (e < -1) e = -1;
      out[i] = q;
    }
    return out;
  }
  // L, R: Float32Array (already filtered, at the target rate)
  function quantizeAudio(L, R, gain, rate, shape = 0.3) {
    const N = L.length;
    if (!N) return new Uint8Array(0);
    const ab = new Float32Array(2 * N);
    for (let i = 0; i < N; i++) { ab[2 * i] = Math.abs(L[i]); ab[2 * i + 1] = Math.abs(R[i]); }
    // 99.5th percentile via sampling for long inputs (exact for short ones)
    let peak;
    if (ab.length <= 4e6) peak = percentile(ab, 99.5);
    else { const s = new Float32Array(4e6); const st = ab.length / 4e6; for (let i = 0; i < 4e6; i++) s[i] = ab[Math.floor(i * st)]; peak = percentile(s, 99.5); }
    if (peak < 1e-4) return new Uint8Array(N).fill(SILENCE);
    const t11 = Math.tanh(1.1);
    const y = (x) => { const o = new Float64Array(N); for (let i = 0; i < N; i++) o[i] = Math.tanh(x[i] / peak * 1.1 * gain) / t11; return o; };
    const l = quantizeChannel(y(L), rate, shape), r = quantizeChannel(y(R), rate, shape);
    const out = new Uint8Array(N);
    for (let i = 0; i < N; i++) out[i] = (l[i] << 4) | r[i];
    return out;
  }
  function audioSlice(audio, k, len) { // audio of packet k (= audio while frame k-1 is shown)
    const a = new Uint8Array(len).fill(SILENCE);
    if (k > 0 && audio) { const s = (k - 1) * len; if (s < audio.length) a.set(audio.subarray(s, Math.min(audio.length, s + len))); }
    return a;
  }

  // ---------------------------------------------------------------- DMG picture
  function dmgIndices(gray, W, H, lo, hi, gamma, dither) {
    const out = new Uint8Array(W * H);
    const useC = hi - lo > 0.05;
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      let f = gray[y * W + x] / 255;
      if (useC) f = Math.min(1, Math.max(0, (f - lo) / (hi - lo)));
      if (gamma !== 1) f = Math.pow(f, gamma);
      let v = f * 3;
      if (dither) v += (BAYER4[(y & 3) * 4 + (x & 3)] + 0.5) / 16 - 0.5;
      const l = Math.min(3, Math.max(0, rint(v)));
      out[y * W + x] = 3 - l;
    }
    return out;
  }
  function tilesFromIdx(idx, tw, th) { // row-major tiles -> 2bpp
    const W = tw * 8, out = new Uint8Array(tw * th * 16);
    let o = 0;
    for (let ty = 0; ty < th; ty++) for (let tx = 0; tx < tw; tx++) for (let r = 0; r < 8; r++) {
      let lo = 0, hi = 0;
      const base = (ty * 8 + r) * W + tx * 8;
      for (let c = 0; c < 8; c++) { const v = idx[base + c]; lo |= (v & 1) << (7 - c); hi |= ((v >> 1) & 1) << (7 - c); }
      out[o++] = lo; out[o++] = hi;
    }
    return out;
  }
  function tilePixels(idx, tw, th) { // (T,64)
    const W = tw * 8, T = tw * th, out = new Uint8Array(T * 64);
    for (let t = 0; t < T; t++) { const tx = t % tw, ty = (t / tw) | 0; for (let r = 0; r < 8; r++) for (let c = 0; c < 8; c++) out[t * 64 + r * 8 + c] = idx[(ty * 8 + r) * W + tx * 8 + c]; }
    return out;
  }

  // ---------------------------------------------------------------- ROM finalize
  function finalize(playerB64, cfg, cfgAddr, maps, mapsAddr, data, nbanks, title, gbc) {
    const p = b64(playerB64);
    const bank0 = new Uint8Array(BANK); bank0.set(p);
    if (!gbc) bank0.fill(191, 0x3000, 0x3800); // DMG default maps
    bank0.set(cfg, cfgAddr); bank0.set(maps, mapsAddr);
    let code = 0; while ((2 << code) < 1 + nbanks) code++;
    const rom = new Uint8Array((2 << code) * BANK).fill(0xFF);
    rom.set(bank0, 0); rom.set(data, BANK);
    const t = String(title || "VIDEOBOY").toUpperCase().replace(/[^\x20-\x5F]/g, "?").slice(0, gbc ? 11 : 15);
    for (let i = 0; i < 16; i++) rom[0x134 + i] = 0;
    if (gbc) for (let i = 0; i < 15; i++) rom[0x134 + i] = 0;
    for (let i = 0; i < t.length; i++) rom[0x134 + i] = t.charCodeAt(i);
    rom[0x143] = gbc ? 0xC0 : 0x00; rom[0x147] = 0x19; rom[0x148] = code; rom[0x149] = 0;
    let c = 0; for (let i = 0x134; i < 0x14D; i++) c = (c - rom[i] - 1) & 0xFF;
    rom[0x14D] = c; rom[0x14E] = rom[0x14F] = 0;
    let g = 0; for (let i = 0; i < rom.length; i++) g = (g + rom[i]) & 0xFFFF;
    rom[0x14E] = g >> 8; rom[0x14F] = g & 0xFF;
    return rom;
  }
  function u16(v) { return [v & 255, (v >> 8) & 255]; }
  function scrollFix(tw, th) { return [(-4 * (tw % 2)) & 255, (-4 * (th % 2)) & 255]; }
  function dmgMaps(tw, th) {
    const x0 = (20 - tw) >> 1, y0 = (18 - th) >> 1, m = new Uint8Array(2048).fill(191);
    for (let i = 0; i < tw * th; i++) { const y = y0 + ((i / tw) | 0), x = x0 + i % tw; m[y * 32 + x] = i; m[1024 + y * 32 + x] = (192 + i) & 255; }
    return m;
  }

  // Bank placement with exact fit (same rules as the players)
  class Placer {
    constructor(maxBanks = MAX_BANKS) { this.buf = new ByteBuf(); this.lastEnd = -1; this.limit = (maxBanks - 1) * BANK; }
    posAfter(pos, len) { return pos + ((pos % BANK) && (pos % BANK) + len > BANK ? BANK - pos % BANK : 0) + len; }
    fits(len, tailLen) { const p = this.posAfter(this.buf.n, len); return this.posAfter(p, tailLen) <= this.limit; }
    add(p) {
      const b = this.buf;
      if (b.n && b.n % BANK === 0) b.a[this.lastEnd] = 0xFF;
      else if (b.n % BANK + p.length > BANK) { b.a[this.lastEnd] = 0xFF; b.fill(0xFF, BANK - b.n % BANK); }
      b.put(p); this.lastEnd = b.n - 1;
    }
    finish() { const nb = Math.ceil(this.buf.n / BANK); this.buf.fill(0xFF, nb * BANK - this.buf.n); return [this.buf.bytes(), nb]; }
  }

  // ---------------------------------------------------------------- DMG normal
  class DmgNormal {
    constructor(o) {
      Object.assign(this, o); // n, tw, th, audio, title, players
      this.T = this.tw * this.th; this.aud = LINES * this.n; this.pkt = this.aud + this.T * 16;
      this.ppb = Math.floor(BANK / this.pkt); this.maxFrames = (MAX_BANKS - 1) * this.ppb - 1;
      this.tiles = []; this.full = false;
    }
    add(idx) {
      if (this.tiles.length >= this.maxFrames) { this.full = true; return null; }
      const t = tilesFromIdx(idx, this.tw, this.th); this.tiles.push(t);
      const k = this.tiles.length - 1, p = new Uint8Array(this.pkt);
      p.set(audioSlice(this.audio, k, this.aud)); p.set(t, this.aud);
      return p;
    }
    finish() {
      const F = this.tiles.length, npk = F + 1, nb = Math.ceil(npk / this.ppb);
      const data = new Uint8Array(nb * BANK);
      for (let k = 0; k < npk; k++) {
        const off = Math.floor(k / this.ppb) * BANK + (k % this.ppb) * this.pkt;
        data.set(audioSlice(this.audio, k, this.aud), off);
        if (k < F) data.set(this.tiles[k], off + this.aud);
      }
      const cfg = [this.n, ...u16(this.aud), ...u16(this.T * 2), ...u16(this.pkt), this.ppb, ...u16(npk), ...scrollFix(this.tw, this.th)];
      return finalize(this.players.dmg, cfg, 0x2F00, dmgMaps(this.tw, this.th), 0x3000, data, nb, this.title, false);
    }
  }

  // ---------------------------------------------------------------- DMG compressed
  function zRuns(changed) {
    const runs = [];
    for (const i of changed) {
      const r = runs[runs.length - 1];
      if (r && r[0] + r[1] === i && r[1] < RUN_MAX) r[1]++; else runs.push([i, 1]);
    }
    return runs;
  }
  class DmgZ {
    constructor(o) {
      Object.assign(this, o); // n, tw, th, audio, title, tol, players
      this.T = this.tw * this.th; this.aud = LINES * this.n; this.cap = SLOTS_Z * this.n;
      this.buf = [null, null]; this.placer = new Placer(); this.k = 0; this.sent = 0; this.full = false;
    }
    add(idx) {
      if (this.full) return null;
      const T = this.T, pix = tilePixels(idx, this.tw, this.th), tiles = tilesFromIdx(idx, this.tw, this.th);
      const b = this.k & 1;
      let changed;
      if (!this.buf[b]) { changed = [...Array(T).keys()]; this.buf[b] = pix.slice(); }
      else {
        changed = [];
        for (let t = 0; t < T; t++) { let d = 0; for (let i = 0; i < 64; i++) if (this.buf[b][t * 64 + i] !== pix[t * 64 + i]) d++; if (d > this.tol) changed.push(t); }
        if (2 * changed.length + zRuns(changed).length + 1 > this.cap) changed = [...Array(T).keys()];
        for (const t of changed) this.buf[b].set(pix.subarray(t * 64, t * 64 + 64), t * 64);
      }
      const p = new ByteBuf(this.aud + T * 16 + 512);
      p.put(audioSlice(this.audio, this.k, this.aud));
      for (const [st, ln] of zRuns(changed)) { const off = st * 16; p.push(ln * 2); p.push(off & 255); p.push(off >> 8); p.put(tiles.subarray(st * 16, (st + ln) * 16)); }
      p.push(0);
      const bytes = p.bytes();
      if (!this.placer.fits(bytes.length, this.aud + 1)) { this.full = true; return null; }
      this.placer.add(bytes); this.sent += changed.length; this.k++;
      return bytes;
    }
    finish() {
      const F = this.k, tail = new Uint8Array(this.aud + 1);
      tail.set(audioSlice(this.audio, F, this.aud)); tail[this.aud] = 0;
      this.placer.add(tail);
      const [data, nb] = this.placer.finish();
      const cfg = [this.n, ...u16(this.aud), ...u16(this.T * 2), 0, 0, 1, ...u16(F + 1), ...scrollFix(this.tw, this.th), 1];
      return finalize(this.players.dmg, cfg, 0x2F00, dmgMaps(this.tw, this.th), 0x3000, data, nb, this.title, false);
    }
  }

  // ---------------------------------------------------------------- GBC
  function gbcTileLoc(t, b) {
    if (b === 0) return t < GBC_SPLIT ? [0, 0x8000 + 16 * t, t] : [1, 0x8000 + 16 * (t - GBC_SPLIT), t - GBC_SPLIT];
    return t < GBC_SPLIT ? [1, 0x8980 + 16 * t, (152 + t) & 255] : [0, 0x9000 + 16 * (t - GBC_SPLIT), t - GBC_SPLIT];
  }
  function gbcMaps(tw, th) {
    const x0 = (20 - tw) >> 1, y0 = (18 - th) >> 1, m = new Uint8Array(4096);
    m.fill(GBC_BORDER, 0, 2048);
    for (let b = 0; b < 2; b++) for (let t = 0; t < tw * th; t++) {
      const [bank, , ix] = gbcTileLoc(t, b), pos = (y0 + ((t / tw) | 0)) * 32 + x0 + t % tw;
      m[b * 1024 + pos] = ix; m[2048 + b * 1024 + pos] = bank << 3;
    }
    return m;
  }
  class GbcQuantizer {
    constructor(tw, th, dither, border, gamma) {
      Object.assign(this, { tw, th, dither, border, gamma });
      this.T = tw * th; this.prev = null; this.prevErr = null; this.count = 0;
      this.thr = new Float64Array(64); for (let y = 0; y < 8; y++) for (let x = 0; x < 8; x++) this.thr[y * 8 + x] = (BAYER4[(y & 3) * 4 + (x & 3)] + 0.5) / 16;
      this.lut = new Float64Array(256); for (let i = 0; i < 256; i++) this.lut[i] = Math.pow(i / 255, gamma) * 31;
      this.X = new Float64Array(this.T * 192);
    }
    tiles(rgba) { // RGBA (W*H*4) -> X (T,64,3)
      const W = this.tw * 8, X = this.X, lut = this.lut;
      for (let t = 0; t < this.T; t++) { const tx = t % this.tw, ty = (t / this.tw) | 0;
        for (let r = 0; r < 8; r++) for (let c = 0; c < 8; c++) { const s = ((ty * 8 + r) * W + tx * 8 + c) * 4, d = (t * 64 + r * 8 + c) * 3; X[d] = lut[rgba[s]]; X[d + 1] = lut[rgba[s + 1]]; X[d + 2] = lut[rgba[s + 2]]; } }
      return X;
    }
    tileErr(X, P) { // (T,8)
      const T = this.T, E = new Float64Array(T * 8);
      for (let t = 0; t < T; t++) for (let p = 0; p < 8; p++) {
        let s = 0; const pb = p * 12;
        for (let i = 0; i < 64; i++) {
          const o = (t * 64 + i) * 3, r = X[o], g = X[o + 1], b = X[o + 2];
          let m = Infinity;
          for (let c = 0; c < 4; c++) { const q = pb + c * 3, dr = r - P[q], dg = g - P[q + 1], db = b - P[q + 2], d = dr * dr + dg * dg + db * db; if (d < m) m = d; }
          s += m;
        }
        E[t * 8 + p] = s;
      }
      return E;
    }
    assign(X, P) {
      const E = this.tileErr(X, P), a = new Uint8Array(this.T); let tot = 0;
      for (let t = 0; t < this.T; t++) { let bp = 0, bv = E[t * 8]; for (let p = 1; p < 8; p++) if (E[t * 8 + p] < bv) { bv = E[t * 8 + p]; bp = p; } a[t] = bp; tot += bv; }
      return [a, tot, E];
    }
    fix(P) { const Q = new Float64Array(96); for (let i = 0; i < 96; i++) Q[i] = Math.min(31, Math.max(0, rint(P[i]))); if (this.border) Q[0] = Q[1] = Q[2] = 0; return Q; }
    members(X, a, p) { const idx = []; for (let t = 0; t < this.T; t++) if (a[t] === p) idx.push(t); return idx; }
    refine(X, P, a, iters = 2) {
      P = Float64Array.from(P);
      for (let p = 0; p < 8; p++) {
        const mem = this.members(X, a, p); if (!mem.length) continue;
        const n = mem.length * 64, lab = new Uint8Array(n);
        for (let it = 0; it < iters; it++) {
          const sum = new Float64Array(12), cnt = new Float64Array(4);
          let j = 0;
          for (const t of mem) for (let i = 0; i < 64; i++, j++) {
            const o = (t * 64 + i) * 3, r = X[o], g = X[o + 1], b = X[o + 2];
            let bc = 0, bd = Infinity;
            for (let c = 0; c < 4; c++) { const q = p * 12 + c * 3, d = (r - P[q]) ** 2 + (g - P[q + 1]) ** 2 + (b - P[q + 2]) ** 2; if (d < bd) { bd = d; bc = c; } }
            lab[j] = bc; sum[bc * 3] += r; sum[bc * 3 + 1] += g; sum[bc * 3 + 2] += b; cnt[bc]++;
          }
          for (let c = 0; c < 4; c++) if (cnt[c]) { P[p * 12 + c * 3] = sum[c * 3] / cnt[c]; P[p * 12 + c * 3 + 1] = sum[c * 3 + 1] / cnt[c]; P[p * 12 + c * 3 + 2] = sum[c * 3 + 2] / cnt[c]; }
        }
      }
      return P;
    }
    pixLum(X, t, i) { const o = (t * 64 + i) * 3; return X[o] * LUM[0] + X[o + 1] * LUM[1] + X[o + 2] * LUM[2]; }
    quantilesInto(P, p, X, tilesList) {
      const px = []; for (const t of tilesList) for (let i = 0; i < 64; i++) px.push([this.pixLum(X, t, i), t, i]);
      px.sort((u, v) => u[0] - v[0]);
      for (let c = 0; c < 4; c++) { const e = px[Math.min(px.length - 1, Math.floor((c + 0.5) / 4 * px.length))], o = (e[1] * 64 + e[2]) * 3; P[p * 12 + c * 3] = X[o]; P[p * 12 + c * 3 + 1] = X[o + 1]; P[p * 12 + c * 3 + 2] = X[o + 2]; }
    }
    cold(X) {
      const T = this.T, means = new Float64Array(T * 3);
      for (let t = 0; t < T; t++) for (let i = 0; i < 64; i++) for (let k = 0; k < 3; k++) means[t * 3 + k] += X[(t * 64 + i) * 3 + k] / 64;
      const lum = Array.from({ length: T }, (_, t) => means[t * 3] * LUM[0] + means[t * 3 + 1] * LUM[1] + means[t * 3 + 2] * LUM[2]);
      const order = [...Array(T).keys()].sort((u, v) => lum[u] - lum[v] || u - v);
      const cent = new Float64Array(24);
      for (let k = 0; k < 8; k++) { const t = order[Math.floor(k * (T - 1) / 7)]; cent.set(means.subarray(t * 3, t * 3 + 3), k * 3); }
      let lab = new Uint8Array(T);
      for (let it = 0; it < 6; it++) {
        for (let t = 0; t < T; t++) { let bk = 0, bd = Infinity; for (let k = 0; k < 8; k++) { const d = (means[t * 3] - cent[k * 3]) ** 2 + (means[t * 3 + 1] - cent[k * 3 + 1]) ** 2 + (means[t * 3 + 2] - cent[k * 3 + 2]) ** 2; if (d < bd) { bd = d; bk = k; } } lab[t] = bk; }
        for (let k = 0; k < 8; k++) { let s0 = 0, s1 = 0, s2 = 0, c = 0; for (let t = 0; t < T; t++) if (lab[t] === k) { s0 += means[t * 3]; s1 += means[t * 3 + 1]; s2 += means[t * 3 + 2]; c++; } if (c) { cent[k * 3] = s0 / c; cent[k * 3 + 1] = s1 / c; cent[k * 3 + 2] = s2 / c; } }
      }
      for (let t = 0; t < T; t++) { let bk = 0, bd = Infinity; for (let k = 0; k < 8; k++) { const d = (means[t * 3] - cent[k * 3]) ** 2 + (means[t * 3 + 1] - cent[k * 3 + 1]) ** 2 + (means[t * 3 + 2] - cent[k * 3 + 2]) ** 2; if (d < bd) { bd = d; bk = k; } } lab[t] = bk; }
      let P = new Float64Array(96);
      for (let k = 0; k < 8; k++) { const mem = this.members(X, lab, k); this.quantilesInto(P, k, X, mem.length ? mem : [order[Math.floor(k * T / 8)]]); }
      let [a] = this.assign(X, P);
      for (let r = 0; r < 3; r++) { P = this.refine(X, P, a, 2); [a] = this.assign(X, P); }
      return P;
    }
    reseedEmpty(X, P, a) {
      const used = new Array(8).fill(0); for (let t = 0; t < this.T; t++) used[a[t]]++;
      if (Math.min(...used) > 0) return P;
      const E = this.tileErr(X, P), d = Array.from({ length: this.T }, (_, t) => E[t * 8 + a[t]]);
      const worst = [...Array(this.T).keys()].sort((u, v) => d[v] - d[u]);
      let j = 0;
      for (let p = 0; p < 8; p++) if (!used[p]) { const t = worst[j++ % worst.length]; this.quantilesInto(P, p, X, [t]); }
      return P;
    }
    quantize(rgba) {
      const X = this.tiles(rgba), cands = [];
      let scene = true;
      if (this.prev) {
        let [a, e] = this.assign(X, this.prev);
        cands.push([e * 0.92, this.prev]);
        let Pw = Float64Array.from(this.prev);
        for (let i = 0; i < 2; i++) { Pw = this.reseedEmpty(X, Pw, a); Pw = this.fix(this.refine(X, Pw, a, 2)); [a, e] = this.assign(X, Pw); }
        cands.push([e, Pw]);
        scene = this.prevErr != null && e > 1.6 * this.prevErr + 1;
      }
      if (scene || this.count % 48 === 0) { const Pc = this.fix(this.cold(X)); const [, e] = this.assign(X, Pc); cands.push([e, Pc]); }
      let best = cands[0]; for (const c of cands) if (c[0] < best[0]) best = c;
      const P = this.fix(best[1]);
      const [a, err] = this.assign(X, P);
      this.prev = P; this.prevErr = err; this.count++;
      // colour index per pixel: ordered dithering between the two closest colours
      const T = this.T, idx = new Uint8Array(T * 64);
      for (let t = 0; t < T; t++) { const pb = a[t] * 12;
        for (let i = 0; i < 64; i++) {
          const o = (t * 64 + i) * 3, r = X[o], g = X[o + 1], bl = X[o + 2];
          let c1 = -1, d1 = Infinity, c2 = -1, d2 = Infinity;
          for (let c = 0; c < 4; c++) { const q = pb + c * 3, d = (r - P[q]) ** 2 + (g - P[q + 1]) ** 2 + (bl - P[q + 2]) ** 2;
            if (d < d1) { c2 = c1; d2 = d1; c1 = c; d1 = d; } else if (d < d2) { c2 = c; d2 = d; } }
          if (!this.dither) { idx[t * 64 + i] = c1; continue; }
          const A = pb + c1 * 3, B = pb + c2 * 3, v0 = P[B] - P[A], v1 = P[B + 1] - P[A + 1], v2 = P[B + 2] - P[A + 2];
          const tt = ((r - P[A]) * v0 + (g - P[A + 1]) * v1 + (bl - P[A + 2]) * v2) / Math.max(v0 * v0 + v1 * v1 + v2 * v2, 1e-9);
          idx[t * 64 + i] = tt > this.thr[i] ? c2 : c1;
        } }
      const Pi = new Uint8Array(96); for (let i = 0; i < 96; i++) Pi[i] = P[i];
      return [Pi, a, idx];
    }
  }
  function gbcTileBytes(idx, T) { // (T,64) -> (T,16)
    const out = new Uint8Array(T * 16);
    for (let t = 0; t < T; t++) for (let r = 0; r < 8; r++) { let lo = 0, hi = 0; for (let c = 0; c < 8; c++) { const v = idx[t * 64 + r * 8 + c]; lo |= (v & 1) << (7 - c); hi |= ((v >> 1) & 1) << (7 - c); } out[t * 16 + r * 2] = lo; out[t * 16 + r * 2 + 1] = hi; }
    return out;
  }
  function gbcPalBytes(P) { const o = new Uint8Array(64); for (let i = 0; i < 32; i++) { const v = P[i * 3] | (P[i * 3 + 1] << 5) | (P[i * 3 + 2] << 10); o[i * 2] = v & 255; o[i * 2 + 1] = v >> 8; } return o; }
  class Gbc {
    constructor(o) {
      Object.assign(this, o); // n, tw, th, audio, title, compress, tol, dither, gamma, players
      this.T = this.tw * this.th; this.aud = 2 * LINES * this.n; this.cap = gbcCap(this.n);
      this.x0 = (20 - this.tw) >> 1; this.y0 = (18 - this.th) >> 1;
      this.q = new GbcQuantizer(this.tw, this.th, this.dither, this.tw < 20 || this.th < 18, this.gamma);
      this.bufPal = [null, null]; this.bufIdx = [null, null];
      this.locs = [0, 1].map(b => Array.from({ length: this.T }, (_, t) => gbcTileLoc(t, b)));
      this.placer = new Placer(); this.k = 0; this.sent = 0; this.full = false; this.lastP = null;
    }
    attrChunks(b, pal, rows) {
      const base = b === 0 ? 0x9800 : 0x9C00, out = [];
      for (const r of rows) {
        const row = new Uint8Array(32);
        for (let tx = 0; tx < this.tw; tx++) { const t = r * this.tw + tx; row[this.x0 + tx] = (this.locs[b][t][0] << 3) | pal[t]; }
        const addr = base + (this.y0 + r) * 32;
        out.push([1, addr, row.subarray(0, 16)], [1, addr + 16, row.subarray(16)]);
      }
      return out;
    }
    static runs(chunks) {
      const s = chunks.slice().sort((u, v) => u[0] - v[0] || u[1] - v[1]), runs = [];
      for (const [bank, addr, data] of s) {
        const r = runs[runs.length - 1];
        if (r && r.bank === bank && r.addr + r.len === addr && r.len < 254 * 16) { r.parts.push(data); r.len += 16; }
        else runs.push({ bank, addr, len: 16, parts: [data] });
      }
      return runs;
    }
    add(rgba) {
      if (this.full) return null;
      const [P, pal, idx] = this.q.quantize(rgba);
      const tiles = gbcTileBytes(idx, this.T), T = this.T, b = this.k & 1;
      let full = !this.compress || !this.bufIdx[b], chunks, npal, nidx, nchanged;
      if (!full) {
        const bp = this.bufPal[b], bi = this.bufIdx[b], thr = this.tol === 0 ? 0 : 2, changed = [];
        for (let t = 0; t < T; t++) {
          let cnt = 0;
          for (let i = 0; i < 64; i++) {
            const o = (bp[t] * 4 + bi[t * 64 + i]) * 3, n = (pal[t] * 4 + idx[t * 64 + i]) * 3;
            const d = Math.max(Math.abs(P[o] - P[n]), Math.abs(P[o + 1] - P[n + 1]), Math.abs(P[o + 2] - P[n + 2]));
            if (d > thr) cnt++;
          }
          if (cnt > this.tol) changed.push(t);
        }
        npal = bp.slice(); nidx = bi.slice();
        for (const t of changed) { npal[t] = pal[t]; nidx.set(idx.subarray(t * 64, t * 64 + 64), t * 64); }
        const rows = [...new Set(changed.map(t => (t / this.tw) | 0))].sort((u, v) => u - v);
        const oa = this.attrChunks(b, bp, rows), na = this.attrChunks(b, npal, rows);
        const att = na.filter((c, i) => { const o = oa[i][2]; for (let j = 0; j < 16; j++) if (o[j] !== c[2][j]) return true; return false; });
        chunks = changed.map(t => [this.locs[b][t][0], this.locs[b][t][1], tiles.subarray(t * 16, t * 16 + 16)]).concat(att);
        nchanged = changed.length;
        if (chunks.length + Gbc.runs(chunks).length + 1 > this.cap) full = true;
      }
      if (full) {
        npal = pal.slice(); nidx = idx.slice(); nchanged = T;
        chunks = Array.from({ length: T }, (_, t) => [this.locs[b][t][0], this.locs[b][t][1], tiles.subarray(t * 16, t * 16 + 16)])
          .concat(this.attrChunks(b, npal, [...Array(this.th).keys()]));
      }
      const p = new ByteBuf(this.aud + 64 + chunks.length * 16 + 64);
      p.put(audioSlice(this.audio, this.k, this.aud)); p.put(gbcPalBytes(P));
      for (const r of Gbc.runs(chunks)) { p.push(r.len / 16); p.push(r.addr & 255); p.push((r.addr >> 8) | (r.bank << 6)); for (const d of r.parts) p.put(d); }
      p.push(0);
      const bytes = p.bytes();
      if (!this.placer.fits(bytes.length, this.aud + 65)) { this.full = true; return null; }
      this.placer.add(bytes);
      this.bufPal[b] = npal; this.bufIdx[b] = nidx; this.sent += nchanged; this.k++; this.lastP = P;
      return bytes;
    }
    finish() {
      const F = this.k, tail = new Uint8Array(this.aud + 65);
      tail.set(audioSlice(this.audio, F, this.aud)); tail.set(gbcPalBytes(this.lastP || new Uint8Array(96)), this.aud); tail[this.aud + 64] = 0;
      this.placer.add(tail);
      const [data, nb] = this.placer.finish();
      const cfg = [this.n, ...u16(this.aud), ...scrollFix(this.tw, this.th), ...u16(F + 1)];
      return finalize(this.players.gbc, cfg, 0x2F00, gbcMaps(this.tw, this.th), 0x3000, data, nb, this.title, true);
    }
  }

  // ---------------------------------------------------------------- ROM reader (preview playback)
  // Decodes a finished ROM exactly like the player does: returns packets with audio + a render function.
  class RomReader {
    constructor(rom) {
      this.rom = rom;
      this.gbc = rom[0x143] === 0xC0;
      const c = rom.subarray(0x2F00, 0x2F10);
      if (this.gbc) {
        this.n = c[0]; this.aud = c[1] | c[2] << 8; this.scx = c[3]; this.scy = c[4]; this.npk = c[5] | c[6] << 8; this.mode = "gbc";
      } else {
        this.n = c[0]; this.aud = c[1] | c[2] << 8; this.T = (c[3] | c[4] << 8) / 2; this.pkt = c[5] | c[6] << 8; this.ppb = c[7];
        this.npk = c[8] | c[9] << 8; this.scx = c[10]; this.scy = c[11]; this.mode = c[12] ? "z" : "normal";
        const m = rom.subarray(0x3000, 0x3400); let x0 = 32, y0 = 32, x1 = -1, y1 = -1;
        for (let y = 0; y < 32; y++) for (let x = 0; x < 32; x++) if (m[y * 32 + x] !== 191) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
        this.tw = x1 - x0 + 1; this.th = y1 - y0 + 1; this.mx = x0; this.my = y0;
      }
      this.rate = this.gbc ? GBC_RATE : DMG_RATE;
      this.offsets = []; // packet start (file offset)
      let bank = 1, ptr = 0x4000;
      for (let k = 0; k < this.npk; k++) {
        if (this.mode === "normal") { this.offsets.push(BANK * (1 + Math.floor(k / this.ppb)) + (k % this.ppb) * this.pkt); continue; }
        const base = bank * BANK + (ptr - 0x4000); this.offsets.push(base);
        let p = base + this.aud + (this.gbc ? 64 : 0);
        for (;;) {
          const cnt = rom[p++];
          if (cnt === 0) { ptr = 0x4000 + (p - bank * BANK); break; }
          if (cnt === 0xFF) { bank++; ptr = 0x4000; break; }
          p += 2 + cnt * (this.gbc ? 16 : 8);
        }
        if (ptr >= 0x8000) { bank++; ptr = 0x4000; }
      }
      this.frames = this.npk - 1;
      this.reset();
    }
    audio() { // all audio bytes in play order
      const out = new Uint8Array(this.npk * this.aud);
      for (let k = 0; k < this.npk; k++) out.set(this.rom.subarray(this.offsets[k], this.offsets[k] + this.aud), k * this.aud);
      return out;
    }
    reset() {
      this.k = 0;
      if (this.gbc) { this.vram = [new Uint8Array(0x2000), new Uint8Array(0x2000)]; this.vram[0].set(this.rom.subarray(0x3000, 0x3800), 0x1800); this.vram[1].set(this.rom.subarray(0x3800, 0x4000), 0x1800); }
      else this.buf = [new Uint8Array(this.T * 16), new Uint8Array(this.T * 16)];
    }
    // apply packet k (must be sequential), returns nothing
    step() {
      const k = this.k, rom = this.rom; let p = this.offsets[k] + this.aud;
      if (this.mode === "normal") this.buf[k & 1].set(rom.subarray(p, p + this.T * 16));
      else if (this.mode === "z") {
        const b = this.buf[k & 1];
        for (;;) { const cnt = rom[p++]; if (cnt === 0 || cnt === 0xFF) break; const off = rom[p] | rom[p + 1] << 8; p += 2; b.set(rom.subarray(p, p + cnt * 8), off); p += cnt * 8; }
      } else {
        this.pal = rom.subarray(p, p + 64); p += 64;
        for (;;) { const cnt = rom[p++]; if (cnt === 0 || cnt === 0xFF) break; const lo = rom[p], hi = rom[p + 1]; p += 2;
          const vb = (hi >> 6) & 1, addr = ((hi & 0xBF) << 8 | lo) - 0x8000; this.vram[vb].set(rom.subarray(p, p + cnt * 16), addr); p += cnt * 16; }
        this.pals = this.pals || []; this.pals[k & 1] = this.pal;
      }
      this.k++;
    }
    seek(k) { if (k < this.k - 1) this.reset(); while (this.k <= k) this.step(); }
    // render frame k (after seek(k)) into RGBA 160x144 using a 4-colour DMG palette (array of [r,g,b])
    render(k, out, dmgPal) {
      if (this.gbc) return this.renderGbc(k, out);
      const b = this.buf[k & 1], W = 160, tw = this.tw, th = this.th;
      const sx = this.scx >= 128 ? this.scx - 256 : this.scx, sy = this.scy >= 128 ? this.scy - 256 : this.scy;
      const ox = this.mx * 8 - sx, oy = this.my * 8 - sy;
      for (let i = 0; i < 160 * 144; i++) { const c = dmgPal[3]; out[i * 4] = c[0]; out[i * 4 + 1] = c[1]; out[i * 4 + 2] = c[2]; out[i * 4 + 3] = 255; }
      for (let t = 0; t < tw * th; t++) { const tx = t % tw, ty = (t / tw) | 0;
        for (let r = 0; r < 8; r++) { const lo = b[t * 16 + r * 2], hi = b[t * 16 + r * 2 + 1], y = oy + ty * 8 + r; if (y < 0 || y >= 144) continue;
          for (let c = 0; c < 8; c++) { const x = ox + tx * 8 + c; if (x < 0 || x >= 160) continue; const v = ((lo >> (7 - c)) & 1) | (((hi >> (7 - c)) & 1) << 1), col = dmgPal[v], o = (y * W + x) * 4; out[o] = col[0]; out[o + 1] = col[1]; out[o + 2] = col[2]; } } }
    }
    renderGbc(k, out) {
      const buf = k & 1, v0 = this.vram[0], v1 = this.vram[1], mb = buf === 0 ? 0x1800 : 0x1C00, pal = this.pals[buf];
      const col = new Uint8Array(96); for (let i = 0; i < 32; i++) { const v = pal[i * 2] | pal[i * 2 + 1] << 8; col[i * 3] = (v & 31) * 255 / 31 | 0; col[i * 3 + 1] = ((v >> 5) & 31) * 255 / 31 | 0; col[i * 3 + 2] = ((v >> 10) & 31) * 255 / 31 | 0; }
      for (let y = 0; y < 144; y++) { const yy = (y + this.scy) & 255, ty = yy >> 3, py = yy & 7;
        for (let x = 0; x < 160; x++) { const xx = (x + this.scx) & 255, tx = xx >> 3, px = xx & 7;
          const ix = v0[mb + ty * 32 + tx], at = v1[mb + ty * 32 + tx], bk = (at >> 3) & 1, pl = at & 7;
          const addr = buf === 0 ? ix * 16 : (ix < 128 ? 0x1000 + ix * 16 : 0x0800 + (ix - 128) * 16);
          const vr = bk ? v1 : v0, lo = vr[addr + py * 2], hi = vr[addr + py * 2 + 1];
          const c = ((lo >> (7 - px)) & 1) | (((hi >> (7 - px)) & 1) << 1), q = (pl * 4 + c) * 3, o = (y * 160 + x) * 4;
          out[o] = col[q]; out[o + 1] = col[q + 1]; out[o + 2] = col[q + 2]; out[o + 3] = 255; } }
    }
  }

  // ---------------------------------------------------------------- planning
  // Estimated bytes per video frame (for the length estimate in the UI)
  function frameBytes(mode, compress, tw, th, n) {
    const T = tw * th;
    if (mode === "gbc") { const full = 2 * LINES * n + 64 + T * 16 + 2 * th * 16 + 13; return compress ? 2 * LINES * n + 64 + (T * 16 + 2 * th * 16) * 0.5 + 40 : full; }
    if (compress) return LINES * n + T * 16 * 0.45 + 30;
    return LINES * n + T * 16;
  }
  function maxSeconds(mode, compress, tw, th, n) {
    const fb = frameBytes(mode, compress, tw, th, n);
    const frames = compress ? (MAX_BANKS - 1) * BANK * 0.985 / fb : (MAX_BANKS - 1) * Math.floor(BANK / fb) - 1;
    return Math.max(0, frames) * n / LCD_FPS;
  }

  return { CPU_HZ, FRAME_CYCLES, LCD_FPS, LINES, BANK, MAX_BANKS, DMG_RATE, GBC_RATE, SILENCE,
    chooseLayoutDMG, chooseLayoutGBC, zMaxTiles, gbcCap, gbcFullSlots, sizeOK, maxSeconds, frameBytes,
    quantizeAudio, dmgIndices, percentile, DmgNormal, DmgZ, Gbc, RomReader, tilesFromIdx };
})();
if (typeof module !== "undefined") module.exports = VB;
