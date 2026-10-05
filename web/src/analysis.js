/**
 * Read a chat as a conversation model: the twin of voxpad/analysis.py, rule for rule. Both must reach
 * the same model for tests/fixtures/analysis_cases.json, so nothing here is left to \s, \d, \w,
 * Number(), the default order of strings or a local-time Date.
 */

import { aggregate, countWords, parseEvents, totals } from "../../voxpad/viewer/viewer.js";
import { AUDIO_EXTENSIONS, CLOCK, DATE, PERIOD, SPACE, parseChat } from "./chat.js";

// Counting has one implementation, the viewer's.
export { aggregate, countWords, parseEvents, totals };

export const DATE_ORDERS = ["DMY", "MDY", "YMD"];
// The zero of every set of decimal digits a timestamp may be written in. A digit is worth its distance from its zero.
export const DIGIT_ZEROS = [
  0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66, 0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090,
  0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90, 0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10,
];
// What separates words, listed by code point so that no invisible character has to survive an editor.
export const WHITESPACE = String.fromCodePoint(
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
  0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff,
);
const WORD = new RegExp(`[^${WHITESPACE}]+`, "g");

// What WhatsApp writes in place of a message, lower-cased. tests/fixtures/analysis_cases.json holds the same lists for the command.
export const PHRASES = {
  deleted: [
    "this message was deleted", "you deleted this message", "se eliminó este mensaje", "eliminaste este mensaje",
    "diese nachricht wurde gelöscht", "du hast diese nachricht gelöscht", "mensagem apagada", "esta mensagem foi apagada",
    "você apagou esta mensagem", "apagou esta mensagem", "ce message a été supprimé", "vous avez supprimé ce message",
    "questo messaggio è stato eliminato", "hai eliminato questo messaggio",
  ],
  edited: [
    "this message was edited", "se editó este mensaje", "diese nachricht wurde bearbeitet", "mensagem editada",
    "ce message a été modifié", "questo messaggio è stato modificato",
  ],
  known_omitted: ["<media omitted>", "<multimedia omitido>"],
  omitted_nouns: [
    "image", "imagen", "immagine", "bild", "audio", "áudio", "video", "vídeo", "vidéo", "sticker", "gif", "document", "documento",
    "dokument", "contact", "tarjeta",
  ],
  omitted_suffixes: ["omitted", "omitido", "omitida", "weggelassen", "omis", "omise", "omesso", "omessa"],
};
const AUDIO_NOUNS = ["audio", "áudio"];

const TRANSCRIPT_PREFIX = "[Voice message transcript: ";
const ATTACHMENT_EXTENSIONS = new Set([
  ...[...AUDIO_EXTENSIONS].map((extension) => extension.slice(1)),
  ..."jpg jpeg png heic gif webp mp4 mov 3gp mkv avi vcf pdf doc docx xls xlsx ppt pptx txt csv zip rar apk".split(" "),
]);
const AUDIO_ENDINGS = [...AUDIO_EXTENSIONS].map((extension) => extension.toUpperCase());
// Names WhatsApp gives its files: IMG-20240304-WA0001.jpg on Android, 00000012-AUDIO-2024-03-04-10-00-00.opus on iOS.
const GENERATED = /^(?:[A-Z]{3}-[0-9]{8}-WA[0-9]{4}\.[0-9A-Za-z]{1,5}|[0-9]{8}-[A-Z]+-[0-9]{4}(?:-[0-9]{2}){5}\.[0-9A-Za-z]{1,5})$/;
const ANDROID_ATTACHMENT = /^([^\n]+) \(([^()\n]{1,40})\)$/u;
const IOS_ATTACHMENT = /<([^<>:：\n]*)[:：]([^<>\n]*)>/gu;
// No digit may stand before the date either; filenameDate looks behind by hand, which Safari 16.0 cannot do in a pattern.
const NAME_DATE = /(20(?:09|[1-9][0-9]))(-?)(0[1-9]|1[0-2])\2(0[1-9]|[12][0-9]|3[01])(?![0-9])/g;
// A letter, a digit or "_" of any script, "." or "-": what Python writes [\w.-].
const JOINED = String.raw`[\p{L}\p{N}_.-]`;
const JOINED_RUN = new RegExp(`${JOINED}+`, "gu");
const JOINED_NAME = new RegExp(`^${JOINED}+$`, "u");

