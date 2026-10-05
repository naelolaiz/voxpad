import { test, expect } from '@playwright/test';
import { readFile } from 'node:fs/promises';

// The viewer is tested by itself: a blank page, the stylesheet, and the module with its export line turned into a
// global. No build and no Python are involved, so a failure here is a failure of voxpad/viewer alone.
const BLANK = '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>viewer</title></head><body></body></html>';
const BUDGET = 25000;
const BIG = { count: 100000, bigDay: 20000 };
const WAIT = { timeout: 15000 };
const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');
const fixture = async () => JSON.parse(await read('../fixtures/viewer-model.json'));
const errors = new WeakMap();

// Nothing here runs Whisper: a step that hangs should fail in seconds, not after the ten minutes the app's tests get.
test.use({ actionTimeout: WAIT.timeout });
test.describe.configure({ timeout: 120000 });

test.beforeEach(({ page }) => {
  const seen = [];
  errors.set(page, seen);
  page.on('pageerror', (error) => seen.push(String(error)));
});

test.afterEach(({ page }) => {
  expect(errors.get(page)).toEqual([]);
});

async function openViewer(page, { host = false } = {}) {
  await page.setContent(BLANK);
  // The app's own stylesheet comes first, as in the bundle, to prove that it cannot restyle the viewer.
  if (host) await page.addStyleTag({ content: await read('../../src/style.css') });
  await page.addStyleTag({ content: await read('../../../voxpad/viewer/viewer.css') });
  const source = await read('../../../voxpad/viewer/viewer.js');
  const script = source.replace(/^export \{([^}]*)\};\s*$/m, 'window.__viewer = {$1};');
  expect(script).not.toBe(source);
  await page.addScriptTag({ type: 'module', content: script });
  await page.waitForFunction(() => window.__viewer, null, WAIT);
  // Remember every observer that watches something and every listener's signal, to see destroy() let go of them.
  await page.evaluate(() => {
    window.__observers = new Set();
    window.__signals = [];
    for (const Kind of [IntersectionObserver, ResizeObserver]) {
      const { observe, disconnect } = Kind.prototype;
      Kind.prototype.observe = function watch(...parameters) { window.__observers.add(this); return observe.apply(this, parameters); };
      Kind.prototype.disconnect = function release() { window.__observers.delete(this); return disconnect.call(this); };
    }
    const { addEventListener } = EventTarget.prototype;
    EventTarget.prototype.addEventListener = function listen(type, handler, options) {
      if (options && options.signal) window.__signals.push(options.signal);
      return addEventListener.call(this, type, handler, options);
    };
  });
}

/**
 * Mount window.__model (or `model`). `audio` says what the host's resolveAudio does: "blob" answers with the Opus
 * fixture, "decoded" only when asked for the decoded copy, "refused" never, and "none" passes no hook at all.
 */
async function mount(page, { model, audio = 'blob', theme = 'light', bucket, width } = {}) {
  const speech = (await read('../fixtures/speech.opus.base64')).trim();
  if (model) await page.evaluate((value) => { window.__model = value; }, model);
  await page.evaluate((setup) => {
    document.body.style.margin = '0';
    const root = document.createElement('div');
    root.id = 'root';
    root.style.setProperty('--vp-height', '100vh');
    if (setup.width) root.style.width = `${setup.width}px`;
    (document.querySelector('.results') || document.body).append(root);
    const bytes = Uint8Array.from(atob(setup.speech), (character) => character.charCodeAt(0));
    const good = () => URL.createObjectURL(new Blob([bytes], { type: 'audio/ogg' }));
    const bad = () => URL.createObjectURL(new Blob([new Uint8Array(4096).fill(7)], { type: 'audio/ogg' }));
    window.__speech = `data:audio/ogg;base64,${setup.speech}`;
    window.__calls = [];
    window.__silent = [];
    window.__events = [];
    const options = { theme: setup.theme, bucket: setup.bucket, onEvents: (events) => window.__events.push(events) };
    if (setup.audio !== 'none') {
      options.resolveAudio = async (src, { decoded }) => {
        window.__calls.push({ src, decoded });
        window.__silent.push(root.querySelector('audio').paused);
        if (setup.audio === 'blob') return good();
        if (setup.audio === 'decoded') return decoded ? good() : bad();
        return decoded ? null : bad();
      };
    }
    window.__root = root;
    window.__handle = window.__viewer.mountViewer(root, window.__model, options);
  }, { audio, theme, bucket, width, speech });
}

/** Runs in the page: a two-person chat of `count` messages over about three years, `bigDay` of them on one day. */
function makeBigModel({ count, bigDay }) {
  const words = 'alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu'.split(' ');
  let seed = 12345;
  const random = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };
  const pad = (value) => String(value).padStart(2, '0');
  const days = 1100;
  const start = Date.UTC(2023, 0, 1);
  const messages = new Array(count);
  const sentence = (length) => {
    let text = '';
    for (let word = 0; word < length; word += 1) text += (word ? ' ' : '') + words[Math.floor(random() * words.length)];
    return text;
  };
  for (let i = 0; i < count; i += 1) {
    let day = Math.floor(i / count * days);
    if (i >= count / 2 && i < count / 2 + bigDay) day = Math.floor(days / 2);
    const chance = random();
    const length = chance < 0.6 ? 1 + Math.floor(random() * 8) : chance < 0.95 ? 8 + Math.floor(random() * 40) : 60 + Math.floor(random() * 200);
    const date = new Date(start + day * 86400000);
    const time = `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(Math.floor(i % 1440 / 60))}:${pad(i % 60)}:00`;
    const sender = random() < 0.5 ? 0 : 1;
    const found = i % 997 === 500;
    // Message 55335 is a match inside a long, collapsed transcript.
    if (i % 50 === 7 || i === 55335) {
      const spoken = sentence(i === 55335 ? 150 : length) + (found ? ' zebra' : '');
      messages[i] = { time, sender, kind: 'voice', text: '', words: 0, voice: { file: `PTT-${i}.opus`, src: `audio/PTT-${i}.opus`, seconds: 5 + i % 90, status: 'ok', text: spoken, words: spoken.split(' ').length } };
    } else {
      const text = sentence(length) + (found ? ' zebra' : '');
      messages[i] = { time, sender, kind: 'text', text, words: text.split(' ').length };
    }
  }
  const bigDate = messages[count / 2].time.slice(0, 10);
  window.__model = {
    schema: 1, title: 'Ana · José', date_order: 'DMY', date_order_ambiguous: false, participants: ['Ana', 'José'], messages,
    events: [{ date: bigDate, label: 'The long day' }], warnings: [],
  };
  return bigDate;
}

const settle = (page) => page.evaluate(() => new Promise((resolve) => {
  let left = 4;
  const frame = () => { left -= 1; if (left) requestAnimationFrame(frame); else setTimeout(resolve, 0); };
  requestAnimationFrame(frame);
}));
const elementCount = (page) => page.evaluate(() => window.__root.querySelectorAll('*').length);
const tab = (page, name) => page.getByRole('tab', { name: new RegExp(`^${name}`) });
const bubble = (page, index) => page.locator(`.vp-msg[data-vp-index="${index}"]`);
const chartKeys = (page) => page.locator('.vp-chart-key');
const chartStatus = (page) => page.locator('.vp-chart-status');
const markers = (page) => page.locator('.vp-marker');

/** The middle of one bucket in one of the four charts, in page coordinates, with the width of a bucket. */
const bucketPoint = (page, chart, index) => page.evaluate(([which, at]) => {
  const box = window.__root.querySelectorAll('.vp-chart-plot')[which].getBoundingClientRect();
  const step = box.width / (Number(window.__root.querySelector('.vp-chart-key').max) + 1);
  return { x: box.left + (at + 0.5) * step, y: box.top + box.height / 2, step };
}, [chart, index]);

/** What the charts draw: per path matching `selector`, how many marks it holds (every bar, band and dot starts with a move). */
const marks = (page, selector) => page.evaluate((query) => [...window.__root.querySelectorAll(query)].map((node) => (node.getAttribute('d').match(/M/g) || []).length), selector);

/** Runs in the page: two people who write on the first and on the last of `days` days, and on every tenth in between. */
function makeSpanModel({ days, start = '2025-01-06' }) {
  const [year, month, day] = start.split('-').map(Number);
  const pad = (value) => String(value).padStart(2, '0');
  const messages = [];
  for (let at = 0; at < days; at += 1) {
    if (at % 10 && at !== days - 1) continue;
    const date = new Date(Date.UTC(year, month - 1, day + at));
    const stamp = `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
    messages.push({ time: `${stamp}T10:00:00`, sender: 0, kind: 'text', text: 'uno dos tres', words: 3 }, { time: `${stamp}T10:05:00`, sender: 1, kind: 'text', text: 'vale', words: 1 });
  }
  window.__model = { schema: 1, title: 'Ana · José', date_order: 'DMY', date_order_ambiguous: false, participants: ['Ana', 'José'], messages, events: [], warnings: [] };
  return messages.length;
}
const audioState = (page) => page.evaluate(() => {
  const audio = window.__root.querySelector('audio');
  return { paused: audio.paused, time: audio.currentTime, src: audio.currentSrc, error: audio.error ? audio.error.code : 0, calls: window.__calls };
});

/** The first bubble that can be seen below the day header stuck to the top of the conversation, and where it starts. */
const reading = (page) => page.evaluate(() => {
  const scroller = window.__root.querySelector('.vp-messages');
  const top = scroller.getBoundingClientRect().top;
  const stuck = [...scroller.querySelectorAll('.vp-day')].find((day) => Math.abs(day.getBoundingClientRect().top - top) < 1);
  const line = top + (stuck ? stuck.getBoundingClientRect().height : 0);
  for (const row of scroller.querySelectorAll('.vp-msg')) {
    const box = row.getBoundingClientRect();
    if (box.bottom > line) return { index: Number(row.dataset.vpIndex), offset: Math.round(box.top - top) };
  }
  return null;
});

/** The same message at the top edge, give or take the pixel a scroll position is rounded to. */
function expectSamePlace(actual, expected) {
  expect(actual.index).toBe(expected.index);
  expect(Math.abs(actual.offset - expected.offset)).toBeLessThanOrEqual(1);
}

/** A window tall enough for the whole fixture, so that every bubble is in the document at once. */
const tall = (page) => page.setViewportSize({ width: 1000, height: 3600 });

async function play(page, index) {
  await bubble(page, index).locator('.vp-play').click();
  await page.waitForFunction(() => { const audio = window.__root.querySelector('audio'); return !audio.paused && audio.currentTime > 0.2; }, null, WAIT);
}

test('tabs follow the ARIA pattern and every control has a name', async ({ page }) => {
  await openViewer(page);
  await mount(page, { model: await fixture() });
  const names = ['Conversation', 'Voice messages', 'Activity', 'Events'];
  await expect(page.getByRole('tablist')).toHaveAttribute('aria-label', /.+/);
  await expect(page.getByRole('tab')).toHaveCount(4);
  const selected = async (name) => {
    for (const other of names) {
      await expect(tab(page, other)).toHaveAttribute('aria-selected', String(other === name));
      await expect(tab(page, other)).toHaveAttribute('tabindex', other === name ? '0' : '-1');
      const panel = page.locator(`#${await tab(page, other).getAttribute('aria-controls')}`);
      await expect(panel).toHaveAttribute('role', 'tabpanel');
      await expect(panel).toHaveAttribute('aria-labelledby', await tab(page, other).getAttribute('id'));
      if (other === name) await expect(panel).toBeVisible(); else await expect(panel).toBeHidden();
    }
    await expect(tab(page, name)).toBeFocused();
  };
  await tab(page, 'Conversation').focus();
  await selected('Conversation');
  for (const [key, name] of [['ArrowRight', 'Voice messages'], ['ArrowRight', 'Activity'], ['End', 'Events'], ['ArrowRight', 'Conversation'],
    ['ArrowLeft', 'Events'], ['ArrowLeft', 'Activity'], ['Home', 'Conversation']]) {
    await page.keyboard.press(key);
    await selected(name);
  }
  // Start a recording so that the now-playing strip is part of the check, and select an event so that the strip
  // pinned under the charts is too; then look at every view.
  await play(page, 4);
  await page.evaluate(() => window.__handle.showMessage(11));
  await page.locator('.vp-banner', { hasText: 'Trip planning call' }).click();
  for (const name of names) {
    await tab(page, name).click();
    if (name === 'Activity') {
      await expect(page.getByRole('region', { name: 'Selected event' })).toBeVisible();
      // With the tooltip of a bucket that has events on show.
      await page.locator('.vp-chart-key').first().focus();
      for (let press = 0; press < 5; press += 1) await page.keyboard.press('ArrowRight');
      await expect(page.locator('.vp-chart-tip .vp-tip-label')).toHaveCount(2);
    }
    const report = await page.evaluate(() => {
      const visible = (node) => node.getClientRects().length > 0;
      const nameOf = (node) => (node.getAttribute('aria-label') || (node.labels && node.labels[0] ? node.labels[0].textContent : '') || node.textContent || '').trim();
      const controls = [...window.__root.querySelectorAll('button, select, input, summary')].filter(visible);
      return {
        count: controls.length,
        unnamed: controls.filter((node) => !nameOf(node)).map((node) => node.className),
        downloads: controls.map(nameOf).filter((name) => /Download (text|JSON)/i.test(name)),
        custom: [...window.__root.querySelectorAll('[role="button"], [onclick], a')].length,
        icons: [...window.__root.querySelectorAll('svg')].filter((node) => node.getAttribute('aria-hidden') !== 'true').length,
        undirected: [...window.__root.querySelectorAll('.vp-text, .vp-transcript, .vp-excerpt, .vp-system-text, .vp-banner-label, .vp-event-label, .vp-pin-label, .vp-tip-label')].filter((node) => node.dir !== 'auto').length,
        names: [...window.__root.querySelectorAll('.vp-name, .vp-legend-name, .vp-tip-name')].filter((node) => node.tagName !== 'BDI' && !node.classList.contains('vp-everyone')).length,
        // Nothing but native controls is a tab stop, apart from the lists and tables that scroll.
        focusable: [...window.__root.querySelectorAll('[tabindex]')].filter((node) => node.tabIndex >= 0 && !/^(BUTTON|INPUT|SELECT)$/.test(node.tagName) && !node.matches('.vp-scroll, .vp-table-wrap')).length,
      };
    });
    expect(report.count).toBeGreaterThan(5);
    expect(report).toMatchObject({ unnamed: [], downloads: [], custom: 0, icons: 0, undirected: 0, names: 0, focusable: 0 });
  }
});

