import assert from "node:assert/strict";
import test from "node:test";

import { buildModel } from "../src/analysis.js";
import { annotatedChat, createReport, indexMessages } from "../src/chat.js";
import { chatEntries, matchReports, merged, previousCandidates, readEventsFile, readReport, reusable } from "../src/previous.js";

const encode = (text) => new TextEncoder().encode(text);
const entry = (path, text = "audio") => ({ path, data: encode(text) });
const mark = String.fromCodePoint(0xfeff);
const read = (content, name = "report.json") => readReport(typeof content === "string" ? content : JSON.stringify(content), name);
const earlier = (results, more = {}) => ({ name: "earlier.json", model: null, results, chats: [], ...more });
const transcript = (file, text, more = {}) => ({ file, status: "ok", text, ...more });
const texts = (files, results) => previousCandidates(files, earlier(results)).map((candidate) => candidate && candidate.text);

test("a report of the command is read with its model and finished results", () => {
  const transcribed = {
    file: "media/voice.opus", status: "ok", text: "hola", messages: [{ chat_file: "chat.txt", timestamp: "01/10/26, 10:00", sender: "Ana" }],
    languages: ["es"], duration_seconds: 1.5, segments: [{ start: 0, end: 1.5, text: "hola", language: "es" }],
    words: [{ word: "hola", start: 0.1, end: 0.4, probability: 0.9 }], bytes: 5, model: "Whisper tiny",
  };
  const failed = { file: "broken.opus", status: "error", text: "", error: "Could not decode audio", messages: [] };
  const report = read({ source: "export", model: "Whisper small", sample_rate: 16000, chunk_seconds: 30, results: [transcribed, failed] }, "transcripts.json");
  assert.deepEqual(report, { name: "transcripts.json", model: "Whisper small", results: [transcribed, failed], chats: [] });
  // A report saved again by an editor that puts a byte order mark first.
  assert.deepEqual(read(`${mark}{"results": []}`, "with mark.json"), { name: "with mark.json", model: null, results: [], chats: [] });
});

test("a download of this app is read without its pending recordings and with its chats", () => {
  const message = { chat_file: "chat.txt", timestamp: "01/10/26, 10:00", sender: "Ana" };
  const text = `${mark}01/10/26, 10:00 - Ana: a.opus (attached)\r\n`;
  const report = read({
    schema_version: 1, source: "export.zip", sample_rate: 16000,
    chats: [
      { file: "chat.txt", original_text: text, annotated_text: `${text}[Voice message transcript: hola]\r\n`, messages: [] },
      { file: "", original_text: "no name" }, { file: "no text.txt" }, { file: 3, original_text: "x" }, "chat.txt", null,
    ],
    results: [
      { file: "a.opus", bytes: 5, messages: [message], status: "ok", text: "hola", languages: ["es"],
        segments: [{ start: 0, end: 2, text: "hola" }], duration_seconds: 2, model: { name: "Whisper base" } },
      { file: "b.opus", messages: [], status: "pending", text: "", languages: [], segments: [] },
      { file: "c.opus", messages: [], status: "error", text: "", languages: [], segments: [], error: "Out of memory" },
    ],
    warnings: [],
    // This app describes its model with more than a name.
    model: { name: "Whisper small", repository: "onnx-community/whisper-small", runtime: "WebGPU" },
  }, "voxpad.json");
  assert.deepEqual([report.name, report.model], ["voxpad.json", "Whisper small"]);
  assert.deepEqual(report.results, [
    { file: "a.opus", status: "ok", text: "hola", messages: [message], languages: ["es"], duration_seconds: 2,
      segments: [{ start: 0, end: 2, text: "hola" }], bytes: 5, model: "Whisper base" },
    { file: "c.opus", status: "error", text: "", error: "Out of memory", messages: [], languages: [], segments: [] },
  ]);
  // The chat as it was exported, byte order mark and line endings included; the annotated copy is not needed.
  assert.deepEqual(report.chats, [{ path: "chat.txt", text }]);
  for (const chats of [undefined, null, "chat.txt", {}, [null]]) assert.deepEqual(read({ results: [], chats }).chats, []);
});