// One of the header's patterns with a group around each of its numbers.
const grouped = (pattern) => pattern.replace(/\\p\{Nd\}\{[0-9,]+\}/g, "($&)");
// The header's own date and clock, so that whatever parseChat took for a timestamp is read field by
// field and nothing is split by hand. Groups: the three date fields, a marker, hour, minute, second, a marker.
const STAMP = new RegExp(`^${grouped(DATE)}[,،]?${SPACE}*(?:(${PERIOD})${SPACE}*)?${grouped(CLOCK)}(?:${SPACE}*(${PERIOD}))?$`, "u");
const PM_MARKERS = ["م", "下午", "午後"];
const MONTH_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const nfc = (text) => text.normalize("NFC");
const pad = (value, width = 2) => String(value).padStart(width, "0");
const escapeRegex = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// Lengths are counted in characters, as Python counts them, not in UTF-16 units.
const size = (text) => Array.from(text).length;
const counted = (count, one, many) => (count === 1 ? one : many.replace("{}", count));

function strip(text, { left = true, right = true } = {}) {
  let start = 0;
  let end = text.length;
  while (left && start < end && WHITESPACE.includes(text[start])) start += 1;
  while (right && end > start && WHITESPACE.includes(text[end - 1])) end -= 1;
  return text.slice(start, end);
}

/** Order strings by code point, as Python does: the default order compares UTF-16 units and puts an emoji before a fullwidth letter. */
function byCodePoint(left, right) {
  const [first, second] = [Array.from(left), Array.from(right)];
  for (let at = 0; at < first.length && at < second.length; at += 1) {
    const difference = first[at].codePointAt(0) - second[at].codePointAt(0);
    if (difference) return difference;
  }
  return first.length - second.length;
}

/** The value of a run of digits of any set, or null when one of them is in no set. */
function number(digits) {
  let value = 0;
  for (const character of digits) {
    const code = character.codePointAt(0);
    const zero = DIGIT_ZEROS.find((candidate) => candidate <= code && code <= candidate + 9);
    if (zero === undefined) return null;
    value = value * 10 + code - zero;
  }
  return value;
}

function monthLength(year, month) {
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  return month === 2 && leap ? 29 : MONTH_DAYS[month - 1];
}

function realDate(year, month, day) {
  if (year === null || month === null || day === null) return null;
  if (!(year >= 1 && year <= 9999 && month >= 1 && month <= 12 && day >= 1 && day <= monthLength(year, month))) return null;
  return [year, month, day];
}

/** Days since the year 1 began, counted with whole numbers and without building a date. */
function ordinal(year, month, day) {
  const before = year - 1;
  let days = before * 365 + Math.floor(before / 4) - Math.floor(before / 100) + Math.floor(before / 400);
  for (let earlier = 1; earlier < month; earlier += 1) days += monthLength(year, earlier);
  return days + day;
}

const isoDay = ([year, month, day]) => `${pad(year, 4)}-${pad(month)}-${pad(day)}`;

/** The date a timestamp gives under an order. The number of digits decides which fields can be a year. */
function stampDate(match, order, forced = false) {
  const [first, second, third] = [match[1], match[2], match[3]];
  if (order === "YMD") {
    // A short year comes first only when the user says so: on its own such a date reads day-first.
    if (size(first) !== 4 && !(forced && size(first) <= 2)) return null;
    let year = number(first);
    if (year !== null && size(first) <= 2) year += 2000;
    return realDate(year, number(second), number(third));
  }
  if (size(first) > 2 || (size(third) !== 2 && size(third) !== 4)) return null;
  let year = number(third);
  if (year !== null && size(third) === 2) year += 2000;
  return order === "DMY" ? realDate(year, number(second), number(first)) : realDate(year, number(first), number(second));
}

function stampClock(match) {
  let hour = number(match[5]);
  const minute = number(match[6]);
  const second = match[7] ? number(match[7]) : 0;
  if (hour === null || minute === null || second === null) return null;
  // Of a marker on each side of the clock, the one after it counts.
  const marker = match[8] || match[4];
  if (marker) hour = hour % 12 + (marker[0] === "p" || marker[0] === "P" || PM_MARKERS.includes(marker) ? 12 : 0);
  if (hour > 23 || minute > 59 || second > 59) return null;
  return [hour, minute, second];
}

