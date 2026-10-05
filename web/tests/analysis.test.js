import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import * as viewer from "../../voxpad/viewer/viewer.js";
import {
  DATE_ORDERS, DIGIT_ZEROS, PHRASES, WHITESPACE, aggregate, applyResult, buildModel, countWords, dateHints, filenameDate,
  inferDateOrder, isAttachmentName, mediaType, parseEvents, parseTimestamp, totals,
} from "../src/analysis.js";
import { parseChat } from "../src/chat.js";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");
// The cases the command is tested against too: written by hand from the rules, not by either program.
const fixture = JSON.parse(await read("../../tests/fixtures/analysis_cases.json"));
const source = await read("../src/analysis.js");

const BUCKETS = ["day", "week", "month"];
const ZONES = [["America/New_York", 300], ["Asia/Kolkata", -330]];
const character = (code) => String.fromCodePoint(code);
const hex = (code) => `U+${code.toString(16).toUpperCase().padStart(4, "0")}`;

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

/** Run here and in both zones: no rule may depend on where the machine stands. */
function everywhere(run) {
  run("here");
  for (const zone of ZONES) inZone(zone, () => run(zone[0]));
}

/** Compare aggregates or totals: voice_seconds within 1e-6, everything else exactly. */
function assertMetrics(actual, expected, label) {
  const strip = (value) => JSON.parse(JSON.stringify(value, (key, item) => (key === "voice_seconds" ? undefined : item)));
  assert.deepEqual(strip(actual), strip(expected), label);
  const seconds = (value) => (Array.isArray(value) ? value : value.series).map((row) => [row.voice_seconds].flat());
  const [got, wanted] = [seconds(actual), seconds(expected)];
  assert.equal(got.length, wanted.length, label);
  got.forEach((row, person) => {
    assert.equal(row.length, wanted[person].length, label);
    row.forEach((value, at) => assert.ok(Math.abs(value - wanted[person][at]) <= 1e-6, `${label}: voice_seconds of participant ${person} at ${at}`));
  });
}

function modelOf(item) {
  const events = item.events_text === null ? null : parseEvents(item.events_text).events;
  return buildModel({ text: item.chat, results: item.results, events, dateOrder: item.date_order, durations: item.durations });
}

const recording = (file, text = "hola", more = {}) => ({ file, messages: [], status: "ok", text, ...more });
const attached = (name, minute = 0) => `10/01/26, 08:0${minute} - Ana: ${name} (file attached)\n`;
const pending = { file: "voice.opus", src: null, seconds: null, status: "pending", text: "", words: 0 };

test("the fixture holds every section and names its cases once", () => {
  assert.deepEqual(Object.keys(fixture).sort(), ["cases", "date_orders", "events", "filename_dates", "phrases", "timestamps", "words"]);
  for (const section of ["timestamps", "date_orders", "filename_dates", "words", "events", "cases"]) assert.ok(fixture[section].length > 0, section);
  const names = fixture.cases.map((item) => item.name);
  assert.equal(new Set(names).size, names.length);
});

test("counting words, events, buckets and totals has one implementation, the viewer's", () => {
  assert.equal(aggregate, viewer.aggregate);
  assert.equal(totals, viewer.totals);
  assert.equal(parseEvents, viewer.parseEvents);
  assert.equal(countWords, viewer.countWords);
});

test("the phrase lists are the ones the command checks", () => {
  assert.deepEqual(PHRASES, fixture.phrases);
  // They are compared with lower-cased text, composed as WhatsApp writes it.
  for (const phrases of Object.values(PHRASES)) assert.deepEqual(phrases.map((phrase) => phrase.toLowerCase().normalize("NFC")), phrases);
});

test("timestamps become local times", () => everywhere((zone) => {
  for (const { raw, order, forced, time } of fixture.timestamps) {
    assert.equal(parseTimestamp(raw, order, forced), time, `${zone}: ${JSON.stringify(raw)} as ${order}${forced ? ", forced" : ""}`);
  }
}));

