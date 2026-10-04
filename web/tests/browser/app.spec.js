import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { zipSync, strToU8 } from 'fflate';
import { assets, assetDirectory, assetHashes } from './assets.js';
import { decodeAudio } from '../../src/audio.js';

async function downloadText(page, button) {
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: button }).click();
  const download = await downloaded;
  return readFile(await download.path(), 'utf8');
}

async function speechFixture() {
  return Buffer.from(await readFile(new URL('../fixtures/speech.opus.base64', import.meta.url), 'utf8'), 'base64');
}

async function serveAssets(context) {
  for (const { name, url } of assets) {
    await context.route(url, (route) => route.fulfill({
      path: `${assetDirectory}${name}`,
      headers: { 'Access-Control-Allow-Origin': '*', 'Content-Type': 'application/octet-stream' },
    }));
  }
}

/** Encode mono 16 kHz samples as 16-bit PCM WAV. */
function wav(samples) {
  const bytes = Buffer.alloc(44 + samples.length * 2);
  bytes.write('RIFF', 0);
  bytes.writeUInt32LE(36 + samples.length * 2, 4);
  bytes.write('WAVEfmt ', 8);
  bytes.writeUInt32LE(16, 16);
  bytes.writeUInt16LE(1, 20);
  bytes.writeUInt16LE(1, 22);
  bytes.writeUInt32LE(16000, 24);
  bytes.writeUInt32LE(32000, 28);
  bytes.writeUInt16LE(2, 32);
  bytes.writeUInt16LE(16, 34);
  bytes.write('data', 36);
  bytes.writeUInt32LE(samples.length * 2, 40);
  samples.forEach((sample, index) => bytes.writeInt16LE(Math.round(Math.max(-1, Math.min(1, sample)) * 32767), 44 + index * 2));
  return bytes;
}

test('chat-only exports preserve original text, render safely, and prepare both downloads', async ({ page }) => {
  const requests = [];
  page.on('request', (request) => requests.push(request));
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Your export never leaves your device.' })).toBeVisible();
  const original = '04/10/26, 09:00 - Ana: <script>window.stolen = true</script>\r\n04/10/26, 09:01 - José: ¡Hola!\r\n';
  await page.locator('#files').setInputFiles({ name: '_chat.txt', mimeType: 'text/plain', buffer: Buffer.from(original) });
  await expect(page.locator('#start')).toHaveText('Prepare downloads →');
  await page.locator('#start').click();
  await expect(page.locator('#results')).toBeVisible();
  await expect(page.locator('#start')).toHaveText('Prepare downloads →');
  expect(await page.evaluate(() => window.stolen)).toBeUndefined();
  expect(await downloadText(page, 'Download text')).toBe(original);
  const report = JSON.parse(await downloadText(page, 'Download JSON'));
  expect(report.chats[0].original_text).toBe(original);
  expect(report.chats[0].messages).toHaveLength(2);
  expect(report.results).toEqual([]);
  expect(requests.every((request) => request.method() === 'GET' && new URL(request.url()).hostname === '127.0.0.1')).toBe(true);
});

