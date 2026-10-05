import { test, expect } from '@playwright/test';
import { zipSync, strToU8 } from 'fflate';
import { MODEL_HOST } from '../../src/models.js';
import {
  CHAT, FIRST, ORIGIN, SECOND, bubble, downloadText, expectLocalRequests, file, notices, play, record, speechFixture, stored, tab, watch,
} from './support.js';

// The conversation view inside the page: shown as soon as a chat is loaded, playing recordings from the
// tab's memory, and never in the way of the downloads. None of these tests runs Whisper.
const WAIT = { timeout: 15000 };
const POLICY = [
  "default-src 'none'", "script-src 'self' blob: 'wasm-unsafe-eval'", 'worker-src blob:', 'child-src blob:', "style-src 'self'", "img-src 'self'",
  'media-src blob:', "connect-src 'self' https://huggingface.co https://*.huggingface.co https://*.hf.co", "base-uri 'none'", "form-action 'none'",
].join('; ');
const problems = new WeakMap();
const isOpen = (page, id) => page.locator(id).evaluate((node) => node.open);

test.use({ actionTimeout: WAIT.timeout });
test.describe.configure({ timeout: 120000 });

test.beforeEach(async ({ page }) => {
  problems.set(page, await watch(page));
});

test.afterEach(async ({ page }) => {
  expect(await problems.get(page)()).toEqual([]);
});

async function exportZip() {
  const speech = await speechFixture();
  return file('export.zip', zipSync({ '_chat.txt': strToU8(CHAT), [FIRST]: speech, [`media/${SECOND}`]: speech, 'photo.jpg': new Uint8Array([1, 2, 3]) }), 'application/zip');
}

test('a chat alone shows the conversation before Start, and the downloads are as before', async ({ page, context }) => {
  const requests = record(context);
  await page.goto('/');
  await expect(page.locator('#files')).toHaveAttribute('accept', /(^|,)\.json(,|$)/);
  await expect(page.locator('#results')).toBeHidden();
  await page.locator('#files').setInputFiles(file('_chat.txt', CHAT));
  await expect(page.locator('#source-summary')).toHaveText('1 chat file · 0 voice messages');
  // Nothing was pressed: the conversation is there to read.
  await expect(page.locator('#start')).toHaveText('Prepare downloads →');
  await expect(page.locator('#results')).toBeVisible();
  await expect(page.locator('#viewer')).toHaveClass(/voxpad-viewer/);
  await expect(page.locator('#viewer')).toHaveAttribute('data-vp-theme', 'light');
  await expect(page.locator('#viewer .vp-title')).toHaveText('José · Ana');
  await expect(bubble(page, 1).locator('.vp-text')).toHaveText('Buenos días, Ana');
  await expect(bubble(page, 1)).toBeVisible();
  // Chat text arrives as text.
  await expect(bubble(page, 3).locator('.vp-text')).toHaveText('<script>window.stolen = true</script>');
  expect(await page.evaluate(() => window.stolen)).toBeUndefined();
  // A voice message without its recording has nothing to press and says where it stands.
  await expect(bubble(page, 2).locator('.vp-voice-status')).toHaveText('Not transcribed yet');
  expect(await page.locator('#viewer .vp-msg .vp-play').count()).toBe(0);
  // The page gives the viewer its height: four fifths of the window.
  expect(Math.round((await page.locator('#viewer').boundingBox()).height)).toBe(Math.round(page.viewportSize().height * 0.8));
  await expect(page.locator('#result-summary')).toHaveText('0 transcribed. Originals preserved.');
  await expect(page.locator('#view-link')).toBeVisible();
  await expect(page.locator('#date-order-choice')).toBeHidden();
  await expect(page.locator('#chat-choice')).toBeHidden();
  // The plain text is still there, folded away under the viewer.
  expect(await isOpen(page, '#plain-text')).toBe(false);
  await page.locator('#plain-text summary').click();
  await expect(page.locator('#chat-preview')).toBeVisible();
  expect(await page.locator('#chat-preview').evaluate((node) => node.textContent)).toBe(CHAT);
  await tab(page, 'Activity').click();
  await expect(page.locator('#viewer').getByRole('table', { name: 'Activity per participant' })).toContainText('Everyone');
  expect(await downloadText(page, 'Download text')).toBe(CHAT);
  const report = JSON.parse(await downloadText(page, 'Download JSON'));
  expect(report.chats[0].original_text).toBe(CHAT);
  expect(report.results).toEqual([]);
  // Removing the selection takes the viewer away with it.
  await page.locator('#clear').click();
  await expect(page.locator('#results')).toBeHidden();
  expect(await page.locator('#viewer').evaluate((node) => [node.childElementCount, node.className])).toEqual([0, 'viewer']);
  expect(await stored(page)).toEqual({ entries: {}, local: 0, session: 0, databases: [], cookies: '' });
  expect(requests.length).toBeGreaterThan(0);
  expectLocalRequests(requests);
  expect(requests.every((request) => new URL(request.url).hostname === '127.0.0.1')).toBe(true);
});