test("the date order is decided once for a chat", () => everywhere((zone) => {
  for (const { timestamps, hints, order, ambiguous } of fixture.date_orders) {
    assert.deepEqual(inferDateOrder(timestamps, hints), { order, ambiguous }, `${zone}: ${JSON.stringify(timestamps)} with ${JSON.stringify(hints)}`);
  }
  assert.deepEqual(inferDateOrder(["3/4/24, 9:10 PM"]), { order: "DMY", ambiguous: true });
  // Either separator of a date may be the dot or the dash that month-first dates are not written with.
  for (const stamps of [["1/2.24, 10:00", "1/3.24, 10:00"], ["1-2/24, 10:00", "1-3/24, 10:00"]]) {
    assert.deepEqual(inferDateOrder(stamps), { order: "DMY", ambiguous: false }, `${zone}: ${stamps[0]}`);
  }
  // Two messages of one day are no step back in time.
  assert.deepEqual(inferDateOrder(["5/5/24, 10:00", "13/6/24, 10:00", "5/5/24, 10:00", "6/13/24, 10:00"]), { order: "MDY", ambiguous: false });
}));

test("attachment names give their date", () => everywhere((zone) => {
  for (const { name, date } of fixture.filename_dates) assert.equal(filenameDate(name), date, `${zone}: ${name}`);
  // Reading one name leaves nothing behind for the next.
  assert.equal(filenameDate("20240230-backup-20240301.zip"), "2024-03-01");
  assert.equal(filenameDate("IMG-20240304-WA0001.jpg"), "2024-03-04");
}));

test("words are runs between the listed whitespace", () => everywhere((zone) => {
  for (const { text, count } of fixture.words) assert.equal(countWords(text), count, `${zone}: ${JSON.stringify(text)}`);
}));

test("events files are read line by line", () => everywhere((zone) => {
  for (const { text, events, warnings } of fixture.events) assert.deepEqual(parseEvents(text), { events, warnings }, `${zone}: ${JSON.stringify(text.slice(0, 40))}`);
}));

test("chats become the models written by hand", () => everywhere((zone) => {
  for (const item of fixture.cases) {
    const model = modelOf(item);
    assert.deepEqual(model, item.model, `${zone}: ${item.name}`);
    // The model describes the parsed chat message by message and is plain JSON.
    assert.equal(model.messages.length, parseChat(item.chat).length, item.name);
    assert.deepEqual(JSON.parse(JSON.stringify(model)), model, item.name);
  }
}));

test("models are counted per day, week and month", () => everywhere((zone) => {
  for (const item of fixture.cases) {
    const model = modelOf(item);
    for (const bucket of BUCKETS) assertMetrics(aggregate(model, bucket), item.aggregates[bucket], `${zone}: ${item.name}: ${bucket}`);
    assertMetrics(totals(model), item.totals, `${zone}: ${item.name}: totals`);
    assert.deepEqual(aggregate(model), aggregate(model, "day"));
  }
}));

test("the whitespace is exactly the listed code points", () => {
  const listed = [
    0x9, 0xa, 0xb, 0xc, 0xd, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
    0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff,
  ];
  assert.deepEqual(Array.from(WHITESPACE, (space) => space.codePointAt(0)), listed);
  // Words are split at these and nowhere else, and they are what is trimmed from a message and a transcript.
  const different = [];
  for (let code = 0; code < 0x110000; code += 1) {
    if (code >= 0xd800 && code <= 0xdfff) continue;
    if ((countWords(`a${character(code)}b`) === 2) !== listed.includes(code)) different.push(hex(code));
  }
  assert.deepEqual(different, []);
  for (const code of listed) {
    const space = character(code);
    const [voice] = buildModel({ text: attached("voice.opus"), results: [recording("voice.opus", `${space}tres${space}cuatro${space}`)] }).messages;
    assert.deepEqual([voice.voice.text, voice.voice.words], [`tres${space}cuatro`, 2], hex(code));
    // A line ends at CR and LF, and parseChat takes the byte order mark out of a chat.
    if ([0xa, 0xd, 0xfeff].includes(code)) continue;
    const [message] = buildModel({ text: `10/01/26, 08:00 - Ana: ${space}uno${space}dos${space}\n` }).messages;
    assert.deepEqual([message.text, message.words], [`uno${space}dos`, 2], hex(code));
  }
});