test('the conversation shows every kind of message, its marks and the events of each day', async ({ page }) => {
  await tall(page);
  await openViewer(page);
  const model = await fixture();
  await mount(page, { model });
  await expect(page.locator('.vp-title')).toHaveText('Ana · José');
  await expect(page.locator('.vp-summary')).toContainText('35 messages');
  await expect(page.locator('.vp-notices')).toContainText('1 notice');
  await expect(page.locator('.vp-msg')).toHaveCount(model.messages.length);
  await expect(bubble(page, 0)).toHaveClass(/vp-system/);
  await expect(bubble(page, 0)).toContainText('end-to-end encrypted');
  // Two people: one side each, the name kept for screen readers only.
  await expect(bubble(page, 1)).toHaveClass(/vp-them/);
  await expect(bubble(page, 2)).toHaveClass(/vp-me/);
  await expect(bubble(page, 3)).toHaveClass(/vp-follow/);
  await expect(bubble(page, 2).locator('bdi.vp-name')).toHaveText('Ana');
  await expect(bubble(page, 5).locator('.vp-edited')).toHaveText('Edited');
  await expect(bubble(page, 5).locator('.vp-time')).toHaveText('09:10');
  await expect(bubble(page, 6).locator('.vp-chip')).toHaveText('Photo');
  await expect(bubble(page, 6).locator('.vp-text')).toHaveText('La vista desde la oficina');
  await expect(bubble(page, 8).locator('.vp-chip')).toHaveText('Sticker');
  await expect(bubble(page, 9)).toContainText('This message was deleted');
  await expect(bubble(page, 10).locator('.vp-text')).toHaveText('Shopping list:\n- pan\n- queso\n- tomates');
  await expect(bubble(page, 16).locator('.vp-chip')).toHaveText('Video');
  await expect(bubble(page, 17).locator('.vp-chip')).toHaveText('Document·Itinerary Lisbon.pdf');
  await expect(bubble(page, 18).locator('.vp-chip')).toHaveText('Media not included');
  await expect(bubble(page, 19).locator('.vp-chip')).toHaveText('Contact·Li Wei.vcf');
  await expect(bubble(page, 20).locator('.vp-chip')).toHaveText('GIF');
  await expect(bubble(page, 23).locator('.vp-time')).toHaveCount(0);
  await expect(bubble(page, 26).locator('.vp-text')).toHaveText('<b>not bold</b> & "quoted" \'single\'');
  // Voice bubbles: a player where there is a recording, the transcript, its words and the status.
  await expect(bubble(page, 4).getByRole('button', { name: /^Play voice message from Ana/ })).toBeVisible();
  await expect(bubble(page, 4).getByRole('slider')).toHaveAttribute('aria-valuetext', '0:00 of 0:04');
  await expect(bubble(page, 4).locator('.vp-voice-time')).toHaveText('0:04');
  await expect(bubble(page, 4).locator('.vp-voice-status')).toHaveText('10 spoken words · en');
  await expect(bubble(page, 11).locator('.vp-transcript')).toHaveClass(/vp-clamp/);
  await bubble(page, 11).getByRole('button', { name: 'Show more' }).click();
  await expect(bubble(page, 11).locator('.vp-transcript')).not.toHaveClass(/vp-clamp/);
  await expect(bubble(page, 11).getByRole('button', { name: 'Show less' })).toBeFocused();
  await expect(bubble(page, 12).locator('.vp-voice-status')).toHaveText('Not transcribed yet');
  await expect(bubble(page, 13).locator('.vp-voice-status')).toHaveText('Transcription failed: The recording could not be decoded.');
  await expect(bubble(page, 13).getByRole('slider')).toBeDisabled();
  await expect(bubble(page, 15).locator('.vp-voice-status')).toHaveText('No speech detected');
  await expect(bubble(page, 21).locator('.vp-voice-status')).toHaveText('Recording not included');
  await expect(bubble(page, 21).locator('.vp-play')).toHaveCount(0);
  await expect(bubble(page, 24).locator('.vp-transcript')).toHaveText('Vale, lo miro mañana y te digo algo.');
  await expect(bubble(page, 24).locator('.vp-text')).toHaveText('Escucha esto');
  // Events are numbered by date; the one outside the chat and the one after it have no banner, and an event
  // on a day without messages waits on the next day that has some, with its date.
  await expect(page.locator('.vp-banner .vp-event-number')).toHaveText(['2', '3', '4', '5']);
  await expect(page.locator('.vp-banner').nth(3)).toContainText('25 Jan 2026');
  await expect(page.locator('.vp-banner').nth(3)).toContainText('Ana away, no signal');
  const days = await page.locator('.vp-day-label').allTextContents();
  expect(days).toHaveLength(7);
  expect(days[0]).toMatch(/Monday,? 5 January 2026/);
  // Nothing is loaded or asked for before somebody presses play.
  expect(await page.evaluate(() => { const audio = window.__root.querySelector('audio'); return [audio.preload, audio.getAttribute('src'), audio.networkState, window.__calls.length]; }))
    .toEqual(['none', null, 0, 0]);
  expect(await page.evaluate(() => Boolean(window.__root.querySelector('.vp-player').closest('.vp-scroll')))).toBe(false);
});

test('a group chat names each sender in a colour of its own', async ({ page }) => {
  await openViewer(page);
  const model = await fixture();
  model.participants = ['Ana', 'José', 'Li'];
  model.messages.forEach((message, at) => { if (message.sender != null) message.sender = at % 3; });
  await mount(page, { model });
  await expect(page.locator('.vp-me')).toHaveCount(0);
  await expect(page.getByLabel('Viewing as')).toBeHidden();
  const colours = await page.evaluate(() => Object.fromEntries([...window.__root.querySelectorAll('.vp-msg .vp-name:not(.vp-sr)')]
    .map((node) => [node.textContent, getComputedStyle(node).color])));
  expect(Object.keys(colours).sort()).toEqual(['Ana', 'José', 'Li']);
  expect(new Set(Object.values(colours)).size).toBe(3);
});

test('search, filters and "viewing as" work on the model and keep the place', async ({ page }) => {
  await tall(page);
  await openViewer(page);
  await mount(page, { model: await fixture() });
  const search = page.getByRole('searchbox', { name: 'Search messages' });
  const status = page.locator('.vp-panel-conversation .vp-find .vp-status');
  await expect(status).toHaveAttribute('role', 'status');
  await search.fill('VUELOS');
  await expect(status).toHaveText('1 of 2 matches');
  // The first match is at the end of a collapsed transcript: going there opens it.
  await expect(bubble(page, 11)).toHaveClass(/vp-current/);
  await expect(bubble(page, 11).locator('.vp-transcript')).not.toHaveClass(/vp-clamp/);
  await expect(bubble(page, 11).locator('mark')).toHaveText('vuelos');
  await expect(bubble(page, 11).locator('mark')).toBeInViewport();
  await page.getByRole('button', { name: 'Next match' }).click();
  await expect(status).toHaveText('2 of 2 matches');
  await expect(bubble(page, 11)).not.toHaveClass(/vp-current/);
  // The second match is in a text whose lower-case form has another length ("İ" becomes two characters), so the
  // places of the match cannot be trusted: the bubble is outlined instead of marked.
  await expect(bubble(page, 22)).toHaveClass(/vp-current/);
  await expect(bubble(page, 22)).toHaveClass(/vp-match/);
  await expect(bubble(page, 22).locator('mark')).toHaveCount(0);
  await page.getByRole('button', { name: 'Next match' }).click();
  await expect(status).toHaveText('1 of 2 matches');
  await page.getByRole('button', { name: 'Previous match' }).click();
  await expect(status).toHaveText('2 of 2 matches');
  await search.press('Shift+Enter');
  await expect(status).toHaveText('1 of 2 matches');
  await search.fill('tomorrow');
  await expect(status).toHaveText('1 of 2 matches');
  await expect(bubble(page, 4).locator('.vp-transcript mark')).toHaveText('tomorrow');
  await expect(bubble(page, 5).locator('.vp-text mark')).toHaveText('tomorrow');
  await search.fill('next SPRING');
  await expect(status).toHaveText('1 of 1 match');
  await expect(bubble(page, 22)).toHaveClass(/vp-match/);
  // A query is text, not a pattern.
  await search.fill('.*');
  await expect(status).toHaveText('No matches');
  await expect(page.getByRole('button', { name: 'Next match' })).toBeDisabled();
  await search.fill('(too');
  await expect(status).toHaveText('No matches');
  await search.fill('');
  await expect(status).toBeHidden();

  const from = page.locator('.vp-panel-conversation').getByLabel('From', { exact: true });
  await from.selectOption({ label: 'José' });
  await expect(page.locator('.vp-msg')).toHaveCount(16);
  await page.getByLabel('Voice only').check();
  await expect(page.locator('.vp-msg')).toHaveCount(5);
  await search.fill('vale');
  await expect(status).toHaveText('1 of 1 match');
  await search.fill('');
  await from.selectOption({ label: 'Ana' });
  await expect(page.locator('.vp-msg')).toHaveCount(3);
  // showMessage drops the filters that hide its target.
  expect(await page.evaluate(() => window.__handle.showMessage(1))).toBe(true);
  await expect(page.locator('.vp-msg')).toHaveCount(35);
  await expect(page.getByLabel('Voice only')).not.toBeChecked();
  await expect(bubble(page, 1)).toBeFocused();

  await expect(bubble(page, 1)).toHaveClass(/vp-them/);
  await page.getByLabel('Viewing as').selectOption({ label: 'José' });
  await expect(bubble(page, 1)).toHaveClass(/vp-me/);
  await expect(bubble(page, 2)).toHaveClass(/vp-them/);

  // A day without messages opens the next one that has some, and says so.
  await page.setViewportSize({ width: 1000, height: 600 });
  await settle(page);
  await page.getByLabel('Jump to date').fill('2026-01-12');
  await expect(page.locator('.vp-jump-note')).toContainText('18 Jan 2026');
  expect((await reading(page)).index).toBe(21);
  await page.getByLabel('Jump to date').fill('2026-01-19');
  await expect(page.locator('.vp-jump-note')).toBeHidden();
  expect((await reading(page)).index).toBe(25);

  // The message being read is the anchor, not the one hidden behind the day header above it: it stays in place when
  // a filter that keeps it is switched off again.
  await page.getByLabel('Voice only').check();
  await page.evaluate(() => window.__handle.showMessage(15));
  expect((await reading(page)).index).toBe(15);
  await page.getByLabel('Voice only').uncheck();
  await settle(page);
  expect((await reading(page)).index).toBe(15);
});

test('scrolling to the very end shows the last message, however wrong the estimated heights were', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 600 });
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await expect(bubble(page, 1)).toBeVisible();
  await expect(bubble(page, 34)).toHaveCount(0);
  await page.evaluate(() => { const scroller = window.__root.querySelector('.vp-messages'); scroller.scrollTop = scroller.scrollHeight; });
  await expect(bubble(page, 34)).toBeInViewport({ ratio: 1 });
  await settle(page);
  expect(await page.evaluate(() => { const scroller = window.__root.querySelector('.vp-messages'); return scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop; })).toBeLessThanOrEqual(1);
  await expect(bubble(page, 1)).toHaveCount(0);
});

test('a 100 000-message chat with a 20 000-message day stays inside the element budget', async ({ page }) => {
  await openViewer(page);
  const bigDay = await page.evaluate(makeBigModel, BIG);
  const started = Date.now();
  await mount(page);
  await expect(page.locator('.vp-msg').first()).toBeVisible();
  expect(Date.now() - started).toBeLessThan(5000);
  await settle(page);
  expect(await page.evaluate(() => window.__model.messages.filter((message) => message.time.startsWith(window.__model.events[0].date)).length)).toBe(BIG.bigDay);
  const units = await page.evaluate(() => {
    const all = [...window.__root.querySelectorAll('.vp-messages .vp-unit')];
    return { count: all.length, filled: all.filter((unit) => unit.childElementCount).length, largest: Math.max(...all.map((unit) => unit.querySelectorAll('.vp-msg').length)),
      display: getComputedStyle(all[0]).display, placeholder: all.at(-1).style.height, anchoring: getComputedStyle(window.__root.querySelector('.vp-messages')).overflowAnchor };
  });
  // 880 days with messages, and the long day alone is 134 units of at most 150 messages.
  expect(units.count).toBe(1014);
  expect(units.filled).toBeLessThan(8);
  expect(units.largest).toBeLessThanOrEqual(150);
  expect(units).toMatchObject({ display: 'flow-root', anchoring: 'none' });
  expect(units.placeholder).toMatch(/^[0-9.]+px$/);
  expect(await elementCount(page)).toBeLessThan(BUDGET);

  // Jump to the long day: its first message arrives under the day's header and the event of that day.
  await page.getByLabel('Jump to date').fill(bigDay);
  await settle(page);
  expect(await reading(page)).toMatchObject({ index: BIG.count / 2 });
  await expect(page.locator('.vp-banner')).toContainText('The long day');
  await expect(page.locator('.vp-banner')).toBeInViewport();
  expect(await elementCount(page)).toBeLessThan(BUDGET);

  // Scrolling back fills units above the reading position, each of another height than its placeholder; a bubble
  // being watched must move by the step alone, and one that was on screen cannot have been emptied.
  let filledAbove = 0;
  for (let step = 0; step < 30; step += 1) {
    const moved = await page.evaluate(async () => {
      const scroller = window.__root.querySelector('.vp-messages');
      const top = scroller.getBoundingClientRect().top;
      const watched = [...scroller.querySelectorAll('.vp-msg')].find((row) => row.getBoundingClientRect().top - top > 40);
      const before = watched.getBoundingClientRect().top;
      const first = () => Number(scroller.querySelector('.vp-msg').dataset.vpIndex);
      const earliest = first();
      scroller.scrollTop -= 300;
      for (let frame = 0; frame < 4; frame += 1) await new Promise((resolve) => { requestAnimationFrame(resolve); });
      await new Promise((resolve) => { setTimeout(resolve, 0); });
      return { connected: watched.isConnected, by: watched.getBoundingClientRect().top - before, filled: first() < earliest };
    });
    expect(moved.connected).toBe(true);
    expect(Math.abs(moved.by - 300)).toBeLessThanOrEqual(2);
    if (moved.filled) filledAbove += 1;
  }
  expect(filledAbove).toBeGreaterThan(0);
  expect((await reading(page)).index).toBeLessThan(BIG.count / 2);
  expect(await elementCount(page)).toBeLessThan(BUDGET);

  // Twenty steps through the matches of a search: each one is brought into view, marked, and counted from the model.
  await page.getByLabel('Jump to date').fill(bigDay);
  const status = page.locator('.vp-panel-conversation .vp-find .vp-status');
  await page.getByRole('searchbox', { name: 'Search messages' }).fill('zebra');
  await expect(status).toHaveText('51 of 100 matches');
  for (let step = 0; step < 20; step += 1) {
    const expected = 500 + 997 * (50 + step);
    const current = await page.evaluate(() => {
      const scroller = window.__root.querySelector('.vp-messages');
      const view = scroller.getBoundingClientRect();
      const rows = [...scroller.querySelectorAll('.vp-current')];
      const mark = rows.length === 1 ? rows[0].querySelector('mark') : null;
      const box = mark ? mark.getBoundingClientRect() : null;
      return { rows: rows.length, index: rows.length ? Number(rows[0].dataset.vpIndex) : -1, mark: mark ? mark.textContent : '',
        visible: Boolean(box && box.top >= view.top && box.bottom <= view.bottom), elements: window.__root.querySelectorAll('*').length,
        filled: scroller.querySelectorAll('.vp-unit:not(:empty)').length };
    });
    expect(current).toMatchObject({ rows: 1, index: expected, mark: 'zebra', visible: true });
    expect(current.elements).toBeLessThan(BUDGET);
    // Units filled for one jump do not pile up behind the next.
    expect(current.filled).toBeLessThanOrEqual(5);
    if (expected === 55335) await expect(bubble(page, expected).getByRole('button', { name: 'Show less' })).toBeVisible();
    await page.getByRole('button', { name: 'Next match' }).click();
    await expect(status).toHaveText(`${52 + step} of 100 matches`);
  }
  await settle(page);
  expect(await elementCount(page)).toBeLessThan(BUDGET);

  // The voice rows are windowed in the same way.
  await tab(page, 'Voice messages').click();
  await expect(page.locator('.vp-panel-voice .vp-find .vp-status')).toHaveText('2,001 voice messages');
  await settle(page);
  expect(await page.locator('.vp-row').count()).toBeLessThanOrEqual(450);
  expect(await page.evaluate(() => window.__root.querySelectorAll('.vp-messages .vp-msg').length)).toBe(0);
  const wordiest = await page.evaluate(() => window.__model.messages.reduce((best, message, index, all) => (message.voice && (best < 0 || message.voice.words > all[best].voice.words) ? index : best), -1));
  await page.getByLabel('Sort').selectOption({ label: 'Most words first' });
  await expect(page.locator('.vp-row').first()).toHaveAttribute('data-vp-index', String(wordiest));
  await page.evaluate(() => { const list = window.__root.querySelector('.vp-voice-list'); list.scrollTop = list.scrollHeight; });
  await settle(page);
  await expect(page.locator('.vp-row').last()).toBeInViewport();
  expect(await elementCount(page)).toBeLessThan(BUDGET);

  // showMessage reaches the very end at once, and an index outside the chat is refused.
  expect(await page.evaluate(() => window.__handle.showMessage(99999))).toBe(true);
  await expect(bubble(page, 99999)).toBeInViewport();
  await expect(tab(page, 'Conversation')).toHaveAttribute('aria-selected', 'true');
  expect(await page.evaluate(() => [window.__handle.showMessage(100000), window.__handle.showMessage(-1), window.__handle.showMessage(1.5)])).toEqual([false, false, false]);
  await settle(page);
  expect(await elementCount(page)).toBeLessThan(BUDGET);
});