function checkOrder(order) {
  if (!DATE_ORDERS.includes(order)) throw new RangeError(`Unknown date order: ${order}`);
}

/**
 * Turn an exported timestamp into YYYY-MM-DDTHH:MM:SS, or null when it is no real date and time.
 * `order` is DMY, MDY or YMD. `forced` says the user chose it, which lets YMD read a one- or
 * two-digit first field as a year of this century.
 */
export function parseTimestamp(raw, order, forced = false) {
  checkOrder(order);
  const match = typeof raw === "string" ? STAMP.exec(raw) : null;
  if (!match) return null;
  const day = stampDate(match, order, forced);
  const clock = stampClock(match);
  if (!day || !clock) return null;
  return `${isoDay(day)}T${pad(clock[0])}:${pad(clock[1])}:${pad(clock[2])}`;
}

/**
 * Decide once for a chat how its dates are written: { order, ambiguous }.
 * `hints[i]` is the date (YYYY-MM-DD) named by an attachment of message i, or null. The order that
 * leaves the fewest timestamps without a date wins; then the one more hints agree with, the one in
 * which fewer messages go back in time, day-first for dotted and dashed dates, and the shorter chat.
 */
export function inferDateOrder(timestamps, hints = null) {
  if (!timestamps.length) return { order: "DMY", ambiguous: false };
  const matches = timestamps.map((raw) => (typeof raw === "string" ? STAMP.exec(raw) : null));
  // Month-first dates are written with slashes. The separators stand right after the first two fields.
  const dotted = matches.some((match, index) => match
    && [match[1].length, match[1].length + 1 + match[2].length].some((at) => ".-".includes(timestamps[index][at])));
  const keys = {};
  const readings = {};
  DATE_ORDERS.forEach((order, rank) => {
    const days = matches.map((match) => (match ? stampDate(match, order) : null));
    let [invalid, agree, back] = [0, 0, 0];
    let [previous, earliest, latest] = [null, null, null];
    days.forEach((day, index) => {
      if (!day) {
        invalid += 1;
        return;
      }
      if (hints && hints[index] != null && hints[index] === isoDay(day)) agree += 1;
      const count = ordinal(...day);
      if (previous === null) {
        earliest = count;
        latest = count;
      } else {
        if (count < previous) back += 1;
        earliest = Math.min(earliest, count);
        latest = Math.max(latest, count);
      }
      previous = count;
    });
    keys[order] = [invalid, -agree, back, order === "MDY" && dotted ? 1 : 0, previous === null ? 0 : latest - earliest, rank];
    readings[order] = days.map((day) => (day ? isoDay(day) : ""));
  });
  const smaller = (left, right) => {
    const at = left.findIndex((value, index) => value !== right[index]);
    return at >= 0 && left[at] < right[at];
  };
  const order = DATE_ORDERS.reduce((best, candidate) => (smaller(keys[candidate], keys[best]) ? candidate : best));
  const ambiguous = DATE_ORDERS.some((other) => other !== order && keys[other].slice(0, 4).every((value, at) => value === keys[order][at])
    && readings[other].some((day, index) => day !== readings[order][index]));
  return { order, ambiguous };
}

/** Whether a name is one WhatsApp gives its files, or ends in an extension attachments have. */
export function isAttachmentName(name) {
  if (GENERATED.test(name)) return true;
  const dot = name.lastIndexOf(".");
  return dot >= 0 && ATTACHMENT_EXTENSIONS.has(name.slice(dot + 1).toLowerCase());
}

