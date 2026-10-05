import assert from "node:assert/strict";
import test from "node:test";

import { annotatedChat, createReport, indexMessages, parseChat, reportResults } from "../src/chat.js";

const encode = (text) => new TextEncoder().encode(text);
const entry = (path, text = "audio") => ({ path, data: encode(text) });
const character = (code) => String.fromCodePoint(code);

test("iOS parsing preserves BOM, CRLF, multiline bodies and original offsets", () => {
  const text = "\uFEFFExport preface\r\n[04/10/26, 09:10:11] \u200EAna: voice.opus (adjunto)\r\nDescripción\r\n[04/10/26, 09:12] Bob: Thanks";
  const messages = parseChat(text);
  assert.equal(messages.length, 2);
  assert.equal(messages[0].sender, "Ana");
  assert.equal(messages[0].timestamp, "04/10/26, 09:10:11");
  assert.equal(messages[0].text, "voice.opus (adjunto)\nDescripción");
  assert.equal(messages[0].start_line, 1);
  assert.equal(messages[0].end_line, 2);
  for (const message of messages) {
    assert.equal(text.slice(message.start_offset, message.end_offset), message.raw);
  }
  assert.equal(messages[0].raw, "[04/10/26, 09:10:11] \u200EAna: voice.opus (adjunto)\r\nDescripción\r\n");
  assert.deepEqual(messages[0].audio_paths, []);
});

test("Android headers and system messages accept localized dates and time markers", () => {
  const cases = [
    ["10/4/26, 9:10\u202FPM - José: Hola", "10/4/26, 9:10\u202FPM"],
    ["2026-10-4, 9:10 p. m. - José: Hola", "2026-10-4, 9:10 p. m."],
    ["[٤/١٠/٢٠٢٦، ٩:١٠ م] José: Hola", "٤/١٠/٢٠٢٦، ٩:١٠ م"],
    ["[2026/10/4, 上午9:10] José: Hola", "2026/10/4, 上午9:10"],
    ["[2026/10/4, 9:10 午後] José: Hola", "2026/10/4, 9:10 午後"],
    ["[04.10.2026, 09：10] José： Hola", "04.10.2026, 09：10"],
  ];
  for (const [text, timestamp] of cases) {
    const [message] = parseChat(text);
    assert.equal(message.timestamp, timestamp);
    assert.equal(message.sender, "José");
    assert.equal(message.text, "Hola");
  }
  const [system] = parseChat("04/10/26, 10:00 - Messages are encrypted.");
  assert.equal(system.sender, null);
  assert.equal(system.text, "Messages are encrypted.");
});

test("blank continuation lines remain part of the complete voice message", () => {
  const text = "04/10/26, 10:00 - Ana: voice.opus\n\nMore context\n04/10/26, 10:01 - Bob: OK\n";
  const [message] = parseChat(text);
  assert.equal(message.text, "voice.opus\n\nMore context");
  assert.equal(message.end_line, 2);
  assert.equal(message.raw, text.slice(0, text.indexOf("04/10/26, 10:01")));
});

test("localized attachment labels match known filenames, including Unicode and punctuation", () => {
  const text = "[04/10/26, 10:00] Ana: <adjunto: café.opus>\n"
    + "[04/10/26, 10:01] Jörg: <Anhang: audio deutsch.M4A>\n"
    + "[04/10/26, 10:02] Chloé: <pièce jointe : special+(1).ogg>\n";
  const index = indexMessages([
    entry("_chat.txt", text), entry("media/cafe\u0301.opus"),
    entry("media/audio deutsch.M4A"), entry("media/special+(1).ogg"),
  ]);
  assert.equal(index.audio.length, 3);
  assert.equal(index.chats.length, 1);
  assert.deepEqual(index.warnings, []);
  assert.equal(index.audio.find((audio) => audio.name === "café.opus").messages[0].sender, "Ana");
  assert.equal(index.audio.find((audio) => audio.name === "audio deutsch.M4A").messages[0].sender, "Jörg");
  assert.equal(index.audio.find((audio) => audio.name === "special+(1).ogg").messages[0].sender, "Chloé");
  assert.equal(index.chats[0].text, text);
});