test('the reading position survives a tab switch, a filter, another width and update()', async ({ page }) => {
  await openViewer(page);
  await page.evaluate(makeBigModel, BIG);
  await mount(page);
  await page.evaluate(() => window.__handle.showMessage(40003));
  await page.evaluate(() => { window.__root.querySelector('.vp-messages').scrollTop += 137; });
  await settle(page);
  const before = await reading(page);
  expect(before.index).toBeGreaterThanOrEqual(40003);
  expect(before.index).toBeLessThan(40010);

  await tab(page, 'Activity').click();
  await settle(page);
  expect(await page.evaluate(() => window.__root.querySelectorAll('.vp-msg').length)).toBe(0);
  // Coming back, the bubbles are there in the same task, not after a frame of empty placeholders.
  expect(await page.evaluate(() => {
    window.__root.querySelector('[data-vp-tab="conversation"]').click();
    return window.__root.querySelectorAll('.vp-msg').length;
  })).toBeGreaterThan(0);
  expectSamePlace(await reading(page), before);
  await settle(page);
  expectSamePlace(await reading(page), before);

  await page.evaluate(() => window.__handle.update(window.__model));
  await settle(page);
  expectSamePlace(await reading(page), before);

  // Another width wraps the bubbles differently: the same message stays at the top edge.
  await page.setViewportSize({ width: 700, height: 720 });
  await settle(page);
  await settle(page);
  expectSamePlace(await reading(page), before);
  await page.setViewportSize({ width: 1280, height: 720 });
  await settle(page);
  await settle(page);
  expectSamePlace(await reading(page), before);

  // With a filter the message itself may be gone: the next one that is left takes its place.
  const sender = await page.evaluate((index) => window.__model.messages[index].sender, before.index);
  await page.locator('.vp-panel-conversation').getByLabel('From', { exact: true }).selectOption(String(1 - sender));
  await settle(page);
  const filtered = await reading(page);
  expect(filtered.index).toBeGreaterThan(before.index);
  expect(filtered.index).toBeLessThan(before.index + 40);
  expect(await page.evaluate((wanted) => [...window.__root.querySelectorAll('.vp-msg')].every((row) => window.__model.messages[Number(row.dataset.vpIndex)].sender === wanted), 1 - sender)).toBe(true);
});

test('a voice bubble plays the recording from a blob URL, and update() never touches the playback', async ({ page }) => {
  await tall(page);
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await play(page, 4);
  let state = await audioState(page);
  expect(state.calls).toEqual([{ src: 'audio/PTT-20260105-WA0001.opus', decoded: false }]);
  expect(state.src).toMatch(/^blob:/);
  await expect(bubble(page, 4).getByRole('button', { name: /^Pause voice message from Ana/ })).toBeVisible();
  await expect(bubble(page, 4).locator('.vp-voice')).toHaveClass(/vp-playing/);
  const strip = page.getByRole('group', { name: 'Now playing' });
  await expect(strip).toBeVisible();
  await expect(strip.locator('.vp-name')).toHaveText('Ana');
  await expect(strip.getByLabel('Speed')).toHaveValue('1');
  await expect(bubble(page, 4).getByRole('slider')).toHaveAttribute('aria-valuetext', /^0:0[0-4] of 0:04$/);
  // The fixture lasts four seconds; let it go round while the test looks at it.
  await page.evaluate(() => {
    const audio = window.__root.querySelector('audio');
    audio.loop = true;
    window.__audio = audio;
    window.__disturbed = [];
    for (const type of ['pause', 'emptied', 'loadstart', 'abort', 'error']) audio.addEventListener(type, () => window.__disturbed.push(type));
    window.__rows = [window.__root.querySelector('.vp-msg[data-vp-index="4"]'), window.__root.querySelector('.vp-msg[data-vp-index="12"]'), window.__root.querySelector('.vp-msg[data-vp-index="2"]')];
  });

  // One transcript arrives: only that bubble is drawn again.
  await page.evaluate(() => {
    Object.assign(window.__model.messages[12].voice, { status: 'ok', text: 'Recién transcrito ahora', words: 3, languages: ['es'] });
    window.__handle.update(window.__model, [12]);
  });
  await expect(bubble(page, 12).locator('.vp-transcript')).toHaveText('Recién transcrito ahora');
  await expect(bubble(page, 12).locator('.vp-voice-status')).toHaveText('3 spoken words · es');
  expect(await page.evaluate(() => [4, 12, 2].map((index, at) => window.__root.querySelector(`.vp-msg[data-vp-index="${index}"]`) === window.__rows[at]))).toEqual([true, false, true]);
  // The bubble that is playing changes too, and a status may go backwards.
  await page.evaluate(() => {
    Object.assign(window.__model.messages[4].voice, { status: 'pending', text: '', words: 0 });
    delete window.__model.messages[4].voice.languages;
    window.__handle.update(window.__model, [4]);
  });
  await expect(bubble(page, 4).locator('.vp-voice-status')).toHaveText('Not transcribed yet');
  await expect(bubble(page, 4).getByRole('button', { name: /^Pause voice message/ })).toBeVisible();
  // A whole new model object of the same conversation.
  await page.evaluate(() => {
    window.__model = JSON.parse(JSON.stringify(window.__model));
    window.__model.title = 'Renamed';
    Object.assign(window.__model.messages[4].voice, { status: 'ok', text: 'Hello again.', words: 2 });
    window.__handle.update(window.__model);
  });
  await expect(page.locator('.vp-title')).toHaveText('Renamed');
  await expect(bubble(page, 4).locator('.vp-transcript')).toHaveText('Hello again.');
  await expect(bubble(page, 4).getByRole('button', { name: /^Pause voice message/ })).toBeVisible();
  await tab(page, 'Voice messages').click();
  await expect(page.locator('.vp-row[data-vp-index="4"]')).toHaveClass(/vp-playing/);
  await expect(page.locator('.vp-row[data-vp-index="12"] .vp-excerpt')).toHaveText('Recién transcrito ahora');
  await tab(page, 'Activity').click();
  await expect(page.getByRole('table', { name: 'Activity per participant' }).locator('tbody tr').nth(1)).toContainText('3 of 5');
  state = await audioState(page);
  expect(state).toMatchObject({ paused: false, error: 0 });
  expect(state.calls).toHaveLength(1);
  expect(await page.evaluate(() => [window.__root.querySelector('audio') === window.__audio, window.__disturbed, window.__root.querySelectorAll('audio').length])).toEqual([true, [], 1]);

  // Speed, seeking and pausing act on the one shared element.
  await strip.getByLabel('Speed').selectOption('2');
  expect(await page.evaluate(() => window.__audio.playbackRate)).toBe(2);
  await strip.getByRole('button', { name: /^Pause voice message/ }).click();
  await expect(strip.getByRole('button', { name: /^Play voice message/ })).toBeVisible();
  expect((await audioState(page)).paused).toBe(true);
  await strip.getByRole('slider').fill('3');
  await expect.poll(async () => (await audioState(page)).time).toBeCloseTo(3, 1);
  await strip.getByRole('button', { name: 'Show in conversation' }).click();
  await expect(tab(page, 'Conversation')).toHaveAttribute('aria-selected', 'true');
  await expect(bubble(page, 4).getByRole('slider')).toHaveAttribute('aria-valuetext', '0:03 of 0:04');
  await strip.getByRole('button', { name: 'Stop and close the player' }).click();
  await expect(strip).toBeHidden();
  await expect(bubble(page, 4).getByRole('button', { name: /^Play voice message/ })).toBeVisible();

  await expect(page.evaluate(() => window.__handle.update({ ...window.__model, messages: window.__model.messages.slice(1) }))).rejects.toThrow(/RangeError/);
});

test('playback goes on while its bubble is emptied, and a bubble filled again shows it', async ({ page }) => {
  await openViewer(page);
  await page.evaluate(makeBigModel, BIG);
  await mount(page);
  await page.evaluate(() => window.__handle.showMessage(50007));
  await play(page, 50007);
  await page.evaluate(() => { window.__audio = window.__root.querySelector('audio'); window.__audio.loop = true; });
  await page.evaluate(() => window.__handle.showMessage(3));
  await settle(page);
  await expect(bubble(page, 50007)).toHaveCount(0);
  const strip = page.getByRole('group', { name: 'Now playing' });
  await expect(strip).toBeVisible();
  const time = (await audioState(page)).time;
  await expect.poll(async () => { const state = await audioState(page); return !state.paused && state.time !== time; }).toBe(true);
  await strip.getByRole('button', { name: 'Show in conversation' }).click();
  await expect(bubble(page, 50007)).toBeInViewport();
  await expect(bubble(page, 50007).getByRole('button', { name: /^Pause voice message/ })).toBeVisible();
  await expect(bubble(page, 50007).locator('.vp-voice')).toHaveClass(/vp-playing/);
  expect(await page.evaluate(() => [window.__root.querySelector('audio') === window.__audio, window.__audio.paused, window.__calls.length])).toEqual([true, false, 1]);
  // Paused, no event repaints anything: a bubble takes the position over at the moment it is filled.
  await strip.getByRole('button', { name: /^Pause voice message/ }).click();
  await strip.getByRole('slider').fill('3');
  await page.evaluate(() => window.__handle.showMessage(3));
  await settle(page);
  await expect(bubble(page, 50007)).toHaveCount(0);
  await strip.getByRole('button', { name: 'Show in conversation' }).click();
  await expect(bubble(page, 50007).getByRole('slider')).toHaveAttribute('aria-valuetext', '0:03 of 0:04');
  await expect(bubble(page, 50007).locator('.vp-voice')).toHaveClass(/vp-active/);
  await bubble(page, 50007).getByRole('button', { name: /^Play voice message from/ }).click();
  await page.waitForFunction(() => !window.__audio.paused, null, WAIT);
  // Another bubble takes over the one element. The first goes back to rest and is silent before the host is even
  // asked for the next recording.
  await play(page, 50057);
  expect(await page.evaluate(() => window.__silent)).toEqual([true, true]);
  await expect(bubble(page, 50007).getByRole('button', { name: /^Play voice message/ })).toBeVisible();
  expect(await page.evaluate(() => [window.__root.querySelectorAll('audio').length, window.__calls.map((call) => call.src)])).toEqual([1, ['audio/PTT-50007.opus', 'audio/PTT-50057.opus']]);
});

test('a recording the browser cannot decode is asked for once more as a decoded copy', async ({ page }) => {
  await openViewer(page);
  await mount(page, { model: await fixture(), audio: 'decoded' });
  await play(page, 4);
  expect((await audioState(page)).calls).toEqual([
    { src: 'audio/PTT-20260105-WA0001.opus', decoded: false }, { src: 'audio/PTT-20260105-WA0001.opus', decoded: true },
  ]);
  await expect(bubble(page, 4).locator('.vp-voice-error')).toBeHidden();
});

test('a recording that cannot be played says so and keeps its transcript', async ({ page }) => {
  await openViewer(page);
  await mount(page, { model: await fixture(), audio: 'refused' });
  await bubble(page, 4).locator('.vp-play').click();
  await expect(bubble(page, 4).locator('.vp-voice-error')).toHaveText('This browser cannot play this recording');
  await expect(bubble(page, 4).locator('.vp-transcript')).toHaveText('Hello, this is a voice message. We will meet tomorrow.');
  await expect(bubble(page, 4).getByRole('button', { name: /^Play voice message/ })).toBeVisible();
  expect((await audioState(page)).calls.map((call) => call.decoded)).toEqual([false, true]);
  await expect(page.getByRole('group', { name: 'Now playing' }).getByRole('status')).toHaveText('This browser cannot play this recording');
});

test('without a host hook the src is the address: it plays, or the bubble says why not', async ({ page }) => {
  await tall(page);
  await openViewer(page);
  const model = await fixture();
  model.messages[11].voice.src = 'data:audio/ogg;base64,AAAA';
  await mount(page, { model, audio: 'none' });
  await page.evaluate(() => { window.__model.messages[4].voice.src = window.__speech; });
  await play(page, 4);
  expect((await audioState(page)).src).toMatch(/^data:audio\/ogg/);
  await bubble(page, 11).locator('.vp-play').click();
  await expect(bubble(page, 11).locator('.vp-voice-error')).toHaveText('This browser cannot play this recording');
  await expect(bubble(page, 11).locator('.vp-transcript')).toContainText('Hola José');
  await expect(bubble(page, 4).getByRole('button', { name: /^Play voice message/ })).toBeVisible();
});

test('the voice messages view lists, sorts, filters and plays in sequence', async ({ page }) => {
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await tab(page, 'Voice messages').click();
  const rows = page.locator('.vp-row');
  // Rows are filled by the observer a frame after the list is rebuilt, so the order is asked for until it is there.
  const order = (expected) => expect.poll(() => rows.evaluateAll((nodes) => nodes.map((node) => Number(node.dataset.vpIndex)))).toEqual(expected);
  await order([4, 11, 12, 13, 15, 21, 24, 31]);
  const head = page.locator('.vp-voice-head');
  await expect(head.locator('.vp-coverage-line')).toHaveText([
    'Spoken words cover 5 of 8 voice messages (1 not transcribed, 1 failed, 1 without a recording).',
    'Voice time covers 5 of 8 voice messages.',
  ]);
  const table = page.getByRole('table', { name: 'Voice messages per participant' });
  await expect(table.locator('thead th')).toHaveText(['Participant', 'Voice notes', 'Voice time', 'Spoken words', 'Words per minute']);
  await expect(table.locator('tbody tr').nth(0).locator('th, td')).toHaveText(['Ana', '3', '3 min 43 s', '138', '37']);
  await expect(table.locator('tbody tr').nth(1).locator('th, td')).toHaveText(['José', '5', '14 s', '8', '–']);
  await expect(rows.nth(0).locator('.vp-name')).toHaveText('Ana');
  await expect(rows.nth(0).locator('.vp-row-date')).toHaveText('5 Jan 2026, 09:05');
  await expect(rows.nth(0).locator('.vp-voice-time')).toHaveText('0:04');
  await expect(rows.nth(0).locator('.vp-row-words')).toHaveText('10 words');
  await expect(rows.nth(1).locator('.vp-excerpt')).toHaveClass(/vp-clamp/);
  await rows.nth(1).getByRole('button', { name: 'Show more' }).click();
  await expect(rows.nth(1).locator('.vp-excerpt')).not.toHaveClass(/vp-clamp/);
  await expect(rows.nth(5).locator('.vp-voice-status')).toHaveText('Recording not included');
  await expect(rows.nth(5).locator('.vp-play')).toHaveCount(0);

  // Recordings of unknown length come last, in the order of the chat.
  await page.getByLabel('Sort').selectOption({ label: 'Longest first' });
  await order([11, 31, 12, 4, 15, 13, 21, 24]);
  await page.getByLabel('Sort').selectOption({ label: 'Shortest first' });
  await order([15, 4, 12, 31, 11, 13, 21, 24]);
  await page.getByLabel('Sort').selectOption({ label: 'Most words first' });
  await order([11, 31, 4, 24, 12, 13, 15, 21]);
  // A transcript that arrives is shown in its row at once, but the row does not move under the reader: the list is
  // put in order again the next time it is opened.
  await page.evaluate(() => {
    Object.assign(window.__model.messages[12].voice, { status: 'ok', text: 'palabra '.repeat(50).trim(), words: 50 });
    window.__handle.update(window.__model, [12]);
  });
  await expect(rows.nth(4).locator('.vp-row-words')).toHaveText('50 words');
  await order([11, 31, 4, 24, 12, 13, 15, 21]);
  await expect(table.locator('tbody tr').nth(1).locator('th, td')).toHaveText(['José', '5', '14 s', '58', '250']);
  await tab(page, 'Activity').click();
  await tab(page, 'Voice messages').click();
  await order([11, 12, 31, 4, 24, 13, 15, 21]);
  await page.getByLabel('Sort').selectOption({ label: 'Newest first' });
  await order([31, 24, 21, 15, 13, 12, 11, 4]);
  await page.locator('.vp-panel-voice').getByLabel('From', { exact: true }).selectOption({ label: 'José' });
  await order([24, 21, 15, 13, 12]);
  await expect(page.locator('.vp-panel-voice .vp-find .vp-status')).toHaveText('5 of 8 voice messages');
  await page.locator('.vp-panel-voice').getByLabel('From', { exact: true }).selectOption({ label: 'Everyone' });
  await page.getByRole('searchbox', { name: 'Search transcripts' }).fill('HERMANA');
  await order([31, 11]);
  await expect(rows.nth(0).locator('mark')).toHaveText('hermana');
  // A collapsed excerpt starts near the match, so the reason a row is listed can be read.
  await expect(rows.nth(1).locator('mark')).toBeVisible();
  await page.getByRole('searchbox', { name: 'Search transcripts' }).fill('');
  await expect(rows).toHaveCount(8);

  // In sequence: when the first recording ends, the next one that can be played starts by itself.
  await page.getByLabel('Sort').selectOption({ label: 'Oldest first' });
  await order([4, 11, 12, 13, 15, 21, 24, 31]);
  await page.getByLabel('Play in sequence').check();
  await rows.nth(0).locator('.vp-play').click();
  await expect(rows.nth(0)).toHaveClass(/vp-active/);
  await expect(rows.nth(1)).toHaveClass(/vp-active/, { timeout: 15000 });
  await expect(rows.nth(0)).not.toHaveClass(/vp-active/);
  expect((await audioState(page)).calls.map((call) => call.src)).toEqual(['audio/PTT-20260105-WA0001.opus', 'audio/PTT-20260110-WA0004.opus']);
  await expect(page.getByRole('group', { name: 'Now playing' }).locator('.vp-player-when')).toHaveText('10 Jan 2026, 10:15');

  await rows.nth(6).getByRole('button', { name: 'Show in conversation' }).click();
  await expect(tab(page, 'Conversation')).toHaveAttribute('aria-selected', 'true');
  await expect(bubble(page, 24)).toBeInViewport();
  await expect(bubble(page, 24)).toBeFocused();
});