test("fields of the wrong type are left out of a result", () => {
  for (const [key, value] of [
    ["languages", ["es", 3]], ["languages", "es"], ["duration_seconds", -1], ["duration_seconds", true], ["duration_seconds", "3"],
    ["segments", [{ start: 0 }, "text"]], ["segments", [[]]], ["words", { word: "hola" }], ["bytes", 2.5], ["bytes", -1], ["bytes", true],
    ["bytes", "5"], ["messages", "none"], ["messages", ["none"]], ["model", 7], ["model", { name: 7 }], ["model", ""],
    ["error", "left out of a transcript"], ["something else", "ignored"], ["__proto__", { status: "error" }],
  ]) {
    const report = read(`{"results": [{"file": "voice.opus", "status": "ok", "text": "hola", ${JSON.stringify(key)}: ${JSON.stringify(value)}}]}`);
    assert.deepEqual(report.results, [{ file: "voice.opus", status: "ok", text: "hola" }], `${key}: ${JSON.stringify(value)}`);
  }
  for (const [key, value] of [["duration_seconds", 0], ["bytes", 0], ["languages", []], ["segments", []], ["words", []], ["messages", []]]) {
    assert.deepEqual(read({ results: [transcript("voice.opus", "hola", { [key]: value })] }).results, [transcript("voice.opus", "hola", { [key]: value })]);
  }
  // Python reads and writes numbers that are not JSON. None may reach a report written later, and a transcript that spells one stays.
  const report = read('{"results": [{"file": "voice.opus", "status": "ok", "duration_seconds": NaN, "bytes": Infinity, '
    + '"segments": [{"start": -Infinity, "end": 1e999, "text": "NaN, \\"Infinity\\" and -Infinity\\\\"}]}]}');
  assert.deepEqual(report.results, [
    { file: "voice.opus", status: "ok", text: "", segments: [{ start: null, end: null, text: 'NaN, "Infinity" and -Infinity\\' }] },
  ]);
  // A number too large to hold is valid JSON and read the same way.
  assert.deepEqual(read('{"results": [{"file": "voice.opus", "status": "ok", "duration_seconds": 1e999, "words": [{"start": 0, "end": 1e999}]}]}').results, [
    { file: "voice.opus", status: "ok", text: "", words: [{ start: 0, end: null }] },
  ]);
  // A model without a name is no model.
  for (const model of ["", { name: "" }, { runtime: "WebGPU" }, 7, null, ["Whisper small"]]) assert.equal(read({ model, results: [] }).model, null, JSON.stringify(model));
  // A failure always says something, as the reports and the annotated chat print it.
  assert.deepEqual(read({ results: [{ file: "voice.opus", status: "error" }, { file: "other.opus", status: "error", error: "" }] }).results, [
    { file: "voice.opus", status: "error", text: "", error: "Transcription failed" },
    { file: "other.opus", status: "error", text: "", error: "Transcription failed" },
  ]);
});

test("a file that is not a report is named", () => {
  const result = transcript("voice.opus", "hola");
  for (const content of [
    "an earlier report", "", "[]", "3", "null", '{"results": {}}', '{"model": "Whisper small"}', '{"results": ["voice.opus"]}', '{"results": [[]]}',
    { results: [{ ...result, file: 3 }] }, { results: [{ ...result, status: null }] }, { results: [{ status: "ok" }] },
    { results: [{ file: "voice.opus" }] }, { results: [{ ...result, text: null }] }, { results: [{ ...result, text: 3 }] }, { results: [result, null] },
    '{"results": [NaN]}', '{"results": [}', "NaN", "[".repeat(100000), undefined,
  ]) {
    const label = typeof content === "string" && content.length > 100 ? "deeply nested" : JSON.stringify(content);
    assert.throws(() => readReport(typeof content === "object" ? JSON.stringify(content) : content, "notes.json"),
      { name: "Error", message: "notes.json is not a VoxPad transcript report." }, label);
  }
});

test("merging keeps one result per recording, preferring later reports and transcripts", () => {
  const result = (file, status, text) => ({ file, status, text });
  const [composed, decomposed] = ["NFC", "NFD"].map((form) => "café.opus".normalize(form));
  assert.notEqual(composed, decomposed);
  const first = earlier([result("a.opus", "ok", "first a"), result("b.opus", "ok", "first b"), result(composed, "error", "failed")]);
  const second = earlier([
    result("b.opus", "error", "second b failed"), result("a.opus", "ok", "second a"), result(decomposed, "error", "failed again"),
    result("c.opus", "error", "c failed"),
  ]);
  const chosen = merged([first, second]);
  assert.deepEqual(chosen.map((item) => [item.file, item.text]), [
    ["a.opus", "second a"], ["b.opus", "first b"], [decomposed, "failed again"], ["c.opus", "c failed"],
  ]);
  // Copies, so that what is done with them leaves the reports as they were read.
  assert.notEqual(chosen[0], second.results[1]);
  assert.deepEqual(merged([]), []);
});