test('a date order can be chosen only where the dates leave a choice', async ({ page }) => {
  await page.goto('/');
  // 01/02 and 03/04 are dates either way round.
  await page.locator('#files').setInputFiles(file('chat.txt', '01/02/26, 10:00 - Ana: uno\n03/04/26, 11:00 - José: dos tres\n'));
  const choice = page.getByLabel(/can be read in more than one order/);
  await expect(choice).toBeVisible();
  await expect(choice).toHaveValue('DMY');
  // Browsers differ on the comma after the weekday.
  await expect(page.locator('#viewer .vp-day-label').first()).toHaveText(/^Sunday,? 1 February 2026$/);
  await expect(page.locator('#viewer .vp-notices')).toContainText('1 notice');
  await tab(page, 'Activity').click();
  await choice.selectOption('MDY');
  // The same viewer goes on, where the reader was, with the dates read the other way.
  await expect(tab(page, 'Activity')).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('#viewer .vp-summary')).toHaveText('2 messages · 2 Jan 2026 – 4 Mar 2026');
  await expect(page.locator('#viewer .vp-notices')).toBeHidden();
  await tab(page, 'Conversation').click();
  await expect(page.locator('#viewer .vp-day-label').first()).toHaveText(/^Friday,? 2 January 2026$/);
  // The choice stays on offer, and the text download does not depend on it.
  await expect(choice).toBeVisible();
  await choice.selectOption('DMY');
  await expect(page.locator('#viewer .vp-day-label').first()).toHaveText(/^Sunday,? 1 February 2026$/);
  expect(await downloadText(page, 'Download text')).toBe('01/02/26, 10:00 - Ana: uno\n03/04/26, 11:00 - José: dos tres\n');
  // A thirteenth day settles it, and nothing is asked.
  await page.locator('#files').setInputFiles(file('chat.txt', '01/02/26, 10:00 - Ana: uno\n13/04/26, 11:00 - José: dos tres\n'));
  await expect(page.locator('#viewer .vp-summary')).toHaveText('2 messages · 1 Feb 2026 – 13 Apr 2026');
  await expect(page.locator('#date-order-choice')).toBeHidden();
});

test('the viewer follows the chosen conversation, and events loaded in it stay', async ({ page }) => {
  const other = '07/01/26, 08:00 - Li: Hola\r\n07/01/26, 08:01 - Ana: Hola Li\r\n';
  await page.goto('/');
  await page.locator('#files').setInputFiles(file('two.zip', zipSync({ 'a/_chat.txt': strToU8(CHAT), 'b/_chat.txt': strToU8(other) }), 'application/zip'));
  await expect(page.locator('#source-summary')).toHaveText('2 chat files · 0 voice messages');
  await expect(page.locator('#chat-choice')).toBeVisible();
  await expect(page.locator('#viewer .vp-title')).toHaveText('José · Ana');
  // An events file chosen inside the viewer.
  await tab(page, 'Events').click();
  await page.locator('#viewer .vp-events-file').setInputFiles(file('events.txt', '2026-01-07 Li is back\n'));
  await expect(page.locator('#viewer .vp-event-card')).toHaveCount(1);
  await page.locator('#chat-select').selectOption({ label: 'b/_chat.txt' });
  await expect(page.locator('#viewer .vp-title')).toHaveText('Ana · Li');
  expect(await page.locator('#viewer').evaluate((node) => [node.querySelectorAll('.vp-head').length, node.querySelectorAll('audio').length])).toEqual([1, 1]);
  await expect(bubble(page, 0).locator('.vp-text')).toHaveText('Hola');
  await expect(page.locator('#viewer .vp-banner .vp-banner-label')).toHaveText('Li is back');
  expect(await downloadText(page, 'Download text')).toBe(other);
});