test('a view without recordings says so once, and the activity table shows what was transcribed', async ({ page }) => {
  await openViewer(page);
  const model = await fixture();
  for (const message of model.messages) if (message.voice) message.voice.src = null;
  await mount(page, { model, audio: 'none' });
  await expect(page.locator('.vp-messages .vp-play')).toHaveCount(0);
  await expect(bubble(page, 4).locator('.vp-voice-kind')).toHaveText('Voice message');
  await expect(bubble(page, 4).locator('.vp-voice-time')).toHaveText('0:04');
  await tab(page, 'Voice messages').click();
  await expect(page.locator('.vp-row')).toHaveCount(8);
  await expect(page.locator('.vp-row .vp-play')).toHaveCount(0);
  await expect(page.locator('.vp-panel-voice').getByText('Recordings are not included in this view.')).toHaveCount(1);
  await tab(page, 'Activity').click();
  const table = page.getByRole('table', { name: 'Activity per participant' });
  await expect(table.locator('thead th')).toHaveText(['Participant', 'Messages', 'Typed words', 'Spoken words', 'Total words', 'Voice notes', 'Transcribed', 'Voice time']);
  await expect(table.locator('tbody tr').nth(0).locator('th, td')).toHaveText(['Ana', '17', '109', '138', '247', '3', '3 of 3', '3 min 43 s']);
  await expect(table.locator('tbody tr').nth(1).locator('th, td')).toHaveText(['José', '16', '39', '8', '47', '5', '2 of 5', '14 s']);
  await expect(table.locator('tbody tr').nth(2).locator('th, td')).toHaveText(['Everyone', '33', '148', '146', '294', '8', '5 of 8', '3 min 57 s']);
  await expect(page.locator('.vp-panel-activity .vp-coverage-line')).toHaveText([
    'Spoken words cover 5 of 8 voice messages (1 not transcribed, 1 failed, 1 without a recording).',
    'Voice time covers 5 of 8 voice messages.',
  ]);
  // A fully transcribed and timed chat needs no such sentence.
  await page.evaluate(() => {
    for (const message of window.__model.messages) if (message.voice) Object.assign(message.voice, { status: 'ok', seconds: 3, text: 'hola', words: 1 });
    window.__handle.update(window.__model);
  });
  await expect(page.locator('.vp-panel-activity .vp-coverage')).toBeHidden();
  await expect(table.locator('tbody tr').nth(2).locator('th, td')).toHaveText(['Everyone', '33', '148', '8', '156', '8', '8 of 8', '24 s']);
});

test('events belong to the viewer: setEvents and a loaded file renumber every view', async ({ page }) => {
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await expect(tab(page, 'Events')).toContainText('6');
  // The same event is selected in the conversation and in the events list.
  await page.evaluate(() => window.__handle.showMessage(11));
  await page.locator('.vp-banner', { hasText: 'Trip planning call' }).click();
  await expect(tab(page, 'Events')).toHaveAttribute('aria-selected', 'true');
  const cards = page.locator('.vp-event-card');
  await expect(cards).toHaveCount(6);
  await expect(cards.locator('.vp-event-number')).toHaveText(['1', '2', '3', '4', '5', '6']);
  await expect(cards.nth(2)).toHaveAttribute('aria-current', 'true');
  await expect(cards.nth(0)).toContainText('Outside the conversation');
  await expect(cards.nth(5)).toContainText('Outside the conversation');
  await expect(cards.nth(3).locator('.vp-event-label')).toContainText('book the train instead.');
  await cards.nth(1).getByRole('button', { name: 'Open this day' }).click();
  await expect(tab(page, 'Conversation')).toHaveAttribute('aria-selected', 'true');
  expect((await reading(page)).index).toBe(6);
  await expect(page.locator('.vp-banner', { hasText: 'José starts the new job' })).toHaveAttribute('aria-current', 'true');
  await expect(page.locator('.vp-banner[aria-current]')).toHaveCount(1);

  // update() does not bring the model's events back; setEvents replaces them and keeps the selection by content.
  await page.evaluate(() => {
    window.__handle.setEvents([{ date: '2026-01-19', label: 'Passport check' }, { date: '2026-01-06', label: 'José starts the new job' }, { date: 'nonsense', label: 'dropped' }]);
    window.__handle.update(window.__model);
  });
  await expect(tab(page, 'Events')).toContainText('2');
  await expect(page.locator('.vp-banner', { hasText: 'José starts the new job' })).toHaveAttribute('aria-current', 'true');
  await expect(page.locator('.vp-banner', { hasText: 'José starts the new job' }).locator('.vp-event-number')).toHaveText('1');
  await page.evaluate(() => window.__handle.showMessage(25));
  await expect(page.locator('.vp-banner', { hasText: 'Passport check' }).locator('.vp-event-number')).toHaveText('2');
  await expect(page.locator('.vp-banner', { hasText: 'Trip planning call' })).toHaveCount(0);
  expect(await page.evaluate(() => window.__events)).toEqual([]);

  await tab(page, 'Events').click();
  await expect(page.getByRole('button', { name: 'Load events file…' })).toBeVisible();
  await page.locator('.vp-events-file').setInputFiles({ name: 'events.txt', mimeType: 'text/plain', buffer: Buffer.from('18/01/2026 | Reunión\nnot an event\n2026-01-05, First day\n') });
  await expect(cards).toHaveCount(2);
  await expect(cards.locator('.vp-event-label')).toHaveText(['First day', 'Reunión']);
  await expect(page.locator('.vp-events-note')).toHaveText('Read 2 events from events.txt.');
  await expect(page.locator('.vp-warning')).toHaveCount(1);
  await expect(page.locator('.vp-warning')).toContainText('2');
  await expect(page.locator('.vp-warning')).not.toContainText('not an event');
  expect(await page.evaluate(() => window.__events)).toEqual([[{ date: '2026-01-05', label: 'First day' }, { date: '2026-01-18', label: 'Reunión' }]]);
  await page.evaluate(() => window.__handle.showMessage(1));
  await expect(page.locator('.vp-banner', { hasText: 'First day' }).locator('.vp-event-number')).toHaveText('1');
  await page.evaluate(() => window.__handle.showMessage(21));
  await expect(page.locator('.vp-banner', { hasText: 'Reunión' }).locator('.vp-event-number')).toHaveText('2');
  // A file without a single dated line is the wrong file: the events stay.
  await tab(page, 'Events').click();
  await page.locator('.vp-events-file').setInputFiles({ name: 'notes.txt', mimeType: 'text/plain', buffer: Buffer.from('hello\nworld\n') });
  await expect(page.locator('.vp-warning')).toHaveCount(2);
  await expect(cards).toHaveCount(2);
  expect(await page.evaluate(() => window.__events.length)).toBe(1);
});

test('update() keeps the tab, the search, the filters, the point of view and what was opened', async ({ page }) => {
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await page.getByLabel('Viewing as').selectOption({ label: 'José' });
  await page.evaluate(() => window.__handle.showMessage(11));
  await page.locator('.vp-banner', { hasText: 'Trip planning call' }).click();
  await tab(page, 'Voice messages').click();
  await page.getByLabel('Sort').selectOption({ label: 'Longest first' });
  await page.locator('.vp-row').first().getByRole('button', { name: 'Show more' }).click();
  await tab(page, 'Conversation').click();
  const status = page.locator('.vp-panel-conversation .vp-find .vp-status');
  await page.getByRole('searchbox', { name: 'Search messages' }).fill('vuelos');
  await page.getByRole('button', { name: 'Next match' }).click();
  await expect(status).toHaveText('2 of 2 matches');
  await page.getByLabel('Voice only').check();
  await expect(status).toHaveText('1 match');
  await page.getByLabel('Voice only').uncheck();
  await expect(status).toHaveText('2 matches');
  // Without a current match, the next one is the first at or after the reading position.
  await page.getByRole('button', { name: 'Next match' }).click();
  await expect(status).toHaveText('2 of 2 matches');
  await page.getByRole('button', { name: 'Next match' }).click();
  await expect(status).toHaveText('1 of 2 matches');
  await expect(bubble(page, 11)).toHaveClass(/vp-current/);
  await settle(page);
  const before = await reading(page);

  for (const changed of [[12], undefined]) {
    await page.evaluate((indices) => {
      window.__model = JSON.parse(JSON.stringify(window.__model));
      window.__handle.update(window.__model, indices);
    }, changed);
    await settle(page);
    await expect(tab(page, 'Conversation')).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('searchbox', { name: 'Search messages' })).toHaveValue('vuelos');
    await expect(status).toHaveText('1 of 2 matches');
    await expect(bubble(page, 11)).toHaveClass(/vp-current/);
    await expect(bubble(page, 11).getByRole('button', { name: 'Show less' })).toBeVisible();
    await expect(page.getByLabel('Viewing as')).toHaveValue('1');
    await expect(bubble(page, 12)).toHaveClass(/vp-me/);
    await expect(page.locator('.vp-banner', { hasText: 'Trip planning call' })).toHaveAttribute('aria-current', 'true');
    expectSamePlace(await reading(page), before);
  }
  await tab(page, 'Voice messages').click();
  await expect(page.getByLabel('Sort')).toHaveValue('longest');
  await expect(page.locator('.vp-row').first().getByRole('button', { name: 'Show less' })).toBeVisible();
  await tab(page, 'Events').click();
  await expect(page.locator('.vp-event-card[aria-current]')).toContainText('Trip planning call');
});

test('destroy() stops the audio, lets go of observers and listeners, and empties the root', async ({ page }) => {
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await play(page, 4);
  await tab(page, 'Voice messages').click();
  expect(await page.evaluate(() => [window.__observers.size > 0, window.__signals.length > 0, window.__root.className, window.__root.getAttribute('data-vp-theme')]))
    .toEqual([true, true, 'voxpad-viewer', 'light']);
  const after = await page.evaluate(async () => {
    const audio = window.__root.querySelector('audio');
    window.__handle.destroy();
    window.__handle.destroy();
    await new Promise((resolve) => { setTimeout(resolve, 300); });
    return {
      children: window.__root.childNodes.length, paused: audio.paused, src: audio.getAttribute('src'), connected: audio.isConnected,
      observers: window.__observers.size, live: window.__signals.filter((signal) => !signal.aborted).length,
      classes: window.__root.className, theme: window.__root.getAttribute('data-vp-theme'),
      later: [window.__handle.update(window.__model), window.__handle.setEvents([]), window.__handle.showMessage(3)],
    };
  });
  expect(after).toEqual({ children: 0, paused: true, src: null, connected: false, observers: 0, live: 0, classes: '', theme: null, later: [undefined, undefined, false] });
  // The root can be used again, for this or another conversation.
  await page.evaluate(() => { window.__handle = window.__viewer.mountViewer(window.__root, window.__model, { theme: 'auto' }); });
  await expect(bubble(page, 4)).toBeVisible();
  await expect(page.locator('#root')).toHaveAttribute('data-vp-theme', 'auto');
  await expect(page.getByRole('group', { name: 'Now playing' })).toBeHidden();
});