test("a result stands for the recording at its path or else for the one with its name", () => {
  const results = [transcript("voice.opus", "top"), { file: "media/other.opus", status: "error", text: "other" }];
  assert.deepEqual(texts(["media/other.opus", "voice.opus"], results), ["other", "top"]);
  // The export was read from another folder, or from its ZIP: names are enough while each is used once.
  assert.deepEqual(texts(["Export/Media/voice.opus", "Export/new.opus", "Export/other.opus"], results), ["top", null, "other"]);
  assert.deepEqual(texts([], results), []);
  assert.deepEqual(texts(["voice.opus"], []), [null]);
});

test("a name used twice stands for no recording", () => {
  const [first, second] = ["first", "second"].map((folder) => transcript(`${folder}/voice.opus`, folder));
  // Paths tell the two apart.
  assert.deepEqual(texts(["first/voice.opus", "second/voice.opus"], [second, first]), ["first", "second"]);
  // Twice in the report, whatever became of them: neither is known to be this recording.
  assert.deepEqual(texts(["voice.opus"], [first, second]), [null]);
  assert.deepEqual(texts(["voice.opus"], [first, { ...second, status: "error" }]), [null]);
  // Twice in the export: the result is not known to be either recording, unless its path says so.
  assert.deepEqual(texts(["a/voice.opus", "b/voice.opus"], [transcript("voice.opus", "top")]), [null, null]);
  assert.deepEqual(texts(["b/voice.opus", "voice.opus"], [transcript("voice.opus", "top")]), [null, "top"]);
});

test("paths and names are compared after Unicode normalisation", () => {
  const [composed, decomposed] = ["NFC", "NFD"].map((form) => "média/café.opus".normalize(form));
  assert.notEqual(composed, decomposed);
  for (const [file, recorded] of [[composed, decomposed], [decomposed, composed]]) {
    const results = [transcript(recorded, "found")];
    assert.deepEqual(texts([file], results), ["found"]);
    assert.deepEqual(texts([file.split("/").pop()], results), ["found"]);
  }
});

test("of results naming one file a transcript is preferred and then the later one", () => {
  const result = (status, text) => ({ file: "voice.opus", status, text });
  for (const [results, expected] of [
    [[result("ok", "first"), result("error", "second")], "first"],
    [[result("error", "first"), result("ok", "second")], "second"],
    [[result("ok", "first"), result("ok", "second")], "second"],
    [[result("error", "first"), result("error", "second")], "second"],
  ]) {
    assert.deepEqual(texts(["voice.opus"], results), [expected]);
    // By name alone, two results are one too many.
    assert.deepEqual(texts(["media/voice.opus"], results), [null]);
  }
});

test("a transcript is reused unless it failed, the recording changed or word times are wanted and missing", () => {
  const heard = transcript("voice.opus", "hola");
  assert.equal(reusable(heard, 5), true);
  assert.equal(reusable(heard, null), true);
  // Silence is a result too.
  assert.equal(reusable({ ...heard, text: "" }, 5), true);
  assert.equal(reusable({ ...heard, bytes: 5 }, 5), true);
  assert.equal(reusable({ ...heard, bytes: 6 }, 5), false);
  assert.equal(reusable({ ...heard, bytes: 6 }, null), false);
  assert.equal(reusable({ ...heard, status: "error", error: "Could not decode audio" }, 5), false);
  assert.equal(reusable(null, 5), false);
  assert.equal(reusable(heard, 5, true), false);
  assert.equal(reusable({ ...heard, words: [] }, 5, true), true);
});

