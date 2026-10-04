/** Read selected WhatsApp exports without expanding unrelated archive media. */
import { AUDIO_EXTENSIONS } from "./chat.js";

// Chat text and recordings are held in memory; other archive media is never read.
export const MAX_EXTRACTED_BYTES = 512 * 1024 * 1024;
const MAX_DIRECTORY_BYTES = 64 * 1024 * 1024;
// The end record is 22 bytes plus a comment of at most 65535.
const END_SEARCH_BYTES = 65557;
const INFLATE_CHUNK_BYTES = 8 * 1024;
const UTF8 = new TextDecoder("utf-8", { fatal: true });
const CP437 = "ÇüéâäàåçêëèïîìÄÅÉæÆôöòûùÿÖÜ¢£¥₧ƒáíóúñÑªº¿⌐¬½¼¡«»░▒▓│┤╡╢╖╕╣║╗╝╜╛┐└┴┬├─┼╞╟╚╔╩╦╠═╬╧╨╤╥╙╘╒╓╫╪┘┌█▄▌▐▀αßΓπΣσµτΦΘΩδ∞φε∩≡±≥≤⌠⌡÷≈°∙·√ⁿ²■ ";
const CRC_TABLE = Uint32Array.from({ length: 256 }, (_, initial) => {
  let value = initial;
  for (let bit = 0; bit < 8; bit += 1) value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
  return value >>> 0;
});

const extension = (path) => path.slice(path.lastIndexOf(".")).toLowerCase();
const supported = (path) => extension(path) === ".txt" || AUDIO_EXTENSIONS.has(extension(path));
const order = (left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0);

function normalizePath(name) {
  const path = name.replaceAll("\\", "/").normalize("NFC");
  const parts = path.split("/");
  if (!path || path.startsWith("/") || /[\u0000-\u001f\u007f]/u.test(path)
      || parts.includes("..") || parts.some((part) => part.includes(":"))) {
    throw new Error(`Unsafe archive path: ${name}`);
  }
  const normalized = parts.filter((part) => part && part !== ".").join("/");
  if (!normalized) throw new Error(`Unsafe archive path: ${name}`);
  return normalized;
}

function decodeName(bytes, flags) {
  if (flags & 0x800) {
    try {
      return UTF8.decode(bytes);
    } catch {
      throw new Error("ZIP contains an invalid UTF-8 filename.");
    }
  }
  return [...bytes].map((byte) => byte < 128 ? String.fromCharCode(byte) : CP437[byte - 128]).join("");
}

/** Read one byte range of the file, so a large archive never has to fit in memory. */
async function readRange(file, start, end) {
  const bytes = new Uint8Array(await file.slice(start, end).arrayBuffer());
  if (bytes.length !== end - start) throw new Error("ZIP size changed while reading.");
  return bytes;
}

function fields(bytes) {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const requireBytes = (offset, length) => {
    if (offset < 0 || offset + length > bytes.length) throw new Error("ZIP has truncated or invalid headers.");
  };
  return {
    requireBytes,
    u16: (offset) => { requireBytes(offset, 2); return view.getUint16(offset, true); },
    u32: (offset) => { requireBytes(offset, 4); return view.getUint32(offset, true); },
  };
}

/**
 * Validate the central directory, and the local headers of chat and audio
 * members, before reading or allocating any member data.
 */
