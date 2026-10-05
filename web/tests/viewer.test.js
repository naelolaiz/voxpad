import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { aggregate, countWords, mountViewer, parseEvents, totals } from '../../voxpad/viewer/viewer.js';

const read = (path) => readFile(new URL(path, import.meta.url), 'utf8');
const source = await read('../../voxpad/viewer/viewer.js');
const styles = await read('../../voxpad/viewer/viewer.css');
const fixture = JSON.parse(await read('./fixtures/viewer-model.json'));

const METRICS = ['messages', 'words_typed', 'words_spoken', 'voice_notes', 'voice_seconds', 'voice_transcribed', 'voice_timed'];
const BUCKETS = ['day', 'week', 'month'];
const ZONES = [['America/New_York', 300], ['Asia/Kolkata', -330]];
// Section 3.2 of the analysis rules: exactly these code points separate words.
const WHITESPACE = [
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff,
];
const hex = (code) => `U+${code.toString(16).toUpperCase().padStart(4, '0')}`;

/** Run with another local time zone, making sure the switch took effect: otherwise the test would guard nothing. */
function inZone([zone, offset], run) {
  const before = process.env.TZ;
  process.env.TZ = zone;
  try {
    assert.equal(new Date(2026, 0, 15, 12).getTimezoneOffset(), offset, `this Node cannot switch to ${zone}`);
    return run();
  } finally {
    if (before === undefined) delete process.env.TZ; else process.env.TZ = before;
  }
}

/** Compare aggregates or totals: voice_seconds within 1e-6, everything else exactly. */
function assertMetrics(actual, expected, label) {
  const strip = (value) => JSON.parse(JSON.stringify(value, (key, item) => (key === 'voice_seconds' ? undefined : item)));
  assert.deepEqual(strip(actual), strip(expected), label);
  const seconds = (value) => (Array.isArray(value) ? value : value.series).map((row) => [row.voice_seconds].flat());
  const [got, wanted] = [seconds(actual), seconds(expected)];
  assert.equal(got.length, wanted.length, label);
  got.forEach((row, person) => {
    assert.equal(row.length, wanted[person].length, label);
    row.forEach((value, at) => assert.ok(Math.abs(value - wanted[person][at]) <= 1e-6, `${label}: voice_seconds of participant ${person} at ${at}`));
  });
}

const voice = (status, seconds, words, extra = {}) => ({ file: status === 'missing' ? null : 'PTT-1.opus', src: null, seconds, status, text: 'x '.repeat(words).trim(), words, ...extra });
const message = (time, sender, kind, words, extra = {}) => ({ time, sender, kind, text: 'x '.repeat(words).trim(), words, ...extra });