test("a stopped download continues: what is transcribed is reused and the rest is left to do", () => {
  const chat = "01/10/26, 10:00 - Ana: a.opus (attached)\n01/10/26, 10:01 - José: b.opus (attached)\n01/10/26, 10:02 - Ana: c.opus (attached)\n";
  const index = indexMessages([entry("chat.txt", chat), entry("a.opus", "first"), entry("b.opus", "second"), entry("c.opus", "third")]);
  const stopped = new Map([
    ["a.opus", { status: "ok", text: "uno", languages: ["es"], duration_seconds: 2, segments: [{ start: 0, end: 2, text: "uno", language: "es" }] }],
    ["b.opus", { status: "pending" }],
    ["c.opus", { status: "error", error: "Out of memory" }],
  ]);
  const download = { ...createReport(index, stopped, "export.zip"), model: { name: "Whisper base" } };
  const report = readReport(`${JSON.stringify(download, null, 2)}\n`, "chat_transcribed.json");
  const match = matchReports([report], index.audio);
  assert.deepEqual([...match.reused], [["a.opus", {
    status: "ok", text: "uno", languages: ["es"], duration_seconds: 2, segments: [{ start: 0, end: 2, text: "uno", language: "es" }], model: "Whisper base",
  }]]);
  // The recording that failed is loaded, so it is transcribed again and its failure is not carried along.
  assert.deepEqual(match.kept, []);
  assert.deepEqual([match.sources, match.byNameOnly, match.changed, match.withoutWords], [[{ name: "chat_transcribed.json", reused: 1 }], 0, 0, 0]);
  // The next download holds the transcript again, with what made it.
  const next = createReport(index, new Map([...match.reused, ["b.opus", { status: "ok", text: "dos" }]]), "export.zip", match.kept);
  assert.deepEqual(next.results.map((result) => [result.file, result.bytes, result.status, result.text, result.model]), [
    ["a.opus", 5, "ok", "uno", "Whisper base"], ["b.opus", 6, "ok", "dos", undefined], ["c.opus", 5, "pending", "", undefined],
  ]);
  assert.match(annotatedChat(index.chats[0], match.reused), /a\.opus \(attached\)\n\[Voice message transcript: uno\]\n/u);
  // A recording that changed since then is transcribed again, and the notice can say why.
  const changed = indexMessages([entry("chat.txt", chat), entry("a.opus", "another recording"), entry("b.opus", "second")]);
  const again = matchReports([report], changed.audio);
  assert.deepEqual([[...again.reused], again.sources, again.changed], [[], [{ name: "chat_transcribed.json", reused: 0 }], 1]);
});

test("a report of another export of the chat is matched by file names", () => {
  const chat = "01/10/26, 10:00 - Ana: PTT-20261001-WA0001.opus (attached)\n01/10/26, 10:01 - José: PTT-20261001-WA0002.opus (attached)\n";
  const index = indexMessages([entry("Chat/_chat.txt", chat), entry("Chat/PTT-20261001-WA0001.opus"), entry("Chat/PTT-20261001-WA0002.opus")]);
  // The command's report: paths of another folder, no sizes, the model at the top.
  const report = read({ source: "export", model: "Whisper small", results: [
    transcript("PTT-20261001-WA0001.opus", "uno", { messages: [{ chat_file: "chat.txt", timestamp: "1/10/26, 10:00", sender: "Ana G." }], duration_seconds: 3.5 }),
    transcript("PTT-20261001-WA0002.opus", "", { messages: [], words: [] }),
  ] }, "transcripts.json");
  const match = matchReports([report], index.audio);
  assert.deepEqual([...match.reused], [
    ["Chat/PTT-20261001-WA0001.opus", { status: "ok", text: "uno", duration_seconds: 3.5, model: "Whisper small" }],
    ["Chat/PTT-20261001-WA0002.opus", { status: "ok", text: "", words: [], model: "Whisper small" }],
  ]);
  assert.deepEqual([match.kept, match.sources, match.byNameOnly, match.changed], [[], [{ name: "transcripts.json", reused: 2 }], 2, 0]);
  // What is written is this export's own: its paths, its messages and the sizes of its files.
  const [first] = createReport(index, match.reused, "export.zip", match.kept).results;
  assert.deepEqual([first.file, first.bytes, first.messages], ["Chat/PTT-20261001-WA0001.opus", 5, [{ chat_file: "Chat/_chat.txt", timestamp: "01/10/26, 10:00", sender: "Ana" }]]);
  assert.equal(matchReports([report], index.audio, { wordTimestamps: true }).reused.size, 1);
  assert.equal(matchReports([report], index.audio, { wordTimestamps: true }).withoutWords, 1);
});