test("the timestamp pattern reads whatever the chat header accepts", () => {
  const cases = [
    ["10/4/26, 9:10 PM - José: Hola", "MDY", "2026-10-04T21:10:00"],
    ["2026-10-4, 9:10 p. m. - José: Hola", "YMD", "2026-10-04T21:10:00"],
    ["[٤/١٠/٢٠٢٦، ٩:١٠ م] José: Hola", "DMY", "2026-10-04T21:10:00"],
    ["[2026/10/4, 上午9:10:05] José: Hola", "YMD", "2026-10-04T09:10:05"],
    ["[2026/10/4, 9:10 午後] José: Hola", "YMD", "2026-10-04T21:10:00"],
    ["[04.10.2026, 09：10] José： Hola", "DMY", "2026-10-04T09:10:00"],
    ["[04.10.2026 09:10:11]José: Hola", "DMY", "2026-10-04T09:10:11"],
    // The whitespace Python's \s knows and JavaScript's does not stands in a header here as it does there.
    [`04/10/26,${character(0x1f)}09:10${character(0x85)}-${character(0x1c)}José: Hola`, "DMY", "2026-10-04T09:10:00"],
    [`[04/10/26, 9:10${character(0x1d)}p.${character(0x1e)}m.] José: Hola`, "DMY", "2026-10-04T21:10:00"],
  ];
  for (const [line, order, time] of cases) {
    const [message] = parseChat(line);
    assert.equal(message.sender, "José", line);
    assert.equal(parseTimestamp(message.timestamp, order), time, line);
  }
  // A byte order mark is no whitespace of a timestamp: parseChat takes it out of a header before anything reads it.
  assert.equal(parseTimestamp(`04/10/26,${character(0xfeff)}09:10`, "DMY"), null);
  assert.equal(parseTimestamp(null, "DMY"), null);
});

test("digits of every listed set have their value and no others are read", () => {
  assert.deepEqual(DIGIT_ZEROS, [
    0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66, 0xde6, 0xe50, 0xed0, 0xf20, 0x1040,
    0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90, 0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0,
    0xaa50, 0xabf0, 0xff10,
  ]);
  for (const zero of DIGIT_ZEROS) {
    const digits = Array.from({ length: 10 }, (unused, value) => character(zero + value));
    for (const digit of digits) assert.match(digit, /^\p{Nd}$/u, hex(zero));
    const [day, year] = [digits[2] + digits[9], digits[1] + digits[9] + digits[8] + digits[7]];
    const stamp = `${day}/${digits[0]}${digits[3]}/${year}, ${digits[1]}${digits[4]}:${digits[5]}${digits[6]}`;
    assert.equal(parseTimestamp(stamp, "DMY"), "1987-03-29T14:56:00", hex(zero));
    assert.deepEqual(parseChat(`${stamp} - Ana: x`).map((message) => message.timestamp), [stamp], hex(zero));
  }
  // A digit the header accepts but the table does not hold: no value is guessed.
  const bold = character(0x1d7d0) + character(0x1d7d7);
  assert.equal(parseChat(`${bold}/03/1987, 14:56 - Ana: x`).length, 1);
  assert.equal(parseTimestamp(`${bold}/03/1987, 14:56`, "DMY"), null);
  assert.deepEqual(inferDateOrder([`${bold}/03/1987, 14:56`, "04/03/1987, 14:56"]), { order: "DMY", ambiguous: true });
});

test("dates are real days of the calendar", () => {
  // The platform's calendar is asked through UTC only, and only here.
  const exists = (year, month, day) => {
    const date = new Date(Date.UTC(2000, 0, 1));
    date.setUTCFullYear(year, month - 1, day);
    return day >= 1 && date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day;
  };
  const pad = (value, width = 2) => String(value).padStart(width, "0");
  for (const year of [1, 1900, 2000, 2023, 2024, 2100, 9999]) {
    for (let month = 1; month <= 12; month += 1) {
      for (let day = 0; day <= 32; day += 1) {
        const expected = exists(year, month, day) ? `${pad(year, 4)}-${pad(month)}-${pad(day)}T10:00:00` : null;
        assert.equal(parseTimestamp(`${day}/${month}/${pad(year, 4)}, 10:00`, "DMY"), expected, `${year}-${month}-${day}`);
      }
    }
  }
  // A chat whose days run across a leap day and a century has no step back and a span counted in whole days.
  assert.deepEqual(inferDateOrder(["28/02/2100, 10:00", "01/03/2100, 10:00", "29/02/2104, 10:00"]), { order: "DMY", ambiguous: false });
});