test('chat content is only ever text: markup in every field stays inert', async ({ page }) => {
  await openViewer(page);
  const attacks = ['<script>window.stolen = true</script>', '<img src=x onerror="window.stolen = true">', '</script><script>window.stolen = 1</script>',
    '<!-- <svg onload="window.stolen = true"> -->', '"><iframe srcdoc="<script>parent.stolen = true</script>"></iframe>', '&lt;b&gt; &amp; javascript:window.stolen=true'];
  const all = attacks.join(' ');
  const model = {
    schema: 1, title: all, date_order: all, date_order_ambiguous: true, participants: [attacks[0], attacks[1], attacks[4]],
    messages: [
      { time: '2026-01-05T09:00:00', sender: null, kind: 'system', text: all, words: 0 },
      ...attacks.map((text, at) => ({ time: `2026-01-05T09:0${at + 1}:00`, sender: at % 3, kind: 'text', text, words: 3, edited: true })),
      { time: '2026-01-06T10:00:00', sender: 0, kind: 'media', text: all, words: 9, media: { type: 'document', file: all } },
      { time: '2026-01-06T10:01:00', sender: 1, kind: 'media', text: '', words: 0, media: { type: attacks[1], file: attacks[1] } },
      { time: '2026-01-06T10:02:00', sender: 2, kind: 'voice', text: all, words: 9, voice: { file: all, src: 'javascript:window.stolen=true', seconds: 3, status: 'ok', text: `${all} ${'stolen '.repeat(120)}`, words: 129, languages: [attacks[1]] } },
      { time: '2026-01-06T10:03:00', sender: 0, kind: 'voice', text: '', words: 0, voice: { file: all, src: null, seconds: null, status: 'error', text: '', words: 0, error: all } },
      { time: all, sender: 1, kind: attacks[0], text: all, words: 9 },
    ],
    events: attacks.map((label) => ({ date: '2026-01-06', label })),
    warnings: attacks,
  };
  await mount(page, { model, audio: 'none' });
  await expect(page.locator('.vp-title')).toHaveText(all);
  await expect(bubble(page, 2).locator('.vp-text')).toHaveText(attacks[1]);
  await page.locator('.vp-notices-label').click();
  await expect(page.locator('.vp-notice').first()).toHaveText(attacks[0]);
  // Marks cut the text into pieces; expanding, playing and every other view draw it again.
  await page.getByRole('searchbox', { name: 'Search messages' }).fill('STOLEN');
  await expect(page.locator('.vp-panel-conversation mark').first()).toHaveText('stolen');
  await bubble(page, 9).getByRole('button', { name: /Show (more|less)/ }).click();
  await bubble(page, 9).locator('.vp-play').click();
  await expect(bubble(page, 9).locator('.vp-voice-error')).toHaveText('This browser cannot play this recording');
  await tab(page, 'Voice messages').click();
  await page.getByRole('searchbox', { name: 'Search transcripts' }).fill('onerror');
  await expect(page.locator('.vp-row mark').first()).toHaveText('onerror');
  await tab(page, 'Activity').click();
  await expect(page.locator('.vp-panel-activity .vp-name').first()).toHaveText(attacks[0]);
  // The charts name people in the legend, the tooltip and the status region, and events in the tooltip and the strip.
  await expect(page.locator('.vp-legend-name')).toHaveText(model.participants);
  await page.locator('.vp-chart-key').first().focus();
  await page.keyboard.press('ArrowRight');
  await expect(page.locator('.vp-chart-tip .vp-tip-name')).toHaveText(model.participants);
  await expect(page.locator('.vp-chart-tip .vp-tip-label')).toHaveText(attacks.slice(0, 3));
  await expect(page.locator('.vp-chart-status')).toContainText(attacks[1]);
  await page.locator('.vp-marker').click();
  await expect(page.locator('.vp-pin-label')).toHaveText(attacks);
  await tab(page, 'Events').click();
  await expect(page.locator('.vp-event-label')).toHaveText(attacks);
  await page.locator('.vp-events-file').setInputFiles({ name: `${attacks[1]}.txt`, mimeType: 'text/plain', buffer: Buffer.from(attacks.map((label) => `2026-01-05 ${label}`).concat(attacks).join('\n')) });
  await expect(page.locator('.vp-events-note')).toContainText(attacks[1]);
  await expect(page.locator('.vp-event-label')).toHaveText(attacks);
  await page.evaluate((labels) => window.__handle.setEvents(labels.map((label) => ({ date: '2026-01-05', label }))), attacks);
  await tab(page, 'Conversation').click();
  await expect(page.locator('.vp-banner')).toHaveCount(attacks.length);
  await settle(page);
  await page.waitForTimeout(300);
  const found = await page.evaluate(() => ({
    stolen: window.stolen,
    foreign: [...window.__root.querySelectorAll('script, img, iframe, object, embed, link, style, a, form, base, meta')].length,
    handlers: [...window.__root.querySelectorAll('*')].filter((node) => [...node.attributes].some((attribute) => attribute.name.startsWith('on') || attribute.name.endsWith('href')
      || attribute.name === 'style' && /url|expression/i.test(attribute.value))).length,
    // Icons are paths; the charts add groups, bands, axis numbers and the hatch patterns, and nothing that can load or run.
    svg: [...window.__root.querySelectorAll('svg *')].filter((node) => !['path', 'g', 'rect', 'text', 'defs', 'pattern'].includes(node.tagName)).length,
    // The only reference inside a chart is a fill pointing at one of its own patterns.
    fills: [...window.__root.querySelectorAll('svg [fill]')].filter((node) => !/^url\(#vp[0-9]+-hatch-[0-6]\)$/.test(node.getAttribute('fill'))).length,
  }));
  expect(found).toEqual({ stolen: undefined, foreign: 0, handlers: 0, svg: 0, fills: 0 });
});

test('the app stylesheet cannot restyle the viewer, and the dark theme needs data-vp-theme="auto"', async ({ browser }) => {
  const measure = async (host, scheme, theme) => {
    const context = await browser.newContext({ colorScheme: scheme, viewport: { width: 1100, height: 800 } });
    const page = await context.newPage();
    await openViewer(page, { host });
    if (host) await page.evaluate(() => { const main = document.createElement('main'); const section = document.createElement('section'); section.className = 'results'; main.append(section); document.body.append(main); });
    // A recording that is refused leaves the now-playing strip open and still, so that it can be measured too.
    await mount(page, { model: await fixture(), theme, width: 880, audio: 'refused' });
    await bubble(page, 4).locator('.vp-play').click();
    await expect(page.getByRole('group', { name: 'Now playing' }).getByRole('status')).toHaveText('This browser cannot play this recording');
    const result = await page.evaluate(() => {
      const root = window.__root;
      const style = (selector, keys) => { const computed = getComputedStyle(root.querySelector(selector)); return Object.fromEntries(keys.map((key) => [key, computed[key]])); };
      // The list of notices is inside a closed details element: it is not drawn, and what a browser reports as its size
      // depends on whether it happened to lay it out.
      const sizes = [...root.querySelectorAll('.vp-head *, .vp-toolbar *, .vp-player *, .vp-unit:first-child *')].filter((node) => !node.closest('.vp-notices-list'))
        .map((node) => { const box = node.getBoundingClientRect(); return `${node.className.baseVal ?? node.className}:${Math.round(box.width)}x${Math.round(box.height)}`; });
      return {
        sizes,
        select: style('.vp-select', ['marginTop', 'fontSize', 'boxSizing', 'borderTopLeftRadius']),
        title: style('.vp-title', ['fontSize', 'marginTop', 'letterSpacing']),
        notices: style('.vp-notices', ['fontSize', 'paddingTop']),
        summary: style('.vp-summary', ['fontSize', 'marginTop']),
        hidden: getComputedStyle(root.querySelector('.vp-panel-events')).display,
        background: getComputedStyle(root).backgroundColor,
        ink: getComputedStyle(root).color,
      };
    });
    await context.close();
    return result;
  };
  const alone = await measure(false, 'light', 'light');
  const hosted = await measure(true, 'light', 'light');
  // The app styles bare select, h2, details and p elements; none of that reaches inside.
  expect(alone.select).toEqual({ marginTop: '0px', fontSize: '13px', boxSizing: 'border-box', borderTopLeftRadius: '8px' });
  expect(alone.title).toEqual({ fontSize: '17px', marginTop: '0px', letterSpacing: '-0.2px' });
  expect(alone.notices).toEqual({ fontSize: '12px', paddingTop: '0px' });
  expect(alone.hidden).toBe('none');
  expect(hosted).toEqual(alone);
  expect(alone.sizes.length).toBeGreaterThan(60);
  // A dark system changes nothing unless the host asked to follow it.
  expect((await measure(false, 'dark', 'light')).background).toBe(alone.background);
  const dark = await measure(false, 'dark', 'auto');
  expect(dark.background).not.toBe(alone.background);
  expect(dark.ink).not.toBe(alone.ink);
  expect((await measure(false, 'light', 'auto')).background).toBe(alone.background);
});

// 320 pixels wide is a small phone; 640 by 360 is what a 1280 by 720 window leaves at a zoom of 200 percent.
for (const [width, height] of [[320, 640], [640, 360]]) {
  test(`at ${width} by ${height} pixels nothing is cut off and the page does not scroll sideways`, async ({ page }) => {
    await page.setViewportSize({ width, height });
    await openViewer(page);
    const model = await fixture();
    model.events.push({ date: '2026-01-19', label: 'palabra '.repeat(88).trim() });
    await mount(page, { model });
    await play(page, 4);
    await page.getByRole('button', { name: 'Filters' }).click();
    await expect(page.getByLabel('Voice only')).toBeVisible();
    await page.evaluate(() => window.__handle.showMessage(11));
    await page.locator('.vp-banner', { hasText: 'Trip planning call' }).click();
    for (const name of ['Conversation', 'Voice messages', 'Activity', 'Events']) {
      await tab(page, name).click();
      if (name === 'Activity') {
        // With a tooltip on show and the events of a day pinned under the charts.
        await page.locator('.vp-chart-key').nth(1).focus();
        for (let press = 0; press < 5; press += 1) await page.keyboard.press('ArrowRight');
        await expect(page.locator('.vp-chart-tip')).toBeVisible();
        await expect(page.locator('.vp-pin-item')).toHaveCount(2);
      }
      await settle(page);
      const layout = await page.evaluate(() => {
        const frame = window.__root.getBoundingClientRect();
        // Tables and charts scroll sideways inside their own region; the region and everything else have to fit.
        const inside = (node) => node.closest('.vp-table-wrap') || (node.closest('.vp-chart-scroll') && !node.classList.contains('vp-chart-scroll'));
        const outside = [...window.__root.querySelectorAll('*')].filter((node) => {
          if (inside(node) || node.classList.contains('vp-sr') || !node.getClientRects().length) return false;
          const box = node.getBoundingClientRect();
          return box.right > frame.right + 1 || box.left < frame.left - 1;
        }).map((node) => String(node.className.baseVal ?? node.className));
        // Text that is cut short with an ellipsis or a hidden overflow, where the whole of it should be read.
        const cut = [...window.__root.querySelectorAll('.vp-event-label, .vp-pin-label, .vp-chart-tip, .vp-chart-title, .vp-coverage-line, .vp-event-who, .vp-key, .vp-legend-item')]
          .filter((node) => node.getClientRects().length && (node.scrollWidth > node.clientWidth + 1 || node.scrollHeight > node.clientHeight + 1)).map((node) => node.className);
        const tabs = window.__root.querySelector('.vp-tabs');
        return { page: document.documentElement.scrollWidth, tabs: tabs.scrollWidth - tabs.clientWidth, outside, cut };
      });
      expect(layout).toEqual({ page: width, tabs: 0, outside: [], cut: [] });
    }
    // The charts scroll under their fixed y axis, a day being never narrower than 12 pixels.
    await tab(page, 'Activity').click();
    const chart = await page.evaluate(() => {
      const scroll = window.__root.querySelector('.vp-chart-scroll');
      const key = window.__root.querySelector('.vp-chart-key');
      return { step: window.__root.querySelector('.vp-chart-plot').getBoundingClientRect().width / (Number(key.max) + 1), scrolls: scroll.scrollWidth > scroll.clientWidth };
    });
    expect(chart.step).toBeGreaterThanOrEqual(12);
    if (width < 640) expect(chart).toEqual({ step: 12, scrolls: true });
  });
}

test('each chart is one tab stop: arrows move along the dates, the status mirrors the tooltip and Enter opens the day', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  const model = await fixture();
  await mount(page, { model });
  await tab(page, 'Activity').click();
  const keys = chartKeys(page);
  const status = chartStatus(page);
  const tip = page.locator('.vp-chart-tip');
  const names = ['Messages per day', 'Words per day', 'Voice notes per day', 'Voice minutes per day'];
  await expect(keys).toHaveCount(4);
  await expect(page.locator('.vp-chart-name')).toHaveText(names);
  for (const [at, name] of names.entries()) {
    await expect(page.getByRole('slider', { name })).toHaveCount(1);
    // 5 January to 10 February: the chat, and the event that follows it closely.
    await expect(keys.nth(at)).toHaveAttribute('max', '36');
    await expect(page.locator(`#${await keys.nth(at).getAttribute('aria-describedby')}`)).toContainText('Enter opens');
  }
  await expect(status).toHaveAttribute('role', 'status');
  await expect(tip).toHaveAttribute('aria-hidden', 'true');
  await expect(tip).toBeHidden();
  // The pictures are hidden from screen readers and take no focus; the range inputs stand for them.
  expect(await page.evaluate(() => [...window.__root.querySelectorAll('.vp-chart-plot, .vp-chart-axis, .vp-chart-ticks')].every((node) => node.getAttribute('aria-hidden') === 'true' && node.tabIndex < 0))).toBe(true);

  await keys.nth(0).focus();
  for (const next of [1, 2, 3]) {
    await page.keyboard.press('Tab');
    await expect(keys.nth(next)).toBeFocused();
    // The input is not painted: the ring of the keyboard focus is drawn around its chart, and around no other.
    expect(await page.evaluate(() => [...window.__root.querySelectorAll('.vp-chart-plot')].map((plot) => { const style = getComputedStyle(plot); return style.outlineStyle === 'solid' && parseFloat(style.outlineWidth) >= 2; })))
      .toEqual([0, 1, 2, 3].map((at) => at === next));
  }
  // The event markers are one more stop, however many there are: Tab does not walk through them.
  await page.keyboard.press('Tab');
  await expect(markers(page).first()).toBeFocused();
  await page.keyboard.press('ArrowRight');
  await expect(markers(page).nth(1)).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(keys.nth(3)).toBeFocused();
  // From the legend the next stop is the first chart, not the box the charts scroll in.
  await page.locator('.vp-legend-item').last().focus();
  await page.keyboard.press('Tab');
  await expect(keys.nth(0)).toBeFocused();

  await keys.nth(0).focus();
  await expect(keys.nth(0)).toHaveAttribute('aria-valuetext', /^Monday,? 5 January 2026$/);
  await expect(status).toHaveText(/^Monday,? 5 January 2026\. Messages: Ana 3, José 2\.$/);
  await page.keyboard.press('ArrowRight');
  await expect(keys.nth(0)).toHaveValue('1');
  await expect(keys.nth(0)).toHaveAttribute('aria-valuetext', /^Tuesday,? 6 January 2026$/);
  await expect(status).toHaveText(/^Tuesday,? 6 January 2026\. Messages: Ana 2, José 3\. Event 2: José starts the new job\.$/);
  await expect(tip).toBeVisible();
  await expect(tip.locator('.vp-tip-title')).toHaveText(/^Tue,? 6 Jan 2026$/);
  await expect(tip.locator('.vp-tip-metric')).toHaveText('Messages');
  await expect(tip.locator('.vp-tip-row')).toHaveText(['Ana2', 'José3']);
  await expect(tip.locator('.vp-tip-event')).toHaveText(['2José starts the new job']);
  // A band marks the same day in all four charts, and the tooltip stays inside them.
  const { step } = await bucketPoint(page, 0, 1);
  expect(await page.evaluate(() => {
    const frame = window.__root.querySelector('.vp-chart-frame').getBoundingClientRect();
    const box = window.__root.querySelector('.vp-chart-tip').getBoundingClientRect();
    return {
      bands: [...window.__root.querySelectorAll('.vp-chart-cursor')].map((node) => (node.hasAttribute('hidden') ? null : Number(node.getAttribute('x')))),
      inside: box.width > 0 && box.left >= frame.left && box.right <= frame.right + 1 && box.top >= frame.top && box.bottom <= frame.bottom + 1,
    };
  })).toEqual({ bands: [step, step, step, step], inside: true });

  // The four charts share the place; each says its own measure.
  await page.keyboard.press('Tab');
  await expect(keys.nth(1)).toHaveValue('1');
  for (let press = 0; press < 4; press += 1) await page.keyboard.press('ArrowRight');
  await expect(keys.nth(1)).toHaveAttribute('aria-valuetext', /^Saturday,? 10 January 2026$/);
  await expect(status).toHaveText(/^Saturday,? 10 January 2026\. Words: Ana 103 \(7 typed, 96 spoken\), José 0\. Spoken words cover 2 of 4 voice notes here\. Event 3: Trip planning call\. Event 4: Long weekend at the coast .+…\.$/);
  await expect(tip.locator('.vp-tip-detail')).toHaveText(['7 typed, 96 spoken']);
  await expect(tip.locator('.vp-tip-note')).toHaveText('Spoken words cover 2 of 4 voice notes here');
  // A long label is cut at 140 characters here; the timeline and the pinned strip have the whole of it.
  const labels = await tip.locator('.vp-tip-label').allTextContents();
  expect(labels).toHaveLength(2);
  expect(labels[0]).toBe('Trip planning call');
  expect(labels[1].length).toBeGreaterThan(130);
  expect(labels[1].length).toBeLessThanOrEqual(140);
  expect(labels[1].endsWith('…')).toBe(true);
  expect(model.events[3].label.startsWith(labels[1].slice(0, -1))).toBe(true);
  await page.keyboard.press('Tab');
  await page.keyboard.press('Tab');
  await expect(keys.nth(3)).toBeFocused();
  await expect(status).toHaveText(/^Saturday,? 10 January 2026\. Voice minutes: Ana 3 min 7 s, José 14 s\. Voice time covers 3 of 4 voice notes here\. Event 3: .+$/);
  await page.keyboard.press('End');
  await expect(keys.nth(3)).toHaveValue('36');
  await expect(status).toHaveText(/^Tuesday,? 10 February 2026\. Voice minutes: Ana 0 s, José 0 s\. Event 6: Flight to Lisbon\.$/);
  await page.keyboard.press('Home');
  await expect(keys.nth(3)).toHaveValue('0');
  // Escape puts the tooltip away without moving the keyboard.
  await page.keyboard.press('Escape');
  await expect(tip).toBeHidden();
  await expect(keys.nth(3)).toBeFocused();
  for (let press = 0; press < 5; press += 1) await page.keyboard.press('ArrowRight');
  await expect(tip).toBeVisible();
  await page.keyboard.press('Enter');
  await expect(tab(page, 'Conversation')).toHaveAttribute('aria-selected', 'true');
  expect((await reading(page)).index).toBe(11);
  await expect(bubble(page, 11)).toBeFocused();
  // Coming back, the place is kept and nothing is pointed at.
  await tab(page, 'Activity').click();
  await expect(keys.nth(0)).toHaveValue('5');
  await expect(tip).toBeHidden();

  // A week or a month opens its first day that has messages.
  await page.getByRole('radio', { name: 'Week' }).check();
  await keys.nth(0).focus();
  await expect(keys.nth(0)).toHaveAttribute('aria-valuetext', /^Week of Monday,? 5 January 2026$/);
  await page.keyboard.press('ArrowRight');
  await expect(status).toHaveText(/^Week of Monday,? 12 January 2026\. Messages: Ana 1, José 3\.$/);
  await page.keyboard.press('Enter');
  expect((await reading(page)).index).toBe(21);
  await expect(page.locator('.vp-jump-note')).toBeHidden();
  await tab(page, 'Activity').click();
  await page.getByRole('radio', { name: 'Month' }).check();
  await keys.nth(0).focus();
  await expect(keys.nth(0)).toHaveAttribute('aria-valuetext', 'January 2026');
  await page.keyboard.press('ArrowRight');
  await expect(status).toHaveText('February 2026. Messages: Ana 3, José 1. Event 6: Flight to Lisbon.');
  await page.keyboard.press('Enter');
  // The first of February is the last day of the chat: it cannot reach the top, but its first message has the keyboard.
  await expect(bubble(page, 30)).toBeFocused();
  await expect(bubble(page, 30)).toBeInViewport({ ratio: 1 });
});

test('pointing at a bar shows the values of its day; a click, or a second tap, opens the day', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await tab(page, 'Activity').click();
  const tip = page.locator('.vp-chart-tip');
  const words = await bucketPoint(page, 1, 5);
  await page.mouse.move(words.x, words.y);
  await expect(tip).toBeVisible();
  await expect(tip.locator('.vp-tip-title')).toHaveText(/^Sat,? 10 Jan 2026$/);
  await expect(tip.locator('.vp-tip-metric')).toHaveText('Words');
  await expect(tip.locator('.vp-tip-row')).toHaveText(['Ana1037 typed, 96 spoken', 'José0']);
  await expect(chartStatus(page)).toHaveText(/^Saturday,? 10 January 2026\. Words: Ana 103 \(7 typed, 96 spoken\), José 0\./);
  await expect(page.locator('.vp-chart-cursor:not([hidden])')).toHaveCount(4);
  await page.mouse.move(words.x + words.step, words.y);
  await expect(tip.locator('.vp-tip-title')).toHaveText(/^Sun,? 11 Jan 2026$/);
  // The same day in another chart: its own measure.
  const notes = await bucketPoint(page, 2, 5);
  await page.mouse.move(notes.x, notes.y);
  await expect(tip.locator('.vp-tip-metric')).toHaveText('Voice notes');
  await expect(tip.locator('.vp-tip-row')).toHaveText(['Ana1', 'José3']);
  // Pointing at nothing puts the tooltip and the band away.
  await page.mouse.move(5, 5);
  await expect(tip).toBeHidden();
  await expect(page.locator('.vp-chart-cursor:not([hidden])')).toHaveCount(0);
  // Hovering does not move the keyboard's place; a click does, and opens the day.
  await expect(chartKeys(page).first()).toHaveValue('0');
  await page.mouse.click(words.x, words.y);
  await expect(tab(page, 'Conversation')).toHaveAttribute('aria-selected', 'true');
  expect((await reading(page)).index).toBe(11);
  await expect(bubble(page, 11)).toBeFocused();
  await tab(page, 'Activity').click();
  await expect(chartKeys(page).first()).toHaveValue('5');

  // A finger cannot hover: the first tap shows the values, the second tap on the same day opens it.
  const spot = await bucketPoint(page, 0, 1);
  const taps = await page.evaluate(({ x, y }) => {
    const tap = () => {
      const target = document.elementFromPoint(x, y);
      target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerType: 'touch', clientX: x, clientY: y }));
      target.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerType: 'touch', clientX: x, clientY: y }));
      target.dispatchEvent(new PointerEvent('pointerleave', { pointerType: 'touch' }));
      target.dispatchEvent(new MouseEvent('click', { bubbles: true, clientX: x, clientY: y }));
      return [window.__root.querySelector('[data-vp-tab="activity"]').getAttribute('aria-selected'), !window.__root.querySelector('.vp-chart-tip').hidden];
    };
    return [tap(), window.__root.querySelector('.vp-chart-tip .vp-tip-title').textContent, tap()];
  }, spot);
  expect(taps[0]).toEqual(['true', true]);
  expect(taps[1]).toMatch(/^Tue,? 6 Jan 2026$/);
  expect(taps[2][0]).toBe('false');
  expect((await reading(page)).index).toBe(6);
});