async function inspectZip(file) {
  const tailStart = Math.max(0, file.size - END_SEARCH_BYTES);
  const tail = await readRange(file, tailStart, file.size);
  let { requireBytes, u16, u32 } = fields(tail);
  let end = -1;
  for (let offset = tail.length - 22; offset >= 0; offset -= 1) {
    if (u32(offset) === 0x06054b50 && offset + 22 + u16(offset + 20) === tail.length) {
      end = offset;
      break;
    }
  }
  if (end < 0) throw new Error("This is not a complete ZIP archive.");
  if (u16(end + 4) || u16(end + 6) || u16(end + 8) !== u16(end + 10)) {
    throw new Error("Split ZIP archives are unsupported. Choose a complete export ZIP.");
  }
  const count = u16(end + 10);
  const centralSize = u32(end + 12);
  const centralStart = u32(end + 16);
  if (count === 0xffff || centralSize === 0xffffffff || centralStart === 0xffffffff) {
    throw new Error("ZIP64 archives are unsupported. Select the extracted chat and audio files instead.");
  }
  if (centralStart + centralSize !== tailStart + end) throw new Error("ZIP has an invalid central directory.");
  if (centralSize > MAX_DIRECTORY_BYTES) throw new Error("ZIP has too many entries to read in the browser.");
  const central = await readRange(file, centralStart, centralStart + centralSize);
  ({ requireBytes, u16, u32 } = fields(central));

  const seen = new Set();
  const members = [];
  const intervals = [];
  let offset = 0;
  let retainedSize = 0;
  for (let index = 0; index < count; index += 1) {
    requireBytes(offset, 46);
    if (u32(offset) !== 0x02014b50) throw new Error("ZIP has an invalid central-directory entry.");
    const flags = u16(offset + 8);
    const method = u16(offset + 10);
    const crc = u32(offset + 16);
    const compressedSize = u32(offset + 20);
    const originalSize = u32(offset + 24);
    const nameLength = u16(offset + 28);
    const extraLength = u16(offset + 30);
    const commentLength = u16(offset + 32);
    const attributes = u32(offset + 38);
    const localOffset = u32(offset + 42);
    const recordEnd = offset + 46 + nameLength + extraLength + commentLength;
    if (recordEnd > centralSize) throw new Error("ZIP has truncated entry metadata.");
    const name = decodeName(central.subarray(offset + 46, offset + 46 + nameLength), flags);
    const path = normalizePath(name);
    if (seen.has(path)) throw new Error(`Duplicate archive path: ${path}`);
    seen.add(path);
    if (((attributes >>> 16) & 0xf000) === 0xa000) throw new Error(`ZIP contains a symbolic link: ${path}`);
    if (u16(offset + 34)) throw new Error("Split ZIP members are unsupported.");
    if (compressedSize === 0xffffffff || originalSize === 0xffffffff || localOffset === 0xffffffff) {
      throw new Error("ZIP64 members are unsupported.");
    }
    const directory = name.endsWith("/") || name.endsWith("\\") || Boolean(attributes & 0x10);
    const keep = !directory && supported(path);
    if (keep) {
      if (flags & (1 | 64)) throw new Error(`Encrypted ZIP member is unsupported: ${path}`);
      if (method !== 0 && method !== 8) throw new Error(`Unsupported ZIP compression for ${path}.`);
      retainedSize += originalSize;
      if (retainedSize > MAX_EXTRACTED_BYTES) throw new Error("ZIP chat and audio exceed the 512 MiB extracted limit.");
      if (method === 0 && compressedSize !== originalSize) throw new Error(`Invalid stored ZIP member size: ${path}`);
    }

    // Skipped media is bounded from the directory alone; its bytes are never read.
    const minimumEnd = localOffset + 30 + nameLength + compressedSize;
    if (minimumEnd > centralStart) throw new Error(`ZIP member extends outside its data region: ${path}`);
    if (keep) members.push({ path, flags, method, crc, originalSize, compressedSize, localOffset });
    else intervals.push({ start: localOffset, end: minimumEnd });
    offset = recordEnd;
  }
  if (offset !== centralSize) throw new Error("ZIP central-directory length is inconsistent.");

  for (const member of members) {
    const { path, flags, localOffset } = member;
    const local = fields(await readRange(file, localOffset, localOffset + 30));
    if (local.u32(0) !== 0x04034b50) throw new Error("ZIP has an invalid local-file header.");
    if (local.u16(6) !== flags || local.u16(8) !== member.method) {
      throw new Error(`ZIP local and central headers disagree: ${path}`);
    }
    const localNameLength = local.u16(26);
    member.dataStart = localOffset + 30 + localNameLength + local.u16(28);
    member.dataEnd = member.dataStart + member.compressedSize;
    if (member.dataEnd > centralStart) throw new Error(`ZIP member extends outside its data region: ${path}`);
    const localName = decodeName(await readRange(file, localOffset + 30, localOffset + 30 + localNameLength), flags);
    if (normalizePath(localName) !== path) throw new Error(`ZIP local and central filenames disagree: ${path}`);
    if (!(flags & 8) && (local.u32(14) !== member.crc
        || local.u32(18) !== member.compressedSize || local.u32(22) !== member.originalSize)) {
      throw new Error(`ZIP local and central sizes/checksums disagree: ${path}`);
    }
    intervals.push({ start: localOffset, end: member.dataEnd });
  }
  intervals.sort((a, b) => a.start - b.start);
  for (let index = 1; index < intervals.length; index += 1) {
    if (intervals[index].start < intervals[index - 1].end) throw new Error("ZIP contains overlapping members.");
  }
  return members.sort(order);
}