test("an order or a bucket that does not exist is refused", () => {
  assert.deepEqual(DATE_ORDERS, ["DMY", "MDY", "YMD"]);
  assert.throws(() => parseTimestamp("18/01/26, 21:45", "DYM"), RangeError);
  assert.throws(() => buildModel({ text: "", dateOrder: "dmy" }), RangeError);
  assert.throws(() => aggregate(buildModel({ text: "" }), "year"), RangeError);
  assert.throws(() => buildModel({ text: null }), TypeError);
});

test("a chosen order is used as given", () => {
  const text = "3/4/24, 9:10 PM - Ana: one\n13/4/24, 9:11 PM - José: two\n";
  const inferred = buildModel({ text });
  assert.deepEqual([inferred.date_order, inferred.messages.map((message) => message.time)], ["DMY", ["2024-04-03T21:10:00", "2024-04-13T21:11:00"]]);
  const chosen = buildModel({ text, dateOrder: "MDY" });
  assert.deepEqual([chosen.date_order, chosen.date_order_ambiguous], ["MDY", false]);
  assert.deepEqual(chosen.messages.map((message) => message.time), ["2024-03-04T21:10:00", null]);
});

test("title, events and playable sources come from the caller", () => {
  const asked = [];
  const sources = { "PTT-20260110-WA0001.opus": "./audio/PTT-20260110-WA0001.opus", "PTT-20260110-WA0002.opus": "", "PTT-20260110-WA0003.opus": 3 };
  const audioSrc = (filename, index) => {
    asked.push([filename, index]);
    return sources[filename];
  };
  const text = "10/01/26, 07:59 - Ana: hola\n" + [1, 2, 3, 4].map((number) => attached(`PTT-20260110-WA000${number}.opus`, number)).join("");
  const events = [{ date: "2026-01-10", label: "Day one", extra: "left out" }];
  const model = buildModel({ text: `${text}10/01/26, 08:05 - Ana: audio omitted\n`, title: "Trip", events, audioSrc });
  assert.deepEqual([model.title, model.events], ["Trip", [{ date: "2026-01-10", label: "Day one" }]]);
  // The name as the chat writes it and the place of the message, which tells two recordings of one name apart.
  assert.deepEqual(asked, [1, 2, 3, 4].map((number) => [`PTT-20260110-WA000${number}.opus`, number]));
  assert.deepEqual(model.messages.slice(1).map((message) => message.voice.src), ["./audio/PTT-20260110-WA0001.opus", null, null, null, null]);
  assert.equal(buildModel({ text: "10/01/26, 08:00 - Ana: a\n10/01/26, 08:00 - Li: b\n10/01/26, 08:00 - José: c\n" }).title, "Ana · José · Li");
  assert.equal(buildModel({ text: "" }).title, "");
});

test("participants are ordered by their messages and then by code point, not by UTF-16 unit", () => {
  const [emoji, fullwidth] = [character(0x1f600), character(0xff21)];
  assert.ok(emoji < fullwidth, "the default order puts the emoji first");
  const text = [emoji, fullwidth, "Zoe", "ana", "Ana", "Ana"].map((name) => `10/01/26, 08:00 - ${name}: x\n`).join("");
  const model = buildModel({ text });
  assert.deepEqual(model.participants, ["Ana", "Zoe", "ana", fullwidth, emoji]);
  assert.equal(model.title, "Ana · Zoe · ana +2");
  assert.deepEqual(model.messages.map((message) => message.sender), [4, 3, 1, 2, 0, 0]);
});

test("recordings are matched after Unicode normalisation", () => {
  const [composed, decomposed] = ["NFC", "NFD"].map((form) => "canción.opus".normalize(form));
  assert.notEqual(composed, decomposed);
  for (const [written, reported] of [[composed, decomposed], [decomposed, composed]]) {
    for (const durations of [{ [reported]: 12.5 }, new Map([[reported, 12.5]])]) {
      const model = buildModel({ text: attached(written) + attached(written, 1), results: [recording(`media/${reported}`, "la la")], durations });
      assert.deepEqual(model.messages[0].voice, { file: composed, src: null, seconds: 12.5, status: "ok", text: "la la", words: 2 });
    }
  }
  // The same recording written both ways in two reports is one recording: the transcript that worked stays.
  const results = [recording(composed, "first"), { ...recording(decomposed, ""), status: "error", error: "failed later" }];
  assert.equal(buildModel({ text: attached(composed), results }).messages[0].voice.text, "first");
});