/** What an attachment is, from its name: audio, gif, sticker, image, video, contact or document. */
export function mediaType(name) {
  const upper = name.toUpperCase();
  const ends = (...endings) => endings.some((ending) => upper.endsWith(ending));
  const starts = (...beginnings) => beginnings.some((beginning) => upper.startsWith(beginning));
  if (ends(...AUDIO_ENDINGS) || starts("PTT-", "AUD-") || upper.includes("-AUDIO-")) return "audio";
  if (upper.includes("GIF")) return "gif";
  if (ends(".WEBP") || starts("STK-") || upper.includes("-STICKER-")) return "sticker";
  if (ends(".JPG", ".JPEG", ".PNG", ".HEIC") || starts("IMG-") || upper.includes("-PHOTO-")) return "image";
  if (ends(".MP4", ".MOV", ".3GP", ".MKV", ".AVI") || starts("VID-") || upper.includes("-VIDEO-")) return "video";
  return ends(".VCF") ? "contact" : "document";
}

/** The first date an attachment name carries, as YYYY-MM-DD: 20240304 or 2024-03-04, from 2009 to 2099, with no digit beside it. */
export function filenameDate(name) {
  NAME_DATE.lastIndex = 0;
  for (let match = NAME_DATE.exec(name); match; match = NAME_DATE.exec(name)) {
    if (match.index > 0 && /[0-9]/.test(name[match.index - 1])) {
      NAME_DATE.lastIndex = match.index + 1;
    } else if (number(match[4]) <= monthLength(number(match[1]), number(match[3]))) {
      return `${match[1]}-${match[3]}-${match[4]}`;
    }
  }
  return null;
}

/**
 * Take the transcript lines VoxPad inserted out of a message: [the rest, the transcripts].
 * They are lines of their own below the message, so one quoted on the message's first line is typed text.
 */
function splitTranscripts(text) {
  const [first, ...lines] = text.split("\n");
  const kept = [first];
  const transcripts = [];
  let current = null;
  for (const line of lines) {
    if (current) current.push(line);
    else if (line.startsWith(TRANSCRIPT_PREFIX)) current = [line];
    else kept.push(line);
    // A transcript with line breaks in it runs on to the line that closes the bracket.
    if (current && line.endsWith("]")) {
      transcripts.push(current.join("\n").slice(TRANSCRIPT_PREFIX.length, -1));
      current = null;
    }
  }
  // Never closed, so it was typed by a person.
  if (current) kept.push(...current);
  return [kept.join("\n"), transcripts];
}

/** Take a closing <This message was edited> out of a text: [the rest, whether it was there]. */
function splitEdited(text) {
  const start = text.lastIndexOf("<");
  if (start < 0 || !text.endsWith(">")) return [text, false];
  const phrase = text.slice(start + 1, -1).toLowerCase();
  if (!PHRASES.edited.includes(phrase) && !(phrase.endsWith(".") && PHRASES.edited.includes(phrase.slice(0, -1)))) return [text, false];
  return [text.slice(0, start), true];
}

const usable = (results) => [...(results || [])].filter((result) => isObject(result) && typeof result.file === "string");
const basename = (file) => nfc(file).split("/").pop();

/** Prepare the search for the recordings the results name: the names that are one run of joined characters, and a pattern for the rest. */
function recordingNames(results) {
  const names = new Set(results.map((result) => basename(result.file)));
  names.delete("");
  const runs = new Set([...names].filter((name) => JOINED_NAME.test(name)));
  // Longest first, as indexMessages looks for them: a name may be the start of another.
  const others = [...names].filter((name) => !runs.has(name)).sort((left, right) => right.length - left.length || byCodePoint(left, right));
  const pattern = others.length ? new RegExp(`(?:${others.map(escapeRegex).join("|")})(?!${JOINED})`, "gu") : null;
  return { runs, pattern };
}

/**
 * The character that ends at `index`, which is two UTF-16 units outside the first 65 536. It is taken
 * out by hand: a pattern anchored at the end only, such as /[\p{L}]$/u, misses such a character in
 * Chrome 153 and Firefox 155.
 */
function characterBefore(text, index) {
  const pair = text.slice(Math.max(0, index - 2), index);
  return pair.codePointAt(0) > 0xffff ? pair : pair.slice(-1);
}