test("filename association rejects substrings and deduplicates references within a message", () => {
  const index = indexMessages([
    entry("chat.txt", "04/10/26, 10:00 - A: other-voice.opus\n"
      + "04/10/26, 10:01 - B: voice.opus.backup\n"
      + "04/10/26, 10:02 - C: voice.opus and voice.opus\n"),
    entry("voice.opus"),
  ]);
  assert.deepEqual(index.audio[0].messages, [{ chat_file: "chat.txt", timestamp: "04/10/26, 10:02", sender: "C" }]);
  assert.equal(index.audio[0].occurrences.length, 1);
  assert.deepEqual(index.chats[0].messages[2].audio_paths, ["voice.opus"]);
});

test("duplicate basenames prefer a neighboring recording and warn when ambiguous", () => {
  const index = indexMessages([
    entry("first/voice.opus"), entry("second/voice.opus"),
    entry("first/chat.txt", "04/10/26, 10:00 - Ana: voice.opus (attached)\n"),
    entry("chat.txt", "04/10/26, 10:01 - Bob: voice.opus (attached)\n"),
  ]);
  assert.equal(index.audio[0].messages[0].sender, "Ana");
  assert.equal(index.audio[1].messages.length, 0);
  assert.equal(index.warnings.length, 1);
  assert.match(index.warnings[0], /Ambiguous attachment.*voice\.opus.*chat\.txt/u);
  assert.deepEqual(index.chats.find((chat) => chat.path === "chat.txt").messages[0].audio_paths, []);
});

test("duplicate normalized paths reject silent overwrites", () => {
  for (const paths of [
    ["voice.opus", "voice.opus"],
    ["media//voice.opus", "media/voice.opus"],
    ["media\\voice.opus", "media/voice.opus"],
    ["./café.opus", "cafe\u0301.opus"],
  ]) {
    assert.throws(() => indexMessages(paths.map((path) => entry(path))), /Duplicate entry path/u);
  }
});

test("unrelated text, invalid UTF-8 and unsupported files do not become chats or audio", () => {
  const index = indexMessages([
    entry("notes.txt", "Export notes"), entry("photo.jpg"), entry("_chat.txt", ""),
    { path: "invalid.txt", data: new Uint8Array([0xff, 0xfe]) },
  ]);
  assert.deepEqual(index.audio, []);
  assert.equal(index.chats.length, 1);
  assert.equal(index.chats[0].path, "_chat.txt");
  assert.equal(index.warnings.length, 1);
  assert.match(index.warnings[0], /non-UTF-8.*invalid\.txt/u);
});

test("missing recordings preserve original chat and ordered messages", () => {
  const text = "[04/10/26, 10:00] Ana: <attached: absent.opus>\r\nAnother line\r\n";
  const index = indexMessages([entry("chat.txt", text)]);
  assert.equal(index.chats.length, 1);
  assert.deepEqual(index.audio, []);
  assert.equal(annotatedChat(index.chats[0], []), text);
  assert.equal(createReport(index, [], "missing.zip").chats[0].original_text, text);
});

test("annotated log inserts after a full CRLF message and preserves all original text", () => {
  const text = "\uFEFFChat preface\r\n[04/10/26, 10:00] Ana: voice.opus (adjunto)\r\nA continuation\r\n[04/10/26, 10:01] Bob: Bye";
  const index = indexMessages([entry("chat.txt", text), entry("voice.opus")]);
  const annotation = "[Voice message transcript: Hola, ¿qué tal?]\r\n";
  const result = annotatedChat(index.chats[0], new Map([["voice.opus", { status: "ok", text: "Hola, ¿qué tal?" }]]));
  assert.equal(result, text.replace("A continuation\r\n", `A continuation\r\n${annotation}`));
  assert.equal(result.replace(annotation, ""), text);
});