test("recordings named by a report are found as whole tokens", () => {
  const results = [recording("voice.opus"), recording("my voice.opus"), recording("my voice.opus (1).opus")];
  const voiceOf = (text) => {
    const [message] = buildModel({ text: `10/01/26, 08:00 - Ana: ${text}\n`, results }).messages;
    return message.kind === "voice" ? [message.voice.file, message.text] : message.kind;
  };
  assert.deepEqual(voiceOf("escucha voice.opus ya"), ["voice.opus", "escucha  ya"]);
  assert.deepEqual(voiceOf("(voice.opus)"), ["voice.opus", "()"]);
  assert.deepEqual(voiceOf("«voice.opus»"), ["voice.opus", "«»"]);
  // The longest name that fits is the recording, and one that runs into other text is none.
  assert.deepEqual(voiceOf("es my voice.opus, sí"), ["my voice.opus", "es , sí"]);
  assert.deepEqual(voiceOf("es my voice.opus (1).opus"), ["my voice.opus (1).opus", "es"]);
  assert.deepEqual(voiceOf("es my voice.opus (1).opusx"), ["my voice.opus", "es  (1).opusx"]);
  // A letter outside the first 65 536 characters is two UTF-16 units and still one neighbour.
  for (const neighbour of ["a", "é", "中", "7", "٧", "_", ".", "-", character(0x1d400), character(0x1d7d0)]) {
    assert.equal(voiceOf(`escucha ${neighbour}voice.opus ya`), "text", neighbour);
    assert.equal(voiceOf(`escucha voice.opus${neighbour} ya`), "text", neighbour);
    assert.deepEqual(voiceOf(`${neighbour}my voice.opus`), ["voice.opus", `${neighbour}my`], neighbour);
    assert.deepEqual(voiceOf(`es ${neighbour}my voice.opus`), ["voice.opus", `es ${neighbour}my`], neighbour);
    assert.deepEqual(voiceOf(`my voice.opus${neighbour}`), "text", neighbour);
  }
  // An emoji is neither letter nor digit, so a name may touch it.
  const emoji = character(0x1f600);
  assert.deepEqual(voiceOf(`${emoji}voice.opus${emoji}`), ["voice.opus", emoji + emoji]);
  assert.deepEqual(voiceOf(`${emoji}my voice.opus${emoji}`), ["my voice.opus", emoji + emoji]);
  // Joined to the text before it, a name is looked for again further on.
  assert.deepEqual(voiceOf("xmy voice.opus (1).opus y my voice.opus (1).opus"), ["voice.opus", "xmy  (1).opus y my voice.opus (1).opus"]);
});

test("entries that are not results and fields of the wrong type are ignored", () => {
  const text = attached("voice.opus");
  for (const results of [null, [], [null, "voice.opus", 3, []], [{ status: "ok", text: "no file" }], [{ file: 3, status: "ok" }],
    [{ file: "voice.opus", status: "pending", text: "not finished" }], [{ file: "voice.opus", text: "no status" }]]) {
    assert.deepEqual(buildModel({ text, results }).messages[0].voice, pending, JSON.stringify(results));
  }
  for (const [value, kept] of [[NaN, null], [Infinity, null], [true, null], ["3", null], [1e400, null], [0, 0], [2, 2]]) {
    const { voice } = buildModel({ text, results: [recording("voice.opus", "hola", { duration_seconds: value })] }).messages[0];
    assert.equal(voice.seconds, kept, String(value));
  }
  let { voice } = buildModel({ text, results: [{ file: "voice.opus", status: "ok", text: null, languages: "es", messages: "none" }] }).messages[0];
  assert.deepEqual(voice, { ...pending, status: "empty" });
  ({ voice } = buildModel({ text, results: [recording("voice.opus", "hola", { languages: ["es", 3, "en"] })] }).messages[0]);
  assert.deepEqual(voice.languages, ["es", "en"]);
  ({ voice } = buildModel({ text, results: [{ file: "voice.opus", status: "error", text: "" }], durations: { "voice.opus": NaN } }).messages[0]);
  // A failure without a reason keeps an empty one: the viewer then says only that it failed.
  assert.deepEqual(voice, { ...pending, status: "error", error: "" });
  ({ voice } = buildModel({ text: `${text}[Voice message transcript: Error: ]\n` }).messages[0]);
  assert.deepEqual(voice, { ...pending, status: "error", error: "" });
  // The results are read, never changed.
  const results = [recording("voice.opus", "  hola  ", { languages: ["es"], messages: [{ chat_file: "chat.txt", timestamp: "10/01/26, 08:00", sender: "Ana" }] })];
  const before = JSON.stringify(results);
  buildModel({ text, results });
  assert.equal(JSON.stringify(results), before);
});

