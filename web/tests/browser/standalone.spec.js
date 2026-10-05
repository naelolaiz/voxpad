import { test, expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

// The page Python writes is opened as people open it: from a folder, with no server. Only then do its content
// security policy, its hashes and its relative recordings show whether they work.
const REPOSITORY = fileURLToPath(new URL('../../../', import.meta.url));
const WAIT = { timeout: 15000 };
const RECORDING = 'PTT-20260105-WA0001.opus';
const WRITE = [
  'import json, sys',
  'from pathlib import Path',
  'from voxpad.viewer import write_viewer',
  'write_viewer(Path(sys.argv[2]), json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")), bucket=sys.argv[3] or None)',
].join('\n');
const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');
const problems = new WeakMap();

test.use({ actionTimeout: WAIT.timeout });
test.describe.configure({ timeout: 120000 });

test.beforeEach(async ({ page }) => {
  const seen = [];
  problems.set(page, seen);
  page.on('pageerror', (error) => seen.push(`error: ${error}`));
  page.on('console', (message) => { if (message.type() === 'error') seen.push(`console: ${message.text()}`); });
  // Installed before the page's own script runs, so that a refusal while it starts is not missed.
  await page.addInitScript(() => {
    window.__violations = [];
    document.addEventListener('securitypolicyviolation', (event) => window.__violations.push(`${event.effectiveDirective}: ${event.blockedURI}`));
  });
});

test.afterEach(async ({ page }) => {
  expect(await page.evaluate(() => window.__violations)).toEqual([]);
  expect(problems.get(page)).toEqual([]);
});

const speech = async () => Buffer.from((await read('../fixtures/speech.opus.base64')).trim(), 'base64');
const bubble = (page, index) => page.locator(`.vp-msg[data-vp-index="${index}"]`);

/** Write the fixture model as a page with python3, the recordings named as `source(src)` says. */
async function writePage(testInfo, name, source, bucket = '') {
  const model = JSON.parse(await read('../fixtures/viewer-model.json'));
  for (const message of model.messages) if (message.voice) message.voice.src = message.voice.src ? source(message.voice.src) : null;
  const data = testInfo.outputPath(`${name}.json`);
  const file = testInfo.outputPath(`${name}.html`);
  await mkdir(testInfo.outputDir, { recursive: true });
  await writeFile(data, JSON.stringify(model));
  execFileSync('python3', ['-c', WRITE, data, file, bucket], { cwd: REPOSITORY, stdio: 'pipe' });
  return { file, model, text: await readFile(file, 'utf8') };
}

async function playing(page, index) {
  await bubble(page, index).locator('.vp-play').click();
  await page.waitForFunction(() => { const audio = document.querySelector('audio'); return !audio.paused && audio.currentTime > 0.2; }, null, WAIT);
  return page.evaluate(() => { const audio = document.querySelector('audio'); return { src: audio.currentSrc, error: audio.error ? audio.error.code : 0 }; });
}

test('the written page shows the conversation and plays a recording that lies beside it', async ({ page }, testInfo) => {
  const { file, text } = await writePage(testInfo, 'conversation', (src) => `./${src}`, 'week');
  await mkdir(testInfo.outputPath('audio'), { recursive: true });
  await writeFile(testInfo.outputPath('audio', RECORDING), await speech());
  expect(text).toContain("media-src 'self'");
  // Tall enough for the whole fixture, so that every bubble is in the document at once.
  await page.setViewportSize({ width: 1000, height: 3600 });
  await page.goto(pathToFileURL(file).href);
  await expect(page).toHaveTitle('Ana · José');
  await expect(bubble(page, 2).locator('.vp-text')).toHaveText('Sí, todo bien. The train was on time for once.');
  await expect(bubble(page, 2)).toBeVisible();
  // Chat text arrives as text: markup in a message is shown, never built.
  await expect(bubble(page, 26).locator('.vp-text')).toHaveText('<b>not bold</b> & "quoted" \'single\'');
  expect(await bubble(page, 26).locator('.vp-text b').count()).toBe(0);
  // The viewer is the whole page, follows the system's theme, and was told which bucket to open with.
  const root = page.locator('#voxpad-root');
  await expect(root).toHaveAttribute('data-vp-theme', 'auto');
  await expect(root).toHaveAttribute('data-bucket', 'week');
  expect(await root.boundingBox()).toEqual({ x: 0, y: 0, ...page.viewportSize() });
  expect(await page.evaluate(() => [document.scripts.length, document.querySelectorAll('style').length, window.stolen])).toEqual([2, 1, undefined]);
  // Nothing is fetched until play is pressed; then the recording comes from the page's own folder.
  expect(await page.evaluate(() => document.querySelector('audio').getAttribute('src'))).toBeNull();
  const state = await playing(page, 4);
  expect(state.error).toBe(0);
  expect(state.src).toBe(pathToFileURL(testInfo.outputPath('audio', RECORDING)).href);
  await expect(bubble(page, 4).getByRole('button', { name: /^Pause voice message from Ana/ })).toBeVisible();
});

test('a page written without recordings offers none and allows itself no media', async ({ page }, testInfo) => {
  const { file, text } = await writePage(testInfo, 'silent', () => null);
  expect(text).not.toContain('media-src');
  expect(text).toContain("default-src 'none'");
  await page.goto(pathToFileURL(file).href);
  await expect(bubble(page, 2).locator('.vp-text')).toHaveText('Sí, todo bien. The train was on time for once.');
  // The transcript is there to read; there is nothing to press.
  await expect(bubble(page, 4).locator('.vp-transcript')).toHaveText('Hello, this is a voice message. We will meet tomorrow.');
  expect(await page.locator('.vp-msg .vp-play').count()).toBe(0);
  await expect(page.locator('#voxpad-root')).not.toHaveAttribute('data-bucket', /.*/);
  await page.getByRole('tab', { name: /^Voice messages/ }).click();
  await expect(page.locator('.vp-row').first()).toBeVisible();
  await page.getByRole('tab', { name: /^Activity/ }).click();
  await expect(page.getByRole('table', { name: 'Activity per participant' })).toBeVisible();
});

test('voxpad-stats turns an export folder into a page that plays its voice messages', async ({ page }, testInfo) => {
  // A folder name that has to be quoted in an address, to see that the page finds the recording all the same.
  const folder = testInfo.outputPath('my export #1');
  await mkdir(folder, { recursive: true });
  await writeFile(`${folder}/chat.txt`, `05/01/26, 09:02 - José: Buenos días, Ana\n05/01/26, 09:05 - Ana: ${RECORDING} (file attached)\n`);
  await writeFile(`${folder}/${RECORDING}`, await speech());
  const file = testInfo.outputPath('conversation.html');
  const said = execFileSync('python3', ['-m', 'voxpad.stats', folder, '--viewer', file, '--no-transcripts'], { cwd: REPOSITORY, stdio: 'pipe' }).toString();
  expect(said).toContain('Voice messages: 1 detected, 1 with a recording, 1 with a duration.');
  const text = await readFile(file, 'utf8');
  expect(text).toContain(`"src":"./my%20export%20%231/${RECORDING}"`);
  // Where the folder is on this computer is nobody's business.
  expect(text).not.toContain(testInfo.outputDir);
  await page.goto(pathToFileURL(file).href);
  await expect(bubble(page, 0).locator('.vp-text')).toHaveText('Buenos días, Ana');
  // The length was read from the recording's header: four seconds.
  await expect(bubble(page, 1).locator('.vp-voice-time')).toHaveText(/0:04/);
  const state = await playing(page, 1);
  expect(state.error).toBe(0);
  expect(state.src).toBe(pathToFileURL(`${folder}/${RECORDING}`).href);
});
