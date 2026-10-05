/**
 * VoxPad conversation viewer: one dependency-free module, bundled into the browser app and inlined
 * verbatim into the HTML file Python writes. Nothing here touches the DOM until mountViewer runs.
 */

const METRICS = ['messages', 'words_typed', 'words_spoken', 'voice_notes', 'voice_seconds', 'voice_transcribed', 'voice_timed'];
const BUCKETS = ['day', 'week', 'month'];
// Whitespace is spelled out: Python's str.split() and JavaScript's \s disagree on U+001C–U+001F and U+0085.
const WORD = /[^\s\u001c-\u001f\u0085]+/g;
const EDGES = /^[\s\u001c-\u001f\u0085]+|[\s\u001c-\u001f\u0085]+$/g;
const ISO_DAY = /^([0-9]{4})-([0-9]{2})-([0-9]{2})/;
const EVENT_DATE = /^([0-9]{1,4})[-\/.]([0-9]{1,2})[-\/.]([0-9]{1,4})/;
const MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

const compare = (left, right) => (left < right ? -1 : left > right ? 1 : 0);
const pad = (value, width = 2) => String(value).padStart(width, '0');
const isVoice = (message) => message.kind === 'voice' && message.voice != null;

/** Count words: maximal runs of characters that are not whitespace. */
function countWords(text) {
  if (typeof text !== 'string') return 0;
  const words = text.match(WORD);
  return words ? words.length : 0;
}

const isLeap = (year) => (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
const monthLength = (year, month) => (month === 2 && isLeap(year) ? 29 : MONTH_DAYS[month - 1]);
const validDate = (year, month, day) => year >= 1 && year <= 9999 && month >= 1 && month <= 12 && day >= 1 && day <= monthLength(year, month);
const isoDate = (year, month, day) => `${pad(year, 4)}-${pad(month)}-${pad(day)}`;

/** The calendar date at the start of a model time or event date, or null when it is not a real date. */
function dayParts(text) {
  const match = typeof text === 'string' ? ISO_DAY.exec(text) : null;
  if (!match) return null;
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])];
  return validDate(year, month, day) ? { year, month, day } : null;
}

/** UTC midnight of a date. Local time is never used, and Date.UTC alone would read the years 0–99 as 1900–1999. */
function utcDate(year, month, day) {
  const date = new Date(Date.UTC(2000, 0, 1));
  date.setUTCFullYear(year, month - 1, day);
  return date;
}

const isoDay = (date) => isoDate(date.getUTCFullYear(), date.getUTCMonth() + 1, date.getUTCDate());

/** The bucket a day falls in, named by its first day: the day itself, the Monday on or before it, or the first of its month. */
function bucketStart(day, bucket) {
  if (bucket === 'day') return day;
  if (bucket === 'month') return `${day.slice(0, 8)}01`;
  const { year, month, day: number } = dayParts(day);
  const date = utcDate(year, month, number);
  date.setUTCDate(date.getUTCDate() - (date.getUTCDay() + 6) % 7);
  return isoDay(date);
}

function nextBucket(key, bucket) {
  const { year, month, day } = dayParts(key);
  if (bucket === 'month') return month === 12 ? isoDate(year + 1, 1, 1) : isoDate(year, month + 1, 1);
  return isoDay(utcDate(year, month, day + (bucket === 'week' ? 7 : 1)));
}

/** Day key per message. A message without a time takes the day of the nearest earlier message that has one, else of the nearest later one. */
function messageDays(messages) {
  const days = new Array(messages.length);
  let known = null;
  let last = '';
  let first = -1;
  for (let i = 0; i < messages.length; i += 1) {
    const time = messages[i].time;
    if (typeof time === 'string') {
      const day = time.slice(0, 10);
      if (day === known || dayParts(day)) {
        known = day;
        last = day;
        if (first < 0) first = i;
      }
    }
    days[i] = last;
  }
  for (let i = 0; i < first; i += 1) days[i] = days[first];
  return days;
}

/** Add one message to a participant's metrics at one position. Buckets and totals share this, so their sums agree. */
function tally(row, at, message) {
  row.messages[at] += 1;
  row.words_typed[at] += message.words;
  if (!isVoice(message)) return;
  const { voice } = message;
  row.voice_notes[at] += 1;
  row.words_spoken[at] += voice.words;
  if (voice.status === 'ok' || voice.status === 'empty') row.voice_transcribed[at] += 1;
  if (typeof voice.seconds === 'number') {
    row.voice_timed[at] += 1;
    row.voice_seconds[at] += voice.seconds;
  }
}

const metricRows = (model, length) => model.participants.map(() => Object.fromEntries(METRICS.map((metric) => [metric, new Array(length).fill(0)])));
const counted = (message, rows) => (message.kind === 'system' || message.sender == null ? undefined : rows[message.sender]);

/** Per participant and per day, week or month: the metrics of every non-system message, over contiguous buckets. */
function aggregate(model, bucket = 'day') {
  if (!BUCKETS.includes(bucket)) throw new RangeError(`Unknown bucket: ${bucket}`);
  const { messages } = model;
  const days = messageDays(messages);
  const buckets = [];
  if (!days.length || !days[0]) return { bucket, buckets, series: metricRows(model, 0) };
  let earliest = days[0];
  let latest = days[0];
  for (const day of days) {
    if (day < earliest) earliest = day;
    if (day > latest) latest = day;
  }
  const end = bucketStart(latest, bucket);
  const place = new Map();
  for (let key = bucketStart(earliest, bucket); ; key = nextBucket(key, bucket)) {
    place.set(key, buckets.length);
    buckets.push(key);
    if (key >= end) break;
  }
  const series = metricRows(model, buckets.length);
  const placeOfDay = new Map();
  for (let i = 0; i < messages.length; i += 1) {
    const row = counted(messages[i], series);
    if (!row) continue;
    let at = placeOfDay.get(days[i]);
    if (at === undefined) {
      at = place.get(bucketStart(days[i], bucket));
      placeOfDay.set(days[i], at);
    }
    tally(row, at, messages[i]);
  }
  return { bucket, buckets, series };
}

/** The same metrics as single numbers per participant, over every non-system message whether or not it has a time. */
function totals(model) {
  const rows = metricRows(model, 1);
  for (const message of model.messages) {
    const row = counted(message, rows);
    if (row) tally(row, 0, message);
  }
  return rows.map((row) => Object.fromEntries(METRICS.map((metric) => [metric, row[metric][0]])));
}

function eventDate(first, second, third) {
  if (first.length === 4) {
    const [year, month, day] = [Number(first), Number(second), Number(third)];
    return validDate(year, month, day) ? isoDate(year, month, day) : null;
  }
  const year = Number(third) + (third.length <= 2 ? 2000 : 0);
  const [day, month] = [Number(first), Number(second)];
  if (validDate(year, month, day)) return isoDate(year, month, day);
  return validDate(year, day, month) ? isoDate(year, day, month) : null;
}

/**
 * Read an events file: one event per line, a date and then a label. Returns { events, warnings };
 * a warning names its line by number only, never by content.
 */
function parseEvents(text) {
  const events = [];
  const warnings = [];
  const lines = typeof text === 'string' ? text.split(/\r\n|\r|\n/) : [];
  lines.forEach((raw, number) => {
    const line = raw.replace(EDGES, '');
    if (!line || line.startsWith('#')) return;
    const match = EVENT_DATE.exec(line);
    const date = match ? eventDate(match[1], match[2], match[3]) : null;
    if (!date) {
      warnings.push(`Events line ${number + 1} has no valid date and was skipped.`);
      return;
    }
    const label = line.slice(match[0].length).replace(/^[ ,|\t]+/, '').replace(EDGES, '');
    events.push({ date, label: label || date });
  });
  events.sort((left, right) => compare(left.date, right.date));
  return { events, warnings };
}