test('one selected event is shown in the conversation, on the charts and in the timeline', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  const model = await fixture();
  await mount(page, { model });
  await tab(page, 'Activity').click();
  const pin = page.getByRole('region', { name: 'Selected event' });
  const cards = page.locator('.vp-event-card');
  const status = chartStatus(page);
  // One marker per day with events: the event's number, or how many there are. The event of November 2025 is too far
  // from the chat to be on the axis; the one nine days after the chat widens the axis.
  await expect(markers(page)).toHaveText(['2', '×2', '5', '6']);
  expect(await markers(page).evaluateAll((nodes) => nodes.map((node) => node.tabIndex))).toEqual([0, -1, -1, -1]);
  await expect(page.locator('.vp-chart-footnote')).toHaveText('1 event far outside the conversation is not on the charts.');
  await expect(pin).toBeHidden();
  await expect(markers(page).nth(0)).toHaveAttribute('aria-label', 'Event 2, 6 Jan 2026: José starts the new job');
  await expect(markers(page).nth(1)).toHaveAttribute('aria-label', '2 events, numbers 3 to 4, 10 Jan 2026');
  for (const [at, index] of [[0, 1], [1, 5], [2, 20], [3, 36]]) {
    const middle = await markers(page).nth(at).evaluate((node) => { const box = node.getBoundingClientRect(); return (box.left + box.right) / 2; });
    const bucket = await bucketPoint(page, 0, index);
    expect(Math.abs(middle - bucket.x)).toBeLessThanOrEqual(Math.max(1, bucket.step / 2));
  }

  // Selecting a marker pins the whole labels of its day under the charts.
  await markers(page).nth(1).click();
  await expect(pin).toBeVisible();
  await expect(pin.locator('.vp-pin-label')).toHaveText([model.events[2].label, model.events[3].label]);
  await expect(pin.locator('.vp-pin-item .vp-event-number')).toHaveText(['3', '4']);
  await expect(pin.locator('.vp-pin-item[aria-current] .vp-event-number')).toHaveText('3');
  await expect(markers(page).nth(1)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.vp-marker[aria-pressed="true"]')).toHaveCount(1);
  await expect(page.locator('.vp-chart-pick:not([hidden])')).toHaveCount(4);
  await expect(status).toHaveText('2 events pinned under the charts.');
  await expect(pin.locator('.vp-pin-item').nth(0).getByRole('button')).toHaveText(['Show in Events', 'Open this day']);
  // "Show in Events" goes to the card of that event, which is now the selected one in the timeline …
  await pin.locator('.vp-pin-item').nth(1).getByRole('button', { name: 'Show in Events' }).click();
  await expect(tab(page, 'Events')).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.vp-event-card[aria-current]')).toHaveCount(1);
  await expect(cards.nth(3)).toHaveAttribute('aria-current', 'true');
  await expect(cards.nth(3).locator('.vp-event-pick')).toBeFocused();
  await expect(cards.nth(3).locator('.vp-event-pick')).toHaveAttribute('aria-pressed', 'true');
  await expect(cards.nth(3)).toBeInViewport();
  // … in the conversation …
  await cards.nth(3).getByRole('button', { name: 'Open this day' }).click();
  await expect(tab(page, 'Conversation')).toHaveAttribute('aria-selected', 'true');
  expect((await reading(page)).index).toBe(11);
  await expect(page.locator('.vp-banner[aria-current]')).toHaveCount(1);
  await expect(page.locator('.vp-banner[aria-current]')).toContainText('Long weekend at the coast');
  // … and on the charts.
  await tab(page, 'Activity').click();
  await expect(markers(page).nth(1)).toHaveAttribute('aria-pressed', 'true');
  await expect(pin.locator('.vp-pin-item[aria-current] .vp-event-number')).toHaveText('4');
  await pin.locator('.vp-pin-item').nth(0).getByRole('button', { name: 'Open this day' }).click();
  await expect(page.locator('.vp-banner[aria-current]')).toContainText('Trip planning call');
  await tab(page, 'Activity').click();

  // The markers are one tab stop, the one that was used last: arrows move between them, Enter and Space select.
  expect(await markers(page).evaluateAll((nodes) => nodes.map((node) => node.tabIndex))).toEqual([-1, 0, -1, -1]);
  await markers(page).nth(1).focus();
  await page.keyboard.press('ArrowRight');
  await expect(markers(page).nth(2)).toBeFocused();
  expect(await markers(page).evaluateAll((nodes) => nodes.map((node) => node.tabIndex))).toEqual([-1, -1, 0, -1]);
  await expect(page.locator('.vp-chart-tip')).toBeVisible();
  await expect(page.locator('.vp-chart-tip .vp-tip-event')).toHaveText(['5Ana away, no signal']);
  await expect(status).toHaveText(/^Sunday,? 25 January 2026\. Event 5: Ana away, no signal\.$/);
  await page.keyboard.press('Enter');
  await expect(pin.locator('.vp-pin-label')).toHaveText(['Ana away, no signal']);
  await expect(status).toHaveText('Event 5 pinned under the charts.');
  await expect(markers(page).nth(2)).toBeFocused();
  await expect(markers(page).nth(2)).toHaveAttribute('aria-pressed', 'true');
  await expect(markers(page).nth(1)).toHaveAttribute('aria-pressed', 'false');
  await page.keyboard.press('Space');
  await expect(pin).toBeHidden();
  await expect(markers(page).nth(2)).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.vp-chart-pick:not([hidden])')).toHaveCount(0);
  await page.keyboard.press('End');
  await expect(markers(page).nth(3)).toBeFocused();
  await page.keyboard.press('Home');
  await expect(markers(page).nth(0)).toBeFocused();
  await page.keyboard.press('ArrowLeft');
  await expect(markers(page).nth(0)).toBeFocused();

  // From the timeline: a card's number and date select it, and "Show in Activity" finds its marker.
  await tab(page, 'Events').click();
  await expect(page.locator('.vp-event-card[aria-current]')).toHaveCount(0);
  await cards.nth(1).getByRole('button', { name: /^Event 2/ }).click();
  await expect(cards.nth(1)).toHaveAttribute('aria-current', 'true');
  await cards.nth(1).getByRole('button', { name: 'Show in Activity' }).click();
  await expect(tab(page, 'Activity')).toHaveAttribute('aria-selected', 'true');
  await expect(markers(page).nth(0)).toBeFocused();
  await expect(markers(page).nth(0)).toHaveAttribute('aria-pressed', 'true');
  await expect(pin.locator('.vp-pin-label')).toHaveText(['José starts the new job']);
  await pin.getByRole('button', { name: 'Clear the selected event' }).click();
  await expect(pin).toBeHidden();
  await expect(markers(page).nth(0)).toBeFocused();
  await tab(page, 'Events').click();
  await expect(page.locator('.vp-event-card[aria-current]')).toHaveCount(0);
  // Choosing the selected card again lets go of it.
  await cards.nth(4).locator('.vp-event-pick').click();
  await expect(cards.nth(4)).toHaveAttribute('aria-current', 'true');
  await cards.nth(4).locator('.vp-event-pick').click();
  await expect(page.locator('.vp-event-card[aria-current]')).toHaveCount(0);
  await expect(cards.nth(4).locator('.vp-event-pick')).toHaveAttribute('aria-pressed', 'false');
  // An event far outside the chat has no marker and no banner: the strip names it and says why.
  await cards.nth(0).locator('.vp-event-pick').click();
  await tab(page, 'Activity').click();
  await expect(pin.locator('.vp-pin-label')).toHaveText(["Met at Li's birthday"]);
  await expect(pin.locator('.vp-flag')).toHaveText('Far outside the conversation, not on the charts');
  await expect(pin.getByRole('button', { name: 'Open this day' })).toHaveCount(0);
  await expect(page.locator('.vp-marker[aria-pressed="true"]')).toHaveCount(0);
  await expect(page.locator('.vp-chart-pick:not([hidden])')).toHaveCount(0);
});

test('the timeline gives every event its whole label and what was written and said that day', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  const model = await fixture();
  await mount(page, { model });
  await tab(page, 'Events').click();
  const cards = page.locator('.vp-event-card');
  await expect(cards).toHaveCount(6);
  await expect(page.locator('.vp-event-label')).toHaveText(model.events.map((event) => event.label));
  await expect(cards.nth(1).getByRole('button', { name: /^Event 2, Tuesday,? 6 January 2026$/ })).toHaveAttribute('aria-pressed', 'false');
  await expect(cards.nth(1).locator('.vp-event-who')).toHaveText(['Ana2 messages · 2 words', 'José3 messages · 13 words']);
  await expect(cards.nth(1).locator('.vp-event-cover')).toHaveCount(0);
  await expect(cards.nth(1).getByRole('button')).toHaveText([/^2Tuesday,? 6 January 2026$/, 'Open this day', 'Show in Activity']);
  // In the conversation the banner of an event is named in one piece, whole label included.
  await page.evaluate(() => window.__handle.showMessage(11));
  await expect(page.getByRole('button', { name: `Event 4: ${model.events[3].label}` })).toBeVisible();
  await page.evaluate(() => window.__handle.showMessage(30));
  await expect(page.getByRole('button', { name: 'Event 5, 25 Jan 2026: Ana away, no signal' })).toBeVisible();
  await tab(page, 'Events').click();
  // The day of two events: its voice notes are only partly transcribed, and the card says so.
  for (const at of [2, 3]) {
    await expect(cards.nth(at).locator('.vp-event-who')).toHaveText(['Ana5 messages · 103 words · 3 min 7 s of voice', 'José5 messages · 0 words · 14 s of voice']);
    await expect(cards.nth(at).locator('.vp-event-cover')).toHaveText('On this day spoken words cover 2 of 4 voice messages, voice time covers 3 of 4.');
  }
  await expect(cards.nth(4).locator('.vp-event-quiet')).toHaveText('No messages on this day.');
  await expect(cards.nth(4).getByRole('button', { name: 'Open this day' })).toBeVisible();
  // Outside the chat: listed and flagged, with nothing to open; the one close to the chat is still on the charts.
  for (const at of [0, 5]) {
    await expect(cards.nth(at)).toHaveClass(/vp-outside/);
    await expect(cards.nth(at).locator('.vp-flag')).toHaveText('Outside the conversation');
    await expect(cards.nth(at).locator('.vp-event-day')).toHaveCount(0);
    await expect(cards.nth(at).getByRole('button', { name: 'Open this day' })).toHaveCount(0);
  }
  await expect(cards.nth(0).getByRole('button', { name: 'Show in Activity' })).toHaveCount(0);
  await expect(cards.nth(5).getByRole('button', { name: 'Show in Activity' })).toBeVisible();

  // A label of 700 characters is shown whole, in lines short enough to read.
  const long = `${'Siete palabras de prueba para leer bien. '.repeat(17)}Fin.`.slice(0, 700);
  await page.evaluate((label) => window.__handle.setEvents([{ date: '2026-01-10', label }, { date: '2026-01-19', label: 'Short' }]), long);
  await expect(cards).toHaveCount(2);
  await expect(cards.nth(0).locator('.vp-event-label')).toHaveText(long);
  const shape = await cards.nth(0).locator('.vp-event-label').evaluate((node) => {
    const style = getComputedStyle(node);
    return { whole: node.scrollHeight <= node.clientHeight + 1 && node.scrollWidth <= node.clientWidth + 1, width: node.getBoundingClientRect().width / parseFloat(style.fontSize),
      size: parseFloat(style.fontSize), leading: parseFloat(style.lineHeight) / parseFloat(style.fontSize), clamp: style.webkitLineClamp };
  });
  expect(shape.whole).toBe(true);
  expect(shape.clamp).toBe('none');
  expect(shape.size).toBeGreaterThanOrEqual(15);
  expect(shape.leading).toBeGreaterThanOrEqual(1.5);
  expect(shape.width).toBeLessThanOrEqual(36.1);
  // The same label pinned under the charts, whole as well.
  await cards.nth(0).getByRole('button', { name: 'Show in Activity' }).click();
  await expect(page.locator('.vp-pin-label')).toHaveText([long]);
  await expect(page.locator('.vp-chart-tip .vp-tip-label')).toHaveText(/^Siete palabras .+…$/);

  // Hundreds of events come a page at a time; an event further down is reached all the same.
  await page.evaluate(() => window.__handle.setEvents(Array.from({ length: 250 }, (item, at) => ({ date: `2026-01-${String(5 + at % 20).padStart(2, '0')}`, label: `Event number ${at}` }))));
  await tab(page, 'Events').click();
  await expect(tab(page, 'Events')).toContainText('250');
  await expect(cards).toHaveCount(100);
  await page.getByRole('button', { name: 'Show 100 more events' }).click();
  await expect(cards).toHaveCount(200);
  await expect(cards.nth(100).locator('.vp-event-pick')).toBeFocused();
  await expect(page.getByRole('button', { name: 'Show 50 more events' })).toBeVisible();
  await page.evaluate(() => window.__handle.setEvents(Array.from({ length: 250 }, (item, at) => ({ date: `2026-01-${String(5 + at % 20).padStart(2, '0')}`, label: `Event number ${at}` }))));
  await expect(cards).toHaveCount(100);
  // The banner of the last event of 19 January, number 190 of 250 by date, opens its card on the second page.
  await page.evaluate(() => window.__handle.showMessage(25));
  await page.locator('.vp-banner', { hasText: 'Event number 234' }).click();
  await expect(cards).toHaveCount(200);
  await expect(page.locator('.vp-event-card[aria-current] .vp-event-number')).toHaveText('190');
  await expect(page.locator('.vp-event-card[aria-current]')).toBeInViewport();
  // On the charts a day with thirteen events is one marker, and the twelve of the selected event's day are pinned.
  await tab(page, 'Activity').click();
  await expect(markers(page)).toHaveCount(20);
  await expect(markers(page).first()).toHaveText('×13');
  await expect(page.locator('.vp-marker[aria-pressed="true"]')).toHaveText('×12');
  await expect(page.locator('.vp-pin-item')).toHaveCount(12);
  await expect(page.locator('.vp-pin-item[aria-current] .vp-pin-label')).toHaveText('Event number 234');
});