test("final unterminated notes get a separator while pending notes remain unchanged", () => {
  const text = "04/10/26, 10:00 - Ana: voice.opus\nMore context";
  const index = indexMessages([entry("chat.txt", text), entry("voice.opus")]);
  assert.equal(annotatedChat(index.chats[0], { "voice.opus": { status: "pending", text: "" } }), text);
  assert.equal(annotatedChat(index.chats[0], [{ path: "voice.opus", status: "error", error: "Decoder failed" }]),
    `${text}\n[Voice message transcript: Error: Decoder failed]\n`);
});

test("annotations treat markup as literal text without accessing a DOM", () => {
  const text = "04/10/26, 10:00 - <script>alert(1)</script>: voice.opus\n";
  const index = indexMessages([entry("chat.txt", text), entry("voice.opus")]);
  const malicious = '<img src=x onerror="alert(1)">';
  assert.equal(index.audio[0].messages[0].sender, "<script>alert(1)</script>");
  assert.equal(annotatedChat(index.chats[0], [{ file: "voice.opus", status: "ok", text: malicious }]),
    `${text}[Voice message transcript: ${malicious}]\n`);
});

test("report includes exact original messages, all recordings and reusable segment/word times", () => {
  const text = "04/10/26, 10:00 - José: voice.opus\n"
    + "04/10/26, 10:01 - Ana: pending.m4a\n";
  const index = indexMessages([entry("chat.txt", text), entry("voice.opus"), entry("pending.m4a")]);
  const segments = [{ start: 0, end: 2.5, text: "Hola", language: "es" }];
  const words = [{ start: 0.1, end: 0.4, word: "Hola", probability: 0.9 }];
  const results = [{ path: "voice.opus", status: "ok", text: "Hola", duration_seconds: 2.5, languages: ["es"], segments, words }];
  const report = createReport(index, results, "export.zip");
  assert.equal(report.schema_version, 1);
  assert.equal(report.source, "export.zip");
  assert.equal(report.sample_rate, 16000);
  assert.equal(report.chats[0].original_text, text);
  assert.deepEqual(report.chats[0].messages.map((message) => message.sender), ["José", "Ana"]);
  assert.equal(report.chats[0].messages[0].raw, "04/10/26, 10:00 - José: voice.opus\n");
  assert.deepEqual(report.results.map((result) => result.file), ["pending.m4a", "voice.opus"]);
  assert.equal(report.results[0].status, "pending");
  assert.deepEqual(report.results[1].segments, segments);
  assert.deepEqual(report.results[1].words, words);
  assert.equal(report.results[1].duration_seconds, 2.5);
  assert.equal(report.results[1].messages[0].chat_file, "chat.txt");
  assert.match(report.chats[0].annotated_text, /Voice message transcript: Hola/u);
  assert.deepEqual(JSON.parse(JSON.stringify(report)), report);
  assert.equal(Object.hasOwn(report.results[1], "data"), false);
  report.results[1].segments[0].text = "Changed";
  assert.equal(segments[0].text, "Hola");
});

test("lines end only at CR LF, CR and LF", () => {
  // A line separator or a form feed typed into a message is part of its text, not the start of a line.
  const [separator, feed] = [character(0x2028), character(0x0c)];
  const messages = parseChat(`01/10/26, 10:00 - Ana: one${separator}two\n01/10/26, 10:01 - Bob: x${feed}y`);
  assert.deepEqual(messages.map((message) => [message.sender, message.text, message.end_line]),
    [["Ana", `one${separator}two`, 0], ["Bob", `x${feed}y`, 1]]);
  // So is every other character that Python's str.splitlines() breaks at.
  for (const code of [0x0b, 0x0c, 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029]) {
    const text = `one${character(code)}01/10/26, 10:01 - Bob: two`;
    const parsed = parseChat(`01/10/26, 10:00 - Ana: ${text}`);
    assert.deepEqual(parsed.map((message) => [message.sender, message.text, message.end_line]), [["Ana", text, 0]], code.toString(16));
  }
  // Each of the three endings ends one line, and an ending that closes the text starts no further line.
  const mixed = parseChat("01/10/26, 10:00 - Ana: one\r\ntwo\rthree\n\n01/10/26, 10:01 - Bob: four\r\n");
  assert.deepEqual(mixed.map((message) => [message.text, message.end_line]), [["one\ntwo\nthree\n", 3], ["four", 4]]);
  assert.deepEqual(parseChat(""), []);
});