test("one warning counts the transcript lines that belong to no recording", () => {
  let text = "10/01/26, 08:00 - Ana: hola\n[Voice message transcript: suelto]\n";
  assert.deepEqual(buildModel({ text }).warnings, ["1 transcript line in the chat belongs to no voice message and was left out."]);
  text += "10/01/26, 08:01 - Ana: audio omitted\n[Voice message transcript: sin grabación]\n[Voice message transcript: otra]\n";
  const model = buildModel({ text });
  assert.deepEqual(model.warnings, ["3 transcript lines in the chat belong to no voice message and were left out."]);
  assert.deepEqual(model.messages.map((message) => [message.kind, message.text]), [["text", "hola"], ["voice", ""]]);
  assert.equal(model.messages[1].voice.status, "missing");
  // Both warnings, in this order.
  text += "10/01/26, 08:02 - Ana: voice.opus (file attached)\n";
  const elsewhere = recording("voice.opus", "hola", { messages: [{ chat_file: "other.txt", timestamp: "1/10/26, 8:02 AM", sender: "Ana" }] });
  assert.deepEqual(buildModel({ text, results: [elsewhere] }).warnings, [
    "3 transcript lines in the chat belong to no voice message and were left out.",
    "1 transcript was matched by file name only; the report seems to come from a different export of this chat.",
  ]);
});

test("attachment names and their kinds", () => {
  for (const [name, kind] of [
    ["PTT-20240101-WA0001.opus", "audio"], ["AUD-20240101-WA0001.m4a", "audio"], ["00000012-AUDIO-2024-03-04-10-00-00.opus", "audio"],
    ["song.MP3", "audio"], ["PTT-20240101-WA0001.xyz", "audio"], ["GIF-20240101-WA0001.mp4", "gif"], ["funny.gif", "gif"],
    ["STK-20240101-WA0001.webp", "sticker"], ["x.webp", "sticker"], ["00000012-STICKER-2024-03-04-10-00-00.webp", "sticker"],
    ["IMG-20240101-WA0001.jpg", "image"], ["photo.JPEG", "image"], ["shot.png", "image"], ["x.heic", "image"],
    ["00000012-PHOTO-2024-03-04-10-00-00.jpg", "image"], ["VID-20240101-WA0001.mp4", "video"], ["clip.mov", "video"], ["x.3gp", "video"],
    ["x.mkv", "video"], ["x.avi", "video"], ["00000012-VIDEO-2024-03-04-10-00-00.mp4", "video"], ["Ana.vcf", "contact"],
    ["DOC-20240101-WA0001.pdf", "document"], ["Informe final.docx", "document"], ["notes.txt", "document"],
    // A name WhatsApp made is an attachment whatever its extension, and its beginning says what it is.
    ["AUD-20240101-WA0001.xyz", "audio"], ["00000012-AUDIO-2024-03-04-10-00-00.xyz", "audio"], ["DOC-20240101-WA0001.abcde", "document"],
  ]) {
    assert.ok(isAttachmentName(name), name);
    assert.equal(mediaType(name), kind, name);
  }
  for (const name of ["alle 20.30", "I paid 12.50", "www.example.com", "v1.2", "Dr.Who", "report", "", ".", "img-20240101-wa0001.xyz",
    "IMG-2024011-WA0001.xyz", "00000012-AUDIO-2024-03-04-10-00.xyz", "x.jpg ", "x.toolong", "IMG-20240101-WA0001.jpg\n", "IMG-٢٠٢٤٠١٠١-WA0001.xyz",
    "img-20240101-WA0001.xyz", "DOC-20240101-WA0001.abcdef"]) {
    assert.ok(!isAttachmentName(name), JSON.stringify(name));
  }
});