test('a viewer that cannot be shown becomes a notice, and the page goes on without it', async ({ page }) => {
  // Stand in for whatever might go wrong inside the viewer: it cannot be built at all.
  await page.addInitScript(() => { window.IntersectionObserver = undefined; });
  await page.goto('/');
  await page.locator('#files').setInputFiles(file('_chat.txt', CHAT));
  await expect(notices(page)).toContainText(['The conversation view could not be shown. The plain text and both downloads are complete.']);
  await expect(page.locator('#error')).toBeHidden();
  await expect(page.locator('#results')).toBeVisible();
  await expect(page.locator('#viewer')).toBeHidden();
  expect(await page.locator('#viewer').evaluate((node) => [node.childElementCount, node.className])).toEqual([0, 'viewer']);
  expect(await isOpen(page, '#plain-text')).toBe(true);
  await expect(page.locator('#chat-preview')).toBeVisible();
  expect(await page.locator('#chat-preview').evaluate((node) => node.textContent)).toBe(CHAT);
  await page.locator('#start').click();
  await expect(page.locator('#progress-message')).toHaveText('Done. Your downloads are ready.');
  expect(await downloadText(page, 'Download text')).toBe(CHAT);
  expect(JSON.parse(await downloadText(page, 'Download JSON')).chats[0].messages).toHaveLength(6);
});

test('recordings without a chat have no viewer: the results appear after Stop, as plain text', async ({ page, context }) => {
  // The model never arrives, so that Stop can be pressed while the page waits for it.
  await context.route(`${MODEL_HOST}**`, () => {});
  await page.goto('/');
  await page.locator('#files').setInputFiles({ name: 'voice.opus', mimeType: 'audio/ogg', buffer: await speechFixture() });
  await expect(page.locator('#source-summary')).toHaveText('0 chat files · 1 voice message');
  await expect(page.locator('#start')).toHaveText('Transcribe voice messages →');
  await expect(page.locator('#results')).toBeHidden();
  await expect(page.locator('#view-link')).toBeHidden();
  await page.locator('#model').selectOption('tiny');
  await page.locator('#start').click();
  await page.locator('#cancel').click();
  await expect(page.locator('#progress-message')).toHaveText('Stopped. Completed transcripts are available; continue when ready. Download JSON to continue another day: select it together with the export.');
  await expect(page.locator('#results')).toBeVisible();
  await expect(page.locator('#viewer')).toBeHidden();
  expect(await page.locator('#viewer').evaluate((node) => node.childElementCount)).toBe(0);
  expect(await isOpen(page, '#plain-text')).toBe(true);
  await expect(page.locator('#chat-preview')).toBeVisible();
  expect(await page.locator('#chat-preview').evaluate((node) => node.textContent)).toBe('voice.opus\n[Not transcribed]\n');
  await expect(page.locator('#result-summary')).toHaveText('0 transcribed · 1 pending. Originals preserved.');
  await expect(page.locator('#start')).toHaveText('Continue / retry transcription →');
  await page.locator('#audio-details summary').click();
  await expect(page.locator('#audio-list li')).toHaveCount(1);
  await expect(page.locator('#audio-list li strong')).toHaveText('voice.opus');
  const report = JSON.parse(await downloadText(page, 'Download JSON'));
  expect(report.results).toMatchObject([{ file: 'voice.opus', status: 'pending', bytes: (await speechFixture()).length }]);
});