// Sunday 28 December 2025 to Monday 5 January 2026: two months, three weeks, a year boundary, messages without a
// time at the start and in the middle, and one message dated before its predecessors.
const boundary = {
  schema: 1, title: 'Ana · José', date_order: 'DMY', date_order_ambiguous: false, participants: ['Ana', 'José'], events: [], warnings: [],
  messages: [
    message(null, 1, 'text', 5),
    message('2025-12-28T10:00:00', null, 'system', 0),
    message('2025-12-28T10:01:00', 0, 'text', 3),
    message('2025-12-31T23:59:59', 1, 'voice', 0, { voice: voice('ok', 30.5, 12) }),
    message(null, 0, 'text', 2),
    message('2026-01-01T00:00:00', 1, 'text', 4),
    message('2026-01-05T08:00:00', 0, 'voice', 1, { voice: voice('pending', null, 0) }),
    message('2026-01-05T09:00:00', 1, 'voice', 0, { voice: voice('empty', 0.25, 0) }),
    message('2026-01-04T22:00:00', 0, 'voice', 0, { voice: voice('error', 2, 0, { error: 'failed' }) }),
    message('2026-01-05T10:00:00', 1, 'media', 0, { media: { type: 'omitted', file: null } }),
    message('2026-01-05T10:01:00', 0, 'deleted', 0),
  ],
};
const series = (rows) => rows.map((row) => Object.fromEntries(METRICS.map((metric, at) => [metric, row[at]])));
const boundaryExpected = {
  day: {
    bucket: 'day',
    buckets: ['2025-12-28', '2025-12-29', '2025-12-30', '2025-12-31', '2026-01-01', '2026-01-02', '2026-01-03', '2026-01-04', '2026-01-05'],
    series: series([
      [[1, 0, 0, 1, 0, 0, 0, 1, 2], [3, 0, 0, 2, 0, 0, 0, 0, 1], [0, 0, 0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 0, 1, 1], [0, 0, 0, 0, 0, 0, 0, 2, 0], [0, 0, 0, 0, 0, 0, 0, 0, 0], [0, 0, 0, 0, 0, 0, 0, 1, 0]],
      [[1, 0, 0, 1, 1, 0, 0, 0, 2], [5, 0, 0, 0, 4, 0, 0, 0, 0], [0, 0, 0, 12, 0, 0, 0, 0, 0], [0, 0, 0, 1, 0, 0, 0, 0, 1], [0, 0, 0, 30.5, 0, 0, 0, 0, 0.25], [0, 0, 0, 1, 0, 0, 0, 0, 1], [0, 0, 0, 1, 0, 0, 0, 0, 1]],
    ]),
  },
  week: {
    bucket: 'week',
    buckets: ['2025-12-22', '2025-12-29', '2026-01-05'],
    series: series([
      [[1, 2, 2], [3, 2, 1], [0, 0, 0], [0, 1, 1], [0, 2, 0], [0, 0, 0], [0, 1, 0]],
      [[1, 2, 2], [5, 4, 0], [0, 12, 0], [0, 1, 1], [0, 30.5, 0.25], [0, 1, 1], [0, 1, 1]],
    ]),
  },
  month: {
    bucket: 'month',
    buckets: ['2025-12-01', '2026-01-01'],
    series: series([
      [[2, 3], [5, 1], [0, 0], [0, 2], [0, 2], [0, 0], [0, 1]],
      [[2, 3], [5, 4], [12, 0], [1, 1], [30.5, 0.25], [1, 1], [1, 1]],
    ]),
  },
};
const boundaryTotals = series([[5, 6, 0, 2, 2, 0, 1], [5, 9, 12, 2, 30.75, 2, 2]]);
// Counted by a separate script, not by the code under test.
const fixtureTotals = series([[17, 109, 138, 3, 223, 3, 3], [16, 39, 8, 5, 14.3, 2, 2]]);

