  // QR codes, drawn here: the box may have no route out, and no library is
  // fetched or vendored (HANDOVER: no external assets). Byte mode, any
  // version and error correction level; ISO/IEC 18004 as Project Nayuki's
  // "QR Code generator" lays it out, written afresh. Used for the Larix
  // Broadcaster link on the Stream (OBS) source (picture.js); test_qr.py
  // holds it to reference matrices made by segno.
  //   QR.encode(text, { ecl: "M", mask: -1 }) -> { size, version, mask, modules: [[bool]] }
  //   QR.svg(text) -> an <svg> string with the four-module quiet zone
  const QR = (() => {
    const ECL = { L: 1, M: 0, Q: 3, H: 2 };   // the format bits of each level
    const ECC_PER_BLOCK = {
      L: [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28, 28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
      M: [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28],
      Q: [-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30, 28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
      H: [-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28, 30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30],
    };
    const BLOCKS = {
      L: [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8, 8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25],
      M: [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49],
      Q: [-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20, 23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68],
      H: [-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25, 25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81],
    };
    const bit = (x, i) => ((x >>> i) & 1) !== 0;
    // Modules left for data and error correction once the patterns are drawn.
    function rawModules(ver) {
      let n = (16 * ver + 128) * ver + 64;
      if (ver >= 2) {
        const align = Math.floor(ver / 7) + 2;
        n -= (25 * align - 10) * align - 55;
        if (ver >= 7) n -= 36;
      }
      return n;
    }
    const dataCodewords = (ver, ecl) => Math.floor(rawModules(ver) / 8) - ECC_PER_BLOCK[ecl][ver] * BLOCKS[ecl][ver];

    // Reed-Solomon over GF(2^8), modulus 0x11D.
    function gfMul(x, y) {
      let z = 0;
      for (let i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11d); z ^= ((y >>> i) & 1) * x; }
      return z & 0xff;
    }
    function rsDivisor(degree) {
      const r = new Array(degree).fill(0);
      r[degree - 1] = 1;
      let root = 1;
      for (let i = 0; i < degree; i++) {
        for (let j = 0; j < r.length; j++) { r[j] = gfMul(r[j], root); if (j + 1 < r.length) r[j] ^= r[j + 1]; }
        root = gfMul(root, 0x02);
      }
      return r;
    }
    function rsRemainder(data, divisor) {
      const r = divisor.map(() => 0);
      for (const b of data) {
        const f = b ^ r.shift();
        r.push(0);
        divisor.forEach((c, i) => { r[i] ^= gfMul(c, f); });
      }
      return r;
    }

    function encode(text, opts = {}) {
      const ecl = opts.ecl || "M", bytes = [...new TextEncoder().encode(text)];
      let ver = opts.version || 0;
      const bitsFor = (v) => 4 + (v <= 9 ? 8 : 16) + bytes.length * 8;
      if (!ver) {
        for (ver = 1; ver <= 40 && bitsFor(ver) > dataCodewords(ver, ecl) * 8; ver++);
        if (ver > 40) throw new RangeError("too long for a QR code");
      } else if (bitsFor(ver) > dataCodewords(ver, ecl) * 8) throw new RangeError("too long for that version");
      // The bit stream: byte mode, the count, the bytes, then padding.
      const bits = [];
      const put = (v, n) => { for (let i = n - 1; i >= 0; i--) bits.push((v >>> i) & 1); };
      put(4, 4);
      put(bytes.length, ver <= 9 ? 8 : 16);
      for (const b of bytes) put(b, 8);
      const cap = dataCodewords(ver, ecl) * 8;
      put(0, Math.min(4, cap - bits.length));
      put(0, (8 - bits.length % 8) % 8);
      for (let pad = 0xec; bits.length < cap; pad ^= 0xec ^ 0x11) put(pad, 8);
      const data = [];
      for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));
      // Error correction per block, then the blocks interleaved.
      const nb = BLOCKS[ecl][ver], eccLen = ECC_PER_BLOCK[ecl][ver], raw = Math.floor(rawModules(ver) / 8);
      const nShort = nb - raw % nb, shortLen = Math.floor(raw / nb), div = rsDivisor(eccLen), blocks = [];
      for (let i = 0, k = 0; i < nb; i++) {
        const dat = data.slice(k, k + shortLen - eccLen + (i < nShort ? 0 : 1));
        k += dat.length;
        const ecc = rsRemainder(dat, div);
        if (i < nShort) dat.push(0);
        blocks.push(dat.concat(ecc));
      }
      const words = [];
      for (let i = 0; i < blocks[0].length; i++)
        for (let j = 0; j < nb; j++) if (i !== shortLen - eccLen || j >= nShort) words.push(blocks[j][i]);

      // The patterns, then the data, then the mask.
      const size = ver * 4 + 17;
      const mod = Array.from({ length: size }, () => new Array(size).fill(false));
      const fn = Array.from({ length: size }, () => new Array(size).fill(false));
      const set = (x, y, dark) => { mod[y][x] = dark; fn[y][x] = true; };
      for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
      for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]])
        for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
          const d = Math.max(Math.abs(dx), Math.abs(dy)), x = cx + dx, y = cy + dy;
          if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, d !== 2 && d !== 4);
        }
      const align = [];
      if (ver > 1) {
        const n = Math.floor(ver / 7) + 2, step = ver === 32 ? 26 : Math.ceil((ver * 4 + 4) / (n * 2 - 2)) * 2;
        align.push(6);
        for (let p = size - 7; align.length < n; p -= step) align.splice(1, 0, p);
      }
      align.forEach((ax, i) => align.forEach((ay, j) => {
        if ((i === 0 && j === 0) || (i === 0 && j === align.length - 1) || (i === align.length - 1 && j === 0)) return;
        for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(ax + dx, ay + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }));
      const drawFormat = (mask) => {
        const d = (ECL[ecl] << 3) | mask;
        let r = d;
        for (let i = 0; i < 10; i++) r = (r << 1) ^ ((r >>> 9) * 0x537);
        const f = ((d << 10) | r) ^ 0x5412;
        for (let i = 0; i <= 5; i++) set(8, i, bit(f, i));
        set(8, 7, bit(f, 6)); set(8, 8, bit(f, 7)); set(7, 8, bit(f, 8));
        for (let i = 9; i < 15; i++) set(14 - i, 8, bit(f, i));
        for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(f, i));
        for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(f, i));
        set(8, size - 8, true);
      };
      drawFormat(0);    // reserves its modules; drawn for real once the mask is known
      if (ver >= 7) {
        let r = ver;
        for (let i = 0; i < 12; i++) r = (r << 1) ^ ((r >>> 11) * 0x1f25);
        const v = (ver << 12) | r;
        for (let i = 0; i < 18; i++) {
          const a = size - 11 + i % 3, b = Math.floor(i / 3);
          set(a, b, bit(v, i)); set(b, a, bit(v, i));
        }
      }
      let i = 0;
      for (let right = size - 1; right >= 1; right -= 2) {
        if (right === 6) right = 5;
        for (let vert = 0; vert < size; vert++) for (let j = 0; j < 2; j++) {
          const x = right - j, up = ((right + 1) & 2) === 0, y = up ? size - 1 - vert : vert;
          if (!fn[y][x] && i < words.length * 8) { mod[y][x] = bit(words[i >>> 3], 7 - (i & 7)); i++; }
        }
      }
      const MASKS = [(x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x) => x % 3 === 0, (x, y) => (x + y) % 3 === 0,
                     (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0, (x, y) => x * y % 2 + x * y % 3 === 0,
                     (x, y) => (x * y % 2 + x * y % 3) % 2 === 0, (x, y) => ((x + y) % 2 + x * y % 3) % 2 === 0];
      const applyMask = (m) => {
        for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (!fn[y][x] && MASKS[m](x, y)) mod[y][x] = !mod[y][x];
      };
      let mask = opts.mask >= 0 ? opts.mask : -1;
      if (mask < 0) {
        let best = Infinity;
        for (let m = 0; m < 8; m++) {
          applyMask(m); drawFormat(m);
          const p = penalty(mod, size);
          if (p < best) { best = p; mask = m; }
          applyMask(m);
        }
      }
      applyMask(mask);
      drawFormat(mask);
      return { size, version: ver, mask, modules: mod };
    }

    // The standard's penalty rules, for choosing the mask (any mask scans;
    // this one scans most easily).
    function penalty(m, size) {
      let p = 0;
      const runs = (get) => {
        for (let a = 0; a < size; a++) {
          let colour = false, run = 0;
          const hist = [0, 0, 0, 0, 0, 0, 0];
          const push = (len) => { if (hist[0] === 0) len += size; hist.pop(); hist.unshift(len); };
          const finders = () => {
            const n = hist[1], core = n > 0 && hist[2] === n && hist[3] === n * 3 && hist[4] === n && hist[5] === n;
            return (core && hist[0] >= n * 4 && hist[6] >= n ? 1 : 0) + (core && hist[6] >= n * 4 && hist[0] >= n ? 1 : 0);
          };
          for (let b = 0; b < size; b++) {
            if (get(a, b) === colour) { run++; if (run === 5) p += 3; else if (run > 5) p++; }
            else { push(run); if (!colour) p += finders() * 40; colour = get(a, b); run = 1; }
          }
          // Close the line with a light run to the edge.
          if (colour) { push(run); run = 0; }
          run += size;
          push(run);
          p += finders() * 40;
        }
      };
      runs((a, b) => m[a][b]);
      runs((a, b) => m[b][a]);
      for (let y = 0; y < size - 1; y++) for (let x = 0; x < size - 1; x++) {
        const c = m[y][x];
        if (c === m[y][x + 1] && c === m[y + 1][x] && c === m[y + 1][x + 1]) p += 3;
      }
      let dark = 0;
      for (const row of m) for (const c of row) if (c) dark++;
      const total = size * size, k = Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1;
      return p + k * 10;
    }

    function svg(text, opts = {}) {
      const q = encode(text, opts), n = q.size + 8;
      let d = "";
      q.modules.forEach((row, y) => row.forEach((c, x) => { if (c) d += `M${x + 4},${y + 4}h1v1h-1z`; }));
      return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${n} ${n}" shape-rendering="crispEdges" role="img"`
        + ` aria-label="QR code"><rect width="${n}" height="${n}" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
    }
    return { encode, svg };
  })();
