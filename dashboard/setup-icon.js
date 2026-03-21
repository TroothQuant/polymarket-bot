'use strict'
// Generates icon.png — run once: node setup-icon.js
const zlib = require('zlib')
const fs   = require('fs')
const path = require('path')

// ── CRC32 ──────────────────────────────────────────────────────────────────
const crcTable = new Uint32Array(256)
for (let i = 0; i < 256; i++) {
  let c = i
  for (let j = 0; j < 8; j++) c = (c & 1) ? 0xEDB88320 ^ (c >>> 1) : c >>> 1
  crcTable[i] = c >>> 0
}
function crc32(buf) {
  let crc = 0xFFFFFFFF
  for (const b of buf) crc = (crcTable[(crc ^ b) & 0xFF] ^ (crc >>> 8)) >>> 0
  return (crc ^ 0xFFFFFFFF) | 0
}

// ── PNG builder ────────────────────────────────────────────────────────────
function makePng(w, h, getPixel) {
  function chunk(type, data) {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length)
    const t = Buffer.from(type)
    const crc = Buffer.alloc(4); crc.writeInt32BE(crc32(Buffer.concat([t, data])))
    return Buffer.concat([len, t, data, crc])
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4)
  ihdr[8] = 8; ihdr[9] = 6 // 8-bit RGBA

  const rows = Buffer.alloc(h * (1 + w * 4))
  for (let y = 0; y < h; y++) {
    rows[y * (1 + w * 4)] = 0 // filter: None
    for (let x = 0; x < w; x++) {
      const [r, g, b, a] = getPixel(x, y)
      const off = y * (1 + w * 4) + 1 + x * 4
      rows[off] = r; rows[off+1] = g; rows[off+2] = b; rows[off+3] = a
    }
  }
  return Buffer.concat([
    Buffer.from([137,80,78,71,13,10,26,10]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(rows)),
    chunk('IEND', Buffer.alloc(0))
  ])
}

// ── Icon design: 256×256, rounded blue square, white "P" ──────────────────
const SIZE   = 256
const RADIUS = 46            // corner radius
const BG     = [22, 82, 240, 255]   // Polymarket blue #1652F0
const FG     = [255, 255, 255, 255] // white
const TRANS  = [0, 0, 0, 0]

function inRoundedRect(x, y) {
  const pad = 10
  const x0 = pad, y0 = pad, x1 = SIZE-pad, y1 = SIZE-pad
  if (x < x0 || x > x1 || y < y0 || y > y1) return false
  // corner circles
  const corners = [[x0+RADIUS,y0+RADIUS],[x1-RADIUS,y0+RADIUS],[x0+RADIUS,y1-RADIUS],[x1-RADIUS,y1-RADIUS]]
  if (x < x0+RADIUS && y < y0+RADIUS) return dist(x,y,corners[0]) <= RADIUS
  if (x > x1-RADIUS && y < y0+RADIUS) return dist(x,y,corners[1]) <= RADIUS
  if (x < x0+RADIUS && y > y1-RADIUS) return dist(x,y,corners[2]) <= RADIUS
  if (x > x1-RADIUS && y > y1-RADIUS) return dist(x,y,corners[3]) <= RADIUS
  return true
}

function dist(x,y,c) { return Math.sqrt((x-c[0])**2+(y-c[1])**2) }

// "P" glyph defined by SDF regions (all values in 0–256 space)
function isP(x, y) {
  const S = SIZE
  // Vertical stem: x=[78,118], y=[52,204]
  const stem = x>=78 && x<=118 && y>=52 && y<=204

  // Bowl: semicircle top-right of stem
  // outer circle: center=(118,108), r=56
  // inner circle: center=(118,108), r=26
  // only right half (x>=118) and top half (y<=164)
  const bCx=118, bCy=108, bOr=56, bIr=26
  const bd = dist(x, y, [bCx, bCy])
  const bowl = bd<=bOr && bd>=bIr && x>=78 && y>=52 && y<=164

  // Bottom cap of bowl (horizontal closing bar): x=[78,174], y=[152,164]
  const cap = x>=78 && x<=174 && y>=150 && y<=164

  // Top cap: x=[78,174], y=[52,64]
  const topCap = x>=78 && x<=174 && y>=52 && y<=64

  return stem || bowl || cap || topCap
}

const png = makePng(SIZE, SIZE, (x, y) => {
  if (!inRoundedRect(x, y)) return TRANS
  if (isP(x, y)) return FG
  return BG
})

const out = path.join(__dirname, 'icon.png')
fs.writeFileSync(out, png)
console.log('✓ icon.png created:', out)