test("a header is read with the whitespace the command reads it with", () => {
  // Python's \s knows U+001C to U+001F and U+0085 and JavaScript's does not: both must cut a chat into the same messages.
  for (const code of [0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0, 0x202f, 0x3000]) {
    const space = character(code);
    const android = parseChat(`04/10/26,${space}09:10${space}-${space}Ana:${space}Hola`);
    assert.deepEqual(android.map((message) => [message.timestamp, message.sender, message.text]), [[`04/10/26,${space}09:10`, "Ana", "Hola"]], code.toString(16));
    const ios = parseChat(`[04/10/26,${space}9:10${space}p.${space}m.]${space}Ana:${space}Hola`);
    assert.deepEqual(ios.map((message) => [message.timestamp, message.sender, message.text]), [[`04/10/26,${space}9:10${space}p.${space}m.`, "Ana", "Hola"]], code.toString(16));
  }
  // A byte order mark is taken out of a line before it is read, so it never stands for a space.
  const mark = character(0xfeff);
  assert.deepEqual(parseChat(`${mark}04/10/26, 09:10 - Ana:${mark} Hola`).map((message) => [message.sender, message.text]), [["Ana", "Hola"]]);
  assert.deepEqual(parseChat(`04/10/26,${mark}09:10 - Ana: Hola`), []);
  assert.deepEqual(parseChat(`04/10/26, 09:10 - Ana:${mark}Hola`).map((message) => message.sender), [null]);
});

test("annotated log keeps line separators and form feeds inside messages", () => {
  const text = `01/10/26, 10:00 - Ana: first.opus one${character(0x2028)}two\n01/10/26, 10:01 - Bob: second.opus x${character(0x0c)}y`;
  const index = indexMessages([entry("chat.txt", text), entry("first.opus"), entry("second.opus")]);
  // Neither character ends a line, so each transcript follows its whole message.
  assert.equal(annotatedChat(index.chats[0], { "first.opus": { status: "ok", text: "uno" }, "second.opus": { status: "ok", text: "dos" } }),
    `${text.replace("\n", "\n[Voice message transcript: uno]\n")}\n[Voice message transcript: dos]\n`);
});