test("of two reports the last given wins unless it has nothing usable", () => {
  const index = indexMessages([entry("a.opus"), entry("b.opus"), entry("c.opus"), entry("d.opus")]);
  const first = earlier([transcript("a.opus", "first a"), transcript("b.opus", "first b", { model: "Whisper tiny" }), transcript("c.opus", "first c")],
    { name: "first.json", model: "Whisper small" });
  const second = earlier([transcript("a.opus", "second a"), { file: "b.opus", status: "error", text: "", error: "failed" }, transcript("c.opus", "second c", { bytes: 99 })],
    { name: "second.json" });
  const match = matchReports([first, second], index.audio);
  assert.deepEqual([...match.reused], [
    ["a.opus", { status: "ok", text: "second a" }],
    // The result's own model says more than its report's.
    ["b.opus", { status: "ok", text: "first b", model: "Whisper tiny" }],
    ["c.opus", { status: "ok", text: "first c", model: "Whisper small" }],
  ]);
  assert.deepEqual(match.sources, [{ name: "first.json", reused: 2 }, { name: "second.json", reused: 1 }]);
  assert.deepEqual([match.byNameOnly, match.changed, match.withoutWords], [3, 0, 0]);
  assert.deepEqual(matchReports([second, first], index.audio).reused.get("a.opus"), { status: "ok", text: "first a", model: "Whisper small" });
  assert.deepEqual(matchReports([], index.audio), { reused: new Map(), kept: [], sources: [], byNameOnly: 0, changed: 0, withoutWords: 0 });
});

test("results whose recording is not loaded are kept for the viewer and the next download", () => {
  const chat = "01/10/26, 10:00 - Ana: PTT-20261001-WA0001.opus (attached)\r\n[Voice message transcript: uno]\r\n"
    + "01/10/26, 10:01 - José: PTT-20261001-WA0002.opus (attached)\r\n01/10/26, 10:02 - Ana: PTT-20261001-WA0003.opus (attached)\r\n";
  // An annotated chat selected with the command's report, and no recording at all.
  const index = indexMessages([entry("chat_with_transcripts.txt", chat)]);
  const messages = [{ chat_file: "chat.txt", timestamp: "01/10/26, 10:00", sender: "Ana" }];
  const older = earlier([
    transcript("PTT-20261001-WA0001.opus", "uno", { messages, duration_seconds: 2.5, bytes: 7 }),
    transcript("PTT-20261001-WA0002.opus", "dos viejo"),
    transcript("PTT-20261001-WA0003.opus", "tres", { duration_seconds: 4, model: "Whisper tiny" }),
  ], { name: "older.json", model: "Whisper small" });
  const newer = earlier([
    transcript("PTT-20261001-WA0002.opus", "dos", { duration_seconds: 1.25 }),
    { file: "PTT-20261001-WA0003.opus", status: "error", text: "", error: "Out of memory" },
  ], { name: "newer.json" });
  const match = matchReports([older, newer], index.audio);
  assert.deepEqual([[...match.reused], match.sources], [[], [{ name: "older.json", reused: 0 }, { name: "newer.json", reused: 0 }]]);
  // One per recording: the later report wins, a transcript wins over a failure, and each says what made it when that is known.
  assert.deepEqual(match.kept, [
    transcript("PTT-20261001-WA0001.opus", "uno", { messages, duration_seconds: 2.5, bytes: 7, model: "Whisper small" }),
    transcript("PTT-20261001-WA0002.opus", "dos", { duration_seconds: 1.25 }),
    transcript("PTT-20261001-WA0003.opus", "tres", { duration_seconds: 4, model: "Whisper tiny" }),
  ]);
  assert.deepEqual(older.results[0], transcript("PTT-20261001-WA0001.opus", "uno", { messages, duration_seconds: 2.5, bytes: 7 }), "the report is left as it was read");
  // The viewer shows transcripts, durations and spoken words without any audio.
  const model = buildModel({ text: index.chats[0].text, results: match.kept });
  assert.deepEqual(model.messages.map(({ voice }) => [voice.status, voice.seconds, voice.words]), [["ok", 2.5, 1], ["ok", 1.25, 1], ["ok", 4, 1]]);
  assert.deepEqual(model.warnings, []);
  // And the download writes them as they were read, after the recordings of this export.
  const report = createReport(index, new Map(), "chat_with_transcripts.txt", match.kept);
  assert.deepEqual(report.results, match.kept);
  const [again] = [readReport(JSON.stringify(report), "next.json")].map((next) => matchReports([next], index.audio));
  assert.deepEqual(again.kept, match.kept);
  // Once a recording is loaded its result leaves the kept ones: reused when it still stands, dropped when it is to be transcribed again.
  const loaded = indexMessages([entry("chat.txt", chat), entry("media/PTT-20261001-WA0001.opus", "not the 7 bytes it had"), entry("media/PTT-20261001-WA0002.opus")]);
  const partly = matchReports([older, newer], loaded.audio);
  assert.deepEqual([...partly.reused], [["media/PTT-20261001-WA0002.opus", { status: "ok", text: "dos", duration_seconds: 1.25 }]]);
  assert.deepEqual(partly.kept.map((result) => [result.file, result.text]), [["PTT-20261001-WA0003.opus", "tres"]]);
  assert.equal(partly.changed, 1);
});