test('a loaded events file renumbers the markers of the charts, and its warnings are kept short', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await tab(page, 'Activity').click();
  await markers(page).nth(2).click();
  await expect(page.locator('.vp-pin-label')).toHaveText(['Ana away, no signal']);
  await tab(page, 'Events').click();
  const lines = ['18/01/2026 | Reunión', '2026-01-25 Ana away, no signal', '2026-01-05, First day', '2024-01-05 Long before', ...Array.from({ length: 11 }, (item, at) => `not a date ${at}`)];
  await page.locator('.vp-events-file').setInputFiles({ name: 'events.txt', mimeType: 'text/plain', buffer: Buffer.from(lines.join('\n')) });
  await expect(page.locator('.vp-events-note')).toHaveText('Read 4 events from events.txt.');
  await expect(page.locator('.vp-event-card .vp-event-label')).toHaveText(['Long before', 'First day', 'Reunión', 'Ana away, no signal']);
  // Eleven lines without a date: the first eight are named by their number, the rest are counted.
  await expect(page.locator('.vp-warning')).toHaveCount(8);
  await expect(page.locator('.vp-warning').first()).toHaveText('Events line 5 has no valid date and was skipped.');
  await expect(page.locator('.vp-warning-more')).toHaveText('And 3 more lines of that kind.');
  expect(await page.evaluate(() => window.__events)).toEqual([[
    { date: '2024-01-05', label: 'Long before' }, { date: '2026-01-05', label: 'First day' }, { date: '2026-01-18', label: 'Reunión' }, { date: '2026-01-25', label: 'Ana away, no signal' },
  ]]);
  // The event that was selected is still in the file: it stays selected, under its new number.
  await expect(page.locator('.vp-event-card[aria-current] .vp-event-number')).toHaveText('4');
  await tab(page, 'Activity').click();
  await expect(markers(page)).toHaveText(['2', '3', '4']);
  await expect(markers(page).nth(2)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.vp-pin-item .vp-event-number')).toHaveText(['4']);
  await expect(page.locator('.vp-chart-footnote')).toHaveText('1 event far outside the conversation is not on the charts.');
  // Without the event after the chat, the axis ends with the chat.
  await expect(chartKeys(page).first()).toHaveAttribute('max', '27');
  // An empty file clears the events everywhere.
  await tab(page, 'Events').click();
  await page.locator('.vp-events-file').setInputFiles({ name: 'none.txt', mimeType: 'text/plain', buffer: Buffer.from('# nothing\n') });
  await expect(page.locator('.vp-event-card')).toHaveCount(0);
  await expect(page.locator('.vp-events .vp-empty')).toBeVisible();
  await expect(page.locator('.vp-warnings')).toBeHidden();
  await tab(page, 'Activity').click();
  await expect(markers(page)).toHaveCount(0);
  await expect(page.locator('.vp-chart-lane-row')).toBeHidden();
  await expect(page.locator('.vp-pin')).toBeHidden();
  await expect(page.locator('.vp-chart-footnote')).toBeHidden();
});

test('the switch groups the bars by day, week or month, and the host can choose the first grouping', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await tab(page, 'Activity').click();
  const group = page.getByRole('radiogroup', { name: 'Group by' });
  const keys = chartKeys(page);
  const bars = () => marks(page, '.vp-chart-plot[data-vp-chart="0"] .vp-bar');
  await expect(group.getByRole('radio')).toHaveCount(3);
  await expect(group.getByRole('radio', { name: 'Day' })).toBeChecked();
  await expect(keys.first()).toHaveAttribute('max', '36');
  // One bar per person and day with messages; spoken words are drawn apart, hatched in the colour of their speaker.
  expect(await bars()).toEqual([6, 7]);
  expect(await marks(page, '.vp-bar-spoken')).toEqual([3, 1]);
  expect(await page.evaluate(() => [...window.__root.querySelectorAll('.vp-bar-spoken')].map((node) => {
    const pattern = window.__root.querySelector(node.getAttribute('fill').slice(4, -1));
    return [pattern.tagName, pattern.getAttribute('class'), pattern.querySelectorAll('.vp-hatch-ink').length, getComputedStyle(pattern.querySelector('.vp-hatch-ink')).stroke];
  }))).toEqual([['pattern', 'vp-s1', 1, 'rgb(42, 120, 214)'], ['pattern', 'vp-s2', 1, 'rgb(235, 104, 52)']]);
  // Saturdays and Sundays are shaded, in the day view only.
  expect(await marks(page, '.vp-chart-shade')).toEqual([10, 10, 10, 10]);
  await expect(page.locator('.vp-key', { hasText: 'Weekend' })).toHaveCount(1);
  // The keyboard's place is Sunday the first of February.
  await keys.first().fill('27');
  await expect(keys.first()).toHaveAttribute('aria-valuetext', /^Sunday,? 1 February 2026$/);

  await group.getByRole('radio', { name: 'Week' }).check();
  await expect(page.locator('.vp-chart-name')).toHaveText(['Messages per week', 'Words per week', 'Voice notes per week', 'Voice minutes per week']);
  await expect(page.getByRole('slider', { name: 'Messages per week' })).toHaveCount(1);
  await expect(keys.first()).toHaveAttribute('max', '5');
  expect(await bars()).toEqual([4, 4]);
  expect(await marks(page, '.vp-chart-shade')).toEqual([0, 0, 0, 0]);
  await expect(page.locator('.vp-key', { hasText: 'Weekend' })).toHaveCount(0);
  await expect(markers(page)).toHaveText(['×3', '5', '6']);
  // The place follows into the week of that Sunday.
  await expect(keys.first()).toHaveValue('3');
  await expect(keys.first()).toHaveAttribute('aria-valuetext', /^Week of Monday,? 26 January 2026$/);
  await keys.first().focus();
  await page.keyboard.press('Home');
  await expect(chartStatus(page)).toHaveText(/^Week of Monday,? 5 January 2026\. Messages: Ana 10, José 10\. Event 2: José starts the new job\. Event 3: Trip planning call\. Event 4: .+$/);
  await page.keyboard.press('End');
  await expect(keys.first()).toHaveAttribute('aria-valuetext', /^Week of Monday,? 9 February 2026$/);

  // The switch is a group of radio buttons: the arrow keys move the choice.
  await group.getByRole('radio', { name: 'Week' }).focus();
  await page.keyboard.press('ArrowRight');
  await expect(group.getByRole('radio', { name: 'Month' })).toBeChecked();
  await expect(keys.first()).toHaveAttribute('max', '1');
  await expect(keys.first()).toHaveAttribute('aria-valuetext', 'February 2026');
  expect(await bars()).toEqual([2, 2]);
  await expect(markers(page)).toHaveText(['×4', '6']);
  await expect(markers(page).first()).toHaveAttribute('aria-label', '4 events, numbers 2 to 5, January 2026');
  await keys.first().focus();
  await page.keyboard.press('Home');
  await expect(chartStatus(page)).toHaveText(/^January 2026\. Messages: Ana 14, José 15\. Event 2: .+ Event 4: .+ and 1 more event\.$/);

  // The choice is the reader's: it survives update() and another tab.
  await page.evaluate(() => window.__handle.update(JSON.parse(JSON.stringify(window.__model))));
  await tab(page, 'Events').click();
  await tab(page, 'Activity').click();
  await expect(group.getByRole('radio', { name: 'Month' })).toBeChecked();
  await expect(keys.first()).toHaveAttribute('max', '1');

  // The host may choose the first grouping; anything but day, week or month is no choice.
  for (const [bucket, name, max] of [['week', 'Week', '5'], ['month', 'Month', '1'], ['year', 'Day', '36'], [undefined, 'Day', '36']]) {
    await page.evaluate((choice) => {
      window.__handle.destroy();
      window.__handle = window.__viewer.mountViewer(window.__root, window.__model, { bucket: choice });
    }, bucket);
    await tab(page, 'Activity').click();
    await expect(page.getByRole('radio', { name })).toBeChecked();
    await expect(chartKeys(page).first()).toHaveAttribute('max', max);
  }
});

test('a long chat scrolls sideways under a fixed y axis from its first day, and turns to weeks by itself only above 730 days', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  await page.evaluate(makeSpanModel, { days: 730 });
  await mount(page);
  await tab(page, 'Activity').click();
  await expect(page.getByRole('radio', { name: 'Day' })).toBeChecked();
  await expect(chartKeys(page).first()).toHaveAttribute('max', '729');
  const measure = () => page.evaluate(() => {
    const scroll = window.__root.querySelector('.vp-chart-scroll');
    const view = scroll.getBoundingClientRect();
    const plots = [...window.__root.querySelectorAll('.vp-chart-plot')];
    const band = window.__root.querySelector('.vp-chart-cursor').getBoundingClientRect();
    const offsets = (selector) => [...window.__root.querySelectorAll(selector)].map((node) => Math.round(node.getBoundingClientRect().left - view.left));
    return {
      step: plots[0].getBoundingClientRect().width / (Number(window.__root.querySelector('.vp-chart-key').max) + 1),
      left: scroll.scrollLeft,
      most: scroll.scrollWidth - scroll.clientWidth,
      together: plots.every((plot) => plot.closest('.vp-chart-scroll') === scroll && plot.getBoundingClientRect().left === plots[0].getBoundingClientRect().left),
      axes: offsets('.vp-chart-axis'),
      titles: offsets('.vp-chart-title'),
      fixed: [...window.__root.querySelectorAll('.vp-legend, .vp-seg, .vp-keys, .vp-chart-tip, .vp-pin')].every((node) => !scroll.contains(node)),
      band: [Math.round(band.left - view.left), Math.round(view.right - band.right)],
    };
  });
  let chart = await measure();
  // A day is 12 pixels wide and the four charts move as one; the chart opens at the first day.
  expect(chart).toMatchObject({ step: 12, left: 0, together: true, axes: [0, 0, 0, 0], titles: [0, 0, 0, 0], fixed: true });
  expect(chart.most).toBeGreaterThan(7000);
  // The last day by keyboard: the charts scroll there, the y axes and the titles stay where they were.
  await chartKeys(page).first().focus();
  await page.keyboard.press('End');
  await expect(chartKeys(page).first()).toHaveValue('729');
  chart = await measure();
  expect(chart).toMatchObject({ step: 12, together: true, axes: [0, 0, 0, 0], titles: [0, 0, 0, 0] });
  expect(chart.left).toBe(chart.most);
  expect(chart.band[0]).toBeGreaterThanOrEqual(44);
  expect(chart.band[1]).toBeGreaterThanOrEqual(0);
  await expect(page.locator('.vp-chart-tip')).toBeVisible();
  // One step back from a place in the middle scrolls no further than needed.
  await page.evaluate(() => { window.__root.querySelector('.vp-chart-scroll').scrollLeft = 3000; });
  await settle(page);
  await page.keyboard.press('ArrowLeft');
  chart = await measure();
  expect(chart.band[0]).toBeGreaterThanOrEqual(44);
  expect(chart.band[1]).toBeGreaterThanOrEqual(0);
  const kept = chart.left;
  // The place is kept over another tab and over update().
  await tab(page, 'Events').click();
  await tab(page, 'Activity').click();
  expect((await measure()).left).toBe(kept);
  await page.evaluate(() => window.__handle.update(window.__model));
  expect((await measure()).left).toBe(kept);

  // One day more and the charts open by week; days are still there for the asking.
  await page.evaluate(() => window.__handle.destroy());
  await page.evaluate(makeSpanModel, { days: 731 });
  await page.evaluate(() => { window.__handle = window.__viewer.mountViewer(window.__root, window.__model, {}); });
  await tab(page, 'Activity').click();
  await expect(page.getByRole('radio', { name: 'Week' })).toBeChecked();
  await expect(chartKeys(page).first()).toHaveAttribute('max', '104');
  await page.getByRole('radio', { name: 'Day' }).check();
  await expect(chartKeys(page).first()).toHaveAttribute('max', '730');
  expect((await measure()).step).toBe(12);
  // A few days fill the width instead, each wider than 12 pixels, and nothing scrolls.
  await page.evaluate(() => window.__handle.destroy());
  await page.evaluate(makeSpanModel, { days: 21 });
  await page.evaluate(() => { window.__handle = window.__viewer.mountViewer(window.__root, window.__model, {}); });
  await tab(page, 'Activity').click();
  chart = await measure();
  expect(chart.step).toBeGreaterThan(30);
  expect(chart.most).toBe(0);
});

test('events close to the chat widen the axis; beyond 21 days or 15 percent of its length they are left off', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  // A chat of 21 days keeps events up to 21 days away; one of 730 days up to 109 days, 15 percent of the 729 between its ends.
  for (const [days, margin] of [[21, 21], [730, 109]]) {
    await page.evaluate(makeSpanModel, { days });
    // The chat begins on 6 January 2025. One event just too early, one just early enough, one on the last day, and the same after it.
    const dates = await page.evaluate(([length, reach]) => {
      const day = (offset) => new Date(Date.UTC(2025, 0, 6 + offset)).toISOString().slice(0, 10);
      window.__model.events = [-reach - 1, -reach, length - 1, length - 1 + reach, length + reach].map((offset, at) => ({ date: day(offset), label: `Event at ${at}` }));
      return window.__model.events.map((event) => event.date);
    }, [days, margin]);
    if (days === 21) await mount(page);
    else await page.evaluate(() => { window.__handle.destroy(); window.__handle = window.__viewer.mountViewer(window.__root, window.__model, {}); });
    await tab(page, 'Activity').click();
    await expect(markers(page)).toHaveText(['2', '3', '4']);
    await expect(page.locator('.vp-chart-footnote')).toHaveText('2 events far outside the conversation are not on the charts.');
    await expect(chartKeys(page).first()).toHaveAttribute('max', String(days + 2 * margin - 1));
    // The first bucket is the day of the earliest event that is kept, the last one that of the latest.
    await chartKeys(page).first().focus();
    await expect(chartStatus(page)).toContainText('Event 2: Event at 1.');
    await page.keyboard.press('End');
    await expect(chartStatus(page)).toContainText('Event 4: Event at 3.');
    expect(await page.evaluate(() => window.__viewer.aggregate(window.__model, 'day').buckets.length)).toBe(days);
    // The timeline lists all five and flags the four outside the chat; only those on the charts can be shown there.
    await tab(page, 'Events').click();
    await expect(page.locator('.vp-event-card .vp-event-date')).toHaveCount(5);
    await expect(page.locator('.vp-event-card .vp-flag')).toHaveCount(4);
    await expect(page.getByRole('button', { name: 'Show in Activity' })).toHaveCount(3);
    await expect(page.getByRole('button', { name: 'Open this day' })).toHaveCount(1);
    expect(dates[2]).toBe(days === 21 ? '2025-01-26' : '2027-01-05');
  }
});