/** Known recordings named in a text as whole tokens: not next to a letter, a digit, "_", "." or "-". */
function recordings(text, { runs, pattern }) {
  const found = [];
  // Nearly every name is one run of such characters, and then a run of the text either is the name or is not.
  if (runs.size) {
    for (const match of text.matchAll(JOINED_RUN)) {
      if (runs.has(match[0])) found.push([match.index, match.index + match[0].length, match[0]]);
    }
  }
  if (!pattern) return found;
  pattern.lastIndex = 0;
  for (let match = pattern.exec(text); match; match = pattern.exec(text)) {
    if (JOINED_NAME.test(characterBefore(text, match.index))) {
      // Joined to what stands before it: look again from the next character.
      pattern.lastIndex = match.index + (text.codePointAt(match.index) > 0xffff ? 2 : 1);
    } else {
      found.push([match.index, match.index + match[0].length, match[0]]);
    }
  }
  found.sort((left, right) => left[0] - right[0] || right[1] - left[1]);
  const kept = [];
  for (const token of found) {
    if (!kept.length || token[0] >= kept[kept.length - 1][1]) kept.push(token);
  }
  return kept;
}

/** Attachment tokens of a text as [start, end, filename], in the order of the text. */
function attachments(text, names) {
  const found = [];
  const first = text.split("\n", 1)[0];
  const android = ANDROID_ATTACHMENT.exec(first);
  if (android && isAttachmentName(android[1])) found.push([0, first.length, android[1]]);
  const candidates = [];
  for (const match of text.matchAll(IOS_ATTACHMENT)) {
    const label = strip(match[1], { left: false });
    const name = strip(match[2], { right: false });
    if (label && size(label) <= 40 && isAttachmentName(name)) candidates.push([match.index, match.index + match[0].length, name]);
  }
  // A name inside an attachment line or bracket is that attachment, not another one.
  for (const token of [...candidates, ...recordings(text, names)]) {
    if (found.every((other) => token[1] <= other[0] || token[0] >= other[1])) found.push(token);
  }
  return found.sort((left, right) => left[0] - right[0]);
}

/** What a message is once transcripts and the edited mark are out: kind, typed text, media, recording and date hint. */
function classify(text, whole, names, repeated) {
  const low = whole.toLowerCase();
  if (PHRASES.deleted.includes(low) || (low.endsWith(".") && PHRASES.deleted.includes(low.slice(0, -1)))) return { kind: "deleted", text: "" };
  const tokens = attachments(text, names);
  if (tokens.length) {
    const files = tokens.map((token) => nfc(token[2]));
    const audio = files.map((name) => mediaType(name) === "audio");
    const voice = audio.indexOf(true);
    // A message holds one voice note; the names of further recordings stay in its text.
    let rest = "";
    let position = 0;
    tokens.forEach(([start, end], index) => {
      if (index !== voice && audio[index]) return;
      rest += text.slice(position, start);
      position = end;
    });
    const reading = { kind: "media", text: strip(rest + text.slice(position)), hint: files.map(filenameDate).find(Boolean) ?? null };
    if (voice < 0) return { ...reading, media: { type: mediaType(files[0]), file: files[0] } };
    return { ...reading, kind: "voice", file: files[voice] };
  }
  const words = whole.match(WORD) || [];
  if (words.length > 4) return { kind: "text", text: whole };
  // What an export without its media leaves of an attachment: <Media omitted>, image omitted, null.
  const [noun, suffix] = [words[0], words[words.length - 1]].map((word) => word?.toLowerCase());
  const inside = whole.slice(1, -1);
  const bracketed = whole.length > 2 && whole.startsWith("<") && whole.endsWith(">") && !inside.includes("<") && !inside.includes(">")
    && (PHRASES.known_omitted.includes(low) || repeated.get(whole) > 1);
  const bare = (words.length === 2 || words.length === 3) && PHRASES.omitted_nouns.includes(noun) && PHRASES.omitted_suffixes.includes(suffix);
  if (bare && AUDIO_NOUNS.includes(noun)) return { kind: "voice", text: "", file: null };
  if (bracketed || bare || whole === "null") return { kind: "media", text: "", media: { type: "omitted", file: null } };
  return { kind: "text", text: whole };
}

/** Read every message that has a sender; null stands for a system message. */
function read(messages, results) {
  const names = recordingNames(results);
  const repeated = new Map();
  const drafts = messages.map((message) => {
    if (message.sender === null) return null;
    const [rest, transcripts] = splitTranscripts(message.text);
    const [text, edited] = splitEdited(rest);
    const whole = strip(text);
    // A bracketed placeholder in a language not listed here still repeats word for word.
    repeated.set(whole, (repeated.get(whole) || 0) + 1);
    return { text, whole, transcripts, edited };
  });
  return drafts.map((draft) => draft
    && { ...classify(draft.text, draft.whole, names, repeated), transcripts: draft.transcripts, edited: draft.edited });
}