test('the module has no imports, one trailing export statement and none of the constructs the page forbids', () => {
  const lines = source.split('\n');
  const written = lines.filter((line) => line.trim());
  assert.equal(written.at(-1), 'export { mountViewer, aggregate, totals, parseEvents, countWords };');
  assert.deepEqual(written.slice(0, -1).filter((line) => /^(import|export)\b/.test(line)), []);
  assert.ok(!source.includes('\r'));
  const forbidden = [
    'innerHTML', 'outerHTML', 'insertAdjacentHTML', 'document.write', 'eval(', 'new Function', "setAttribute('style'", 'setAttribute("style"',
    'localStorage', 'sessionStorage', 'indexedDB', 'caches.', 'document.cookie', 'fetch(', 'XMLHttpRequest', 'WebSocket', 'sendBeacon',
    'new Worker', 'import(', 'import.meta',
    // Python inlines the file verbatim into a script element.
    '<script', '</script', '<!--', '-->',
    // A search is a plain substring search, and chat times are never handed to the platform's date parser.
    'RegExp(', 'Date.parse', 'toLocaleDateString', 'toLocaleTimeString', 'getTimezoneOffset',
    // Newer than the browser floor (Chrome 97, Firefox 102, Safari 16).
    'structuredClone', 'findLast', 'toSorted', 'toReversed', 'toSpliced', 'groupBy', 'fromAsync', 'withResolvers',
  ];
  const lower = source.toLowerCase();
  assert.deepEqual(forbidden.filter((text) => lower.includes(text.toLowerCase())), []);
  assert.deepEqual(source.match(/\.(?:get|set)(?:FullYear|Month|Date|Day|Hours|Minutes|Seconds)\(/g), null, 'local-time accessors');
  assert.deepEqual((source.match(/new Date\([^)]*/g) || []).filter((call) => !call.startsWith('new Date(Date.UTC(')), []);
  const formats = lines.filter((line) => line.includes('Intl.DateTimeFormat('));
  assert.ok(formats.length > 0);
  assert.deepEqual(formats.filter((line) => !line.includes("timeZone: 'UTC'")), []);
  assert.doesNotMatch(source, /^\s*await\b/m);
});

test('importing the module needs no DOM, and mounting without a root is refused', () => {
  assert.equal(typeof globalThis.document, 'undefined');
  assert.throws(() => mountViewer(null, fixture), TypeError);
  assert.throws(() => mountViewer({}, { participants: [] }), TypeError);
});

test('the stylesheet isolates itself, scopes every rule and uses nothing the inlined page cannot carry', () => {
  const rules = styles.split('\n').filter((line) => line.trim());
  assert.equal(rules[0], '.voxpad-viewer, .voxpad-viewer *, .voxpad-viewer *::before, .voxpad-viewer *::after { box-sizing: border-box; }');
  assert.equal(rules[1], '.voxpad-viewer :where(button, select, input, textarea, details, summary, progress, table, h2, h3, h4, p, ul, ol, li, a) { all: revert; box-sizing: border-box; }');
  assert.equal(rules[2], '.voxpad-viewer [hidden] { display: none !important; }');
  const lower = styles.toLowerCase();
  assert.deepEqual(['@import', 'url(', 'light-dark(', '@scope', '@layer', '@container', 'container-type', ':has(', '</style', 'expression(']
    .filter((text) => lower.includes(text)), []);
  // No smooth scrolling anywhere (overscroll-behavior is another property).
  assert.doesNotMatch(lower, /(?<![a-z-])scroll-behavior/);
  assert.doesNotMatch(styles, /outline(?:-style|-width)?\s*:\s*(?:none|0)\b/, 'focus outlines are never removed');
  // Every selector starts at the root class; apart from the preamble and the root itself, each one names a vp- class.
  const bare = styles.replace(/\/\*[\s\S]*?\*\//g, '').replace(/@media[^{]*\{/g, '').replace(/@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*\s*\}/g, '');
  const selectors = [...bare.matchAll(/(?:^|\})\s*([^{}]+)\{/g)].flatMap((match) => match[1].split(',')).map((selector) => selector.trim()).filter(Boolean);
  assert.ok(selectors.length > 50);
  const root = /^\.voxpad-viewer(?:\[data-vp-theme="auto"\])?$/;
  const preamble = new Set(['.voxpad-viewer *', '.voxpad-viewer *::before', '.voxpad-viewer *::after', '.voxpad-viewer [hidden]']);
  const strays = selectors.filter((selector) => !root.test(selector) && !preamble.has(selector)
    && !selector.startsWith('.voxpad-viewer :where(') && !/^\.voxpad-viewer(?:\[data-vp-theme="auto"\])? \.vp-[a-z0-9-]+/.test(selector)
    // The continuation of the :where() list of the preamble.
    && !/^[a-z0-9]+\)?$/.test(selector));
  assert.deepEqual(strays, []);
});

test('text meets a contrast of 4.5:1 on every surface it is drawn on, in the light and the dark theme', () => {
  const tokens = (block) => Object.fromEntries([...block.matchAll(/--vp-([a-z0-9-]+):\s*(#[0-9a-f]{6})\b/g)].map((match) => [match[1], match[2]]));
  const light = tokens(styles.slice(styles.indexOf('.voxpad-viewer {'), styles.indexOf('@media (prefers-color-scheme: dark)')));
  const darkStart = styles.indexOf('@media (prefers-color-scheme: dark)');
  const dark = tokens(styles.slice(darkStart, styles.indexOf('}', styles.indexOf('color-scheme: dark;', darkStart))));
  assert.deepEqual(Object.keys(dark).sort(), Object.keys(light).sort(), 'the dark theme redefines every colour');
  const channel = (value) => (value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  const luminance = (colour) => [1, 3, 5].map((at) => channel(parseInt(colour.slice(at, at + 2), 16) / 255))
    .reduce((sum, value, at) => sum + value * [0.2126, 0.7152, 0.0722][at], 0);
  const contrast = (one, other) => {
    const [high, low] = [luminance(one), luminance(other)].sort((left, right) => right - left);
    return (high + 0.05) / (low + 0.05);
  };
  const surfaces = ['paper', 'surface', 'me', 'them'];
  const pairs = [
    ...['ink', 'muted', 'accent', 'problem', 'p0', 'p1', 'p2', 'p3', 'p4', 'p5', 'p6'].flatMap((text) => surfaces.map((surface) => [text, surface])),
    ['on-accent', 'accent'], ['mark-ink', 'mark'], ['warn', 'warn-bg'], ['event', 'event-bg'], ['event-bg', 'event'],
    // Charts and events: notes in the warning colour on the plain surface, and the strip of a pinned event.
    ['warn', 'surface'], ['ink', 'event-bg'], ['accent', 'event-bg'], ['muted', 'shade'],
  ];
  for (const [name, theme] of [['light', light], ['dark', dark]]) {
    for (const [text, surface] of pairs) {
      assert.ok(theme[text] && theme[surface], `${name}: --vp-${text} and --vp-${surface} are defined`);
      const ratio = contrast(theme[text], theme[surface]);
      assert.ok(ratio >= 4.5, `${name}: --vp-${text} on --vp-${surface} is ${ratio.toFixed(2)}:1`);
    }
    // Focus rings, the outlines of matches and the marks of the charts are not text: 3:1 against what they are drawn on.
    for (const [line, surface] of [...surfaces.map((surface) => ['focus', surface]), ['mark-line', 'paper'], ['mark-line', 'surface'], ['accent', 'paper'],
      ['event', 'surface'], ['warn', 'shade'], ['c0', 'surface']]) {
      const ratio = contrast(theme[line], theme[surface]);
      assert.ok(ratio >= 3, `${name}: --vp-${line} against --vp-${surface} is ${ratio.toFixed(2)}:1`);
    }
  }
  // The bars wear six colours that were validated as a set, in this order, for colour-blind readers on both surfaces
  // (adjacent pairs at least 8 apart in OKLab under simulated protanopia and deuteranopia, 15 for full-colour vision).
  // Another colour or another order has to be validated again before it is written here.
  const series = (theme) => [1, 2, 3, 4, 5, 6].map((slot) => theme[`c${slot}`]);
  assert.deepEqual(series(light), ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300']);
  assert.deepEqual(series(dark), ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300']);
  assert.equal(light.surface, '#fffefa');
  assert.equal(dark.surface, '#18221d');
  // Three of the light fills are below 3:1 on the surface: the legend, the tooltip, the status region and the table
  // carry every value they show, and the bars are told apart by position and by the gap between them.
  for (const [name, theme] of [['light', light], ['dark', dark]]) {
    for (const colour of series(theme)) assert.ok(contrast(colour, theme.surface) >= 2, `${name}: ${colour} on the surface`);
  }
});

test('a word is a run of characters between exactly the whitespace of the shared rule', () => {
  assert.equal(countWords(''), 0);
  assert.equal(countWords('   \n\t '), 0);
  assert.equal(countWords('one'), 1);
  assert.equal(countWords('  Sí, todo   bien.\nThe train… was on-time 😍 '), 8);
  assert.equal(countWords(undefined), 0);
  for (const code of WHITESPACE) {
    const space = String.fromCodePoint(code);
    assert.equal(countWords(`a${space}b`), 2, hex(code));
    assert.equal(countWords(`${space}a${space}${space}b${space}`), 2, hex(code));
  }
  // Look-alikes that neither Python's split() nor the rule treats as whitespace.
  for (const code of [0x00ad, 0x180e, 0x200b, 0x200c, 0x200d, 0x200e, 0x2060, 0x2800, 0x3164]) {
    assert.equal(countWords(`a${String.fromCodePoint(code)}b`), 1, hex(code));
  }
});

test('an events file gives one dated event per line, sorted by date, and warnings that name only a line number', () => {
  const text = [
    '# birthdays and trips',
    '',
    '2026-01-17, First date',
    '14/02/2026 | Valentine',
    '01.03.26\tMoved to Berlin',
    '2026/01/05 New year party',
    '03-04-2026',
    '12/31/2025 month first',
    'no date in this line',
    '31/02/2026 impossible',
    '2026-13-01 thirteenth month',
    '   17.01.2026   ,  Second on that day  ',
    '  # indented comment',
  ].join('\n');
  const { events, warnings } = parseEvents(text);
  assert.deepEqual(events, [
    { date: '2025-12-31', label: 'month first' },
    { date: '2026-01-05', label: 'New year party' },
    { date: '2026-01-17', label: 'First date' },
    { date: '2026-01-17', label: 'Second on that day' },
    { date: '2026-02-14', label: 'Valentine' },
    { date: '2026-03-01', label: 'Moved to Berlin' },
    { date: '2026-04-03', label: '2026-04-03' },
  ]);
  assert.equal(warnings.length, 3);
  [9, 10, 11].forEach((line, at) => {
    assert.deepEqual(warnings[at].match(/[0-9]+/g), [String(line)]);
    assert.doesNotMatch(warnings[at], /date in this|impossible|thirteenth|2026/);
  });
});

test('events lines end at CR LF, CR and LF only, and nothing else is read as a date', () => {
  const separator = String.fromCodePoint(0x2028);
  const feed = String.fromCodePoint(0x0c);
  const { events, warnings } = parseEvents(`2026-01-03 c\r\nbad\r2026-01-02 b${separator}still b\n2026-01-01 a${feed}still a`);
  assert.deepEqual(events, [
    { date: '2026-01-01', label: `a${feed}still a` },
    { date: '2026-01-02', label: `b${separator}still b` },
    { date: '2026-01-03', label: 'c' },
  ]);
  assert.equal(warnings.length, 1);
  assert.deepEqual(warnings[0].match(/[0-9]+/g), ['2']);
  assert.deepEqual(parseEvents(''), { events: [], warnings: [] });
  assert.deepEqual(parseEvents('\n\n# only a comment\n'), { events: [], warnings: [] });
  // A leap day exists only in a leap year, and a two-digit year is in this century.
  assert.deepEqual(parseEvents('29/02/24 leap\n29/02/2023 not one').events, [{ date: '2024-02-29', label: 'leap' }]);
  // Digits of other scripts are not dates: the rule is the same in Python and here.
  assert.deepEqual(parseEvents('٢٠٢٦-٠١-٠١ x').events, []);
});

test('aggregates cover contiguous days, Monday weeks and months, and place messages without a time', () => {
  for (const bucket of BUCKETS) assertMetrics(aggregate(boundary, bucket), boundaryExpected[bucket], bucket);
  assertMetrics(aggregate(boundary), boundaryExpected.day, 'default bucket');
  assertMetrics(totals(boundary), boundaryTotals, 'totals');
  assert.throws(() => aggregate(boundary, 'year'), RangeError);
});

test('a chat without any time has no buckets but keeps its totals', () => {
  const timeless = { ...boundary, messages: boundary.messages.map((item) => ({ ...item, time: null })) };
  const empty = series([[[], [], [], [], [], [], []], [[], [], [], [], [], [], []]]);
  for (const bucket of BUCKETS) assert.deepEqual(aggregate(timeless, bucket), { bucket, buckets: [], series: empty });
  assertMetrics(totals(timeless), boundaryTotals, 'totals');
  assert.deepEqual(aggregate({ ...boundary, messages: [] }, 'week'), { bucket: 'week', buckets: [], series: empty });
  assert.deepEqual(aggregate({ ...boundary, participants: [], messages: [] }), { bucket: 'day', buckets: [], series: [] });
  assert.deepEqual(totals({ ...boundary, participants: [], messages: [] }), []);
});

test('buckets follow the Gregorian calendar across leap days and century years', () => {
  const days = (first, last) => aggregate({ ...boundary, messages: [message(`${first}T12:00:00`, 0, 'text', 1), message(`${last}T12:00:00`, 1, 'text', 1)] }, 'day').buckets;
  assert.deepEqual(days('2024-02-28', '2024-03-01'), ['2024-02-28', '2024-02-29', '2024-03-01']);
  assert.deepEqual(days('2023-02-28', '2023-03-01'), ['2023-02-28', '2023-03-01']);
  assert.deepEqual(days('2100-02-28', '2100-03-01'), ['2100-02-28', '2100-03-01']);
  assert.deepEqual(days('2000-02-28', '2000-03-01'), ['2000-02-28', '2000-02-29', '2000-03-01']);
  // The latest day is the maximum, not the last in file order.
  assert.deepEqual(days('2026-03-09', '2026-03-07'), ['2026-03-07', '2026-03-08', '2026-03-09']);
  const one = (day, bucket) => aggregate({ ...boundary, messages: [message(`${day}T00:30:00`, 0, 'text', 1)] }, bucket).buckets;
  assert.deepEqual(one('2026-03-08', 'week'), ['2026-03-02']);
  assert.deepEqual(one('2026-03-09', 'week'), ['2026-03-09']);
  assert.deepEqual(one('2024-12-31', 'week'), ['2024-12-30']);
  assert.deepEqual(one('2027-01-01', 'week'), ['2026-12-28']);
  assert.deepEqual(one('2026-10-25', 'month'), ['2026-10-01']);
  assert.deepEqual(one('0099-12-31', 'week'), ['0099-12-28']);
});

test('the viewer fixture is a consistent schema 1 model with the totals counted by hand', () => {
  assert.equal(fixture.schema, 1);
  assert.deepEqual(Object.keys(fixture).sort(), ['date_order', 'date_order_ambiguous', 'events', 'messages', 'participants', 'schema', 'title', 'warnings']);
  const kinds = new Set();
  const statuses = new Set();
  const media = new Set();
  for (const item of fixture.messages) {
    kinds.add(item.kind);
    assert.deepEqual(Object.keys(item).filter((key) => !['edited', 'media', 'voice'].includes(key)).sort(), ['kind', 'sender', 'text', 'time', 'words']);
    assert.equal(item.words, item.sender == null ? 0 : countWords(item.text));
    assert.equal(item.kind === 'voice', 'voice' in item);
    assert.equal(item.kind === 'media', 'media' in item);
    assert.equal(item.kind === 'system', item.sender === null);
    if (item.time !== null) assert.match(item.time, /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}$/);
    if (item.media) media.add(item.media.type);
    if (item.voice) {
      statuses.add(item.voice.status);
      assert.equal(item.voice.words, countWords(item.voice.text));
      assert.equal('error' in item.voice, item.voice.status === 'error');
    }
  }
  assert.deepEqual([...kinds].sort(), ['deleted', 'media', 'system', 'text', 'voice']);
  assert.deepEqual([...statuses].sort(), ['empty', 'error', 'missing', 'ok', 'pending']);
  assert.deepEqual([...media].sort(), ['contact', 'document', 'gif', 'image', 'omitted', 'sticker', 'video']);
  assert.deepEqual(parseEvents(fixture.events.map((event) => `${event.date} ${event.label}`).join('\n')).events, fixture.events);
  assertMetrics(totals(fixture), fixtureTotals, 'totals');
  const day = aggregate(fixture, 'day');
  assert.equal(day.buckets.length, 28);
  assert.deepEqual([day.buckets[0], day.buckets[27]], ['2026-01-05', '2026-02-01']);
  // The message without a time (index 23) is counted on the day of the message before it.
  assert.equal(day.series[1].messages[day.buckets.indexOf('2026-01-18')], 3);
  const week = aggregate(fixture, 'week');
  assert.deepEqual(week.buckets, ['2026-01-05', '2026-01-12', '2026-01-19', '2026-01-26']);
  assert.deepEqual(week.series.map((row) => row.messages), [[10, 1, 3, 3], [10, 3, 1, 2]]);
  const month = aggregate(fixture, 'month');
  assert.deepEqual(month.buckets, ['2026-01-01', '2026-02-01']);
  assert.deepEqual(month.series.map((row) => row.messages), [[14, 3], [15, 1]]);
  for (const result of [day, week, month]) {
    const sums = result.series.map((row) => Object.fromEntries(METRICS.map((metric) => {
      let sum = 0;
      for (const value of row[metric]) sum += value;
      return [metric, sum];
    })));
    assertMetrics(sums, fixtureTotals, `${result.bucket} buckets add up to the totals`);
  }
});

test('aggregates do not depend on the time zone of the machine', () => {
  const everything = () => JSON.stringify([boundary, fixture].map((model) => [BUCKETS.map((bucket) => aggregate(model, bucket)), totals(model)]));
  const reference = everything();
  for (const zone of ZONES) {
    inZone(zone, () => {
      assert.equal(everything(), reference, zone[0]);
      for (const bucket of BUCKETS) assertMetrics(aggregate(boundary, bucket), boundaryExpected[bucket], `${zone[0]} ${bucket}`);
    });
  }
});

// The cases Python is tested against too (tests/fixtures/analysis_cases.json): the fixture is the arbiter between the
// two implementations, so nothing here is derived from the code under test.
test('the shared analysis cases give the same words, events, aggregates and totals as in Python, in any time zone', async () => {
  const shared = JSON.parse(await read('../../tests/fixtures/analysis_cases.json'));
  assert.ok(shared.words.length >= 30 && shared.events.length >= 3 && shared.cases.length >= 10, 'the fixture holds its cases');
  const seen = { dated: 0, undated: 0, years: 0, timeless: 0, voice: 0, events: 0 };
  const check = (zone) => {
    for (const { text, count } of shared.words) assert.equal(countWords(text), count, `${zone}: ${JSON.stringify(text)}`);
    for (const { text, events, warnings } of shared.events) assert.deepEqual(parseEvents(text), { events, warnings }, `${zone}: ${JSON.stringify(text)}`);
    for (const item of shared.cases) {
      const label = `${zone} ${item.name}`;
      const before = JSON.stringify(item.model);
      for (const bucket of BUCKETS) {
        const result = aggregate(item.model, bucket);
        assertMetrics(result, item.aggregates[bucket], `${label}: ${bucket}`);
        if (!result.buckets.length) continue;
        // As long as one message has a time, every message is in some bucket.
        const sums = result.series.map((row) => Object.fromEntries(METRICS.map((metric) => {
          let sum = 0;
          for (const value of row[metric]) sum += value;
          return [metric, sum];
        })));
        assertMetrics(sums, item.totals, `${label}: the ${bucket} buckets add up to the totals`);
      }
      assertMetrics(totals(item.model), item.totals, `${label}: totals`);
      // The counts written in the model are countWords of its texts, and its events are parseEvents of the events file.
      item.model.messages.forEach((entry, at) => {
        assert.equal(entry.words, entry.sender == null ? 0 : countWords(entry.text), `${label}: typed words of message ${at}`);
        if (entry.voice) assert.equal(entry.voice.words, countWords(entry.voice.text), `${label}: spoken words of message ${at}`);
      });
      if (item.events_text != null) assert.deepEqual(parseEvents(item.events_text).events, item.model.events, `${label}: events`);
      assert.equal(JSON.stringify(item.model), before, `${label}: the model is not changed`);
      const month = item.aggregates.month.buckets;
      seen[month.length ? 'dated' : 'undated'] += 1;
      if (month.length && month[0].slice(0, 4) !== month[month.length - 1].slice(0, 4)) seen.years += 1;
      if (month.length && item.model.messages.some((entry) => entry.time === null)) seen.timeless += 1;
      if (item.totals.some((row) => row.voice_notes > row.voice_transcribed) && item.totals.some((row) => row.voice_seconds > 0)) seen.voice += 1;
      if (item.model.events.length) seen.events += 1;
    }
  };
  check('here');
  // What the comparison is worth: chats with and without dates, a year boundary, messages without a time, voice notes
  // that are only partly transcribed, and events.
  for (const [kind, count] of Object.entries(seen)) assert.ok(count > 0, `no shared case has ${kind}`);
  for (const zone of ZONES) inZone(zone, () => check(zone[0]));
});