test('the legend shows six participants and folds the rest into Others; each can be switched off', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  const model = await fixture();
  model.participants = ['Ana', 'José', 'Li', 'Mariam', 'Noor', 'Olu', 'Pía', 'Quentin'];
  model.messages.forEach((message, at) => { if (message.sender != null) message.sender = at % 8; });
  await mount(page, { model });
  await tab(page, 'Activity').click();
  const items = page.getByRole('group', { name: 'Participants in the charts' }).getByRole('button');
  const series = () => page.evaluate(() => [...window.__root.querySelectorAll('.vp-chart-plot[data-vp-chart="0"] .vp-bar')].map((node) => node.getAttribute('class').replace('vp-bar ', '')));
  await expect(items).toHaveText(['Ana', 'José', 'Li', 'Mariam', 'Noor', 'Olu', 'Others']);
  for (const item of await items.all()) await expect(item).toHaveAttribute('aria-pressed', 'true');
  expect(await series()).toEqual(['vp-s1', 'vp-s2', 'vp-s3', 'vp-s4', 'vp-s5', 'vp-s6', 'vp-s0']);
  // Seven fills that differ, the same in the legend, the table and the bars.
  const fills = await page.evaluate(() => ({
    bars: [...window.__root.querySelectorAll('.vp-chart-plot[data-vp-chart="0"] .vp-bar')].map((node) => getComputedStyle(node).fill),
    legend: [...window.__root.querySelectorAll('.vp-legend-item .vp-swatch')].map((node) => getComputedStyle(node).backgroundColor),
    table: [...window.__root.querySelectorAll('.vp-table .vp-swatch')].map((node) => getComputedStyle(node).backgroundColor),
  }));
  expect(new Set(fills.bars).size).toBe(7);
  expect(fills.legend).toEqual(fills.bars);
  expect(fills.table).toEqual([...fills.bars, fills.bars[6]]);
  // "Others" is the sum of everybody after the sixth.
  const expected = await page.evaluate(() => window.__viewer.aggregate(window.__model, 'day').series.map((row) => row.messages[1]));
  expect(expected[6] + expected[7]).toBeGreaterThan(1);
  await chartKeys(page).first().focus();
  await page.keyboard.press('ArrowRight');
  await expect(chartStatus(page)).toContainText(`Messages: Ana ${expected[0]}, José ${expected[1]}, Li ${expected[2]}, Mariam ${expected[3]}, Noor ${expected[4]}, Olu ${expected[5]}, Others ${expected[6] + expected[7]}.`);
  await expect(page.locator('.vp-chart-tip .vp-tip-name')).toHaveText(['Ana', 'José', 'Li', 'Mariam', 'Noor', 'Olu', 'Others']);
  // The bars of seven series need more room than 12 pixels a day.
  expect((await bucketPoint(page, 0, 0)).step).toBeGreaterThanOrEqual(38);

  // Switching one off takes its bars away; the others keep their colours.
  await items.nth(1).click();
  await expect(items.nth(1)).toHaveAttribute('aria-pressed', 'false');
  await expect(items.nth(1)).toBeFocused();
  expect(await series()).toEqual(['vp-s1', 'vp-s3', 'vp-s4', 'vp-s5', 'vp-s6', 'vp-s0']);
  await chartKeys(page).first().focus();
  await expect(chartStatus(page)).toContainText(`Messages: Ana ${expected[0]}, Li ${expected[2]}, Mariam ${expected[3]}, Noor ${expected[4]}, Olu ${expected[5]}, Others ${expected[6] + expected[7]}.`);
  await expect(page.locator('.vp-chart-tip .vp-tip-name')).toHaveText(['Ana', 'Li', 'Mariam', 'Noor', 'Olu', 'Others']);
  await items.nth(6).focus();
  await page.keyboard.press('Enter');
  await expect(items.nth(6)).toHaveAttribute('aria-pressed', 'false');
  expect(await series()).toEqual(['vp-s1', 'vp-s3', 'vp-s4', 'vp-s5', 'vp-s6']);
  // The choice outlives update(); the table goes on listing everybody.
  await page.evaluate(() => window.__handle.update(JSON.parse(JSON.stringify(window.__model))));
  await expect(items.nth(1)).toHaveAttribute('aria-pressed', 'false');
  expect(await series()).toEqual(['vp-s1', 'vp-s3', 'vp-s4', 'vp-s5', 'vp-s6']);
  await expect(page.getByRole('table', { name: 'Activity per participant' }).locator('tbody tr')).toHaveCount(9);
  // Nobody left would be an empty chart: switching the last one off brings everybody back.
  for (const at of [0, 2, 3, 4, 5]) await items.nth(at).click();
  for (const item of await items.all()) await expect(item).toHaveAttribute('aria-pressed', 'true');
  expect(await series()).toHaveLength(7);

  // The timeline folds the same people into "Others".
  await tab(page, 'Events').click();
  await expect(page.locator('.vp-event-card').nth(1).locator('.vp-event-who')).toHaveText(['Ana1 message · 0 words', 'José1 message · 0 words', 'Li1 message · 8 words', 'Others2 messages · 7 words']);

  // One person alone needs no legend.
  await page.evaluate(() => {
    window.__handle.destroy();
    const solo = JSON.parse(JSON.stringify(window.__model));
    solo.participants = ['Ana'];
    for (const message of solo.messages) if (message.sender != null) message.sender = 0;
    window.__handle = window.__viewer.mountViewer(window.__root, solo, {});
  });
  await tab(page, 'Activity').click();
  await expect(chartKeys(page)).toHaveCount(4);
  await expect(page.locator('.vp-legend')).toBeHidden();
  expect(await series()).toEqual(['vp-s1']);
});

test('buckets whose voice notes are not all transcribed or timed are marked, and say so', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await tab(page, 'Activity').click();
  const tip = page.locator('.vp-chart-tip');
  const legend = page.getByRole('group', { name: 'Participants in the charts' });
  await expect(page.locator('.vp-panel-activity .vp-coverage-line')).toHaveCount(2);
  await expect(page.locator('.vp-chart-note')).toHaveText(['', '5 of 8 voice notes transcribed', '', '5 of 8 voice notes timed']);
  // 10 and 18 January hold José's voice notes without a transcript, and two without a known length.
  expect(await marks(page, '.vp-chart-gaps')).toEqual([0, 2, 0, 2]);
  await expect(page.locator('.vp-key')).toHaveText(['Typed words', 'Spoken words', 'Weekend', 'Voice notes without a transcript or a length']);
  const dots = await page.evaluate(() => {
    const plot = window.__root.querySelector('.vp-chart-plot[data-vp-chart="1"]');
    const box = plot.querySelector('.vp-chart-gaps').getBoundingClientRect();
    const first = plot.getBoundingClientRect();
    return { from: box.left - first.left, to: box.right - first.left, visible: getComputedStyle(plot.querySelector('.vp-chart-gaps')).fill !== getComputedStyle(window.__root).backgroundColor };
  });
  const { step } = await bucketPoint(page, 1, 0);
  expect(Math.floor(dots.from / step)).toBe(5);
  expect(Math.floor(dots.to / step)).toBe(13);
  expect(dots.visible).toBe(true);
  const words = await bucketPoint(page, 1, 13);
  await page.mouse.move(words.x, words.y);
  await expect(tip.locator('.vp-tip-title')).toHaveText(/^Sun,? 18 Jan 2026$/);
  await expect(tip.locator('.vp-tip-note')).toHaveText('Spoken words cover 1 of 2 voice notes here');
  await page.mouse.move(words.x + words.step, words.y);
  await expect(tip.locator('.vp-tip-title')).toHaveText(/^Mon,? 19 Jan 2026$/);
  await expect(tip.locator('.vp-tip-note')).toHaveCount(0);
  const minutes = await bucketPoint(page, 3, 13);
  await page.mouse.move(minutes.x, minutes.y);
  await expect(tip.locator('.vp-tip-row')).toHaveText(['Ana0 s', 'Joséunknown']);
  await expect(tip.locator('.vp-tip-note')).toHaveText('Voice time covers 0 of 2 voice notes here');
  // The charts of messages and of voice notes are complete: no mark and no note on that day.
  const count = await bucketPoint(page, 2, 13);
  await page.mouse.move(count.x, count.y);
  await expect(tip.locator('.vp-tip-row')).toHaveText(['Ana0', 'José2']);
  await expect(tip.locator('.vp-tip-note')).toHaveCount(0);
  await page.mouse.move(5, 5);

  // The marks belong to the bars that are shown: without José nothing is missing.
  await legend.getByRole('button', { name: 'José' }).click();
  expect(await marks(page, '.vp-chart-gaps')).toEqual([0, 0, 0, 0]);
  await expect(page.locator('.vp-key')).toHaveText(['Typed words', 'Spoken words', 'Weekend']);
  await legend.getByRole('button', { name: 'José' }).click();
  expect(await marks(page, '.vp-chart-gaps')).toEqual([0, 2, 0, 2]);

  // A transcript whose length is not known completes the words of its day, not its minutes.
  await page.evaluate(() => {
    Object.assign(window.__model.messages[21].voice, { status: 'ok', text: 'hola', words: 1 });
    window.__handle.update(window.__model, [21]);
  });
  expect(await marks(page, '.vp-chart-gaps')).toEqual([0, 1, 0, 2]);
  await expect(page.locator('.vp-chart-note')).toHaveText(['', '6 of 8 voice notes transcribed', '', '5 of 8 voice notes timed']);

  // Once everything is transcribed and timed there is nothing to mark.
  await page.evaluate(() => {
    for (const message of window.__model.messages) if (message.voice) Object.assign(message.voice, { status: 'ok', seconds: 3, text: 'hola', words: 1 });
    window.__handle.update(window.__model);
  });
  expect(await marks(page, '.vp-chart-gaps')).toEqual([0, 0, 0, 0]);
  await expect(page.locator('.vp-chart-note')).toHaveText(['', '', '', '']);
  await expect(page.locator('.vp-key')).toHaveText(['Typed words', 'Spoken words', 'Weekend']);
  await expect(page.locator('.vp-panel-activity .vp-coverage')).toBeHidden();
  // A chat without voice notes keeps its four charts and says that two are empty.
  await page.evaluate(() => {
    for (const message of window.__model.messages) {
      if (message.voice) {
        message.kind = 'text';
        delete message.voice;
      }
    }
    window.__handle.update(window.__model);
  });
  await expect(page.locator('.vp-chart-note')).toHaveText(['', '', 'nothing to show', 'nothing to show']);
  await expect(chartKeys(page)).toHaveCount(4);
});

test('update() and setEvents() keep the grouping, the hidden participant, the place, the focus and the pinned event of the charts', async ({ page }) => {
  await page.setViewportSize({ width: 400, height: 900 });
  await openViewer(page);
  await mount(page, { model: await fixture() });
  await tab(page, 'Activity').click();
  const keys = chartKeys(page);
  const legend = page.getByRole('group', { name: 'Participants in the charts' });
  await markers(page).nth(2).click();
  await legend.getByRole('button', { name: 'Ana' }).click();
  await keys.nth(1).focus();
  await page.keyboard.press('End');
  await page.evaluate(() => {
    window.__scroll = window.__root.querySelector('.vp-chart-scroll');
    window.__kept = [...window.__root.querySelectorAll('.vp-chart-key, .vp-chart-plot, .vp-seg-input, .vp-chart-tip')];
    window.__left = window.__scroll.scrollLeft;
  });
  expect(await page.evaluate(() => window.__left)).toBeGreaterThan(0);
  const same = () => page.evaluate(() => {
    const now = [...window.__root.querySelectorAll('.vp-chart-key, .vp-chart-plot, .vp-seg-input, .vp-chart-tip')];
    return [window.__root.querySelector('.vp-chart-scroll') === window.__scroll, now.length === window.__kept.length && now.every((node, at) => node === window.__kept[at]), window.__scroll.scrollLeft === window.__left];
  });
  for (const [changed, transcribed] of [[[12], '6 of 8'], [undefined, '7 of 8']]) {
    await page.evaluate(([indices, index]) => {
      Object.assign(window.__model.messages[index].voice, { status: 'ok', text: 'uno dos tres', words: 3, seconds: 12 });
      delete window.__model.messages[index].voice.error;
      window.__model = JSON.parse(JSON.stringify(window.__model));
      window.__handle.update(window.__model, indices);
    }, [changed, changed ? 12 : 13]);
    // The numbers are new, at once: in the titles, in the table and in the bars (José now has spoken words on two days) …
    await expect(page.locator('.vp-chart-note').nth(1)).toHaveText(`${transcribed} voice notes transcribed`);
    await expect(page.getByRole('table', { name: 'Activity per participant' }).locator('tbody tr').nth(1)).toContainText(changed ? '3 of 5' : '4 of 5');
    expect(await marks(page, '.vp-bar-spoken')).toEqual([2]);
    // … and everything the reader set is as it was, in the same elements.
    expect(await same()).toEqual([true, true, true]);
    await expect(keys.nth(1)).toBeFocused();
    await expect(keys.nth(1)).toHaveValue('36');
    await expect(page.locator('.vp-chart-tip')).toBeVisible();
    await expect(page.getByRole('radio', { name: 'Day' })).toBeChecked();
    await expect(legend.getByRole('button', { name: 'Ana' })).toHaveAttribute('aria-pressed', 'false');
    await expect(markers(page).nth(2)).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.vp-pin-label')).toHaveText(['Ana away, no signal']);
    // The day itself, read with the keyboard, and back to the end.
    await keys.nth(1).fill('5');
    await expect(chartStatus(page)).toContainText(changed ? 'Words: José 3 (0 typed, 3 spoken).' : 'Words: José 6 (0 typed, 6 spoken).');
    await page.keyboard.press('End');
    expect(await same()).toEqual([true, true, true]);
  }
  // A button of the pinned strip keeps the keyboard as well, though the strip is written again.
  const shown = page.getByRole('region', { name: 'Selected event' }).getByRole('button', { name: 'Show in Events' });
  await shown.focus();
  await page.evaluate(() => window.__handle.update(window.__model, [12]));
  await expect(shown).toBeFocused();
  // The day of an event in the timeline follows too, and the card being used keeps the keyboard.
  await tab(page, 'Events').click();
  const card = page.locator('.vp-event-card').nth(2);
  await expect(card.locator('.vp-event-who').nth(1)).toHaveText('José5 messages · 6 words · 26 s of voice');
  await expect(card.locator('.vp-event-cover')).toHaveCount(0);
  await card.getByRole('button', { name: 'Open this day' }).focus();
  // An update that arrives while another tab is open is drawn when the charts are next shown, at the same place.
  await page.evaluate(() => {
    Object.assign(window.__model.messages[11].voice, { seconds: 60 });
    Object.assign(window.__model.messages[24].voice, { seconds: 60 });
    window.__handle.update(window.__model, [11, 24]);
  });
  await expect(card.locator('.vp-event-who').nth(0)).toHaveText('Ana5 messages · 103 words · 1 min 0 s of voice');
  await expect(card.getByRole('button', { name: 'Open this day' })).toBeFocused();
  await tab(page, 'Activity').click();
  await expect(page.locator('.vp-chart-note').nth(3)).toHaveText('7 of 8 voice notes timed');
  expect(await same()).toEqual([true, true, true]);

  // New events renumber the markers; the pinned event is kept when it is still among them, and let go when it is not.
  await page.evaluate(() => window.__handle.setEvents([{ date: '2026-01-25', label: 'Ana away, no signal' }, { date: '2026-01-06', label: 'Earlier' }]));
  await expect(markers(page)).toHaveText(['1', '2']);
  await expect(markers(page).nth(1)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.vp-pin-item .vp-event-number')).toHaveText(['2']);
  await page.evaluate(() => window.__handle.setEvents([{ date: '2026-01-06', label: 'Earlier' }]));
  await expect(markers(page)).toHaveText(['1']);
  await expect(markers(page).first()).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.vp-pin')).toBeHidden();
  await expect(page.getByRole('radio', { name: 'Day' })).toBeChecked();
  await expect(legend.getByRole('button', { name: 'Ana' })).toHaveAttribute('aria-pressed', 'false');
});

test('the charts of a 100 000-message chat open by week, at once and with few elements', async ({ page }) => {
  await page.setViewportSize({ width: 1000, height: 900 });
  await openViewer(page);
  await page.evaluate(makeBigModel, BIG);
  await mount(page);
  const buckets = await page.evaluate(() => Object.fromEntries(['day', 'week', 'month'].map((bucket) => [bucket, window.__viewer.aggregate(window.__model, bucket).buckets.length])));
  expect(buckets.day).toBeGreaterThan(730);
  const started = Date.now();
  await tab(page, 'Activity').click();
  await expect(page.getByRole('radio', { name: 'Week' })).toBeChecked();
  await expect(chartKeys(page).first()).toHaveAttribute('max', String(buckets.week - 1));
  expect(Date.now() - started).toBeLessThan(5000);
  // Every series of a chart is one path, however many bars it holds.
  const drawn = () => page.evaluate(() => ({ charts: window.__root.querySelectorAll('.vp-charts *').length, bars: window.__root.querySelectorAll('.vp-chart-plot[data-vp-chart="0"] .vp-bar').length }));
  expect((await drawn()).bars).toBe(2);
  expect((await drawn()).charts).toBeLessThan(1000);
  expect(await elementCount(page)).toBeLessThan(BUDGET);
  // By day it is 13 200 pixels of chart and still a handful of elements.
  const switched = Date.now();
  await page.getByRole('radio', { name: 'Day' }).check();
  await expect(chartKeys(page).first()).toHaveAttribute('max', String(buckets.day - 1));
  expect(Date.now() - switched).toBeLessThan(5000);
  expect((await bucketPoint(page, 0, 0)).step).toBe(12);
  expect((await drawn()).charts).toBeLessThan(1000);
  expect((await marks(page, '.vp-chart-plot[data-vp-chart="0"] .vp-bar')).every((count) => count > 800)).toBe(true);
  expect(await elementCount(page)).toBeLessThan(BUDGET);
  // The long day is one tall bar; its bucket opens in the conversation at the first of its 20 000 messages.
  const long = await page.evaluate(() => window.__viewer.aggregate(window.__model, 'day').buckets.indexOf(window.__model.events[0].date));
  await chartKeys(page).first().focus();
  await chartKeys(page).first().fill(String(long));
  await expect(chartStatus(page)).toContainText('Event 1: The long day');
  await page.keyboard.press('Enter');
  await settle(page);
  expect((await reading(page)).index).toBe(BIG.count / 2);
  expect(await elementCount(page)).toBeLessThan(BUDGET);
});