test("annotated log replaces the transcripts a message already ends with", () => {
  // The annotated copy of an earlier run, given as the chat.
  const text = "01/10/26, 10:00 - Ana: first.opus (attached)\r\n"
    + "[Voice message transcript: an earlier transcript]\r\n"
    + "01/10/26, 10:01 - Bob: second.opus (attached)\r\n"
    + "A caption [Voice message transcript: typed by hand]\r\n"
    + "[Voice message transcript: Error: the decoder said\n"
    + "two lines]\r\n"
    + "01/10/26, 10:02 - Ana: third.opus (attached)\r\n"
    + "[Voice message transcript: stays until this recording is reached]\r\n"
    + "01/10/26, 10:03 - Bob: fourth.opus (attached)\r\n"
    + "[Voice message transcript: not at the end of the message]\r\n"
    + "because this line follows it\r\n"
    + "01/10/26, 10:04 - Ana: [Voice message transcript: only quoted]\r\n"
    + "01/10/26, 10:05 - Bob: fifth.opus (attached)\r\n"
    + `${character(0x200e)}[Voice message transcript: after a mark that is not shown]`;
  const recordings = ["first", "second", "third", "fourth", "fifth"].map((name) => entry(`${name}.opus`));
  const results = {
    "first.opus": { status: "ok", text: "uno" }, "second.opus": { status: "ok", text: "" }, "third.opus": { status: "pending" },
    "fourth.opus": { status: "ok", text: "cuatro" }, "fifth.opus": { status: "error", error: "Out of memory" },
  };
  const annotated = annotatedChat(indexMessages([entry("chat.txt", text), ...recordings]).chats[0], results);
  assert.equal(annotated, "01/10/26, 10:00 - Ana: first.opus (attached)\r\n"
    + "[Voice message transcript: uno]\r\n"
    + "01/10/26, 10:01 - Bob: second.opus (attached)\r\n"
    + "A caption [Voice message transcript: typed by hand]\r\n"
    + "[Voice message transcript: No speech detected]\r\n"
    + "01/10/26, 10:02 - Ana: third.opus (attached)\r\n"
    + "[Voice message transcript: stays until this recording is reached]\r\n"
    + "01/10/26, 10:03 - Bob: fourth.opus (attached)\r\n"
    + "[Voice message transcript: not at the end of the message]\r\n"
    + "because this line follows it\r\n"
    + "[Voice message transcript: cuatro]\r\n"
    + "01/10/26, 10:04 - Ana: [Voice message transcript: only quoted]\r\n"
    + "01/10/26, 10:05 - Bob: fifth.opus (attached)\r\n"
    + "[Voice message transcript: Error: Out of memory]\r\n");
  // Writing the same results over the copy changes nothing: no transcript appears twice.
  assert.equal(annotatedChat(indexMessages([entry("chat.txt", annotated), ...recordings]).chats[0], results), annotated);
  // Without a result for it a message keeps the transcript it has.
  assert.equal(annotatedChat(indexMessages([entry("chat.txt", annotated), ...recordings]).chats[0], {}), annotated);
  // Every transcript at the end of a message goes, and the line the message starts with never does.
  const two = "01/10/26, 10:00 - Ana: first.opus second.opus\n[Voice message transcript: an earlier one]\n[Voice message transcript: and\nanother]\n"
    + "01/10/26, 10:01 - Bob: [Voice message transcript: third.opus]\n";
  assert.equal(annotatedChat(indexMessages([entry("chat.txt", two), ...recordings]).chats[0], { ...results, "third.opus": { status: "ok", text: "tres" } }),
    "01/10/26, 10:00 - Ana: first.opus second.opus\n[Voice message transcript: uno]\n[Voice message transcript: No speech detected]\n"
    + "01/10/26, 10:01 - Bob: [Voice message transcript: third.opus]\n[Voice message transcript: tres]\n");
});

test("report records the size of each recording and keeps the imported results whose recording is not loaded", () => {
  const index = indexMessages([entry("chat.txt", "04/10/26, 10:00 - José: voice.opus\n"), entry("voice.opus", "doce bytés!")]);
  const imported = [
    { file: "media/elsewhere.opus", status: "ok", text: "Otra", bytes: 7, messages: [{ chat_file: "chat.txt", timestamp: "04/10/26, 10:02", sender: "Ana" }],
      languages: ["es"], segments: [{ start: 0, end: 1, text: "Otra" }], model: "Whisper small" },
    { file: "failed.opus", status: "error", text: "", error: "Could not decode audio" },
  ];
  const results = [{ path: "voice.opus", status: "ok", text: "Hola" }];
  const report = createReport(index, results, "export.zip", imported);
  // Bytes, not characters: a later run compares them with the size of the file.
  assert.deepEqual(report.results.map((result) => [result.file, result.bytes]), [["voice.opus", 12], ["media/elsewhere.opus", 7], ["failed.opus", undefined]]);
  assert.deepEqual(Object.keys(report.results[0]), ["file", "bytes", "messages", "status", "text", "languages", "segments"]);
  assert.deepEqual(report.results.slice(1), imported);
  assert.deepEqual(reportResults(index, results, imported), report.results);
  assert.deepEqual(JSON.parse(JSON.stringify(report)), report);
  report.results[1].segments[0].text = "Changed";
  report.results[1].messages[0].sender = "Changed";
  assert.deepEqual([imported[0].segments[0].text, imported[0].messages[0].sender], ["Otra", "Ana"]);
  // A recording that is not transcribed yet has its size too, and without imported results the report lists the recordings only.
  assert.deepEqual(createReport(index, [], "export.zip").results.map((result) => [result.file, result.bytes, result.status]), [["voice.opus", 12, "pending"]]);
  assert.deepEqual(reportResults(index, new Map()), createReport(index, [], "export.zip").results);
});
