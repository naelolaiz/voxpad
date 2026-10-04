import assert from "node:assert/strict";
import test from "node:test";

import { annotatedChat, createReport, indexMessages, parseChat } from "../src/chat.js";

const encode = (text) => new TextEncoder().encode(text);
const entry = (path, text = "audio") => ({ path, data: encode(text) });

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