/** For each message of parseChat, the date its first dated attachment name carries (YYYY-MM-DD), or null. */
export function dateHints(messages, results = null) {
  return read(messages, usable(results)).map((reading) => reading?.hint ?? null);
}

const seconds = (value) => (typeof value === "number" && Number.isFinite(value) ? value : null);

/** One result per file, the last that worked or else the last that failed, listed under the name of the file. */
function latestResults(results) {
  const latest = new Map();
  for (const result of results) {
    if (result.status !== "ok" && result.status !== "error") continue;
    const file = nfc(result.file);
    if (!latest.has(file) || result.status === "ok" || latest.get(file).status === "error") latest.set(file, result);
  }
  const byName = new Map();
  for (const result of latest.values()) {
    const name = basename(result.file);
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push(result);
  }
  return byName;
}

/** What a transcript line of an annotated chat says, in the shape of a result. */
function transcriptResult(line) {
  if (line === "No speech detected") return { status: "ok", text: "" };
  if (line.startsWith("Error: ")) return { status: "error", text: "", error: line.slice("Error: ".length) };
  return { status: "ok", text: line };
}

/** What a finished result says about a recording: the fields of a voice item that a transcript decides. */
function heard(result) {
  const text = typeof result.text === "string" ? strip(result.text) : "";
  const voice = {
    seconds: seconds(result.duration_seconds), status: result.status === "error" ? "error" : text ? "ok" : "empty", text, words: countWords(text),
  };
  const languages = Array.isArray(result.languages) ? result.languages.filter((language) => typeof language === "string") : [];
  if (languages.length) voice.languages = languages;
  if (voice.status === "error") voice.error = typeof result.error === "string" ? result.error : "";
  return voice;
}

const contexts = (result) => (Array.isArray(result.messages) ? result.messages.filter(isObject) : []);

/** The voice item of a message that names a recording, and whether its transcript was found by file name alone. */
function voiceItem(name, message, index, transcripts, byName, durations, audioSrc) {
  let candidates = byName.get(name) || [];
  if (candidates.length > 1) {
    // The same name in two folders of an export: the report says which message each belongs to.
    candidates = candidates.filter((result) => contexts(result)
      .some((context) => context.timestamp === message.timestamp && context.sender === message.sender));
    // Reports of one export laid out differently, a ZIP and its folder, both name the recording:
    // a transcript is preferred to a failure, then the later report.
    const transcribed = candidates.filter((result) => result.status === "ok");
    candidates = (transcribed.length ? transcribed : candidates).slice(-1);
  }
  let result = candidates.length === 1 ? candidates[0] : null;
  const line = transcripts.length ? transcriptResult(transcripts[0]) : null;
  let byNameOnly = false;
  if (result && result.status === "error" && line && line.status === "ok") {
    // A failure in a report does not take away the transcript the chat itself carries.
    result = line;
  } else if (result) {
    byNameOnly = Array.isArray(result.messages) && result.messages.length > 0
      && !contexts(result).some((context) => context.timestamp === message.timestamp);
  } else if (line) {
    result = line;
  }
  const source = audioSrc ? audioSrc(name, index) : null;
  const voice = { file: name, src: typeof source === "string" && source ? source : null, seconds: null, status: "pending", text: "", words: 0 };
  if (result) Object.assign(voice, heard(result));
  if (voice.seconds === null) voice.seconds = seconds(durations.get(name));
  return [voice, byNameOnly];
}

/**
 * Build the conversation model of a chat; `messages[i]` describes `parseChat(text)[i]`.
 * `results` are the entries of transcript reports (each with its `file`), `events` a list as
 * parseEvents returns it, `dateOrder` DMY, MDY or YMD when the user chose it, `audioSrc(filename,
 * messageIndex)` gives what the viewer can play for a recording (or null), and `durations` maps
 * recording names to seconds, as an object or a Map, for recordings whose result has none.
 */
