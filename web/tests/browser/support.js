import { expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { assets, assetDirectory } from './assets.js';

// What the tests of the conversation view and of earlier reports share.
export const ORIGIN = 'http://127.0.0.1:4173';
export const FIRST = 'PTT-20260105-WA0001.opus';
export const SECOND = 'PTT-20260106-WA0002.opus';
// The file names carry their dates, so the chat can only be read day first.
export const CHAT = [
  '05/01/26, 09:00 - Messages and calls are end-to-end encrypted.',
  '05/01/26, 09:02 - José: Buenos días, Ana',
  `05/01/26, 09:05 - Ana: ${FIRST} (file attached)`,
  '06/01/26, 10:00 - José: <script>window.stolen = true</script>',
  `06/01/26, 10:01 - José: ${SECOND} (file attached)`,
  '06/01/26, 10:05 - Ana: See you soon!',
  '',
].join('\r\n');
export const SPOKEN = 'Hello, this is a voice message. We will meet tomorrow.';

export const file = (name, content, mimeType = 'text/plain') => ({ name, mimeType, buffer: Buffer.from(content) });
export const json = (name, content) => file(name, JSON.stringify(content, null, 2), 'application/json');
export const bubble = (page, index) => page.locator(`#viewer .vp-msg[data-vp-index="${index}"]`);
export const tab = (page, name) => page.locator('#viewer').getByRole('tab', { name: new RegExp(`^${name}`) });
export const notices = (page) => page.locator('#warnings li');

export async function speechFixture() {
  return Buffer.from((await readFile(new URL('../fixtures/speech.opus.base64', import.meta.url), 'utf8')).trim(), 'base64');
}

export async function downloadText(page, button) {
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: button }).click();
  const download = await downloaded;
  return readFile(await download.path(), 'utf8');
}

export async function serveAssets(context) {
  for (const { name, url } of assets) {
    await context.route(url, (route) => route.fulfill({
      path: `${assetDirectory}${name}`,
      headers: { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/octet-stream' },
    }));
  }
}

/** Collect what goes wrong in a page: uncaught errors, and anything its security policy had to refuse. */
export async function watch(page) {
  const seen = [];
  page.on('pageerror', (error) => seen.push(`error: ${error}`));
  await page.addInitScript(() => {
    window.__violations = [];
    document.addEventListener('securitypolicyviolation', (event) => window.__violations.push(`${event.effectiveDirective}: ${event.blockedURI}`));
  });
  return async () => [...seen, ...await page.evaluate(() => window.__violations)];
}

/** Everything the page keeps in the browser. Only the speech model may ever be there. */
export const stored = (page) => page.evaluate(async () => {
  const entries = {};
  for (const name of await caches.keys()) {
    for (const request of await (await caches.open(name)).keys()) {
      const body = await (await (await caches.open(name)).match(request)).arrayBuffer();
      const digest = await crypto.subtle.digest('SHA-256', body);
      entries[request.url] = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
    }
  }
  return { entries, local: localStorage.length, session: sessionStorage.length, databases: await indexedDB.databases(), cookies: document.cookie };
});

/**
 * Every request is a plain download from this site, from the model host, or of bytes the tab itself
 * holds: a recording that plays is fetched from a blob: address, which has no host to compare.
 */
export function expectLocalRequests(requests, { marker } = {}) {
  const allowed = new Set(assets.map(({ url }) => url));
  for (const request of requests) {
    expect(request.method).toBe('GET');
    expect(request.body).toBeNull();
    if (marker) expect(request.url).not.toContain(marker);
    const own = request.url.startsWith('blob:') ? request.url.startsWith(`blob:${ORIGIN}/`) : new URL(request.url).hostname === '127.0.0.1';
    expect(own || allowed.has(request.url), request.url.slice(0, 80)).toBe(true);
  }
}

export const record = (context) => {
  const requests = [];
  context.on('request', (request) => requests.push({ url: request.url(), method: request.method(), body: request.postData() }));
  return requests;
};

/** Press play in a voice bubble and wait until sound has really come out of the shared player. */
export async function play(page, index) {
  await bubble(page, index).locator('.vp-play').click();
  await expect(bubble(page, index).getByRole('button', { name: /^Pause voice message/ })).toBeVisible();
  await page.waitForFunction(() => { const audio = document.querySelector('#viewer audio'); return !audio.paused && audio.currentTime > 0.2; });
  return page.evaluate(() => {
    const audio = document.querySelector('#viewer audio');
    return { src: audio.currentSrc, error: audio.error ? audio.error.code : 0, duration: audio.duration };
  });
}