test("a download opened alone shows its conversation, and a report of the command has none", () => {
  const text = `${mark}01/10/26, 10:00 - Ana: a.opus (attached)\r\n01/10/26, 10:01 - José: hola\r\n`;
  const index = indexMessages([entry("Chat/chat.txt", text), entry("Chat/a.opus", "first")]);
  const download = createReport(index, new Map([["Chat/a.opus", { status: "ok", text: "uno dos", duration_seconds: 2 }]]), "export.zip");
  const report = readReport(JSON.stringify(download), "chat_transcribed.json");
  const alone = indexMessages(chatEntries(report));
  assert.deepEqual(alone.chats.map((chat) => [chat.path, chat.text]), [["Chat/chat.txt", text]]);
  assert.deepEqual([alone.audio, alone.warnings], [[], []]);
  const match = matchReports([report], alone.audio);
  assert.deepEqual(match.kept.map((result) => result.file), ["Chat/a.opus"]);
  const [voice] = buildModel({ text: alone.chats[0].text, results: match.kept }).messages;
  assert.deepEqual(voice.voice, { file: "a.opus", src: null, seconds: 2, status: "ok", text: "uno dos", words: 2 });
  // Downloaded again it is the same report: nothing is lost by opening it without the export.
  assert.deepEqual(createReport(alone, new Map(), "chat_transcribed.json", match.kept).results, download.results);
  assert.deepEqual(chatEntries(read({ source: "export", model: "Whisper small", results: [transcript("a.opus", "uno")] })), []);
});

test("an events file is told from a chat and from other text", () => {
  const events = "# trips\n2026-01-17, First date\n14/02/2026 | Valentine\n\n01.03.26\tMoved\n2026/01/05 Party\n";
  assert.deepEqual(readEventsFile("events.txt", encode(events)), {
    events: [
      { date: "2026-01-05", label: "Party" }, { date: "2026-01-17", label: "First date" }, { date: "2026-02-14", label: "Valentine" },
      { date: "2026-03-01", label: "Moved" },
    ],
    warnings: [],
  });
  assert.deepEqual(readEventsFile("events.txt", encode(`${mark}2026-01-17 One`)).events, [{ date: "2026-01-17", label: "One" }]);
  // Four in five of the lines that say something must be dated: one stray line among five is a slip, two are another kind of file.
  const slip = readEventsFile("events.txt", encode(`${events}not dated\n`));
  assert.deepEqual([slip.events.length, slip.warnings], [4, ["Events line 7 has no valid date and was skipped."]]);
  assert.equal(readEventsFile("events.txt", encode(`${events}not dated\nnor this\n`)), null);
  for (const text of ["", "\n\n# only a comment\n", "Notes about the trip\n", "no date\n2026-01-17 One\n", "2026-01-17 One\n2026-01-18 Two\n2026-01-19 Three\nno date\n"]) {
    assert.equal(readEventsFile("notes.txt", encode(text)), null, text);
  }
  // The chat test comes first: every line of a chat starts with a date too.
  assert.equal(readEventsFile("chat.txt", encode("17/01/2026, 10:00 - Ana: hola\n18/01/2026, 10:00 - José: hola\n")), null);
  assert.equal(readEventsFile("events.txt", encode(`${events}17/01/2026, 10:00 - Ana: hola\n`)), null);
  for (const name of ["_chat.txt", "_CHAT.TXT", "Chat/_chat.txt"]) assert.equal(readEventsFile(name, encode(events)), null, name);
  // Text that is not UTF-8 is not read at all, rather than with its bytes replaced.
  assert.equal(readEventsFile("events.txt", new Uint8Array([...encode("2026-01-17 One "), 0xff, 0xfe])), null);
  // At most 1 MiB.
  const line = "2026-01-17 One\n";
  const fill = (bytes) => encode(line + "#".repeat(bytes - line.length));
  assert.deepEqual(readEventsFile("events.txt", fill(1048576)).events, [{ date: "2026-01-17", label: "One" }]);
  assert.equal(readEventsFile("events.txt", fill(1048577)), null);
});