const TABS = [['conversation', 'Conversation'], ['voice', 'Voice messages'], ['activity', 'Activity'], ['events', 'Events']];
const UNIT_CAP = 150;
const WINDOW_MARGIN = 1200;
const WINDOW_SLACK = 100;
const MAX_MARKS = 200;
const COLOURS = 6;
const LONG_TRANSCRIPT = 360;
const LONG_EXCERPT = 110;
const EVENTS_FILE_LIMIT = 1048576;
const EVENT_PAGE = 100;
const WARNING_LINES = 8;
// Charts, in CSS pixels. A day is never narrower than MIN_STEP.day; a wider chart scrolls under its fixed y axis.
const AXIS_WIDTH = 44;
const PLOT_HEIGHT = 108;
const PLOT_TOP = 14;
const PLOT_FOOT = 5;
const BAR_ROUND = 4;
const BAR_WIDEST = 24;
const MIN_STEP = { day: 12, week: 16, month: 36 };
const MAX_STEP = 120;
const AUTO_DAYS = 730;
const NEAR_DAYS = 21;
const NEAR_SHARE = 0.15;
const TIP_LABEL = 140;
const TIP_EVENTS = 3;
const MARKER_HEIGHT = 24;
const MARKER_ROWS = 4;
const BUCKET_NAMES = { day: 'Day', week: 'Week', month: 'Month' };
const CHARTS = ['Messages', 'Words', 'Voice notes', 'Voice minutes'];
const [WORDS_CHART, MINUTES_CHART] = [1, 3];
const SVG = 'http://www.w3.org/2000/svg';
const CANNOT_PLAY = 'This browser cannot play this recording';
const CANNOT_LOAD = 'This recording could not be loaded';
const ORDER_NAMES = { DMY: 'day/month/year', MDY: 'month/day/year', YMD: 'year/month/day' };
const MEDIA = {
  image: ['Photo', 'photo'], video: ['Video', 'video'], sticker: ['Sticker', 'sticker'], gif: ['GIF', 'photo'],
  contact: ['Contact', 'contact'], document: ['Document', 'document'], audio: ['Audio', 'mic'], omitted: ['Media not included', 'off'],
};
const DAY_STYLES = {
  long: { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' },
  short: { day: 'numeric', month: 'short', year: 'numeric' },
  medium: { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' },
  month: { month: 'long', year: 'numeric' },
  monthShort: { month: 'short', year: 'numeric' },
  monthOnly: { month: 'short' },
};
// 24 × 24 paths; true marks a filled glyph, the others are drawn with a stroke.
const ICONS = {
  play: ['M8 5.2v13.6L19 12z', true],
  pause: ['M7 5h3.6v14H7zM13.4 5H17v14h-3.6z', true],
  up: ['M6 15l6-6 6 6'],
  down: ['M6 9l6 6 6-6'],
  close: ['M6 6l12 12M18 6L6 18'],
  mic: ['M12 3.5a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0v-5a3 3 0 0 0-3-3zM5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3'],
  photo: ['M4 6h16v12H4zM4 15.5l4.5-4.5 4 4 2.5-2.5 5 4.5M15.5 9.5h.01'],
  video: ['M4 6.5h11v11H4zM15 10.5l5-3v9l-5-3'],
  document: ['M7 3.5h7l4 4v13H7zM14 3.5v4h4M9.5 12.5h5M9.5 16h5'],
  contact: ['M12 12a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM5 20a7 7 0 0 1 14 0'],
  sticker: ['M4 4h16v9l-7 7H4zM20 13h-7v7'],
  off: ['M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17zM6 6l12 12'],
  filter: ['M4 7h16M7 12h10M10 17h4'],
  locate: ['M4 12h11M11 7.5l4.5 4.5-4.5 4.5M19.5 5v14'],
};

let mounted = 0;
const dayFormats = new Map();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function icon(name) {
  const [shape, solid] = ICONS[name];
  const svg = document.createElementNS(SVG, 'svg');
  svg.setAttribute('class', solid ? 'vp-icon vp-icon-solid' : 'vp-icon');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const path = document.createElementNS(SVG, 'path');
  path.setAttribute('d', shape);
  svg.append(path);
  return svg;
}

function option(value, label) {
  const node = el('option', '', label);
  node.value = String(value);
  return node;
}

const formatNumber = (value) => value.toLocaleString('en-US');
const plural = (count, one, many = `${one}s`) => `${formatNumber(count)} ${count === 1 ? one : many}`;

/** 73.9 → "1:14". */
function formatClock(seconds) {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total % 3600 / 60);
  return total >= 3600 ? `${Math.floor(total / 3600)}:${pad(minutes)}:${pad(total % 60)}` : `${minutes}:${pad(total % 60)}`;
}

/** A total of seconds in words: "42 s", "12 min 5 s", "3 h 12 min". */
function formatSpan(seconds) {
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} s`;
  if (total < 3600) return `${Math.floor(total / 60)} min ${total % 60} s`;
  return `${Math.floor(total / 3600)} h ${Math.floor(total % 3600 / 60)} min`;
}

/** Name a day key for people. Always formatted in UTC, so the reader's time zone never moves a date. */
function formatDay(day, style = 'long') {
  const parts = dayParts(day);
  if (!parts) return 'No date';
  if (!dayFormats.has(style)) dayFormats.set(style, new Intl.DateTimeFormat('en-GB', { ...DAY_STYLES[style], timeZone: 'UTC' }));
  return dayFormats.get(style).format(utcDate(parts.year, parts.month, parts.day));
}

/** First position in an ascending array whose value is not below `value`. */
function lowerBound(sorted, value) {
  let low = 0;
  let high = sorted.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (sorted[middle] < value) low = middle + 1; else high = middle;
  }
  return low;
}

function shape(tag, className) {
  const node = document.createElementNS(SVG, tag);
  if (className) node.setAttribute('class', className);
  return node;
}

/** Days since the epoch of a day key, for telling how far apart two dates are. */
function dayNumber(day) {
  const { year, month, day: number } = dayParts(day);
  return Math.round(utcDate(year, month, number).getTime() / 86400000);
}

/** Name a bucket by the key of its first day: a day, "Week of …" or a month. */
function bucketLabel(key, bucket, style = 'medium') {
  if (bucket === 'month') return formatDay(key, 'month');
  return bucket === 'week' ? `Week of ${formatDay(key, style === 'long' ? 'long' : 'short')}` : formatDay(key, style);
}

/** Shorten a label to `limit` characters, never between the two halves of one character. */
function cut(text, limit) {
  if (text.length <= limit) return text;
  return `${Array.from(text).slice(0, limit - 1).join('').replace(EDGES, '')}…`;
}

/** A y axis from zero to a round number at or above `highest`, in at most five steps of 1, 2 or 5 times a power of ten. */
function niceScale(highest, whole) {
  if (!(highest > 0)) return { top: 1, ticks: [0], digits: 0 };
  const rough = highest / 4;
  const power = 10 ** Math.floor(Math.log10(rough));
  let step = [1, 2, 5, 10].map((factor) => factor * power).find((value) => value >= rough * 0.999);
  if (whole) step = Math.max(1, Math.round(step));
  const digits = step >= 1 ? 0 : Math.ceil(-Math.log10(step) - 1e-9);
  const count = Math.ceil(highest / step - 1e-9);
  const ticks = [];
  for (let at = 0; at <= count; at += 1) ticks.push(Number((at * step).toFixed(digits)));
  return { top: ticks[count], ticks, digits };
}

const hundredth = (value) => Math.round(value * 100) / 100;

/** One bar as a closed path: square at the baseline, rounded at the end that shows the value. */
function barPath(x, y, width, height, round) {
  const radius = hundredth(Math.min(round, width / 2, height));
  const [top, bottom] = [hundredth(y), hundredth(y + height)];
  if (radius < 0.75) return `M${x},${top}h${width}V${bottom}h${-width}z`;
  const corner = `a${radius},${radius} 0 0 1 ${radius},`;
  return `M${x},${bottom}V${hundredth(y + radius)}${corner}${-radius}h${hundredth(width - 2 * radius)}${corner}${radius}V${bottom}z`;
}

const dotPath = (x, y, radius) => `M${hundredth(x - radius)},${y}a${radius},${radius} 0 1 0 ${2 * radius},0a${radius},${radius} 0 1 0 ${-2 * radius},0z`;

/** How much of the voice messages the spoken words and the voice time cover. */
function coverage(model) {
  const result = { notes: 0, transcribed: 0, timed: 0, pending: 0, failed: 0, missing: 0, playable: 0 };
  for (const message of model.messages) {
    if (!isVoice(message) || message.sender == null) continue;
    const { voice } = message;
    result.notes += 1;
    if (voice.status === 'ok' || voice.status === 'empty') result.transcribed += 1;
    else if (voice.status === 'error') result.failed += 1;
    else if (voice.status === 'missing') result.missing += 1;
    else result.pending += 1;
    if (typeof voice.seconds === 'number') result.timed += 1;
    if (voice.src) result.playable += 1;
  }
  return result;
}

/** A partly transcribed chat must never read as silence: the sentences shown wherever spoken words or voice time are. */
function coverageSentences(cover) {
  const sentences = [];
  if (cover.transcribed < cover.notes) {
    const [transcribed, notes, pending, failed, missing] = [cover.transcribed, cover.notes, cover.pending, cover.failed, cover.missing].map(formatNumber);
    sentences.push(`Spoken words cover ${transcribed} of ${notes} voice messages (${pending} not transcribed, ${failed} failed, ${missing} without a recording).`);
  }
  if (cover.timed < cover.notes) sentences.push(`Voice time covers ${formatNumber(cover.timed)} of ${formatNumber(cover.notes)} voice messages.`);
  return sentences;
}

function normaliseEvents(events) {
  const kept = [];
  for (const event of Array.isArray(events) ? events : []) {
    if (!event || typeof event.date !== 'string' || event.date.length !== 10 || !dayParts(event.date)) continue;
    kept.push({ date: event.date, label: typeof event.label === 'string' && event.label ? event.label : event.date });
  }
  return kept.sort((left, right) => compare(left.date, right.date));
}

/**
 * Put `text` into `parent` with every occurrence of `needle` (already lower-cased) in a mark element.
 * Returns true when the text matches but lower-casing changed its length, so the offsets cannot be trusted.
 */
function markText(parent, text, needle) {
  const folded = needle ? text.toLowerCase() : '';
  let at = needle ? folded.indexOf(needle) : -1;
  if (at < 0 || folded.length !== text.length) {
    parent.textContent = text;
    return at >= 0;
  }
  let from = 0;
  for (let marks = 0; at >= 0 && marks < MAX_MARKS; marks += 1) {
    if (at > from) parent.append(document.createTextNode(text.slice(from, at)));
    parent.append(el('mark', 'vp-mark', text.slice(at, at + needle.length)));
    from = at + needle.length;
    at = folded.indexOf(needle, from);
  }
  if (from < text.length) parent.append(document.createTextNode(text.slice(from)));
  return false;
}

/**
 * Keep a long list cheap. Every unit always owns one placeholder in `holder`; only units near the viewport hold rows.
 * A unit is { start, end } over positions of the caller's list. Hooks: render(unit) → { children, rows } with one row per
 * position, estimate(unit) → px, header(unit) → px that the sticky header of a filled unit hides at the top of the scroller,
 * gap → px a revealed row keeps clear below that, visible() and resized().
 */
function windowedList(scroller, holder, hooks) {
  let units = [];
  let width = 0;
  const live = new Set();
  const owner = new WeakMap();

  function distance(unit) {
    const view = scroller.getBoundingClientRect();
    const box = unit.node.getBoundingClientRect();
    return Math.max(0, view.top - box.bottom, box.top - view.bottom);
  }

  function fill(unit) {
    if (unit.live) return;
    const before = unit.node.getBoundingClientRect();
    const above = before.top < scroller.getBoundingClientRect().top;
    const atEnd = scroller.scrollTop > 0 && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 1;
    const { children, rows } = hooks.render(unit);
    unit.node.replaceChildren(...children);
    unit.node.style.height = '';
    unit.rows = rows;
    unit.live = true;
    live.add(unit);
    const after = unit.node.getBoundingClientRect();
    unit.height = after.height;
    // Anchoring is ours (overflow-anchor: none). Somebody who scrolled to the very end stays there; otherwise a unit
    // above the reading position that is taller or shorter than its placeholder must not move what is being read.
    // Its lower edge goes back where it was, which is the change in height unless the browser clamped the position.
    if (atEnd) scroller.scrollTop = scroller.scrollHeight;
    else if (above) scroller.scrollTop += after.bottom - before.bottom;
  }

  function release(unit) {
    // A unit holding the keyboard focus stays, or the focus would fall back to the page.
    if (!unit.live || unit.node.contains(document.activeElement)) return;
    if (hooks.visible()) {
      const height = unit.node.getBoundingClientRect().height;
      if (height) unit.height = height;
    }
    unit.node.replaceChildren();
    unit.node.style.height = `${unit.height ?? hooks.estimate(unit)}px`;
    unit.rows = null;
    unit.live = false;
    live.delete(unit);
  }

  /**
   * The observer reports changes only: a unit filled for a jump that never comes near the viewport, or one kept
   * because it held the focus, would stay filled for good. Empty whatever is left far outside the margin.
   */
  function prune() {
    if (!hooks.visible()) return;
    for (const unit of live) if (distance(unit) > WINDOW_MARGIN + WINDOW_SLACK) release(unit);
  }

  // An entry can be a frame older than a jump made since, so one that the geometry clearly contradicts is left
  // to the observer's next report.
  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const unit = owner.get(entry.target);
      if (!unit || unit.node.parentNode !== holder) continue;
      if (!hooks.visible()) release(unit);
      else if (entry.isIntersecting) { if (!unit.live && distance(unit) <= WINDOW_MARGIN + WINDOW_SLACK) fill(unit); }
      else if (unit.live && distance(unit) >= WINDOW_MARGIN - WINDOW_SLACK) release(unit);
    }
    prune();
  }, { root: scroller, rootMargin: `${WINDOW_MARGIN}px 0px` });

  // Bubbles wrap differently at another width, so measured heights are worth nothing after a resize.
  const resizer = new ResizeObserver(() => {
    const now = scroller.offsetWidth;
    if (!now || now === width) return;
    const first = !width;
    width = now;
    if (first) return;
    for (const unit of units) {
      unit.height = null;
      if (!unit.live) unit.node.style.height = `${hooks.estimate(unit)}px`;
    }
    hooks.resized();
  });
  resizer.observe(scroller);

  function setUnits(next) {
    observer.disconnect();
    live.clear();
    units = next;
    const fragment = document.createDocumentFragment();
    for (const unit of units) {
      unit.node = el('div', 'vp-unit');
      unit.node.style.height = `${hooks.estimate(unit)}px`;
      unit.height = null;
      unit.rows = null;
      unit.live = false;
      owner.set(unit.node, unit);
      fragment.append(unit.node);
    }
    holder.replaceChildren(fragment);
    for (const unit of units) observer.observe(unit.node);
  }

  function unitAt(position) {
    let low = 0;
    let high = units.length - 1;
    while (low < high) {
      const middle = (low + high + 1) >> 1;
      if (units[middle].start <= position) low = middle; else high = middle - 1;
    }
    return low;
  }

  function fillAround(at) {
    for (const near of [at - 1, at, at + 1]) if (units[near]) fill(units[near]);
  }

  /** Bring the row at `position` to `offset` px below the scroller's top edge; by default just clear of the unit's header. */
  function reveal(position, offset) {
    if (!units.length || !hooks.visible()) return null;
    const at = unitAt(position);
    fillAround(at);
    const row = units[at].rows[position - units[at].start];
    scroller.scrollTop += row.getBoundingClientRect().top - scroller.getBoundingClientRect().top - (offset ?? hooks.header(units[at]) + hooks.gap);
    prune();
    return row.isConnected ? row : null;
  }

  /** Bring a unit's top edge, and so its header, to the top of the scroller. */
  function revealUnit(at) {
    if (!units[at] || !hooks.visible()) return null;
    fillAround(at);
    scroller.scrollTop += units[at].node.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
    prune();
    return units[at].rows ? units[at].rows[0] : null;
  }

  /**
   * The first row that can be seen below the scroller's top edge and whatever sticks to it, as { position, offset }:
   * a place that survives re-layout. A row brought there by reveal() is its own anchor.
   */
  function anchor() {
    if (!hooks.visible()) return null;
    const top = scroller.getBoundingClientRect().top;
    let best = null;
    for (const unit of live) {
      if (best && unit.start > best.start) continue;
      if (unit.node.getBoundingClientRect().bottom > top + hooks.header(unit)) best = unit;
    }
    if (!best) return null;
    const line = top + hooks.header(best);
    let low = 0;
    let high = best.rows.length - 1;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (best.rows[middle].getBoundingClientRect().bottom > line) high = middle; else low = middle + 1;
    }
    return { position: best.start + low, offset: best.rows[low].getBoundingClientRect().top - top };
  }

  function elementAt(position) {
    const unit = units[unitAt(position)];
    return unit && unit.live && position >= unit.start && position < unit.end ? unit.rows[position - unit.start] : null;
  }

  /** Swap one row that is in the DOM, keeping the keyboard focus on the control that had it. */
  function replace(position, fresh) {
    const unit = units[unitAt(position)];
    const old = elementAt(position);
    if (!old) return false;
    const focused = old.contains(document.activeElement) ? document.activeElement : null;
    const action = focused === old ? '' : focused?.dataset.vpAction;
    old.replaceWith(fresh);
    unit.rows[position - unit.start] = fresh;
    if (focused) (action ? fresh.querySelector(`[data-vp-action="${action}"]`) || fresh : fresh).focus({ preventScroll: true });
    return true;
  }

  function refresh() {
    for (const unit of live) {
      const { children, rows } = hooks.render(unit);
      unit.node.replaceChildren(...children);
      unit.rows = rows;
    }
  }

  function destroy() {
    observer.disconnect();
    resizer.disconnect();
    live.clear();
  }

  return { setUnits, unitAt, reveal, revealUnit, anchor, elementAt, replace, refresh, destroy, units: () => units };
}

/**
 * Mount the viewer in `root` for one conversation model (schema 1).
 * options: theme ("light", or "auto" to follow the system), bucket ("day", "week" or "month" for the charts; without it
 * they show days, and weeks for a chat of more than 730 days), resolveAudio(src, { decoded }) and onEvents(events).
 * Returns { update(model, changed), setEvents(events), showMessage(index), destroy() }.
 *
 * Inside, in this order: `state` (everything the user can change), what is derived from the model, small builders, the
 * head with its tabs, then one object per part (player, conversation, voices, activity, eventsView), each with enter()
 * and leave() for its tab, and last the actions the parts share and the listeners on the root.
 */
function mountViewer(root, model, options = {}) {
  if (!root || !model || !Array.isArray(model.messages) || !Array.isArray(model.participants)) {
    throw new TypeError('mountViewer needs an element and a conversation model.');
  }
  mounted += 1;
  const uid = `vp${mounted}`;
  const controller = new AbortController();
  const listen = (target, type, handler, extra) => target.addEventListener(type, handler, { ...extra, signal: controller.signal });
  const resolveAudio = typeof options.resolveAudio === 'function' ? options.resolveAudio : null;
  const onEvents = typeof options.onEvents === 'function' ? options.onEvents : null;
  const host = { viewer: root.classList.contains('voxpad-viewer'), theme: root.getAttribute('data-vp-theme') };
  const timers = new Set();
  let fields = 0;

  // Everything the user can change, kept across update(). Events are numbered by their position here, plus one.
  // `bucket` is the host's or the reader's choice; while it is empty the charts choose by the length of the chat.
  const state = {
    tab: 'conversation',
    bucket: BUCKETS.includes(options.bucket) ? options.bucket : '',
    hidden: new Set(),
    cursor: '',
    chartScroll: 0,
    eventsShown: EVENT_PAGE,
    events: normaliseEvents(model.events),
    eventWarnings: [],
    selectedEvent: -1,
    filter: { sender: -1, voiceOnly: false },
    viewAs: 0,
    query: '',
    needle: '',
    matches: [],
    match: -1,
    expanded: new Set(),
    voice: { sort: 'oldest', sender: -1, query: '', sequence: false, expanded: new Set() },
    playing: -1,
    phase: 'idle',
    rate: 1,
    failed: new Map(),
    stale: { voice: 'list', activity: true, events: true },
    destroyed: false,
  };

  let messages = model.messages;
  let days = [];
  let span = { first: '', last: '' };
  let voiceTotal = 0;
  let haystack = null;
  let sums = null;
  let cover = null;
  let sets = {};

  function derive() {
    messages = model.messages;
    days = messageDays(messages);
    span = { first: '', last: '', from: 0, to: 0, margin: 0 };
    voiceTotal = 0;
    for (let i = 0; i < messages.length; i += 1) {
      const day = days[i];
      if (day && (!span.first || day < span.first)) span.first = day;
      if (day > span.last) span.last = day;
      if (isVoice(messages[i])) voiceTotal += 1;
    }
    if (span.first) {
      span.from = dayNumber(span.first);
      span.to = dayNumber(span.last);
      span.margin = Math.max(NEAR_DAYS, NEAR_SHARE * (span.to - span.from));
    }
    haystack = null;
    sums = null;
    cover = null;
    sets = {};
  }

  const sumsNow = () => (sums ??= totals(model));
  const coverNow = () => (cover ??= coverage(model));
  const setNow = (bucket) => (sets[bucket] ??= aggregate(model, bucket));
  const nameOf = (sender) => model.participants[sender] ?? 'Unknown';

  /** The bucket of the charts: the host's or the reader's choice, else days, and weeks only above two years of chat. */
  function bucketNow() {
    if (state.bucket) return state.bucket;
    const length = span.first ? span.to - span.from + 1 : 0;
    return length <= AUTO_DAYS ? 'day' : length <= AUTO_DAYS * 7 ? 'week' : 'month';
  }

  /** What the charts tell apart: the six most active participants, and everybody else as one. */
  function seriesList() {
    const list = model.participants.slice(0, COLOURS).map((name, at) => ({ key: String(at), name, members: [at], slot: at + 1 }));
    if (model.participants.length > COLOURS) {
      list.push({ key: 'others', name: 'Others', members: model.participants.slice(COLOURS).map((name, at) => at + COLOURS), slot: 0 });
    }
    return list;
  }

  /** Where an event falls: inside the chat, near enough to stay on the charts' axis, or far outside. */
  function eventPlace(event) {
    if (!span.first) return 'far';
    if (event.date >= span.first && event.date <= span.last) return 'inside';
    const at = dayNumber(event.date);
    return at >= span.from - span.margin && at <= span.to + span.margin ? 'near' : 'far';
  }

  /**
   * Before a part of the page is built again: when one of its buttons has the keyboard, a function that hands the
   * keyboard to the button that takes its place (the same action for the same event), so an update does not drop it.
   */
  function heldControl(part) {
    const active = document.activeElement;
    if (!active || !part.contains(active) || !active.dataset.vpAction) return null;
    const { vpAction: action, vpEvent: number } = active.dataset;
    return (fresh) => {
      for (const node of fresh.querySelectorAll('[data-vp-action]')) {
        if (node.dataset.vpAction === action && node.dataset.vpEvent === number) {
          node.focus({ preventScroll: true });
          return;
        }
      }
    };
  }

  /** The colour of a chart series as a small square: slots 1 to 6 are the participants in order, 0 everybody else. */
  function swatch(slot) {
    const node = el('span', `vp-swatch vp-s${slot}`);
    node.setAttribute('aria-hidden', 'true');
    return node;
  }
  const colourOf = (sender) => (sender >= 0 && sender < COLOURS ? `vp-p${sender + 1}` : 'vp-p0');
  const clockOf = (index) => (typeof messages[index].time === 'string' ? messages[index].time.slice(11, 16) : '');
  const stampOf = (index) => `${formatDay(days[index], 'short')}${clockOf(index) ? `, ${clockOf(index)}` : ''}`;
  const isLong = (text, limit) => text.length > limit || text.split('\n').length > 4;

  function later(run, delay) {
    const id = setTimeout(() => {
      timers.delete(id);
      if (!state.destroyed) run();
    }, delay);
    timers.add(id);
    return id;
  }

  function cancel(id) {
    clearTimeout(id);
    timers.delete(id);
  }

  function nameNode(sender, className = 'vp-name') {
    return el('bdi', `${className} ${colourOf(sender)}`, nameOf(sender));
  }

  function textButton(className, action, label, glyph) {
    const node = el('button', `vp-control ${className}`);
    node.type = 'button';
    node.dataset.vpAction = action;
    if (glyph) node.append(icon(glyph));
    node.append(el('span', 'vp-button-label', label));
    return node;
  }

  function iconButton(className, action, label, glyph) {
    const node = el('button', `vp-control ${className}`);
    node.type = 'button';
    node.dataset.vpAction = action;
    node.setAttribute('aria-label', label);
    node.append(icon(glyph));
    return node;
  }

  /** A control with its label beside it. The label points at the control instead of wrapping it, so its text alone is the name. */
  function field(label, control) {
    fields += 1;
    control.id = `${uid}-field-${fields}`;
    const name = el('label', 'vp-field-label', label);
    name.htmlFor = control.id;
    const node = el('div', 'vp-field');
    node.append(name, control);
    return node;
  }

  function checkBox(label) {
    const box = el('input', 'vp-control vp-checkbox');
    box.type = 'checkbox';
    const node = el('label', 'vp-check');
    node.append(box, el('span', 'vp-check-label', label));
    return { node, box };
  }

  function senderSelect(select, chosen) {
    select.replaceChildren(option(-1, 'Everyone'), ...model.participants.map((name, at) => option(at, name)));
    select.value = String(chosen);
  }

  function statusText(voice) {
    if (voice.status === 'pending') return 'Not transcribed yet';
    if (voice.status === 'empty') return 'No speech detected';
    if (voice.status === 'missing') return 'Recording not included';
    // A report read without a reason carries these two words as its error.
    if (voice.status === 'error') return voice.error && voice.error !== 'Transcription failed' ? `Transcription failed: ${voice.error}` : 'Transcription failed';
    const languages = Array.isArray(voice.languages) && voice.languages.length ? ` · ${voice.languages.join(', ')}` : '';
    return `${plural(voice.words, 'spoken word')}${languages}`;
  }

  /** A table in a region that scrolls sideways by itself: on a phone the columns do not fit, and the page must not move. */
  function table(caption, columns, rows) {
    const node = el('table', 'vp-table');
    node.append(el('caption', 'vp-sr', caption));
    const head = el('tr', 'vp-table-row');
    columns.forEach((label, at) => {
      const cell = el('th', at ? 'vp-cell vp-cell-number' : 'vp-cell', label);
      cell.scope = 'col';
      head.append(cell);
    });
    const header = el('thead', 'vp-table-head');
    header.append(head);
    const body = el('tbody', 'vp-table-body');
    for (const [lead, ...cells] of rows) {
      const row = el('tr', 'vp-table-row');
      const first = el('th', 'vp-cell');
      first.scope = 'row';
      first.append(lead);
      row.append(first, ...cells.map((text) => el('td', 'vp-cell vp-cell-number', text)));
      body.append(row);
    }
    node.append(header, body);
    const wrap = el('div', 'vp-table-wrap');
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'region');
    wrap.setAttribute('aria-label', caption);
    wrap.append(node);
    return wrap;
  }

  function coverageBlock(extra = []) {
    const lines = [...coverageSentences(coverNow()), ...extra];
    const block = el('div', 'vp-coverage');
    block.hidden = !lines.length;
    block.append(...lines.map((line) => el('p', 'vp-coverage-line', line)));
    return block;
  }

  root.classList.add('voxpad-viewer');
  root.setAttribute('data-vp-theme', options.theme === 'auto' ? 'auto' : 'light');

  const title = el('h2', 'vp-title');
  const summary = el('p', 'vp-summary');
  const notices = el('details', 'vp-notices');
  const noticesLabel = el('summary', 'vp-control vp-notices-label');
  const noticesList = el('ul', 'vp-notices-list');
  notices.append(noticesLabel, noticesList);
  const heading = el('div', 'vp-heading');
  heading.append(title, summary, notices);
  const tablist = el('div', 'vp-tabs');
  tablist.setAttribute('role', 'tablist');
  tablist.setAttribute('aria-label', 'Views of this conversation');
  const head = el('div', 'vp-head');
  head.append(heading, tablist);
  const body = el('div', 'vp-body');
  const tabs = {};
  const counts = {};
  const panels = {};
  for (const [name, label] of TABS) {
    const tab = el('button', 'vp-control vp-tab');
    tab.type = 'button';
    tab.id = `${uid}-tab-${name}`;
    tab.dataset.vpTab = name;
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-controls', `${uid}-panel-${name}`);
    counts[name] = el('span', 'vp-tab-count');
    tab.append(el('span', 'vp-tab-label', label), counts[name]);
    const panel = el('div', `vp-panel vp-panel-${name}`);
    panel.id = `${uid}-panel-${name}`;
    panel.dataset.vpPanel = name;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', tab.id);
    panel.hidden = true;
    tabs[name] = tab;
    panels[name] = panel;
    tablist.append(tab);
    body.append(panel);
  }

  function renderHead() {
    title.textContent = model.title || model.participants.join(' · ') || 'Conversation';
    const count = plural(messages.length, 'message');
    summary.textContent = span.first ? `${count} · ${formatDay(span.first, 'short')} – ${formatDay(span.last, 'short')}` : count;
    const lines = Array.isArray(model.warnings) ? model.warnings.map(String) : [];
    if (model.date_order_ambiguous) {
      lines.push(`The dates of this chat can be read in more than one order; they were read as ${ORDER_NAMES[model.date_order] || model.date_order}.`);
    }
    notices.hidden = !lines.length;
    noticesLabel.textContent = plural(lines.length, 'notice');
    noticesList.replaceChildren(...lines.map((line) => el('li', 'vp-notice', line)));
    counts.voice.textContent = voiceTotal ? formatNumber(voiceTotal) : '';
    counts.events.textContent = state.events.length ? formatNumber(state.events.length) : '';
  }

  // One audio element and its strip live outside the scrollers, so emptying a unit never interrupts playback.
  const player = (() => {
    const audio = el('audio', 'vp-audio');
    audio.preload = 'none';
    const strip = el('div', 'vp-player');
    strip.hidden = true;
    strip.setAttribute('role', 'group');
    strip.setAttribute('aria-label', 'Now playing');
    const play = iconButton('vp-play', 'play', 'Play', 'play');
    const who = el('bdi', 'vp-name');
    const when = el('span', 'vp-player-when');
    const what = el('div', 'vp-player-what');
    what.append(who, when);
    const seek = el('input', 'vp-control vp-seek');
    seek.type = 'range';
    seek.min = '0';
    seek.step = '0.1';
    seek.setAttribute('aria-label', 'Position in the voice message');
    const time = el('span', 'vp-voice-time');
    const problem = el('span', 'vp-voice-error');
    problem.setAttribute('role', 'status');
    const speed = el('select', 'vp-control vp-select vp-speed');
    speed.append(option(1, '1×'), option(1.5, '1.5×'), option(2, '2×'));
    const show = iconButton('vp-icon-button', 'show', 'Show in conversation', 'locate');
    const stop = iconButton('vp-icon-button', 'stop', 'Stop and close the player', 'close');
    const track = el('div', 'vp-player-track');
    track.append(seek, time);
    strip.append(audio, play, what, track, problem, field('Speed', speed), show, stop);
    let token = 0;       // outdates an awaited resolveAudio when another recording is chosen meanwhile
    let retried = false; // the decoded copy of the current recording has been asked for
    let startAt = 0;
    let painted = -1;

    const describe = (index) => `voice message from ${nameOf(messages[index].sender)}, ${stampOf(index)}`;

    function setIcon(button, name) {
      button.querySelector('path').setAttribute('d', ICONS[name][0]);
    }

    /** Show the playback state on one voice bubble, voice row or the strip; a bubble that is filled again re-attaches here. */
    function paint(node, index) {
      const { voice } = messages[index];
      const active = index === state.playing;
      const running = active && (state.phase === 'playing' || state.phase === 'loading');
      const moving = active && (state.phase === 'playing' || state.phase === 'paused');
      const length = moving && Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : voice.seconds;
      const at = moving ? audio.currentTime : 0;
      node.classList.toggle('vp-active', active);
      node.classList.toggle('vp-playing', running);
      const button = node.querySelector('.vp-play');
      if (button) {
        setIcon(button, running ? 'pause' : 'play');
        button.setAttribute('aria-label', `${running ? 'Pause' : 'Play'} ${describe(index)}`);
      }
      const bar = node.querySelector('.vp-seek');
      if (bar) {
        bar.disabled = length == null;
        bar.max = String(length ?? 0);
        bar.value = String(Math.min(at, length ?? 0));
        bar.setAttribute('aria-valuetext', length == null ? 'Length unknown' : `${formatClock(at)} of ${formatClock(length)}`);
      }
      const label = node.querySelector('.vp-voice-time');
      if (label) label.textContent = length == null ? (moving ? formatClock(at) : '') : moving ? `${formatClock(at)} / ${formatClock(length)}` : formatClock(length);
      const note = node.querySelector('.vp-voice-error');
      if (note) {
        note.textContent = state.failed.get(index) || '';
        note.hidden = !note.textContent;
      }
    }

    function repaint(index) {
      for (const node of root.querySelectorAll(`[data-vp-voice="${index}"]`)) paint(node, index);
    }

    function sync() {
      const index = state.playing;
      if (painted >= 0 && painted !== index) {
        strip.removeAttribute('data-vp-voice');
        repaint(painted);
      }
      painted = index;
      strip.hidden = index < 0;
      if (index < 0) return;
      strip.dataset.vpVoice = String(index);
      play.dataset.vpIndex = String(index);
      seek.dataset.vpIndex = String(index);
      show.dataset.vpIndex = String(index);
      who.textContent = nameOf(messages[index].sender);
      who.className = `vp-name ${colourOf(messages[index].sender)}`;
      when.textContent = stampOf(index);
      repaint(index);
    }

    function fail(index, message) {
      state.failed.set(index, message);
      state.phase = 'error';
      sync();
    }

    function start() {
      const playing = audio.play();
      // A browser may still want a gesture after the awaited lookup: stay loaded, so the next press plays at once.
      if (playing) playing.catch((error) => { if (error && error.name === 'NotAllowedError' && !state.destroyed) { state.phase = 'paused'; sync(); } });
    }

    async function load(index, decoded) {
      const { src } = messages[index].voice;
      token += 1;
      const mine = token;
      retried = decoded;
      state.playing = index;
      state.phase = 'loading';
      state.failed.delete(index);
      // The recording before this one must not go on sounding while the host prepares the next.
      if (!audio.paused) audio.pause();
      sync();
      let url = src;
      if (resolveAudio) {
        try { url = await resolveAudio(src, { decoded }); } catch { url = null; }
        if (mine !== token || state.destroyed) return;
      }
      if (!url) {
        fail(index, CANNOT_PLAY);
        return;
      }
      audio.src = url;
      audio.defaultPlaybackRate = state.rate;
      audio.playbackRate = state.rate;
      start();
    }

    function toggle(index) {
      if (!messages[index] || !isVoice(messages[index]) || !messages[index].voice.src) return;
      if (index !== state.playing || state.phase === 'error' || state.phase === 'idle') {
        startAt = 0;
        load(index, false);
      } else if (state.phase !== 'loading') {
        if (audio.paused) start(); else audio.pause();
      }
    }

    function seekTo(index, seconds) {
      if (!messages[index] || !isVoice(messages[index]) || !messages[index].voice.src || !Number.isFinite(seconds)) return;
      if (index !== state.playing || state.phase === 'error' || state.phase === 'idle') {
        startAt = seconds;
        load(index, false);
      } else if (state.phase === 'loading') {
        startAt = seconds;
      } else {
        audio.currentTime = seconds;
        sync();
      }
    }

    function close() {
      token += 1;
      audio.pause();
      state.playing = -1;
      state.phase = 'idle';
      sync();
    }

    listen(audio, 'loadedmetadata', () => {
      if (startAt > 0) audio.currentTime = startAt;
      startAt = 0;
      sync();
    });
    listen(audio, 'playing', () => { state.phase = 'playing'; sync(); });
    listen(audio, 'pause', () => { if (state.phase === 'playing') { state.phase = 'paused'; sync(); } });
    for (const type of ['timeupdate', 'durationchange', 'seeked']) listen(audio, type, () => { if (state.playing >= 0) sync(); });
    listen(audio, 'ended', () => {
      const next = state.voice.sequence ? voices.after(state.playing) : -1;
      if (next >= 0) {
        startAt = 0;
        load(next, false);
      } else {
        state.phase = 'paused';
        sync();
      }
    });
    listen(audio, 'error', () => {
      const index = state.playing;
      if (index < 0 || !audio.error || state.phase === 'error') return;
      const undecodable = audio.error.code === 3 || audio.error.code === 4; // MEDIA_ERR_DECODE, MEDIA_ERR_SRC_NOT_SUPPORTED
      if (undecodable && resolveAudio && !retried) load(index, true);
      else fail(index, undecodable ? CANNOT_PLAY : CANNOT_LOAD);
    });
    listen(speed, 'change', () => {
      state.rate = Number(speed.value) || 1;
      audio.defaultPlaybackRate = state.rate;
      audio.playbackRate = state.rate;
    });

    function destroy() {
      token += 1;
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    }

    return { strip, audio, paint, sync, toggle, seekTo, close, destroy };
  })();

  /** One voice bubble for the conversation. */
  function voiceBubble(index, needle) {
    const { voice } = messages[index];
    const box = el('div', 'vp-voice');
    box.dataset.vpVoice = String(index);
    const controls = el('div', 'vp-voice-controls');
    if (voice.src) {
      const play = iconButton('vp-play', 'play', 'Play', 'play');
      play.dataset.vpIndex = String(index);
      const seek = el('input', 'vp-control vp-seek');
      seek.type = 'range';
      seek.min = '0';
      seek.step = '0.1';
      seek.dataset.vpIndex = String(index);
      seek.setAttribute('aria-label', 'Position in the voice message');
      controls.append(play, seek);
    } else {
      controls.classList.add('vp-voice-silent');
      controls.append(icon('mic'), el('span', 'vp-voice-kind', 'Voice message'));
    }
    controls.append(el('span', 'vp-voice-time'));
    box.append(controls);
    let outlined = false;
    if (voice.text) {
      const transcript = el('div', 'vp-transcript');
      transcript.dir = 'auto';
      outlined = markText(transcript, voice.text, needle);
      box.append(transcript);
      if (isLong(voice.text, LONG_TRANSCRIPT)) {
        const open = state.expanded.has(index);
        transcript.classList.toggle('vp-clamp', !open);
        const more = textButton('vp-link', 'expand', open ? 'Show less' : 'Show more');
        more.dataset.vpIndex = String(index);
        more.setAttribute('aria-expanded', String(open));
        box.append(more);
      }
    }
    box.append(el('div', voice.status === 'error' ? 'vp-voice-status vp-problem' : 'vp-voice-status', statusText(voice)), el('div', 'vp-voice-error'));
    player.paint(box, index);
    return { box, outlined };
  }

  const conversation = (() => {
    const panel = panels.conversation;
    const search = el('input', 'vp-control vp-input vp-search');
    search.type = 'search';
    search.placeholder = 'Search messages and transcripts';
    search.autocomplete = 'off';
    search.spellcheck = false;
    search.setAttribute('aria-label', 'Search messages');
    const previous = iconButton('vp-icon-button', 'match-previous', 'Previous match', 'up');
    const next = iconButton('vp-icon-button', 'match-next', 'Next match', 'down');
    const status = el('span', 'vp-status');
    status.setAttribute('role', 'status');
    const toggle = textButton('vp-button vp-filters-toggle', 'filters', 'Filters', 'filter');
    const filters = el('div', 'vp-filters');
    filters.id = `${uid}-filters`;
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-controls', filters.id);
    const sender = el('select', 'vp-control vp-select');
    const voiceOnly = checkBox('Voice only');
    const date = el('input', 'vp-control vp-input vp-date');
    date.type = 'date';
    const viewAs = el('select', 'vp-control vp-select');
    const viewField = field('Viewing as', viewAs);
    const note = el('span', 'vp-status vp-jump-note');
    note.setAttribute('role', 'status');
    const find = el('div', 'vp-find');
    find.append(search, previous, next, status, toggle);
    filters.append(field('From', sender), voiceOnly.node, field('Jump to date', date), viewField, note);
    const bar = el('div', 'vp-toolbar');
    bar.append(find, filters);
    const scroller = el('div', 'vp-scroll vp-messages');
    scroller.tabIndex = 0;
    scroller.setAttribute('role', 'group');
    scroller.setAttribute('aria-label', 'Messages');
    const empty = el('p', 'vp-empty');
    const holder = el('div', 'vp-units');
    scroller.append(empty, holder);
    panel.append(bar, scroller);

    let list = [];       // message indices left by the filters, ascending
    let dayUnits = [];   // [day, its first unit], sorted by day: times are not guaranteed to run forwards
    let dayKeys = [];    // the days of dayUnits, for searching
    let saved = null;    // the reading position as { index, offset }
    let typing = 0;
    let ticking = false;

    const showing = () => state.tab === 'conversation' && !state.destroyed;
    const twoPeople = () => model.participants.length === 2;
    const current = () => (state.match >= 0 ? state.matches[state.match] : -1);
    const follows = (position, unit) => position > unit.start && messages[list[position]].sender != null
      && messages[list[position]].sender === messages[list[position - 1]].sender;

    function positionOf(index) {
      const position = lowerBound(list, index);
      return list[position] === index ? position : -1;
    }

    function banner(number, day) {
      const event = state.events[number];
      const node = el('button', 'vp-control vp-banner');
      node.type = 'button';
      node.dataset.vpAction = 'event';
      node.dataset.vpEvent = String(number);
      // Named in one piece: the parts are laid out apart, and would be read without a pause between them.
      node.setAttribute('aria-label', `Event ${number + 1}${event.date === day ? '' : `, ${formatDay(event.date, 'short')}`}: ${event.label}`);
      node.append(el('span', 'vp-event-number', String(number + 1)));
      if (event.date !== day) node.append(el('span', 'vp-banner-date', formatDay(event.date, 'short')));
      const label = el('span', 'vp-banner-label', event.label);
      label.dir = 'auto';
      node.append(label);
      if (number === state.selectedEvent) {
        node.classList.add('vp-selected');
        node.setAttribute('aria-current', 'true');
      }
      return node;
    }

    function chip(media) {
      const [label, glyph] = MEDIA[media && media.type] || MEDIA.document;
      const node = el('div', media && media.type === 'omitted' ? 'vp-chip vp-chip-absent' : 'vp-chip');
      node.append(icon(glyph), el('span', 'vp-chip-label', label));
      // Photos and videos are named by the phone; only a document's or a contact's name says something.
      if (media && media.file && (media.type === 'document' || media.type === 'contact')) {
        node.append(el('span', 'vp-chip-dot', '·'), el('bdi', 'vp-chip-file', media.file));
      }
      return node;
    }

    function bubble(index, follow) {
      const message = messages[index];
      const row = el('div', 'vp-msg');
      row.dataset.vpIndex = String(index);
      row.tabIndex = -1;
      let outlined = false;
      if (message.kind === 'system' || message.sender == null) {
        row.classList.add('vp-system');
        const line = el('span', 'vp-system-text');
        line.dir = 'auto';
        outlined = markText(line, message.text || '', state.needle);
        row.append(line);
      } else {
        const mine = twoPeople() && message.sender === state.viewAs;
        row.classList.add(mine ? 'vp-me' : 'vp-them');
        if (follow) row.classList.add('vp-follow');
        const box = el('div', 'vp-bubble');
        // In a two-person chat the side says who is speaking; the name stays for screen readers.
        const name = nameNode(message.sender);
        if (twoPeople() || follow) name.classList.add('vp-sr');
        box.append(name);
        if (isVoice(message)) {
          const voice = voiceBubble(index, state.needle);
          outlined = voice.outlined;
          box.append(voice.box);
        } else if (message.kind === 'media') {
          box.append(chip(message.media));
        } else if (message.kind === 'deleted') {
          const gone = el('div', 'vp-chip vp-chip-absent');
          gone.append(icon('off'), el('span', 'vp-chip-label', 'This message was deleted'));
          box.append(gone);
        }
        if (message.text) {
          const text = el('div', 'vp-text');
          text.dir = 'auto';
          outlined = markText(text, message.text, state.needle) || outlined;
          box.append(text);
        }
        const meta = el('div', 'vp-meta');
        if (message.edited) meta.append(el('span', 'vp-edited', 'Edited'));
        if (clockOf(index)) meta.append(el('span', 'vp-time', clockOf(index)));
        box.append(meta);
        row.append(box);
      }
      if (outlined) row.classList.add('vp-match');
      if (index === current()) row.classList.add('vp-current');
      return row;
    }

    const windowed = windowedList(scroller, holder, {
      render(unit) {
        const header = el('div', 'vp-day');
        header.append(el('span', 'vp-day-label', formatDay(unit.day)));
        const rows = [];
        for (let position = unit.start; position < unit.end; position += 1) rows.push(bubble(list[position], follows(position, unit)));
        return { children: [header, ...unit.events.map((number) => banner(number, unit.day)), ...rows], rows };
      },
      estimate: (unit) => 26 + 64 * (unit.end - unit.start) + 44 * unit.events.length,
      header: (unit) => unit.node.firstElementChild.offsetHeight,
      // Less than the smallest margin between two bubbles, so the bubble before a revealed one stays out of sight.
      gap: 2,
      visible: showing,
      resized: () => restore(),
    });

    /** The place being read, in message terms, so it survives a new list, a new width or a hidden tab. */
    function hold() {
      const at = windowed.anchor();
      if (at) saved = { index: list[at.position], offset: at.offset };
      return saved;
    }

    function restore() {
      if (!saved || !list.length || !showing()) return;
      const position = Math.min(lowerBound(list, saved.index), list.length - 1);
      windowed.reveal(position, list[position] === saved.index ? saved.offset : undefined);
    }

    /** An event sits on the first unit of its day, or of the next day that has messages; outside the chat it has no banner. */
    function placeEvents(units) {
      for (const unit of units) unit.events = [];
      state.events.forEach((event, number) => {
        if (!span.first || event.date < span.first || event.date > span.last) return;
        const at = lowerBound(dayKeys, event.date);
        if (dayUnits[at]) units[dayUnits[at][1]].events.push(number);
      });
    }

    function findMatches() {
      const kept = current();
      state.matches = [];
      if (state.needle) {
        haystack ??= new Array(messages.length);
        for (const index of list) {
          if (haystack[index] === undefined) {
            const message = messages[index];
            haystack[index] = (isVoice(message) && message.voice.text ? `${message.text}\n${message.voice.text}` : message.text || '').toLowerCase();
          }
          if (haystack[index].includes(state.needle)) state.matches.push(index);
        }
      }
      const at = kept >= 0 ? lowerBound(state.matches, kept) : -1;
      state.match = at >= 0 && state.matches[at] === kept ? at : -1;
      announce();
    }

    function announce() {
      const count = state.matches.length;
      previous.disabled = !count;
      next.disabled = !count;
      if (!state.needle) status.textContent = '';
      else if (!count) status.textContent = 'No matches';
      else if (state.match < 0) status.textContent = plural(count, 'match', 'matches');
      else status.textContent = `${formatNumber(state.match + 1)} of ${plural(count, 'match', 'matches')}`;
    }

    /** Cut the filtered list into units: runs of one day, at most UNIT_CAP messages each. */
    function rebuild() {
      const { sender: who, voiceOnly: voice } = state.filter;
      list = [];
      for (let i = 0; i < messages.length; i += 1) {
        if ((who < 0 || messages[i].sender === who) && (!voice || isVoice(messages[i]))) list.push(i);
      }
      const units = [];
      for (let start = 0, at = 1; at <= list.length; at += 1) {
        if (at === list.length || days[list[at]] !== days[list[start]] || at - start >= UNIT_CAP) {
          units.push({ start, end: at, day: days[list[start]], events: [] });
          start = at;
        }
      }
      const firsts = new Map();
      units.forEach((unit, at) => { if (!firsts.has(unit.day)) firsts.set(unit.day, at); });
      dayUnits = [...firsts].sort((left, right) => compare(left[0], right[0]));
      dayKeys = dayUnits.map(([day]) => day);
      placeEvents(units);
      windowed.setUnits(units);
      empty.hidden = list.length > 0;
      empty.textContent = messages.length ? 'No messages match these filters.' : 'This conversation has no messages.';
      findMatches();
      restore();
    }

    function renderControls() {
      senderSelect(sender, state.filter.sender);
      voiceOnly.box.checked = state.filter.voiceOnly;
      viewField.hidden = !twoPeople();
      viewAs.replaceChildren(...model.participants.map((name, at) => option(at, name)));
      viewAs.value = String(state.viewAs);
      date.min = span.first;
      date.max = span.last;
      date.disabled = !span.first;
    }

    function rerender(index) {
      const position = positionOf(index);
      if (position < 0 || !windowed.elementAt(position)) return false;
      return windowed.replace(position, bubble(index, follows(position, windowed.units()[windowed.unitAt(position)])));
    }

    /** Redraw what is in the DOM without losing the place: for a new query, another point of view or new events. */
    function redraw() {
      const held = showing() ? windowed.anchor() : null;
      windowed.refresh();
      if (held) windowed.reveal(held.position, held.offset);
    }

    function clearFilters() {
      state.filter = { sender: -1, voiceOnly: false };
      renderControls();
      rebuild();
    }

    /** Scroll to one message; filters that hide it are dropped first. */
    function show(index, flash) {
      if (positionOf(index) < 0) clearFilters();
      const position = positionOf(index);
      if (position < 0) return null;
      const at = windowed.unitAt(position);
      if (position === windowed.units()[at].start) windowed.revealUnit(at);
      const row = position === windowed.units()[at].start ? windowed.elementAt(position) : windowed.reveal(position);
      hold();
      if (row && flash) {
        row.classList.add('vp-flash');
        row.focus({ preventScroll: true });
        later(() => row.classList.remove('vp-flash'), 1600);
      }
      return row;
    }

    function go(match) {
      const old = holder.querySelector('.vp-current');
      if (old) old.classList.remove('vp-current');
      state.match = match;
      const index = state.matches[match];
      const voice = isVoice(messages[index]) ? messages[index].voice : null;
      if (voice && voice.text && isLong(voice.text, LONG_TRANSCRIPT) && !state.expanded.has(index) && voice.text.toLowerCase().includes(state.needle)) {
        state.expanded.add(index);
        rerender(index);
      }
      const row = show(index, false);
      if (row) {
        row.classList.add('vp-current');
        // A match far down a long bubble would still be out of sight.
        const mark = row.querySelector('.vp-mark');
        const view = scroller.getBoundingClientRect();
        if (mark && mark.getBoundingClientRect().bottom > view.bottom) scroller.scrollTop += mark.getBoundingClientRect().top - view.top - view.height / 3;
      }
      announce();
    }

    function step(direction) {
      const count = state.matches.length;
      if (!count) return;
      if (state.match >= 0) {
        go((state.match + direction + count) % count);
        return;
      }
      const after = lowerBound(state.matches, hold() ? saved.index : 0);
      go(direction > 0 ? after % count : (after - 1 + count) % count);
    }

    function setQuery(text) {
      cancel(typing);
      if (text === state.query) return;
      hold();
      state.query = text;
      state.needle = text.toLowerCase();
      state.match = -1;
      findMatches();
      redraw();
      if (state.matches.length) go(lowerBound(state.matches, saved ? saved.index : 0) % state.matches.length);
    }

    function toggleExpanded(index) {
      if (state.expanded.has(index)) state.expanded.delete(index); else state.expanded.add(index);
      rerender(index);
      const position = positionOf(index);
      const row = position < 0 ? null : windowed.elementAt(position);
      if (row && row.getBoundingClientRect().top < scroller.getBoundingClientRect().top) windowed.reveal(position);
    }

    /** Open a day: the first unit of that day, or of the next day that has messages. */
    function jump(day, dropFilters) {
      let at = lowerBound(dayKeys, day);
      if (dropFilters && (state.filter.sender >= 0 || state.filter.voiceOnly) && (!dayUnits[at] || dayUnits[at][0] !== day)) {
        clearFilters();
        at = lowerBound(dayKeys, day);
      }
      const found = dayUnits[Math.min(at, dayUnits.length - 1)];
      if (!found) return null;
      note.textContent = found[0] === day ? '' : `No messages on ${formatDay(day, 'short')}; showing ${formatDay(found[0], 'short')}.`;
      const row = windowed.revealUnit(found[1]);
      hold();
      return row;
    }

    /** update(model, changed): redraw only the changed bubbles that are in the DOM. */
    function patch(indices) {
      if (haystack) for (const index of indices) haystack[index] = undefined;
      if (state.needle) findMatches();
      if (!showing()) return;
      const held = windowed.anchor();
      let redrawn = false;
      for (const index of indices) redrawn = rerender(index) || redrawn;
      if (redrawn && held) windowed.reveal(held.position, held.offset);
    }

    function setEvents() {
      placeEvents(windowed.units());
      redraw();
    }

    listen(scroller, 'scroll', () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        if (showing()) hold();
      });
    }, { passive: true });
    listen(search, 'input', () => {
      cancel(typing);
      typing = later(() => setQuery(search.value), 150);
    });
    listen(search, 'keydown', (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      setQuery(search.value);
      step(event.shiftKey ? -1 : 1);
    });
    listen(sender, 'change', () => {
      hold();
      state.filter.sender = Number(sender.value);
      rebuild();
    });
    listen(voiceOnly.box, 'change', () => {
      hold();
      state.filter.voiceOnly = voiceOnly.box.checked;
      rebuild();
    });
    listen(date, 'change', () => { if (dayParts(date.value)) jump(date.value, false); });
    listen(viewAs, 'change', () => {
      state.viewAs = Number(viewAs.value);
      redraw();
    });

    function toggleFilters() {
      const open = !filters.classList.contains('vp-open');
      filters.classList.toggle('vp-open', open);
      toggle.setAttribute('aria-expanded', String(open));
    }

    return {
      enter: restore, leave: hold, rebuild, renderControls, patch, show, jump, step, toggleExpanded, toggleFilters, setEvents,
      destroy: windowed.destroy,
    };
  })();

  const voices = (() => {
    const panel = panels.voice;
    const sort = el('select', 'vp-control vp-select');
    sort.append(option('oldest', 'Oldest first'), option('newest', 'Newest first'), option('longest', 'Longest first'),
      option('shortest', 'Shortest first'), option('wordiest', 'Most words first'), option('briefest', 'Fewest words first'));
    const sender = el('select', 'vp-control vp-select');
    const search = el('input', 'vp-control vp-input vp-search');
    search.type = 'search';
    search.placeholder = 'Search transcripts';
    search.autocomplete = 'off';
    search.spellcheck = false;
    search.setAttribute('aria-label', 'Search transcripts');
    const sequence = checkBox('Play in sequence');
    const status = el('span', 'vp-status');
    status.setAttribute('role', 'status');
    const bar = el('div', 'vp-toolbar');
    const find = el('div', 'vp-find');
    find.append(search, status);
    const filters = el('div', 'vp-filters vp-open');
    filters.append(field('Sort', sort), field('From', sender), sequence.node);
    bar.append(find, filters);
    const scroller = el('div', 'vp-scroll vp-voice-list');
    scroller.tabIndex = 0;
    scroller.setAttribute('role', 'group');
    scroller.setAttribute('aria-label', 'Voice messages');
    const lead = el('div', 'vp-voice-head');
    const empty = el('p', 'vp-empty');
    const holder = el('div', 'vp-units');
    scroller.append(lead, empty, holder);
    panel.append(bar, scroller);

    let list = [];     // voice message indices in the order shown
    let saved = null;
    let typing = 0;
    let ticking = false;
    let dirty = false; // rows were redrawn in place and may no longer be where the sort or the search puts them

    const showing = () => state.tab === 'voice' && !state.destroyed;
    const needle = () => state.voice.query.toLowerCase();

    function row(index) {
      const message = messages[index];
      const { voice } = message;
      const node = el('div', 'vp-row');
      node.dataset.vpIndex = String(index);
      node.dataset.vpVoice = String(index);
      node.tabIndex = -1;
      if (voice.src) {
        const play = iconButton('vp-play', 'play', 'Play', 'play');
        play.dataset.vpIndex = String(index);
        node.append(play);
      } else {
        const mark = el('span', 'vp-row-glyph');
        mark.append(icon('mic'));
        node.append(mark);
      }
      const main = el('div', 'vp-row-main');
      const top = el('div', 'vp-row-top');
      top.append(nameNode(message.sender), el('span', 'vp-row-date', stampOf(index)), el('span', 'vp-voice-time'));
      if (voice.status === 'ok') top.append(el('span', 'vp-row-words', plural(voice.words, 'word')));
      main.append(top);
      const actions = el('div', 'vp-row-actions');
      if (voice.text) {
        const open = state.voice.expanded.has(index);
        const long = isLong(voice.text, LONG_EXCERPT);
        const excerpt = el('div', 'vp-excerpt');
        excerpt.dir = 'auto';
        // A collapsed excerpt starts near the first match, or the reason the row is listed would be cut off.
        const folded = voice.text.toLowerCase();
        const at = needle() && folded.length === voice.text.length ? folded.indexOf(needle()) : -1;
        const from = !open && at > 60 ? voice.text.indexOf(' ', at - 40) + 1 : 0;
        if (markText(excerpt, from ? `… ${voice.text.slice(from)}` : voice.text, needle())) node.classList.add('vp-match');
        excerpt.classList.toggle('vp-clamp', long && !open);
        main.append(excerpt);
        if (long) {
          const more = textButton('vp-link', 'excerpt', open ? 'Show less' : 'Show more');
          more.dataset.vpIndex = String(index);
          more.setAttribute('aria-expanded', String(open));
          actions.append(more);
        }
      }
      if (voice.status !== 'ok') main.append(el('div', voice.status === 'error' ? 'vp-voice-status vp-problem' : 'vp-voice-status', statusText(voice)));
      const show = textButton('vp-link', 'show', 'Show in conversation');
      show.dataset.vpIndex = String(index);
      actions.append(show);
      main.append(el('div', 'vp-voice-error'), actions);
      node.append(main);
      player.paint(node, index);
      return node;
    }

    const windowed = windowedList(scroller, holder, {
      render(unit) {
        const rows = [];
        for (let position = unit.start; position < unit.end; position += 1) rows.push(row(list[position]));
        return { children: rows, rows };
      },
      estimate: (unit) => 92 * (unit.end - unit.start),
      header: () => 0,
      gap: 0,
      visible: showing,
      resized: () => restore(),
    });

    function hold() {
      const at = windowed.anchor();
      if (at) saved = { index: list[at.position], offset: at.offset };
      return saved;
    }

    function restore() {
      if (!saved || !showing()) return;
      const position = list.indexOf(saved.index);
      if (position >= 0) windowed.reveal(position, saved.offset);
    }

    /** Per person: notes, time, spoken words and pace. The pace counts only notes that have both a transcript and a length. */
    function renderLead() {
      const sumsRows = sumsNow();
      const pace = model.participants.map(() => ({ words: 0, seconds: 0 }));
      for (const message of messages) {
        if (!isVoice(message) || !pace[message.sender]) continue;
        const { voice } = message;
        if (voice.status === 'ok' && typeof voice.seconds === 'number' && voice.seconds > 0) {
          pace[message.sender].words += voice.words;
          pace[message.sender].seconds += voice.seconds;
        }
      }
      const rows = [];
      model.participants.forEach((name, at) => {
        const sum = sumsRows[at];
        if (!sum.voice_notes) return;
        rows.push([nameNode(at), formatNumber(sum.voice_notes), sum.voice_timed ? formatSpan(sum.voice_seconds) : '–',
          sum.voice_transcribed ? formatNumber(sum.words_spoken) : '–', pace[at].seconds ? formatNumber(Math.round(pace[at].words / pace[at].seconds * 60)) : '–']);
      });
      const coverNotes = coverNow();
      const block = coverageBlock(coverNotes.notes && !coverNotes.playable ? ['Recordings are not included in this view.'] : []);
      lead.replaceChildren(block);
      if (rows.length) lead.append(table('Voice messages per participant', ['Participant', 'Voice notes', 'Voice time', 'Spoken words', 'Words per minute'], rows));
    }

    function order(kind) {
      const seconds = (index) => messages[index].voice.seconds;
      const words = (index) => messages[index].voice.words;
      const unknownLast = (left, right) => (seconds(left) == null) - (seconds(right) == null);
      if (kind === 'newest') return (left, right) => right - left;
      if (kind === 'longest') return (left, right) => unknownLast(left, right) || seconds(right) - seconds(left) || left - right;
      if (kind === 'shortest') return (left, right) => unknownLast(left, right) || seconds(left) - seconds(right) || left - right;
      if (kind === 'wordiest') return (left, right) => words(right) - words(left) || left - right;
      if (kind === 'briefest') return (left, right) => words(left) - words(right) || left - right;
      return null;
    }

    function rebuild() {
      const who = state.voice.sender;
      const wanted = needle();
      list = [];
      for (let i = 0; i < messages.length; i += 1) {
        if (!isVoice(messages[i]) || (who >= 0 && messages[i].sender !== who)) continue;
        if (wanted && !(messages[i].voice.text || '').toLowerCase().includes(wanted)) continue;
        list.push(i);
      }
      const sorter = order(state.voice.sort);
      if (sorter) list.sort(sorter);
      dirty = false;
      const units = [];
      for (let start = 0; start < list.length; start += UNIT_CAP) units.push({ start, end: Math.min(start + UNIT_CAP, list.length) });
      windowed.setUnits(units);
      empty.hidden = list.length > 0;
      empty.textContent = voiceTotal ? 'No voice messages match.' : 'This conversation has no voice messages.';
      status.textContent = list.length === voiceTotal ? plural(voiceTotal, 'voice message') : `${formatNumber(list.length)} of ${plural(voiceTotal, 'voice message')}`;
      restore();
    }

    function renderControls() {
      senderSelect(sender, state.voice.sender);
      sort.value = state.voice.sort;
      sequence.box.checked = state.voice.sequence;
    }

    function enter() {
      if (state.stale.voice) {
        hold();
        renderLead();
      }
      if (state.stale.voice === 'list') rebuild(); else restore();
      state.stale.voice = false;
    }

    function redrawRow(index) {
      const position = list.indexOf(index);
      return position >= 0 && windowed.elementAt(position) ? windowed.replace(position, row(index)) : false;
    }

    /**
     * update(model, changed): redraw the changed rows that are in the DOM. Rows do not move under the reader: the order
     * and the search are applied again when the list is next sorted, filtered or opened.
     */
    function patch(indices) {
      if (!showing()) return;
      dirty = true;
      const held = windowed.anchor();
      let redrawn = false;
      for (const index of indices) redrawn = redrawRow(index) || redrawn;
      if (redrawn && held) windowed.reveal(held.position, held.offset);
    }

    function toggleExpanded(index) {
      const open = state.voice.expanded;
      if (open.has(index)) open.delete(index); else open.add(index);
      redrawRow(index);
    }

    function leave() {
      hold();
      if (dirty) state.stale.voice = 'list';
    }

    /** The next recording that can be played after `index`, in the order shown. */
    function after(index) {
      if (state.stale.voice === 'list' && !list.length) rebuild();
      for (let position = list.indexOf(index) + 1; position > 0 && position < list.length; position += 1) {
        if (messages[list[position]].voice.src) return list[position];
      }
      return -1;
    }

    function change(apply) {
      apply();
      saved = null;
      rebuild();
      scroller.scrollTop = 0;
    }

    listen(scroller, 'scroll', () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        if (showing()) hold();
      });
    }, { passive: true });
    listen(sort, 'change', () => change(() => { state.voice.sort = sort.value; }));
    listen(sender, 'change', () => change(() => { state.voice.sender = Number(sender.value); }));
    listen(sequence.box, 'change', () => { state.voice.sequence = sequence.box.checked; });
    listen(search, 'input', () => {
      cancel(typing);
      typing = later(() => { if (search.value !== state.voice.query) change(() => { state.voice.query = search.value; }); }, 150);
    });

    return { enter, leave, renderControls, patch, toggleExpanded, after, destroy: windowed.destroy };
  })();

  // Activity: the summary table, then one chart per metric over a shared time axis. The charts are built once and only
  // drawn again, so the keyboard focus, the horizontal position and the pinned event survive update().
  const activity = (() => {
    const panel = panels.activity;
    const box = el('div', 'vp-activity');
    const lead = el('div', 'vp-activity-lead');
    const charts = el('div', 'vp-charts');
    box.append(lead, charts);
    panel.append(box);

    const switchLabel = el('span', 'vp-field-label', 'Group by');
    switchLabel.id = `${uid}-bucket`;
    const switcher = el('div', 'vp-seg');
    switcher.setAttribute('role', 'radiogroup');
    switcher.setAttribute('aria-labelledby', switchLabel.id);
    const radios = {};
    for (const bucket of BUCKETS) {
      const input = el('input', 'vp-seg-input');
      input.type = 'radio';
      input.name = switchLabel.id;
      input.value = bucket;
      const item = el('label', 'vp-seg-item');
      item.append(input, el('span', 'vp-seg-label', BUCKET_NAMES[bucket]));
      radios[bucket] = input;
      switcher.append(item);
    }
    const switchField = el('div', 'vp-field');
    switchField.append(switchLabel, switcher);
    const legend = el('div', 'vp-legend');
    legend.setAttribute('role', 'group');
    legend.setAttribute('aria-label', 'Participants in the charts');
    const keys = el('div', 'vp-keys');
    const bar = el('div', 'vp-chart-bar');
    bar.append(switchField, legend, keys);
    const hint = el('span', 'vp-sr', 'Left and Right move along the dates. Enter opens the date in the conversation.');
    hint.id = `${uid}-chart-hint`;
    const status = el('div', 'vp-sr vp-chart-status');
    status.setAttribute('role', 'status');
    const none = el('p', 'vp-empty', 'No message of this conversation has a date, so there is nothing to chart.');
    const scroller = el('div', 'vp-chart-scroll');
    // Some browsers put every scrolling box in the tab order; here the charts and the markers do the scrolling.
    scroller.tabIndex = -1;
    const canvas = el('div', 'vp-chart-canvas');
    const tip = el('div', 'vp-chart-tip');
    tip.hidden = true;
    tip.setAttribute('aria-hidden', 'true');
    const frame = el('div', 'vp-chart-frame');
    scroller.append(canvas);
    frame.append(scroller, tip);
    const footnote = el('p', 'vp-chart-footnote');
    const pin = el('div', 'vp-pin');
    pin.hidden = true;
    pin.setAttribute('role', 'region');
    pin.setAttribute('aria-label', 'Selected event');
    charts.append(bar, hint, status, none, frame, footnote, pin);

    // One chart: a fixed y axis, then the plot with a range input over it. The input is the chart's one tab stop and
    // gives it the keyboard and a name; the picture itself is hidden from screen readers.
    const plots = CHARTS.map((title, which) => {
      const name = el('span', 'vp-chart-name');
      const note = el('span', 'vp-chart-note');
      const heading = el('h3', 'vp-chart-title');
      heading.append(name, ' ', note);
      const axis = shape('svg', 'vp-chart-axis');
      axis.setAttribute('aria-hidden', 'true');
      axis.setAttribute('width', AXIS_WIDTH);
      axis.setAttribute('height', PLOT_HEIGHT);
      const key = el('input', 'vp-chart-key');
      key.type = 'range';
      key.min = '0';
      key.step = '1';
      key.dataset.vpChart = String(which);
      key.setAttribute('aria-describedby', hint.id);
      const plot = shape('svg', 'vp-chart-plot');
      plot.setAttribute('aria-hidden', 'true');
      plot.setAttribute('focusable', 'false');
      plot.setAttribute('height', PLOT_HEIGHT);
      plot.dataset.vpChart = String(which);
      const part = {};
      for (const [layer, tag] of [['shade', 'path'], ['pick', 'rect'], ['cursor', 'rect'], ['grid', 'path'], ['bars', 'g'], ['base', 'path'], ['gaps', 'path']]) {
        part[layer] = shape(tag, `vp-chart-${layer}`);
        plot.append(part[layer]);
      }
      for (const band of [part.pick, part.cursor]) {
        band.setAttribute('y', 0);
        band.setAttribute('height', PLOT_HEIGHT - PLOT_FOOT);
        band.setAttribute('hidden', '');
      }
      const body = el('div', 'vp-chart-body');
      body.append(key, plot);
      const row = el('div', 'vp-chart-row');
      row.append(axis, body);
      const section = el('div', 'vp-chart-panel');
      section.append(heading, row);
      canvas.append(section);
      return { title, name, note, axis, key, plot, ...part };
    });

    // Spoken words are hatched in the colour of their speaker; the patterns live in the plot that uses them.
    const hatches = shape('defs');
    for (let slot = 0; slot <= COLOURS; slot += 1) {
      const pattern = shape('pattern', `vp-s${slot}`);
      pattern.id = `${uid}-hatch-${slot}`;
      pattern.setAttribute('patternUnits', 'userSpaceOnUse');
      pattern.setAttribute('width', 5);
      pattern.setAttribute('height', 5);
      const ground = shape('rect', 'vp-hatch-ground');
      ground.setAttribute('width', 5);
      ground.setAttribute('height', 5);
      const ink = shape('path', 'vp-hatch-ink');
      ink.setAttribute('d', 'M-1,1l2,-2M0,5l5,-5M4,6l2,-2');
      pattern.append(ground, ink);
      hatches.append(pattern);
    }
    plots[WORDS_CHART].plot.prepend(hatches);

    const ticks = shape('svg', 'vp-chart-ticks');
    ticks.setAttribute('aria-hidden', 'true');
    ticks.setAttribute('height', 17);
    const months = el('div', 'vp-chart-months');
    months.setAttribute('aria-hidden', 'true');
    const dates = el('div', 'vp-chart-dates');
    dates.append(ticks, months);
    const datesRow = el('div', 'vp-chart-row');
    datesRow.append(el('div', 'vp-chart-gutter'), dates);
    const lane = el('div', 'vp-chart-lane');
    lane.setAttribute('role', 'group');
    lane.setAttribute('aria-label', 'Events on the charts');
    const laneRow = el('div', 'vp-chart-row vp-chart-lane-row');
    laneRow.append(el('div', 'vp-chart-gutter', 'Events'), lane);
    canvas.append(datesRow, laneRow);

    let view = null;   // what the charts show (buckets, series, values, events) and, once laid out, its geometry
    let seen = 0;      // the width the charts were last laid out for
    let hot = null;    // the bucket under the pointer or the keyboard: { which: a chart, or -1 for an event marker; at }
    let touched = '';  // the kind of pointer that last pressed on the charts
    let tapped = -1;   // the bucket a finger tapped last
    let stop = '';     // the bucket whose event marker is the tab stop of the markers
    let away = true;   // the tab was left: the horizontal position has to be put back

    const showing = () => state.tab === 'activity' && !state.destroyed;

    function summary() {
      const rows = sumsNow().map((sum, at) => {
        const who = document.createDocumentFragment();
        who.append(swatch(at < COLOURS ? at + 1 : 0), nameNode(at));
        return [who, sum];
      });
      if (rows.length > 1) {
        const all = Object.fromEntries(METRICS.map((metric) => [metric, 0]));
        for (const [, sum] of rows) for (const metric of METRICS) all[metric] += sum[metric];
        rows.push([el('span', 'vp-name vp-everyone', 'Everyone'), all]);
      }
      return table('Activity per participant',
        ['Participant', 'Messages', 'Typed words', 'Spoken words', 'Total words', 'Voice notes', 'Transcribed', 'Voice time'],
        rows.map(([who, sum]) => [who, formatNumber(sum.messages), formatNumber(sum.words_typed), formatNumber(sum.words_spoken),
          formatNumber(sum.words_typed + sum.words_spoken), formatNumber(sum.voice_notes),
          sum.voice_notes ? `${formatNumber(sum.voice_transcribed)} of ${formatNumber(sum.voice_notes)}` : '–', sum.voice_timed ? formatSpan(sum.voice_seconds) : '–']));
    }

    function drawLegend() {
      const focused = legend.contains(document.activeElement) ? document.activeElement.dataset.vpSeries : null;
      // One participant needs no legend: the titles say what is drawn.
      legend.hidden = view.all.length < 2;
      legend.replaceChildren(...view.all.map((item) => {
        const node = el('button', 'vp-control vp-legend-item');
        node.type = 'button';
        node.dataset.vpAction = 'legend';
        node.dataset.vpSeries = item.key;
        node.setAttribute('aria-pressed', String(!state.hidden.has(item.key)));
        node.append(swatch(item.slot), el('bdi', 'vp-legend-name', item.name));
        return node;
      }));
      if (focused != null) legend.querySelector(`[data-vp-series="${focused}"]`)?.focus({ preventScroll: true });
      const mark = (kind, text) => {
        const node = el('span', 'vp-key');
        const sign = el('span', `vp-swatch vp-swatch-${kind}`);
        sign.setAttribute('aria-hidden', 'true');
        node.append(sign, el('span', 'vp-key-label', text));
        return node;
      };
      const marks = [mark('typed', 'Typed words'), mark('spoken', 'Spoken words')];
      if (view.bucket === 'day') marks.push(mark('weekend', 'Weekend'));
      if (view.panels[WORDS_CHART].gaps || view.panels[MINUTES_CHART].gaps) marks.push(mark('gap', 'Voice notes without a transcript or a length'));
      keys.replaceChildren(...marks);
    }

    /** Everything that does not depend on the width: the buckets, the series that are shown, their values and the events. */
    function compute() {
      const bucket = bucketNow();
      const data = setNow(bucket);
      const all = seriesList();
      const shown = all.filter((item) => !state.hidden.has(item.key));
      let bucketKeys = data.buckets;
      let before = 0;
      let far = 0;
      const markers = [];
      const markerAt = new Map();
      const bucketOf = new Map();
      if (bucketKeys.length) {
        // The axis is the chat's, widened to the events that lie close before or after it.
        const [first, last] = [bucketKeys[0], bucketKeys[bucketKeys.length - 1]];
        let [earliest, latest] = [first, last];
        const near = [];
        state.events.forEach((event, number) => {
          if (eventPlace(event) === 'far') {
            far += 1;
            return;
          }
          const key = bucketStart(event.date, bucket);
          near.push([key, number]);
          if (key < earliest) earliest = key;
          if (key > latest) latest = key;
        });
        const early = [];
        for (let key = earliest; key < first; key = nextBucket(key, bucket)) early.push(key);
        const late = [];
        for (let key = last; key < latest;) {
          key = nextBucket(key, bucket);
          late.push(key);
        }
        before = early.length;
        bucketKeys = [...early, ...bucketKeys, ...late];
        const place = new Map(bucketKeys.map((key, at) => [key, at]));
        for (const [key, number] of near) {
          const at = place.get(key);
          if (!markerAt.has(at)) {
            markerAt.set(at, { at, events: [] });
            markers.push(markerAt.get(at));
          }
          markerAt.get(at).events.push(number);
          bucketOf.set(number, at);
        }
      } else {
        far = state.events.length;
      }
      const count = bucketKeys.length;
      const rows = shown.map((item) => {
        const row = Object.fromEntries(METRICS.map((metric) => [metric, new Array(count).fill(0)]));
        for (const member of item.members) {
          for (const metric of METRICS) {
            const source = data.series[member][metric];
            for (let at = 0; at < source.length; at += 1) row[metric][before + at] += source[at];
          }
        }
        return row;
      });
      const highest = (stacks) => {
        let most = 0;
        for (const [lower, upper] of stacks) {
          for (let at = 0; at < count; at += 1) most = Math.max(most, lower[at] + (upper ? upper[at] : 0));
        }
        return most;
      };
      // A bucket whose voice notes are not all transcribed (or timed) shows less than was said: it gets a mark.
      const lacking = (metric) => {
        const flags = new Array(count).fill(false);
        for (const row of rows) {
          for (let at = 0; at < count; at += 1) if (row.voice_notes[at] > row[metric][at]) flags[at] = true;
        }
        return flags.includes(true) ? flags : null;
      };
      const stacks = [
        rows.map((row) => [row.messages, null]),
        rows.map((row) => [row.words_typed, row.words_spoken]),
        rows.map((row) => [row.voice_notes, null]),
        rows.map((row) => [row.voice_seconds.map((seconds) => seconds / 60), null]),
      ];
      const gaps = [null, lacking('voice_transcribed'), null, lacking('voice_timed')];
      const start = count ? dayParts(bucketKeys[0]) : null;
      view = {
        bucket, count, all, shown, rows, markers, markerAt, bucketOf, far,
        keys: bucketKeys,
        weekday: start ? utcDate(start.year, start.month, start.day).getUTCDay() : 0,
        panels: stacks.map((list, which) => ({ stacks: list, gaps: gaps[which], most: highest(list) })),
        cursor: Math.max(0, bucketKeys.indexOf(state.cursor)),
        step: 0,
        width: 0,
        bar: null,
      };
      for (const item of view.panels) item.scale = niceScale(item.most, item !== view.panels[MINUTES_CHART]);
      radios[bucket].checked = true;
      none.hidden = count > 0;
      bar.hidden = !count;
      frame.hidden = !count;
      laneRow.hidden = !markers.length;
      footnote.hidden = !count || !far;
      footnote.textContent = `${plural(far, 'event')} far outside the conversation ${far === 1 ? 'is' : 'are'} not on the charts.`;
      drawLegend();
      const notes = coverNow();
      const share = (count) => `${formatNumber(count)} of ${formatNumber(notes.notes)} voice notes`;
      plots.forEach(({ title, name, note, key }, which) => {
        name.textContent = `${title} per ${bucket}`;
        key.setAttribute('aria-label', `${title} per ${bucket}`);
        if (which === WORDS_CHART && notes.transcribed < notes.notes) note.textContent = `${share(notes.transcribed)} transcribed`;
        else if (which === MINUTES_CHART && notes.timed < notes.notes) note.textContent = `${share(notes.timed)} timed`;
        else note.textContent = view.panels[which].most ? '' : 'nothing to show';
      });
    }

    const tickText = (tick, scale) => (!tick ? '0' : scale.digits ? tick.toFixed(scale.digits) : tick >= 10000 ? `${formatNumber(tick / 1000)}k` : formatNumber(tick));

    function drawDates() {
      const { bucket, count, step, width } = view;
      ticks.setAttribute('width', width);
      // Days are named at every Monday, weeks by the day their Monday falls on, months by their name.
      const every = bucket === 'week' ? Math.ceil(20 / step) : 1;
      const labels = [];
      let marks = '';
      for (let at = 0; at < count; at += every) {
        if (bucket === 'day' && (view.weekday + at) % 7 !== 1) continue;
        const middle = (at + 0.5) * step;
        marks += `M${Math.floor(middle) + 0.5},0v4`;
        const label = shape('text', 'vp-chart-tick');
        label.setAttribute('x', middle);
        label.setAttribute('y', 14);
        label.setAttribute('text-anchor', 'middle');
        label.textContent = bucket === 'month' ? formatDay(view.keys[at], 'monthOnly') : String(Number(view.keys[at].slice(8)));
        labels.push(label);
      }
      const line = shape('path', 'vp-chart-grid');
      line.setAttribute('d', marks);
      ticks.replaceChildren(line, ...labels);
      // Under them the months (or the years of a chart of months), whose names stay in sight while their dates are.
      const blocks = [];
      for (let at = 0; at < count; at += 1) {
        const group = view.keys[at].slice(0, bucket === 'month' ? 4 : 7);
        if (blocks.length && blocks[blocks.length - 1].group === group) blocks[blocks.length - 1].size += step;
        else blocks.push({ group, size: step });
      }
      months.replaceChildren(...blocks.map(({ group, size }) => {
        const node = el('div', 'vp-chart-month');
        node.style.width = `${size}px`;
        let text = group;
        if (bucket !== 'month') text = size >= 64 ? formatDay(`${group}-01`, 'monthShort') : size >= 32 ? formatDay(`${group}-01`, 'monthOnly') : '';
        if (text) node.append(el('span', 'vp-chart-month-label', text));
        return node;
      }));
    }

    function markerName(marker) {
      const [first] = marker.events;
      const event = state.events[first];
      if (marker.events.length === 1) return `Event ${first + 1}, ${formatDay(event.date, 'short')}: ${cut(event.label, 80)}`;
      return `${marker.events.length} events, numbers ${first + 1} to ${marker.events[marker.events.length - 1] + 1}, ${bucketLabel(view.keys[marker.at], view.bucket, 'short')}`;
    }

    /** One marker per bucket with events, carrying the event's number or how many there are; rows keep neighbours apart. */
    function drawLane() {
      const { step, width, markers } = view;
      const focused = lane.contains(document.activeElement) ? document.activeElement.dataset.vpMarker : null;
      const ends = [];
      const nodes = markers.map((marker) => {
        const label = marker.events.length > 1 ? `×${marker.events.length}` : String(marker.events[0] + 1);
        const wide = Math.max(24, 12 + 7 * label.length);
        const left = Math.max(0, Math.min(width - wide, Math.round((marker.at + 0.5) * step - wide / 2)));
        let row = ends.findIndex((end) => end + 2 <= left);
        if (row < 0) row = ends.length < MARKER_ROWS ? ends.push(0) - 1 : ends.indexOf(Math.min(...ends));
        ends[row] = left + wide;
        const node = el('button', 'vp-control vp-marker', label);
        node.type = 'button';
        node.tabIndex = -1;
        node.dataset.vpAction = 'marker';
        node.dataset.vpMarker = String(marker.at);
        node.style.left = `${left}px`;
        node.style.top = `${row * (MARKER_HEIGHT + 4)}px`;
        node.style.width = `${wide}px`;
        node.setAttribute('aria-label', markerName(marker));
        return node;
      });
      lane.style.width = `${width}px`;
      lane.style.height = `${Math.max(1, ends.length) * (MARKER_HEIGHT + 4)}px`;
      lane.replaceChildren(...nodes);
      const kept = nodes.find((node) => view.keys[Number(node.dataset.vpMarker)] === stop) || nodes[0];
      if (kept) kept.tabIndex = 0;
      if (focused != null) lane.querySelector(`[data-vp-marker="${focused}"]`)?.focus({ preventScroll: true });
    }

    /** The geometry for the width there is, and every mark drawn again. */
    function layout() {
      if (!view || !view.count || !showing() || !scroller.clientWidth) return;
      const { bucket, count } = view;
      const seats = Math.max(1, view.shown.length);
      const least = Math.max(MIN_STEP[bucket], Math.ceil((4 * seats - 1) / 0.72));
      const step = Math.max(least, Math.min(MAX_STEP, Math.floor((scroller.clientWidth - AXIS_WIDTH) / count)));
      const inner = step >= 30 ? 2 : 1;
      const space = step - Math.max(3, Math.round(step * 0.28));
      const wide = Math.max(1, Math.min(BAR_WIDEST, Math.floor((space - inner * (seats - 1)) / seats)));
      const offset = Math.round((step - wide * seats - inner * (seats - 1)) / 2);
      const width = step * count;
      Object.assign(view, { step, width, bar: { wide, inner, offset } });
      seen = scroller.clientWidth;
      canvas.style.width = `${AXIS_WIDTH + width}px`;
      canvas.style.setProperty('--vp-chart-view', `${seen}px`);
      const base = PLOT_HEIGHT - PLOT_FOOT;
      const area = base - PLOT_TOP;
      const opens = (at) => (bucket === 'month' ? view.keys[at].slice(5, 7) === '01' : view.keys[at].slice(0, 7) !== view.keys[at - 1].slice(0, 7));
      let shade = '';
      let rules = '';
      for (let at = 0; at < count; at += 1) {
        const weekday = (view.weekday + at) % 7;
        if (bucket === 'day' && (weekday === 0 || weekday === 6)) shade += `M${at * step},0h${step}V${base}h${-step}z`;
        if (at && opens(at)) rules += `M${at * step + 0.5},${PLOT_TOP - 8}V${base}`;
      }
      plots.forEach((target, which) => {
        const { stacks, gaps, scale } = view.panels[which];
        target.plot.setAttribute('width', width);
        target.shade.setAttribute('d', shade);
        let grid = rules;
        const labels = [];
        for (const tick of scale.ticks) {
          const y = Math.round(base - tick / scale.top * area);
          if (tick) grid += `M0,${y + 0.5}H${width}`;
          const label = shape('text', 'vp-chart-tick');
          label.setAttribute('x', AXIS_WIDTH - 8);
          label.setAttribute('y', y + 3.5);
          label.setAttribute('text-anchor', 'end');
          label.textContent = tickText(tick, scale);
          labels.push(label);
        }
        target.axis.replaceChildren(...labels);
        target.grid.setAttribute('d', grid);
        target.base.setAttribute('d', `M0,${base + 0.5}H${width}`);
        const drawn = stacks.map(() => ['', '']);
        for (let at = 0; at < count; at += 1) {
          for (let seat = 0; seat < stacks.length; seat += 1) {
            const [lower, upper] = stacks[seat];
            const low = lower[at] > 0 ? Math.max(1.5, lower[at] / scale.top * area) : 0;
            const high = upper && upper[at] > 0 ? Math.max(1.5, upper[at] / scale.top * area) : 0;
            if (!low && !high) continue;
            const x = at * step + offset + seat * (wide + inner);
            if (low) drawn[seat][0] += barPath(x, base - low, wide, low, high ? 0 : BAR_ROUND);
            // The surface shows through between typed and spoken, as it does between two bars.
            const gap = low && high > inner + 1.5 ? inner : 0;
            if (high) drawn[seat][1] += barPath(x, base - low - high, wide, high - gap, BAR_ROUND);
          }
        }
        const bars = [];
        view.shown.forEach((item, seat) => {
          const solid = shape('path', `vp-bar vp-s${item.slot}`);
          solid.setAttribute('d', drawn[seat][0]);
          bars.push(solid);
          if (!drawn[seat][1]) return;
          const hatched = shape('path', 'vp-bar-spoken');
          hatched.setAttribute('d', drawn[seat][1]);
          hatched.setAttribute('fill', `url(#${uid}-hatch-${item.slot})`);
          bars.push(hatched);
        });
        target.bars.replaceChildren(...bars);
        let dots = '';
        if (gaps) {
          for (let at = 0; at < count; at += 1) if (gaps[at]) dots += dotPath((at + 0.5) * step, 5, 2.25);
        }
        target.gaps.setAttribute('d', dots);
        target.key.max = String(count - 1);
      });
      drawDates();
      drawLane();
      setCursor(view.cursor);
      select();
      hot = null;
      settle();
    }

    function setCursor(at) {
      view.cursor = Math.max(0, Math.min(view.count - 1, at));
      state.cursor = view.keys[view.cursor];
      const text = bucketLabel(state.cursor, view.bucket, 'long');
      for (const { key } of plots) {
        key.value = String(view.cursor);
        key.setAttribute('aria-valuetext', text);
      }
    }

    /** Scroll sideways just far enough to bring a bucket, and its marker when given, out from under the y axis or in from the right. */
    function reach(at, marker) {
      const left = Math.min(at * view.step, marker ? marker.offsetLeft : Infinity);
      const right = Math.max((at + 1) * view.step, marker ? marker.offsetLeft + marker.offsetWidth : 0);
      const room = scroller.clientWidth - AXIS_WIDTH;
      if (left < scroller.scrollLeft) scroller.scrollLeft = left;
      else if (right > scroller.scrollLeft + room) scroller.scrollLeft = right - room;
    }

    function valueOf(which, row, at) {
      if (which === WORDS_CHART) {
        const [typed, spoken] = [row.words_typed[at], row.words_spoken[at]];
        return [formatNumber(typed + spoken), typed + spoken ? `${formatNumber(typed)} typed, ${formatNumber(spoken)} spoken` : ''];
      }
      if (which === MINUTES_CHART) return [row.voice_timed[at] ? formatSpan(row.voice_seconds[at]) : row.voice_notes[at] ? 'unknown' : '0 s', ''];
      return [formatNumber(which ? row.voice_notes[at] : row.messages[at]), ''];
    }

    /** What a bucket holds for one chart (or for its event marker alone): the nodes of the tooltip and the same in words. */
    function describe(which, at) {
      const key = view.keys[at];
      const nodes = [el('div', 'vp-tip-title', bucketLabel(key, view.bucket))];
      const said = [bucketLabel(key, view.bucket, 'long')];
      if (which >= 0) {
        nodes.push(el('div', 'vp-tip-metric', plots[which].title));
        const parts = view.shown.map((item, seat) => {
          const [value, detail] = valueOf(which, view.rows[seat], at);
          const line = el('div', 'vp-tip-row');
          line.append(swatch(item.slot), el('bdi', 'vp-tip-name', item.name), el('strong', 'vp-tip-value', value));
          if (detail) line.append(el('span', 'vp-tip-detail', detail));
          nodes.push(line);
          return `${item.name} ${value}${detail ? ` (${detail})` : ''}`;
        });
        said.push(`${plots[which].title}: ${parts.join(', ')}`);
        const sum = (metric) => view.rows.reduce((total, row) => total + row[metric][at], 0);
        const [notes, transcribed, timed] = [sum('voice_notes'), sum('voice_transcribed'), sum('voice_timed')];
        let gap = '';
        if (which === WORDS_CHART && transcribed < notes) gap = `Spoken words cover ${transcribed} of ${notes} voice notes here`;
        if (which === MINUTES_CHART && timed < notes) gap = `Voice time covers ${timed} of ${notes} voice notes here`;
        if (gap) {
          nodes.push(el('div', 'vp-tip-note', gap));
          said.push(gap);
        }
      }
      const marker = view.markerAt.get(at);
      for (const number of marker ? marker.events.slice(0, TIP_EVENTS) : []) {
        const label = cut(state.events[number].label, TIP_LABEL);
        const text = el('span', 'vp-tip-label', label);
        text.dir = 'auto';
        const line = el('div', 'vp-tip-event');
        line.append(el('span', 'vp-event-number', String(number + 1)), text);
        nodes.push(line);
        said.push(`Event ${number + 1}: ${label.replace(/[.\s]+$/, '')}`);
      }
      if (marker && marker.events.length > TIP_EVENTS) {
        const rest = `and ${plural(marker.events.length - TIP_EVENTS, 'more event')}`;
        nodes.push(el('div', 'vp-tip-note', rest));
        said.push(rest);
      }
      return { nodes, text: `${said.join('. ')}.` };
    }

    /** Put the tooltip beside its bucket, inside the frame; a bucket scrolled out of sight has none. */
    function place() {
      const { which, at } = hot;
      const edge = frame.getBoundingClientRect();
      const anchor = (which < 0 ? lane : plots[which].plot).getBoundingClientRect();
      const middle = plots[0].plot.getBoundingClientRect().left - edge.left + (at + 0.5) * view.step;
      if (middle < AXIS_WIDTH || middle > edge.width) {
        tip.hidden = true;
        return;
      }
      tip.hidden = false;
      const size = tip.getBoundingClientRect();
      const reachOut = view.step / 2 + 10;
      let left = middle + reachOut;
      let top = which < 0 ? anchor.top - edge.top - size.height - 6 : anchor.top - edge.top + 4;
      if (left + size.width > edge.width) left = middle - reachOut - size.width;
      if (left < 0) {
        // No room on either side: above the chart, or below it, so that the bars in question stay in sight.
        left = Math.max(0, Math.min(edge.width - size.width, middle - size.width / 2));
        if (which >= 0) top = anchor.top - edge.top - size.height - 4 >= 0 ? anchor.top - edge.top - size.height - 4 : anchor.bottom - edge.top + 4;
      }
      tip.style.left = `${Math.round(left)}px`;
      tip.style.top = `${Math.round(Math.max(0, Math.min(edge.height - size.height, top)))}px`;
    }

    /** Show what one bucket holds: a band across the four charts, the tooltip, and the same words for screen readers. */
    function show(which, at) {
      if (hot && hot.which === which && hot.at === at && !tip.hidden) return;
      hot = { which, at };
      for (const { cursor } of plots) {
        cursor.removeAttribute('hidden');
        cursor.setAttribute('x', at * view.step);
        cursor.setAttribute('width', view.step);
      }
      const { nodes, text } = describe(which, at);
      tip.replaceChildren(...nodes);
      status.textContent = text;
      place();
    }

    function hide() {
      hot = null;
      tapped = -1;
      tip.hidden = true;
      for (const { cursor } of plots) cursor.setAttribute('hidden', '');
    }

    /** With the pointer gone, show what the keyboard is on, or nothing. */
    function settle() {
      const active = document.activeElement;
      const drawn = view && view.step && active && canvas.contains(active);
      if (drawn && active.classList.contains('vp-chart-key')) show(Number(active.dataset.vpChart), view.cursor);
      else if (drawn && active.classList.contains('vp-marker')) show(-1, Number(active.dataset.vpMarker));
      else hide();
    }

    /** Open a bucket in the conversation: its day, or the first day of a week or month that has messages. */
    function open(at) {
      const key = view.keys[at];
      let day = key;
      if (view.bucket !== 'day') {
        const daily = setNow('day');
        const end = nextBucket(key, view.bucket);
        for (let i = lowerBound(daily.buckets, key); i < daily.buckets.length && daily.buckets[i] < end; i += 1) {
          if (daily.series.some((row) => row.messages[i] > 0)) {
            day = daily.buckets[i];
            break;
          }
        }
      }
      openDay(day);
    }

    /** Paint the selected event: its marker, a band across the charts, and its whole label pinned under them. */
    function select() {
      if (!view || state.stale.activity) return;
      const number = state.selectedEvent;
      const at = view.bucketOf.has(number) ? view.bucketOf.get(number) : -1;
      for (const node of lane.children) {
        const chosen = Number(node.dataset.vpMarker) === at;
        node.classList.toggle('vp-selected', chosen);
        node.setAttribute('aria-pressed', String(chosen));
      }
      for (const { pick } of plots) {
        pick.toggleAttribute('hidden', at < 0 || !view.step);
        pick.setAttribute('x', Math.max(0, at) * view.step);
        pick.setAttribute('width', view.step);
      }
      const numbers = number < 0 ? [] : at < 0 ? [number] : view.markerAt.get(at).events;
      pin.hidden = !numbers.length;
      pin.classList.toggle('vp-pin-many', numbers.length > 1);
      if (!numbers.length) {
        pin.replaceChildren();
        return;
      }
      const head = el('div', 'vp-pin-head');
      head.append(el('span', 'vp-pin-title', numbers.length > 1 ? `${numbers.length} events · ${bucketLabel(view.keys[at], view.bucket, 'long')}` : 'Selected event'),
        iconButton('vp-icon-button', 'unpin', 'Clear the selected event', 'close'));
      const held = heldControl(pin);
      pin.replaceChildren(head, ...numbers.map((item) => {
        const event = state.events[item];
        const where = eventPlace(event);
        const node = el('div', 'vp-pin-item');
        node.dataset.vpEvent = String(item);
        if (item === number) {
          node.classList.add('vp-selected');
          node.setAttribute('aria-current', 'true');
        }
        const top = el('div', 'vp-pin-top');
        top.append(el('span', 'vp-sr', 'Event '), el('span', 'vp-event-number', String(item + 1)), el('span', 'vp-sr', ', '), el('span', 'vp-event-date', formatDay(event.date)));
        if (where !== 'inside') top.append(el('span', 'vp-flag', where === 'far' ? 'Far outside the conversation, not on the charts' : 'Outside the conversation'));
        const label = el('p', 'vp-pin-label', event.label);
        label.dir = 'auto';
        const actions = el('div', 'vp-event-actions');
        const list = textButton('vp-link', 'show-event', 'Show in Events');
        list.dataset.vpEvent = String(item);
        actions.append(list);
        if (where === 'inside') {
          const day = textButton('vp-link', 'open-day', 'Open this day');
          day.dataset.vpEvent = String(item);
          actions.append(day);
        }
        node.append(top, label, actions);
        return node;
      }));
      if (held) held(pin);
    }

    /** A marker was chosen: pin its events, or let go of them when they were pinned already. */
    function pinAt(at) {
      const marker = view && view.markerAt.get(at);
      if (!marker) return;
      const pinned = marker.events.includes(state.selectedEvent);
      selectEvent(pinned ? -1 : marker.events[0]);
      const what = marker.events.length > 1 ? `${marker.events.length} events` : `Event ${marker.events[0] + 1}`;
      status.textContent = pinned ? `${what} no longer pinned.` : `${what} pinned under the charts.`;
    }

    function unpin() {
      const at = view ? view.bucketOf.get(state.selectedEvent) : undefined;
      selectEvent(-1);
      const node = at === undefined ? null : lane.querySelector(`[data-vp-marker="${at}"]`);
      (node || plots[0].key).focus({ preventScroll: true });
    }

    /** Bring the marker of an event into view and put the keyboard on it. */
    function reveal(number) {
      const at = view ? view.bucketOf.get(number) : undefined;
      const node = at === undefined || !view.step ? null : lane.querySelector(`[data-vp-marker="${at}"]`);
      if (!node) return;
      reach(at, node);
      node.focus();
    }

    function toggle(key) {
      if (!view) return;
      if (state.hidden.has(key)) state.hidden.delete(key); else state.hidden.add(key);
      if (view.all.every((item) => state.hidden.has(item.key))) state.hidden.clear();
      compute();
      layout();
    }

    function render() {
      const focused = lead.contains(document.activeElement);
      const region = summary();
      lead.replaceChildren(coverageBlock(), region);
      if (focused) region.focus({ preventScroll: true });
      compute();
      state.stale.activity = false;
      layout();
    }

    function enter() {
      if (state.stale.activity) render();
      else if (scroller.clientWidth !== seen) layout();
      if (away) scroller.scrollLeft = state.chartScroll;
      away = false;
    }

    function leave() {
      state.chartScroll = scroller.scrollLeft;
      away = true;
      hide();
    }

    const indexAt = (event, plot) => Math.max(0, Math.min(view.count - 1, Math.floor((event.clientX - plot.getBoundingClientRect().left) / view.step)));

    listen(canvas, 'pointermove', (event) => {
      if (event.pointerType === 'touch' || !view || !view.step) return;
      const plot = event.target.closest('.vp-chart-plot');
      const marker = plot ? null : event.target.closest('.vp-marker');
      if (plot) show(Number(plot.dataset.vpChart), indexAt(event, plot));
      else if (marker) show(-1, Number(marker.dataset.vpMarker));
      else settle();
    });
    listen(canvas, 'pointerleave', (event) => { if (event.pointerType !== 'touch') settle(); });
    listen(canvas, 'pointerdown', (event) => { touched = event.pointerType; });
    listen(canvas, 'click', (event) => {
      const plot = event.target.closest('.vp-chart-plot');
      if (!plot || !view || !view.step) return;
      const at = indexAt(event, plot);
      setCursor(at);
      // A finger cannot hover: its first tap shows what the bars stand for, a second tap on them opens the day.
      if ((touched === 'touch' || touched === 'pen') && tapped !== at) {
        hot = null;
        show(Number(plot.dataset.vpChart), at);
        tapped = at;
        return;
      }
      open(at);
    });
    // A tooltip left by a tap goes away with the next press anywhere else.
    listen(root, 'pointerdown', (event) => { if (!tip.hidden && !canvas.contains(event.target)) settle(); });
    listen(scroller, 'scroll', () => {
      state.chartScroll = scroller.scrollLeft;
      if (hot) place();
    }, { passive: true });
    for (const { key } of plots) {
      const which = Number(key.dataset.vpChart);
      const follow = () => {
        if (!view || !view.step) return;
        setCursor(Number(key.value));
        reach(view.cursor);
        show(which, view.cursor);
      };
      listen(key, 'input', follow);
      listen(key, 'focus', follow);
      listen(key, 'blur', settle);
      listen(key, 'keydown', (event) => {
        if (!view || !view.step) return;
        if (event.key === 'Enter') {
          event.preventDefault();
          open(view.cursor);
        } else if (event.key === 'Escape') {
          tip.hidden = true;
        }
      });
    }
    listen(lane, 'focusin', (event) => {
      const node = event.target.closest('.vp-marker');
      if (!node || !view || !view.step) return;
      const at = Number(node.dataset.vpMarker);
      for (const other of lane.children) other.tabIndex = other === node ? 0 : -1;
      stop = view.keys[at];
      reach(at, node);
      show(-1, at);
    });
    listen(lane, 'focusout', settle);
    listen(lane, 'keydown', (event) => {
      const nodes = [...lane.children];
      const at = nodes.indexOf(event.target);
      let to = -1;
      if (event.key === 'ArrowRight') to = Math.min(nodes.length - 1, at + 1);
      else if (event.key === 'ArrowLeft') to = Math.max(0, at - 1);
      else if (event.key === 'Home') to = 0;
      else if (event.key === 'End') to = nodes.length - 1;
      else if (event.key === 'Escape') tip.hidden = true;
      if (at < 0 || to < 0) return;
      event.preventDefault();
      nodes[to].focus();
    });
    listen(switcher, 'change', (event) => {
      if (!BUCKETS.includes(event.target.value) || !event.target.checked) return;
      state.bucket = event.target.value;
      if (state.cursor) state.cursor = bucketStart(state.cursor, state.bucket);
      hide();
      compute();
      layout();
      scroller.scrollLeft = 0;
      if (view.step) reach(view.cursor);
    });

    // Laid out for the width there is: another width means other bars, and a narrower chart scrolls.
    const resizer = new ResizeObserver(() => { if (showing() && scroller.clientWidth && scroller.clientWidth !== seen) layout(); });
    resizer.observe(scroller);

    return { enter, leave, render, select, pinAt, unpin, reveal, toggle, destroy: () => resizer.disconnect() };
  })();

  // Events: a timeline of numbered cards, each with its whole label and what was written and said that day, and the
  // control that loads another events file.
  const eventsView = (() => {
    const panel = panels.events;
    const load = textButton('vp-button', 'load-events', 'Load events file…');
    const file = el('input', 'vp-events-file');
    file.type = 'file';
    file.accept = '.txt,text/plain';
    file.hidden = true;
    file.setAttribute('aria-label', 'Events file');
    const note = el('p', 'vp-events-note');
    note.setAttribute('role', 'status');
    const warnings = el('ul', 'vp-warnings');
    const bar = el('div', 'vp-events-bar');
    bar.append(load, file, el('span', 'vp-hint', 'One event per line: a date, then a label.'), note, warnings);
    const empty = el('p', 'vp-empty', 'No events. Load an events file to mark dates in the conversation and on the charts.');
    const list = el('ol', 'vp-event-list');
    const further = textButton('vp-button vp-events-more', 'more-events', '');
    const box = el('div', 'vp-events');
    box.append(bar, empty, list, further);
    panel.append(box);

    /** What each person wrote and said on one day, from the same day buckets as the charts. */
    function dayActivity(day) {
      const data = setNow('day');
      const at = data.buckets.length ? dayNumber(day) - dayNumber(data.buckets[0]) : -1;
      const lines = [];
      const voice = { notes: 0, transcribed: 0, timed: 0 };
      for (const item of at >= 0 && at < data.buckets.length ? seriesList() : []) {
        const sum = Object.fromEntries(METRICS.map((metric) => [metric, 0]));
        for (const member of item.members) for (const metric of METRICS) sum[metric] += data.series[member][metric][at];
        if (!sum.messages) continue;
        voice.notes += sum.voice_notes;
        voice.transcribed += sum.voice_transcribed;
        voice.timed += sum.voice_timed;
        const figures = [plural(sum.messages, 'message'), plural(sum.words_typed + sum.words_spoken, 'word')];
        if (sum.voice_notes) figures.push(sum.voice_timed ? `${formatSpan(sum.voice_seconds)} of voice` : plural(sum.voice_notes, 'voice note'));
        const line = el('li', 'vp-event-who');
        line.append(swatch(item.slot), item.members.length === 1 ? nameNode(item.members[0]) : el('bdi', 'vp-name vp-p0', item.name),
          el('span', 'vp-event-figures', figures.join(' · ')));
        lines.push(line);
      }
      const node = el('div', 'vp-event-day');
      if (!lines.length) {
        node.append(el('p', 'vp-event-quiet', 'No messages on this day.'));
        return node;
      }
      const people = el('ul', 'vp-event-people');
      people.append(...lines);
      node.append(people);
      // A day whose voice notes are not all transcribed must not read as a quiet day.
      const partly = [];
      if (voice.transcribed < voice.notes) partly.push(`spoken words cover ${voice.transcribed} of ${plural(voice.notes, 'voice message')}`);
      if (voice.timed < voice.notes) partly.push(`voice time covers ${voice.timed} of ${voice.notes}`);
      if (partly.length) node.append(el('p', 'vp-event-cover', `On this day ${partly.join(', ')}.`));
      return node;
    }

    function card(event, number) {
      const where = eventPlace(event);
      const chosen = number === state.selectedEvent;
      const node = el('li', where === 'inside' ? 'vp-event-card' : 'vp-event-card vp-outside');
      node.dataset.vpEvent = String(number);
      // The number and the date are the card's button: choosing a card selects its event in every view.
      const pick = el('button', 'vp-control vp-event-pick');
      pick.type = 'button';
      pick.dataset.vpAction = 'pick';
      pick.dataset.vpEvent = String(number);
      pick.setAttribute('aria-pressed', String(chosen));
      pick.setAttribute('aria-label', `Event ${number + 1}, ${formatDay(event.date)}`);
      pick.append(el('span', 'vp-event-number', String(number + 1)), el('span', 'vp-event-date', formatDay(event.date)));
      const top = el('div', 'vp-event-top');
      top.append(pick);
      if (where !== 'inside') top.append(el('span', 'vp-flag', 'Outside the conversation'));
      const label = el('p', 'vp-event-label', event.label);
      label.dir = 'auto';
      const body = el('div', 'vp-event-body');
      body.append(top, label);
      const actions = el('div', 'vp-event-actions');
      if (where === 'inside') {
        body.append(dayActivity(event.date));
        const open = textButton('vp-link', 'open-day', 'Open this day');
        open.dataset.vpEvent = String(number);
        actions.append(open);
      }
      if (where !== 'far') {
        const chart = textButton('vp-link', 'show-chart', 'Show in Activity');
        chart.dataset.vpEvent = String(number);
        actions.append(chart);
      }
      if (actions.childElementCount) body.append(actions);
      node.append(body);
      if (chosen) {
        node.classList.add('vp-selected');
        node.setAttribute('aria-current', 'true');
      }
      return node;
    }

    function render() {
      const lines = state.eventWarnings;
      warnings.hidden = !lines.length;
      warnings.replaceChildren(...lines.slice(0, WARNING_LINES).map((line) => el('li', 'vp-warning', line)));
      if (lines.length > WARNING_LINES) warnings.append(el('li', 'vp-warning-more', `And ${plural(lines.length - WARNING_LINES, 'more line')} of that kind.`));
      // A file may hold thousands of dated lines: the cards come a page at a time.
      const count = Math.min(state.events.length, state.eventsShown);
      const held = heldControl(list);
      list.replaceChildren(...state.events.slice(0, count).map(card));
      if (held) held(list);
      list.hidden = !count;
      empty.hidden = count > 0;
      further.hidden = count >= state.events.length;
      further.lastChild.textContent = `Show ${plural(Math.min(EVENT_PAGE, state.events.length - count), 'more event')}`;
      state.stale.events = false;
    }

    /** Bring the card of one event to the top and put the keyboard on it. */
    function reveal(number) {
      if (!Number.isInteger(number) || number < 0 || number >= state.events.length) return;
      if (number >= state.eventsShown) {
        state.eventsShown = Math.ceil((number + 1) / EVENT_PAGE) * EVENT_PAGE;
        render();
      }
      const node = list.children[number];
      if (!node) return;
      box.scrollTop += node.getBoundingClientRect().top - box.getBoundingClientRect().top - 12;
      node.querySelector('.vp-event-pick').focus({ preventScroll: true });
    }

    function more() {
      const first = state.eventsShown;
      state.eventsShown += EVENT_PAGE;
      render();
      if (list.children[first]) list.children[first].querySelector('.vp-event-pick').focus();
    }

    listen(file, 'change', async () => {
      const [chosen] = file.files;
      if (!chosen) return;
      let parsed = { events: [], warnings: ['This file is too large to be an events file.'] };
      if (chosen.size <= EVENTS_FILE_LIMIT) {
        try { parsed = parseEvents(await chosen.text()); } catch { parsed = { events: [], warnings: ['This file could not be read.'] }; }
      }
      if (state.destroyed) return;
      file.value = '';
      state.eventWarnings = parsed.warnings;
      // A file without a single dated line is the wrong file, not a wish to clear the events.
      if (!parsed.events.length && parsed.warnings.length) {
        note.textContent = 'No dated lines were found; the events were left as they were.';
        render();
        return;
      }
      setEvents(parsed.events);
      note.textContent = `Read ${plural(state.events.length, 'event')} from ${chosen.name}.`;
      if (onEvents) onEvents(state.events.map((event) => ({ ...event })));
    });

    return { enter() { if (state.stale.events) render(); }, leave() {}, render, reveal, more, pick: () => file.click() };
  })();

  const views = { conversation, voice: voices, activity, events: eventsView };

  function showTab(name) {
    if (!tabs[name] || state.destroyed) return;
    const moved = name !== state.tab;
    if (moved) views[state.tab].leave();
    state.tab = name;
    for (const [key] of TABS) {
      tabs[key].setAttribute('aria-selected', String(key === name));
      tabs[key].tabIndex = key === name ? 0 : -1;
      panels[key].hidden = key !== name;
    }
    if (moved) views[name].enter();
  }

  /**
   * The selected event is one index into state.events, shared by the three views that show events: the banners of the
   * conversation, the cards of the timeline, and the markers of the charts with the strip pinned under them.
   */
  function selectEvent(number) {
    state.selectedEvent = Number.isInteger(number) && number >= 0 && number < state.events.length ? number : -1;
    for (const node of root.querySelectorAll('.vp-banner, .vp-event-card')) {
      const selected = Number(node.dataset.vpEvent) === state.selectedEvent;
      node.classList.toggle('vp-selected', selected);
      if (selected) node.setAttribute('aria-current', 'true'); else node.removeAttribute('aria-current');
      const pick = node.querySelector('.vp-event-pick');
      if (pick) pick.setAttribute('aria-pressed', String(selected));
    }
    activity.select();
  }

  /** Show a day in Conversation, dropping filters that hide it, and put the keyboard on its first message. */
  function openDay(day) {
    showTab('conversation');
    const row = conversation.jump(day, true);
    if (row) row.focus({ preventScroll: true });
  }

  function showMessage(index) {
    if (state.destroyed || !Number.isInteger(index) || index < 0 || index >= messages.length) return false;
    showTab('conversation');
    return Boolean(conversation.show(index, true));
  }

  /**
   * Mark the derived views stale and redraw the one that is showing. The voice list is put in order again when `lists`
   * is set or nobody is looking at it; under a reader only its header is redrawn.
   */
  function invalidate(lists) {
    sums = null;
    cover = null;
    sets = {};
    state.stale.activity = true;
    state.stale.events = true;
    state.stale.voice = lists || state.tab !== 'voice' ? 'list' : state.stale.voice || 'head';
    if (state.tab !== 'conversation') views[state.tab].enter();
  }

  function setEvents(events) {
    if (state.destroyed) return;
    const selected = state.events[state.selectedEvent];
    state.events = normaliseEvents(events);
    state.selectedEvent = selected ? state.events.findIndex((event) => event.date === selected.date && event.label === selected.label) : -1;
    state.eventsShown = EVENT_PAGE;
    counts.events.textContent = state.events.length ? formatNumber(state.events.length) : '';
    conversation.setEvents();
    state.stale.activity = true;
    state.stale.events = true;
    if (state.tab === 'activity' || state.tab === 'events') views[state.tab].enter();
  }

  function update(next, changed) {
    if (state.destroyed) return;
    if (!next || !Array.isArray(next.messages) || !Array.isArray(next.participants) || next.messages.length !== messages.length) {
      throw new RangeError('update() takes the conversation the viewer was mounted with; destroy it and mount again for another chat.');
    }
    model = next;
    if (Array.isArray(changed)) {
      messages = model.messages;
      const indices = changed.filter((index) => Number.isInteger(index) && index >= 0 && index < messages.length);
      conversation.patch(indices);
      voices.patch(indices);
      invalidate(false);
      return;
    }
    conversation.leave();
    derive();
    const people = model.participants.length;
    if (state.filter.sender >= people) state.filter.sender = -1;
    if (state.voice.sender >= people) state.voice.sender = -1;
    if (state.viewAs >= people) state.viewAs = 0;
    renderHead();
    conversation.renderControls();
    voices.renderControls();
    conversation.rebuild();
    invalidate(true);
    player.sync();
  }

  function destroy() {
    if (state.destroyed) return;
    state.destroyed = true;
    controller.abort();
    for (const id of timers) clearTimeout(id);
    timers.clear();
    player.destroy();
    conversation.destroy();
    voices.destroy();
    activity.destroy();
    root.replaceChildren();
    if (!host.viewer) root.classList.remove('voxpad-viewer');
    if (host.theme == null) root.removeAttribute('data-vp-theme'); else root.setAttribute('data-vp-theme', host.theme);
  }

  listen(tablist, 'keydown', (event) => {
    const names = TABS.map(([name]) => name);
    const at = names.indexOf(event.target.dataset.vpTab);
    if (at < 0) return;
    let to = -1;
    if (event.key === 'ArrowRight') to = (at + 1) % names.length;
    else if (event.key === 'ArrowLeft') to = (at + names.length - 1) % names.length;
    else if (event.key === 'Home') to = 0;
    else if (event.key === 'End') to = names.length - 1;
    if (to < 0) return;
    event.preventDefault();
    showTab(names[to]);
    tabs[names[to]].focus();
  });

  // Rows come and go with the window, so their controls are handled here instead of holding listeners of their own.
  listen(root, 'click', (event) => {
    const tab = event.target.closest('.vp-tab');
    if (tab) {
      showTab(tab.dataset.vpTab);
      return;
    }
    const control = event.target.closest('[data-vp-action]');
    if (!control || control.disabled) return;
    const index = Number(control.dataset.vpIndex);
    const number = Number(control.dataset.vpEvent);
    switch (control.dataset.vpAction) {
      case 'play': player.toggle(index); break;
      case 'stop': player.close(); break;
      case 'show': showMessage(index); break;
      case 'expand': conversation.toggleExpanded(index); break;
      case 'excerpt': voices.toggleExpanded(index); break;
      case 'match-previous': conversation.step(-1); break;
      case 'match-next': conversation.step(1); break;
      case 'filters': conversation.toggleFilters(); break;
      case 'load-events': eventsView.pick(); break;
      case 'more-events': eventsView.more(); break;
      case 'event': case 'show-event': selectEvent(number); showTab('events'); eventsView.reveal(number); break;
      case 'pick': selectEvent(number === state.selectedEvent ? -1 : number); break;
      case 'open-day': selectEvent(number); if (state.events[number]) openDay(state.events[number].date); break;
      case 'show-chart': selectEvent(number); showTab('activity'); activity.reveal(number); break;
      case 'marker': activity.pinAt(Number(control.dataset.vpMarker)); break;
      case 'unpin': activity.unpin(); break;
      case 'legend': activity.toggle(control.dataset.vpSeries); break;
      default: break;
    }
  });
  // Dragging the bar of the recording that is playing scrubs it; on another bubble, letting go starts it there.
  listen(root, 'input', (event) => {
    if (!event.target.classList.contains('vp-seek')) return;
    const index = Number(event.target.dataset.vpIndex);
    if (index === state.playing) player.seekTo(index, Number(event.target.value));
  });
  listen(root, 'change', (event) => {
    if (!event.target.classList.contains('vp-seek')) return;
    const index = Number(event.target.dataset.vpIndex);
    if (index !== state.playing) player.seekTo(index, Number(event.target.value));
  });

  derive();
  root.replaceChildren(head, body, player.strip);
  renderHead();
  conversation.renderControls();
  voices.renderControls();
  showTab('conversation');
  conversation.rebuild();

  return { update, setEvents, showMessage, destroy };
}

export { mountViewer, aggregate, totals, parseEvents, countWords };