export function buildModel({ text, title = "", results = null, events = null, dateOrder = null, audioSrc = null, durations = null }) {
  if (dateOrder !== null) checkOrder(dateOrder);
  const messages = parseChat(text);
  const reported = usable(results);
  const readings = read(messages, reported);
  const { order, ambiguous } = dateOrder === null
    ? inferDateOrder(messages.map((message) => message.timestamp), readings.map((reading) => reading?.hint ?? null))
    : { order: dateOrder, ambiguous: false };
  const byName = latestResults(reported);
  const known = new Map();
  for (const [name, value] of durations instanceof Map ? durations : Object.entries(durations || {})) {
    if (typeof name === "string") known.set(nfc(name), value);
  }
  const sent = new Map();
  for (const message of messages) {
    if (message.sender !== null) sent.set(message.sender, (sent.get(message.sender) || 0) + 1);
  }
  const participants = [...sent.keys()].sort((left, right) => sent.get(right) - sent.get(left) || byCodePoint(left, right));
  const places = new Map(participants.map((name, place) => [name, place]));
  let [unused, byNameOnly] = [0, 0];
  const entries = messages.map((message, index) => {
    const entry = { time: parseTimestamp(message.timestamp, order, dateOrder !== null), sender: null, kind: "system", text: message.text, words: 0 };
    const reading = readings[index];
    if (!reading) return entry;
    Object.assign(entry, { sender: places.get(message.sender), kind: reading.kind, text: reading.text, words: countWords(reading.text) });
    if (reading.edited) entry.edited = true;
    let { transcripts } = reading;
    if (reading.kind === "media") {
      entry.media = reading.media;
    } else if (reading.kind === "voice" && reading.file === null) {
      entry.voice = { file: null, src: null, seconds: null, status: "missing", text: "", words: 0 };
    } else if (reading.kind === "voice") {
      const [voice, nameOnly] = voiceItem(reading.file, message, index, transcripts, byName, known, audioSrc);
      entry.voice = voice;
      if (nameOnly) byNameOnly += 1;
      // The first transcript line is this recording's, whether or not a report replaced it.
      transcripts = transcripts.slice(1);
    }
    unused += transcripts.length;
    return entry;
  });
  const warnings = [];
  if (unused) {
    warnings.push(counted(unused, "1 transcript line in the chat belongs to no voice message and was left out.",
      "{} transcript lines in the chat belong to no voice message and were left out."));
  }
  if (byNameOnly) {
    warnings.push(`${counted(byNameOnly, "1 transcript was matched", "{} transcripts were matched")}`
      + " by file name only; the report seems to come from a different export of this chat.");
  }
  return {
    schema: 1,
    title: title || participants.slice(0, 3).join(" · ") + (participants.length > 3 ? ` +${participants.length - 3}` : ""),
    date_order: order,
    date_order_ambiguous: ambiguous,
    participants,
    messages: entries,
    events: [...(events || [])].map((event) => ({ date: event.date, label: event.label })),
    warnings,
  };
}

/**
 * Bring the voice items of the messages at `messageIndices` up to date with one result, in place,
 * and return the indices whose item changed. A result that is neither ok nor error makes the item
 * pending again. When the result names its `file`, only items of that recording are touched. The
 * model's warnings and a duration the result does not give stay as they are.
 */
export function applyResult(model, messageIndices, result) {
  const name = typeof result?.file === "string" ? basename(result.file) : null;
  const finished = isObject(result) && (result.status === "ok" || result.status === "error");
  const changed = [];
  for (const index of messageIndices) {
    const voice = model.messages[index]?.voice;
    if (!voice || voice.file === null || (name !== null && voice.file !== name)) continue;
    const next = finished ? heard(result) : { seconds: null, status: "pending", text: "", words: 0 };
    const item = { file: voice.file, src: voice.src, ...next, seconds: next.seconds ?? voice.seconds };
    const before = JSON.stringify(voice);
    // The viewer may hold on to the item itself, so it is rewritten, not replaced.
    for (const key of Object.keys(voice)) delete voice[key];
    Object.assign(voice, item);
    if (JSON.stringify(voice) !== before) changed.push(index);
  }
  return changed;
}