async function crc32(bytes) {
  let crc = 0xffffffff;
  for (let start = 0; start < bytes.length; start += 4 * 1024 * 1024) {
    const end = Math.min(bytes.length, start + 4 * 1024 * 1024);
    for (let index = start; index < end; index += 1) crc = CRC_TABLE[(crc ^ bytes[index]) & 255] ^ (crc >>> 8);
    if (end < bytes.length) await new Promise((resolve) => setTimeout(resolve, 0));
  }
  return (crc ^ 0xffffffff) >>> 0;
}

async function inflateMember(compressed, member, AsyncInflate) {
  // The aggregate advertised size was checked before this fixed allocation.
  const output = new Uint8Array(member.originalSize);
  let written = 0;
  let pending;
  const inflater = new AsyncInflate((error, chunk, final) => {
    if (!pending) return;
    const { resolve, reject } = pending;
    pending = null;
    if (error) { reject(error); return; }
    if (written + chunk.length > member.originalSize || written + chunk.length > MAX_EXTRACTED_BYTES) {
      reject(new Error(`ZIP member exceeds its advertised extracted size: ${member.path}`));
      return;
    }
    output.set(chunk, written);
    written += chunk.length;
    resolve(final);
  });
  try {
    let final = false;
    let position = 0;
    do {
      const next = Math.min(position + INFLATE_CHUNK_BYTES, compressed.length);
      final = await new Promise((resolve, reject) => {
        pending = { resolve, reject };
        // AsyncInflate transfers its input. Copy only this bounded chunk so the
        // rest of the member remains readable, and await it before pushing more.
        inflater.push(compressed.slice(position, next), next === compressed.length);
      });
      position = next;
    } while (position < compressed.length);
    if (!final || written !== member.originalSize) throw new Error(`ZIP member has an incorrect extracted size: ${member.path}`);
    return output;
  } finally {
    inflater.terminate();
  }
}

function checkedFile(file) {
  if (!file || typeof file.name !== "string" || typeof file.arrayBuffer !== "function"
      || !Number.isSafeInteger(file.size) || file.size < 0) throw new TypeError("Choose valid files from your device.");
  return file;
}

async function readDirect(files) {
  const seen = new Set();
  const selected = [];
  let retainedSize = 0;
  for (const file of files) {
    checkedFile(file);
    const path = normalizePath(file.webkitRelativePath || file.name);
    if (seen.has(path)) throw new Error(`Duplicate selected path: ${path}`);
    seen.add(path);
    if (!supported(path)) continue;
    retainedSize += file.size;
    if (retainedSize > MAX_EXTRACTED_BYTES) throw new Error("Selected chat and audio files exceed the 512 MiB limit.");
    selected.push({ path, file });
  }
  const entries = [];
  for (const { path, file } of selected) {
    const data = new Uint8Array(await file.arrayBuffer());
    if (data.length !== file.size) throw new Error(`File size changed while reading: ${path}`);
    entries.push({ path, data });
  }
  return entries.sort(order);
}

/** Read one ZIP, chat text, or recording. Only audio and .txt entries are retained. */
export async function readExport(file) {
  checkedFile(file);
  if (extension(file.name) !== ".zip") return readDirect([file]);
  if (typeof file.slice !== "function") throw new TypeError("Choose valid files from your device.");
  const members = await inspectZip(file);
  let AsyncInflate;
  if (members.some((member) => member.method === 8)) ({ AsyncInflate } = await import("fflate"));
  const entries = [];
  for (const member of members) {
    const compressed = await readRange(file, member.dataStart, member.dataEnd);
    const data = member.method === 0 ? compressed : await inflateMember(compressed, member, AsyncInflate);
    if (data.length !== member.originalSize || await crc32(data) !== member.crc) {
      throw new Error(`ZIP member failed its size/checksum check: ${member.path}`);
    }
    entries.push({ path: member.path, data });
  }
  return entries;
}

/** Read a ZIP alone, or a selection of extracted chat texts and recordings. */
export async function readFiles(files) {
  const selected = Array.from(files || []);
  if (!selected.length) throw new Error("Choose a WhatsApp ZIP, chat text, or audio file.");
  selected.forEach(checkedFile);
  if (selected.some((file) => extension(file.name) === ".zip")) {
    if (selected.length !== 1) throw new Error("Choose one ZIP alone, or select the extracted chat and audio files together.");
    return readExport(selected[0]);
  }
  return readDirect(selected);
}
