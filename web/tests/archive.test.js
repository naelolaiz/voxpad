import assert from "node:assert/strict";
import test from "node:test";
import { deflateRawSync } from "node:zlib";

import { MAX_EXTRACTED_BYTES, MAX_INPUT_BYTES, readExport, readFiles } from "../src/archive.js";

const encode = (text) => new TextEncoder().encode(text);
const decode = (bytes) => new TextDecoder().decode(bytes);

function file(name, data, relative = "") {
  const bytes = typeof data === "string" ? encode(data) : data;
  return { name, size: bytes.length, webkitRelativePath: relative, arrayBuffer: async () => bytes.slice().buffer };
}

function checksum(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/** Small independent ZIP fixtures, including deliberately inconsistent headers. */
function zip(entries) {
  const local = [];
  const central = [];
  let localSize = 0;
  for (const entry of entries) {
    const name = entry.nameBytes || encode(entry.name);
    const localName = entry.localName ? encode(entry.localName) : name;
    const payload = typeof entry.data === "string" ? encode(entry.data) : entry.data || new Uint8Array();
    const method = entry.method || 0;
    const compressed = entry.compressed || (method === 8 ? new Uint8Array(deflateRawSync(payload)) : payload);
    const size = entry.size ?? payload.length;
    const crc = entry.crc ?? checksum(payload);
    const flags = entry.flags ?? 0x800;
    const header = new Uint8Array(30 + localName.length + compressed.length);
    const localView = new DataView(header.buffer);
    localView.setUint32(0, 0x04034b50, true);
    localView.setUint16(4, 20, true);
    localView.setUint16(6, flags, true);
    localView.setUint16(8, method, true);
    localView.setUint32(14, crc, true);
    localView.setUint32(18, compressed.length, true);
    localView.setUint32(22, entry.localSize ?? size, true);
    localView.setUint16(26, localName.length, true);
    header.set(localName, 30);
    header.set(compressed, 30 + localName.length);
    local.push(header);

    const record = new Uint8Array(46 + name.length);
    const view = new DataView(record.buffer);
    view.setUint32(0, 0x02014b50, true);
    view.setUint16(4, 0x314, true);
    view.setUint16(6, 20, true);
    view.setUint16(8, flags, true);
    view.setUint16(10, method, true);
    view.setUint32(16, crc, true);
    view.setUint32(20, compressed.length, true);
    view.setUint32(24, size, true);
    view.setUint16(28, name.length, true);
    view.setUint32(38, entry.attributes || 0, true);
    view.setUint32(42, localSize, true);
    record.set(name, 46);
    central.push(record);
    localSize += header.length;
  }
  const centralSize = central.reduce((total, bytes) => total + bytes.length, 0);
  const end = new Uint8Array(22);
  const view = new DataView(end.buffer);
  view.setUint32(0, 0x06054b50, true);
  view.setUint16(8, entries.length, true);
  view.setUint16(10, entries.length, true);
  view.setUint32(12, centralSize, true);
  view.setUint32(16, localSize, true);
  const result = new Uint8Array(localSize + centralSize + 22);
  let offset = 0;
  for (const bytes of [...local, ...central, end]) { result.set(bytes, offset); offset += bytes.length; }
  return result;
}

test("stored ZIP reads only audio/text and preserves Unicode paths and UTF-8 bytes", async () => {
  const text = "\uFEFF[04/10/26, 10:00] José: café.opus\r\n";
  const entries = await readExport(file("chat.ZIP", zip([
    { name: "Chat/_chat.txt", data: text },
    { name: "Chat/media/cafe\u0301.opus", data: "123456789", crc: 0xcbf43926 },
    { name: "Chat/photo.jpg", data: "image" },
    { name: "Chat/video.mp4", data: "video" },
  ])));
  assert.deepEqual(entries.map((entry) => entry.path), ["Chat/_chat.txt", "Chat/media/café.opus"]);
  assert.deepEqual(entries[0].data, encode(text));
  assert.equal(decode(entries[1].data), "123456789");
});

test("deflated ZIP decodes asynchronously and verifies complete member content", async () => {
  const content = "Hola José\n".repeat(5000);
  const entries = await readExport(file("chat.zip", zip([{ name: "chat.txt", data: content, method: 8 }])));
  assert.equal(entries.length, 1);
  assert.equal(decode(entries[0].data), content);
});

test("all unsafe paths are rejected, including paths on ignored images", async () => {
  for (const name of ["../voice.opus", "a/../../voice.opus", "/voice.opus", "..\\voice.opus", "C:\\voice.opus", "\\\\server\\voice.opus", "a/voice.opus:secret", "../ignored.jpg"]) {
    await assert.rejects(readExport(file("chat.zip", zip([{ name, data: "audio" }]))), /Unsafe archive path/u);
  }
});

test("ZIP symlinks and duplicate normalized Unicode/path names are rejected", async () => {
  await assert.rejects(readExport(file("chat.zip", zip([{ name: "voice.opus", data: "../outside", attributes: (0xa1ff << 16) >>> 0 }]))), /symbolic link/u);
  for (const names of [["voice.opus", "voice.opus"], ["media//voice.opus", "media/voice.opus"], ["café.opus", "cafe\u0301.opus"]]) {
    await assert.rejects(readExport(file("chat.zip", zip(names.map((name) => ({ name, data: "audio" }))))), /Duplicate archive path/u);
  }
});

test("advertised extracted limits are checked using tiny fixtures before inflation", async () => {
  const bomb = zip([{ name: "voice.opus", method: 8, compressed: new Uint8Array([255]), size: MAX_EXTRACTED_BYTES + 1 }]);
  await assert.rejects(readExport(file("bomb.zip", bomb)), /512 MiB extracted limit/u);
  const aggregate = zip([
    { name: "one.opus", method: 8, compressed: new Uint8Array([255]), size: MAX_EXTRACTED_BYTES / 2 },
    { name: "two.opus", method: 8, compressed: new Uint8Array([255]), size: MAX_EXTRACTED_BYTES / 2 + 1 },
  ]);
  await assert.rejects(readExport(file("bomb.zip", aggregate)), /512 MiB extracted limit/u);
});

test("unsupported large or broken video/photo payloads never inflate", async () => {
  const entries = await readExport(file("chat.zip", zip([
    { name: "chat.txt", data: "chat" },
    { name: "huge.mp4", method: 8, compressed: new Uint8Array([255]), size: MAX_EXTRACTED_BYTES + 1 },
  ])));
  assert.deepEqual(entries.map((entry) => entry.path), ["chat.txt"]);
});

test("lying DEFLATE sizes reject actual excess output rather than silently truncating", async () => {
  const bytes = zip([{ name: "voice.opus", data: "a".repeat(9000), method: 8, size: 1 }]);
  await assert.rejects(readExport(file("chat.zip", bytes)), /exceeds its advertised extracted size/u);
});

test("corrupt data and disagreeing local headers fail clearly", async () => {
  await assert.rejects(readExport(file("chat.zip", zip([{ name: "voice.opus", data: "audio", crc: 0 }]))), /checksum/u);
  await assert.rejects(readExport(file("chat.zip", zip([{ name: "voice.opus", data: "audio", localName: "other.opus" }]))), /filenames disagree/u);
  await assert.rejects(readExport(file("chat.zip", zip([{ name: "voice.opus", data: "audio", localSize: 2 }]))), /sizes\/checksums disagree/u);
  await assert.rejects(readExport(file("chat.zip", new Uint8Array([0, 1, 2]))), /complete ZIP/u);
});

test("malformed UTF-8 ZIP filenames and ZIP64 headers reject before decoding", async () => {
  await assert.rejects(readExport(file("chat.zip", zip([{ nameBytes: new Uint8Array([255]), data: "audio" }]))), /invalid UTF-8 filename/u);
  const bytes = zip([{ name: "voice.opus", data: "audio" }]);
  const view = new DataView(bytes.buffer);
  view.setUint16(bytes.length - 14, 0xffff, true);
  view.setUint16(bytes.length - 12, 0xffff, true);
  await assert.rejects(readExport(file("chat.zip", bytes)), /ZIP64/u);
});

test("selection size limits are checked before reading file buffers", async () => {
  const oversized = { name: "chat.zip", size: MAX_INPUT_BYTES + 1, arrayBuffer() { throw new Error("Must not read oversized file"); } };
  await assert.rejects(readExport(oversized), /100 MiB input limit/u);
  let reads = 0;
  const files = ["one.opus", "two.opus"].map((name) => ({ name, size: MAX_INPUT_BYTES / 2 + 1, async arrayBuffer() { reads += 1; return new ArrayBuffer(0); } }));
  await assert.rejects(readFiles(files), /100 MiB input limit/u);
  assert.equal(reads, 0);
});

test("multiple extracted files preserve folder context and skip unsupported media", async () => {
  const entries = await readFiles([
    file("voice.opus", "audio", "Chat/media/voice.opus"),
    file("chat.txt", "José", "Chat/chat.txt"),
    { name: "photo.jpg", size: 1, arrayBuffer() { throw new Error("Must not read photo"); } },
  ]);
  assert.deepEqual(entries.map((entry) => entry.path), ["Chat/chat.txt", "Chat/media/voice.opus"]);
  assert.equal(decode(entries[0].data), "José");
  assert.deepEqual(await readExport(file("voice.opus", "audio")), [{ path: "voice.opus", data: encode("audio") }]);
});

test("direct selections reject duplicates, mixed ZIP uploads, empty selection and changed sizes", async () => {
  await assert.rejects(readFiles([file("café.opus", "one"), file("cafe\u0301.opus", "two")]), /Duplicate selected path/u);
  await assert.rejects(readFiles([file("chat.zip", zip([])), file("voice.opus", "audio")]), /one ZIP alone/u);
  await assert.rejects(readFiles([]), /Choose a WhatsApp/u);
  await assert.rejects(readExport({ name: "voice.opus", size: 1, arrayBuffer: async () => new ArrayBuffer(2) }), /size changed/u);
});