test('a voice message plays from the tab through one object URL; nothing is stored and nothing is sent', async ({ page, context }) => {
  // No request is routed in this test: Playwright then reports the blob: requests too, and they are checked with the rest.
  const requests = record(context);
  // Follow every object URL made for audio from its making to its release.
  await page.addInitScript(() => {
    const { createObjectURL, revokeObjectURL } = URL;
    window.__audio = { made: [], live: new Set() };
    URL.createObjectURL = (blob) => {
      const url = createObjectURL.call(URL, blob);
      if (blob.type.startsWith('audio/')) {
        window.__audio.made.push([blob.type, blob.size]);
        window.__audio.live.add(url);
      }
      return url;
    };
    URL.revokeObjectURL = (url) => {
      window.__audio.live.delete(url);
      return revokeObjectURL.call(URL, url);
    };
  });
  const urls = () => page.evaluate(() => ({ made: window.__audio.made, live: [...window.__audio.live] }));
  const speech = await speechFixture();
  await page.goto('/');
  expect(await page.locator('meta[http-equiv="Content-Security-Policy"]').getAttribute('content')).toBe(POLICY);
  await page.locator('#files').setInputFiles(await exportZip());
  await expect(page.locator('#source-summary')).toHaveText('1 chat file · 2 voice messages');
  // Loading an export prepares nothing to play.
  await expect(bubble(page, 2).locator('.vp-play')).toBeVisible();
  expect(await urls()).toEqual({ made: [], live: [] });
  expect(await page.locator('#viewer audio').evaluate((audio) => [audio.preload, audio.getAttribute('src')])).toEqual(['none', null]);

  const first = await play(page, 2);
  expect(first.error).toBe(0);
  expect(first.src.startsWith(`blob:${ORIGIN}/`)).toBe(true);
  expect(first.duration).toBeCloseTo(65630 / 16000, 1);
  expect(await urls()).toEqual({ made: [['audio/ogg', speech.length]], live: [first.src] });
  await expect(page.locator('#viewer').getByRole('group', { name: 'Now playing' }).locator('.vp-name')).toHaveText('Ana');
  // The next recording takes the place of the first: its URL is released, and one stays alive.
  const second = await play(page, 4);
  expect(second.error).toBe(0);
  expect(second.src).not.toBe(first.src);
  expect(await urls()).toEqual({ made: [['audio/ogg', speech.length], ['audio/ogg', speech.length]], live: [second.src] });

  // Media may come from the tab's memory and from nowhere else: another address is refused before any request is made.
  const refused = (url) => page.evaluate((address) => new Promise((resolve) => {
    document.addEventListener('securitypolicyviolation', (event) => resolve(`${event.effectiveDirective} ${event.blockedURI}`), { once: true });
    new Audio(address).load();
  }), url);
  expect(await refused('https://upload.example/voice.ogg')).toBe('media-src https://upload.example/voice.ogg');
  expect(await refused('data:audio/wav;base64,UklGRg==')).toBe('media-src data');
  expect(await refused(`${ORIGIN}/mark.svg`)).toBe(`media-src ${ORIGIN}/mark.svg`);
  await page.evaluate(() => { window.__violations.length = 0; });

  expect(await stored(page)).toEqual({ entries: {}, local: 0, session: 0, databases: [], cookies: '' });
  // Every request went to this site or to bytes of the tab itself, and none names a recording.
  expectLocalRequests(requests, { marker: FIRST });
  expect(requests.filter((request) => !request.url.startsWith('blob:')).every((request) => new URL(request.url).hostname === '127.0.0.1')).toBe(true);
  expect(requests.some((request) => request.url.includes('upload.example'))).toBe(false);

  // Leaving the page releases the recording, and so does removing the selection.
  await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
  expect((await urls()).live).toEqual([]);
  const third = await play(page, 2);
  expect((await urls()).live).toEqual([third.src]);
  await page.locator('#clear').click();
  await expect(page.locator('#results')).toBeHidden();
  expect((await urls()).live).toEqual([]);
  expect((await urls()).made).toHaveLength(3);
});

test('a recording the browser cannot play is decoded and played as WAV', async ({ page }) => {
  // Stand in for a browser without Ogg/Opus: what it is given under that name is not audio to it.
  await page.addInitScript(() => {
    const { createObjectURL } = URL;
    window.__blobs = [];
    URL.createObjectURL = (blob) => {
      if (!blob.type.startsWith('audio/')) return createObjectURL.call(URL, blob);
      window.__blobs.push(blob);
      return createObjectURL.call(URL, blob.type === 'audio/ogg' ? new Blob([new Uint8Array(4096).fill(7)], { type: blob.type }) : blob);
    };
  });
  await page.goto('/');
  await page.locator('#files').setInputFiles([file('_chat.txt', CHAT), { name: FIRST, mimeType: 'audio/ogg', buffer: await speechFixture() }]);
  await expect(page.locator('#source-summary')).toHaveText('1 chat file · 1 voice message');
  const state = await play(page, 2);
  expect(state.error).toBe(0);
  expect(state.duration).toBeCloseTo(65630 / 16000, 3);
  await expect(bubble(page, 2).locator('.vp-voice-error')).toBeHidden();
  // Asked once as it is and once decoded: mono, 16 kHz, 16 bits, every sample of the recording.
  const made = await page.evaluate(async () => {
    const view = new DataView(await window.__blobs[1].arrayBuffer());
    const text = (offset) => String.fromCharCode(view.getUint8(offset), view.getUint8(offset + 1), view.getUint8(offset + 2), view.getUint8(offset + 3));
    let loudest = 0;
    for (let offset = 44; offset < view.byteLength; offset += 2) loudest = Math.max(loudest, Math.abs(view.getInt16(offset, true)));
    return {
      types: window.__blobs.map((blob) => blob.type), size: view.byteLength, riff: [text(0), view.getUint32(4, true), text(8), text(12), text(36), view.getUint32(40, true)],
      format: [view.getUint32(16, true), view.getUint16(20, true), view.getUint16(22, true), view.getUint32(24, true), view.getUint32(28, true), view.getUint16(32, true), view.getUint16(34, true)],
      loudest,
    };
  });
  expect(made.types).toEqual(['audio/ogg', 'audio/wav']);
  expect(made.size).toBe(44 + 65630 * 2);
  expect(made.riff).toEqual(['RIFF', 36 + 65630 * 2, 'WAVE', 'fmt ', 'data', 65630 * 2]);
  expect(made.format).toEqual([16, 1, 1, 16000, 32000, 2, 16]);
  expect(made.loudest).toBeGreaterThan(1000);
  expect(made.loudest).toBeLessThanOrEqual(32767);
});
