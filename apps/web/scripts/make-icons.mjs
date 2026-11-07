/**
 * Generates every RupeeRail icon, for the web app and the extension, from the shapes below.
 * Run from apps/web with `node scripts/make-icons.mjs`. It renders with installed Chrome, so sizes come out exactly as a browser draws them.
 */

import { chromium } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";

const WEB = path.join(import.meta.dirname, "../public");
const EXTENSION = path.join(import.meta.dirname, "../../../extension/static/icons");
const BLUE = "#1b5e9e";
const SAFFRON = "#ffb95f";

function rupee(scale, strokeWidth) {
  return `<g transform="translate(256 256) scale(${scale}) translate(-256 -288)" fill="none" stroke="#fff" stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round"><path d="M156 150H356M156 226H356M156 150h66c54 0 88 32 88 76s-34 76-88 76h-66l148 124"/></g>`;
}

const FRAME = `<path d="M84 176V124a40 40 0 0 1 40-40h52M336 84h52a40 40 0 0 1 40 40v52M428 336v52a40 40 0 0 1-40 40h-52M176 428h-52a40 40 0 0 1-40-40v-52" fill="none" stroke="${SAFFRON}" stroke-width="36" stroke-linecap="round"/>`;
const svg = (body) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">${body}</svg>\n`;

/** The icon: a QR scanner's frame around the rupee sign. */
const TILE = svg(`<rect width="512" height="512" rx="120" fill="${BLUE}"/>${FRAME}${rupee(0.66, 60)}`);
/** A frame turns to mush at 16px, so the smallest size is the rupee sign alone. */
const SMALL = svg(`<rect width="512" height="512" rx="120" fill="${BLUE}"/>${rupee(1.12, 62)}`);
/** No transparent corners, and everything kept within the safe zone that launchers don't crop. */
const SQUARE = (scale) => svg(`<rect width="512" height="512" fill="${BLUE}"/><g transform="translate(256 256) scale(${scale}) translate(-256 -256)">${FRAME}${rupee(0.66, 60)}</g>`);

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage();

async function png(source, size) {
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(`<!doctype html><body style="margin:0;background:transparent"><img width="${size}" height="${size}" style="display:block" src="data:image/svg+xml,${encodeURIComponent(source)}"></body>`);
  await page.locator("img").evaluate((image) => image.decode());
  return page.screenshot({ omitBackground: true });
}

/** An .ico is a small directory of PNGs. */
function ico(images) {
  const header = Buffer.alloc(6 + 16 * images.length);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(images.length, 4);
  let offset = header.length;
  images.forEach(({ size, data }, index) => {
    const entry = 6 + 16 * index;
    header.writeUInt8(size, entry);
    header.writeUInt8(size, entry + 1);
    header.writeUInt16LE(1, entry + 4);
    header.writeUInt16LE(32, entry + 6);
    header.writeUInt32LE(data.length, entry + 8);
    header.writeUInt32LE(offset, entry + 12);
    offset += data.length;
  });
  return Buffer.concat([header, ...images.map((image) => image.data)]);
}

writeFileSync(path.join(WEB, "icon.svg"), TILE);
writeFileSync(path.join(WEB, "pwa-192x192.png"), await png(TILE, 192));
writeFileSync(path.join(WEB, "pwa-512x512.png"), await png(TILE, 512));
writeFileSync(path.join(WEB, "maskable-icon-512x512.png"), await png(SQUARE(0.72), 512));
writeFileSync(path.join(WEB, "apple-touch-icon-180x180.png"), await png(SQUARE(0.84), 180));
writeFileSync(path.join(WEB, "favicon.ico"), ico([
  { size: 16, data: await png(SMALL, 16) },
  { size: 32, data: await png(TILE, 32) },
  { size: 48, data: await png(TILE, 48) },
]));

mkdirSync(EXTENSION, { recursive: true });
writeFileSync(path.join(EXTENSION, "icon-16.png"), await png(SMALL, 16));
for (const size of [32, 48, 128]) writeFileSync(path.join(EXTENSION, `icon-${size}.png`), await png(TILE, size));

await browser.close();