test("an attachment line or bracket is one attachment, with a label of at most forty characters", () => {
  const read = (text, results = null) => {
    const [message] = buildModel({ text: `10/01/26, 08:00 - Ana: ${text}\n`, results }).messages;
    return [message.kind, message.media?.file ?? message.voice?.file ?? null, message.text];
  };
  // An emoji is one character of a label, though two UTF-16 units.
  const emoji = character(0x1f600);
  assert.deepEqual(read(`IMG-20240304-WA0001.jpg (${emoji.repeat(40)})`), ["media", "IMG-20240304-WA0001.jpg", ""]);
  assert.equal(read(`IMG-20240304-WA0001.jpg (${emoji.repeat(41)})`)[0], "text");
  assert.deepEqual(read(`<${emoji.repeat(40)}: IMG-20240304-WA0001.jpg> mira`), ["media", "IMG-20240304-WA0001.jpg", "mira"]);
  assert.equal(read(`<${emoji.repeat(41)}: IMG-20240304-WA0001.jpg> mira`)[0], "text");
  // A bracket inside an attachment line is part of that file's name, and a recording named inside another attachment is not a second one.
  assert.deepEqual(read("x <attached: IMG-20240304-WA0001.jpg> y.pdf (file attached)"), ["media", "x <attached: IMG-20240304-WA0001.jpg> y.pdf", ""]);
  const results = [recording("voice.opus"), recording("foto voice.opus .jpg")];
  assert.deepEqual(read("<adjunto: foto de voice.opus .jpg>", results), ["media", "foto de voice.opus .jpg", ""]);
  assert.deepEqual(read("mira foto voice.opus .jpg", results), ["media", "foto voice.opus .jpg", "mira"]);
});

test("what only resembles a transcript line, an edited mark or a placeholder stays typed text", () => {
  const read = (lines) => buildModel({ text: lines.map((line, minute) => `10/01/26, 08:0${minute} - Ana: ${line}\n`).join("") })
    .messages.map((message) => [message.kind, message.text, message.words, message.edited ?? false]);
  // A transcript is a line of its own.
  const model = buildModel({ text: "10/01/26, 08:00 - Ana: hola\nA caption [Voice message transcript: typed by hand]\n" });
  assert.deepEqual([model.messages[0].text, model.messages[0].words, model.warnings], ["hola\nA caption [Voice message transcript: typed by hand]", 9, []]);
  // The mark closes the text with ">".
  assert.deepEqual(read(["ya voy <This message was edited)", "ya voy <This message was edited>", "ya voy <This message was edited.>"]), [
    ["text", "ya voy <This message was edited)", 6, false], ["text", "ya voy", 2, true], ["text", "ya voy", 2, true],
  ]);
  // A bracketed placeholder of a language that is not listed has at most four words, no bracket inside, and is written twice in the chat.
  assert.deepEqual(read([
    "<archivo de medios omitido>", "<archivo de medios omitido>", "<un archivo de medios omitido>", "<un archivo de medios omitido>",
    "<Media <omitted>", "<Media <omitted>", "<sigh>",
  ]).map(([kind]) => kind), ["media", "media", "text", "text", "text", "text", "text"]);
});

test("date hints name the first dated attachment of each message", () => {
  const messages = parseChat(
    "3/4/24, 9:10 PM - Group created\n"
    + "3/4/24, 9:11 PM - Ana: IMG-20240304-WA0001.jpg (file attached)\n"
    + "3/4/24, 9:12 PM - Ana: <attached: Informe.pdf> <attached: 00000012-PHOTO-2024-03-05-10-00-00.jpg>\n"
    + "3/4/24, 9:13 PM - Ana: sent IMG-20240306-WA0002.jpg and voice-2024-03-07.opus\n"
    + "3/4/24, 9:14 PM - Ana: This message was deleted\n",
  );
  assert.deepEqual(dateHints(messages), [null, "2024-03-04", "2024-03-05", null, null]);
  assert.deepEqual(dateHints(messages, [recording("media/voice-2024-03-07.opus")]), [null, "2024-03-04", "2024-03-05", "2024-03-07", null]);
});