test('real Whistle transcribes Opus in place, keeps reusable JSON, and sends/stores only public assets', async ({ page, context }) => {
  const requests = [];
  context.on('request', (request) => requests.push({ url: request.url(), method: request.method(), body: request.postData() }));
  await serveAssets(context);
  const speech = await speechFixture();
  const privateMarker = 'PRIVATE-CONVERSATION-938725';
  const filename = 'PRIVATE-VOICE-627184.opus';
  const original = `[04/10/26, 09:00:00] Ana: ${privateMarker}\r\n[04/10/26, 09:01:00] José: <attached: ${filename}>\r\nVoice note description\r\n[04/10/26, 09:02:00] Ana: See you soon!\r\n`;
  const archive = zipSync({ '_chat.txt': strToU8(original), [filename]: speech, 'photo.jpg': new Uint8Array([1, 2, 3]) });
  await page.goto('/');
  await page.locator('#files').setInputFiles({ name: 'private-export.zip', mimeType: 'application/zip', buffer: Buffer.from(archive) });
  await expect(page.locator('#source-summary')).toHaveText('1 chat file · 1 voice message');
  await page.locator('#language').selectOption('en');
  await page.locator('#start').click();
  await expect(page.locator('#progress-message')).toHaveText('Done. Your downloads are ready.', { timeout: 120000 });
  await expect(page.locator('#result-summary')).toContainText('1 transcribed');
  const report = JSON.parse(await downloadText(page, 'Download JSON'));
  const result = report.results[0];
  expect(result.status).toBe('ok');
  expect(result.text.toLowerCase()).toContain('voice message');
  expect(result.text.toLowerCase()).toContain('tomorrow');
  expect(result.segments).toHaveLength(1);
  expect(result.duration_seconds).toBeGreaterThan(3);
  expect(result.messages[0]).toMatchObject({ sender: 'José', timestamp: '04/10/26, 09:01:00', chat_file: '_chat.txt' });
  expect(report.chats[0].original_text).toBe(original);
  const log = await downloadText(page, 'Download text');
  expect(log).toContain(privateMarker);
  expect(log.indexOf(result.text)).toBeGreaterThan(log.indexOf('Voice note description'));
  expect(log.indexOf(result.text)).toBeLessThan(log.indexOf('See you soon!'));
  expect(report.chats[0].annotated_text).toBe(log);

  const allowed = new Set(assets.map(({ url }) => url));
  for (const request of requests) {
    expect(request.method).toBe('GET');
    expect(request.body).toBeNull();
    expect(request.url).not.toContain(privateMarker);
    expect(request.url).not.toContain(filename);
    expect(new URL(request.url).hostname === '127.0.0.1' || allowed.has(request.url)).toBe(true);
  }
  const stored = await page.evaluate(async () => {
    const entries = {};
    for (const name of await caches.keys()) {
      for (const request of await (await caches.open(name)).keys()) {
        const body = await (await (await caches.open(name)).match(request)).arrayBuffer();
        const digest = await crypto.subtle.digest('SHA-256', body);
        entries[request.url] = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('');
      }
    }
    return { entries, local: localStorage.length, session: sessionStorage.length, databases: await indexedDB.databases() };
  });
  expect(stored.entries).toEqual(await assetHashes());
  expect(stored.local).toBe(0);
  expect(stored.session).toBe(0);
  expect(stored.databases).toEqual([]);
  await page.screenshot({ path: 'test-results/voxpad-desktop.png', fullPage: true });
});

test('recordings longer than 30 seconds are transcribed in consecutive parts', async ({ page, context }) => {
  await serveAssets(context);
  const speech = await decodeAudio(new Uint8Array(await speechFixture()));
  const copies = 8;
  const samples = new Float32Array(speech.length * copies);
  for (let copy = 0; copy < copies; copy += 1) samples.set(speech, copy * speech.length);
  const seconds = samples.length / 16000;
  expect(seconds).toBeGreaterThan(30);
  expect(seconds).toBeLessThan(60);
  await page.goto('/');
  await page.locator('#files').setInputFiles({ name: 'long-note.wav', mimeType: 'audio/wav', buffer: wav(samples) });
  await expect(page.locator('#source-summary')).toHaveText('0 chat files · 1 voice message');
  await page.locator('#language').selectOption('en');
  await page.locator('#start').click();
  await expect(page.locator('#progress-message')).toHaveText('Done. Your downloads are ready.', { timeout: 120000 });
  const result = JSON.parse(await downloadText(page, 'Download JSON')).results[0];
  expect(result.status).toBe('ok');
  expect(result.duration_seconds).toBeCloseTo(seconds, 2);
  expect(result.segments.map(({ start, end }) => [start, end])).toEqual([[0, 30], [30, result.duration_seconds]]);
  // Speech on both sides of the 30-second boundary reaches the transcript.
  expect(result.segments[0].text.toLowerCase()).toContain('voice message');
  expect(result.segments[1].text.toLowerCase()).toContain('tomorrow');
  expect(result.text).toBe(result.segments.map((segment) => segment.text).join(' '));
  expect(await downloadText(page, 'Download text')).toBe(`long-note.wav\n${result.text}\n`);
});

test('unsafe exports fail without initializing transcription', async ({ page }) => {
  const archive = zipSync({ '../_chat.txt': strToU8('private chat') });
  await page.goto('/');
  await page.locator('#files').setInputFiles({ name: 'unsafe.zip', mimeType: 'application/zip', buffer: Buffer.from(archive) });
  await expect(page.locator('#error')).toBeVisible();
  await expect(page.locator('#start')).toBeDisabled();
});