test("a finished result is written into the voice items it belongs to, in place", () => {
  const text = `10/01/26, 07:59 - Ana: hola\n${attached("voice.opus")}${attached("voice.opus", 1)}${attached("other.opus", 2)}10/01/26, 08:03 - Ana: audio omitted\n`;
  const model = buildModel({ text, audioSrc: (name) => `media/${name}`, durations: { "voice.opus": 3 } });
  const items = model.messages.map((message) => message.voice);
  const result = { file: "media/voice.opus", status: "ok", text: "  uno dos  ", languages: ["es", 7], duration_seconds: 2.5 };
  const finished = { file: "voice.opus", src: "media/voice.opus", seconds: 2.5, status: "ok", text: "uno dos", words: 2, languages: ["es"] };
  // A text message, another recording, a recording that is not included and a place outside the chat are left alone.
  assert.deepEqual(applyResult(model, [1, 0, 3, 4, 99, 1], result), [1]);
  assert.deepEqual(model.messages[1].voice, finished);
  assert.equal(model.messages[1].voice, items[1], "the item itself is rewritten");
  assert.deepEqual(model.messages[2].voice, { ...pending, src: "media/voice.opus", seconds: 3 });
  assert.deepEqual(model.messages[3].voice, { ...pending, file: "other.opus", src: "media/other.opus" });
  assert.equal(model.messages[4].voice.status, "missing");
  // Applied again it changes nothing; the model is then what building it with the result gives.
  assert.deepEqual(applyResult(model, [1, 2], result), [2]);
  assert.deepEqual(applyResult(model, [1, 2], result), []);
  assert.deepEqual(model, buildModel({ text, results: [result], audioSrc: (name) => `media/${name}` }));
  assert.deepEqual(aggregate(model).series[0].words_spoken, [4]);
  // A result without its file is trusted to belong to the messages given.
  assert.deepEqual(applyResult(model, [3], { status: "ok", text: "" }), [3]);
  assert.deepEqual(model.messages[3].voice, { ...pending, file: "other.opus", src: "media/other.opus", status: "empty" });
  // Statuses may move backwards. What is known of the recording's length stays; languages and the reason of a failure go.
  assert.deepEqual(applyResult(model, [1], { file: "voice.opus", status: "error", error: "Out of memory" }), [1]);
  assert.deepEqual(model.messages[1].voice, { file: "voice.opus", src: "media/voice.opus", seconds: 2.5, status: "error", text: "", words: 0, error: "Out of memory" });
  assert.deepEqual(Object.keys(model.messages[1].voice), ["file", "src", "seconds", "status", "text", "words", "error"]);
  assert.deepEqual(applyResult(model, [1, 2], { file: "voice.opus", status: "pending" }), [1, 2]);
  assert.deepEqual(model.messages[1].voice, { ...pending, src: "media/voice.opus", seconds: 2.5 });
  assert.deepEqual(applyResult(model, [1, 2], { file: "voice.opus", status: "pending" }), []);
  assert.deepEqual(applyResult(model, [], result), []);
  // A voice message whose recording is not in the export has nothing a result could describe.
  assert.deepEqual(applyResult(model, [4], { status: "ok", text: "uno" }), []);
  assert.deepEqual(model.messages[4].voice, { ...pending, file: null, status: "missing" });
});

test("the module leaves no rule to a primitive that Python reads differently", () => {
  const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  const forbidden = [
    // Unicode-wide in Python, ASCII in JavaScript.
    "\\s", "\\d", "\\w", "\\b", "\\S", "\\D", "\\W",
    // Local time, the platform's date parser and its number parser.
    "new Date", "Date.", "Number(", "parseInt", "parseFloat",
    // The default order of strings and the language of the machine.
    ".sort()", "localeCompare", "toLocale", "Intl.",
    // Not in Safari 16.0.
    "(?<=", "(?<!",
  ];
  assert.deepEqual(forbidden.filter((text) => code.includes(text)), []);
  assert.doesNotMatch(code, /\.split\(\/|trim(?:Start|End)?\(/);
  // A pattern that ends at the end of the text also starts at its start. Anchored at the end only, a class with the u flag
  // misses a character outside the first 65 536 in Chrome 153 and Firefox 155: /[\p{L}]$/u does not find U+1D400 there.
  const anchored = code.split("\n").filter((line) => /\$[`/]/.test(line));
  assert.equal(anchored.length, 4);
  assert.deepEqual(anchored.filter((line) => !/[`/]\^/.test(line)), []);
});
