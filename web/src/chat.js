/** WhatsApp export indexing and lossless text reports; no browser or model required. */

export const AUDIO_EXTENSIONS = new Set([
  ".opus", ".ogg", ".oga", ".m4a", ".aac", ".mp3", ".wav", ".flac", ".amr", ".aif", ".aiff",
]);

const INVISIBLE = /[\uFEFF\u200E\u200F\u202A-\u202E\u2066-\u2069]/gu;
const DATE = String.raw`\p{Nd}{1,4}[./-]\p{Nd}{1,2}[./-]\p{Nd}{1,4}`;
const PERIOD = String.raw`(?:[aApP]\.?\s*[mM]\.?|[صم]|上午|下午|午前|午後)`;
const CLOCK = String.raw`\p{Nd}{1,2}[:：]\p{Nd}{2}(?:[:：]\p{Nd}{2})?`;
const TIME = String.raw`(?:${PERIOD}\s*)?${CLOCK}(?:\s*${PERIOD})?`;
const HEADER = new RegExp(
  String.raw`^(?:\[(?<ios>${DATE}[,،]?\s*${TIME})\]\s*|(?<android>${DATE}[,،]?\s+${TIME})\s+-\s+)(?<body>.*)$`,
  "u",
);

const order = (left, right) => (left < right ? -1 : left > right ? 1 : 0);
const basename = (path) => path.slice(path.lastIndexOf("/") + 1);
const parent = (path) => path.slice(0, Math.max(0, path.lastIndexOf("/")));
const extension = (name) => name.slice(name.lastIndexOf(".")).toLowerCase();
const escapeRegex = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function normalizePath(value) {
  if (typeof value !== "string" || !value) throw new TypeError("An entry must have a nonempty path.");
  const path = value.replaceAll("\\", "/").normalize("NFC");
  const parts = path.split("/");
  if (path.startsWith("/") || /^[a-z]:/iu.test(path) || parts.includes("..")) {
    throw new Error(`Unsafe entry path: ${value}`);
  }
  const normalized = parts.filter((part) => part && part !== ".").join("/");
  if (!normalized) throw new Error(`Empty entry path: ${value}`);
  return normalized;
}

/**
 * Return messages in their original order. Lines are zero-based; offsets refer to
 * the original JavaScript string, including its unchanged line endings.
 */
export function parseChat(text) {
  if (typeof text !== "string") throw new TypeError("Chat text must be a string.");
  const messages = [];
  let lineNumber = 0;
  for (const line of text.matchAll(/([^\r\n]*)(\r\n|\r|\n|$)/g)) {
    if (!line[0]) break;
    const clean = line[1].replace(INVISIBLE, "");
    const match = HEADER.exec(clean);
    if (match) {
      const body = match.groups.body;
      const separator = /[:：]\s/u.exec(body);
      messages.push({
        timestamp: match.groups.ios || match.groups.android,
        sender: separator ? body.slice(0, separator.index) : null,
        text: separator ? body.slice(separator.index + separator[0].length) : body,
        raw: line[0],
        start_line: lineNumber,
        end_line: lineNumber,
        start_offset: line.index,
        end_offset: line.index + line[0].length,
        audio_paths: [],
      });
    } else if (messages.length) {
      const message = messages[messages.length - 1];
      message.text += `\n${clean}`;
      message.raw += line[0];
      message.end_line = lineNumber;
      message.end_offset = line.index + line[0].length;
    }
    lineNumber += 1;
  }
  return messages;
}

/**
 * Index {path, data: Uint8Array} entries. Attachment labels may be in any
 * language: association uses exported filenames, with a same-folder preference.
 */
export function indexMessages(entries) {
  const warnings = [];
  const seen = new Set();
  const files = entries.map((entry) => {
    const path = normalizePath(entry.path);
    if (seen.has(path)) throw new Error(`Duplicate entry path: ${path}`);
    seen.add(path);
    if (!(entry.data instanceof Uint8Array)) throw new TypeError(`Entry data must be a Uint8Array: ${path}`);
    return { path, name: basename(path), data: entry.data };
  }).sort((left, right) => order(left.path, right.path));

  const audio = files.filter((file) => AUDIO_EXTENSIONS.has(extension(file.name)))
    .map((file) => ({ ...file, messages: [], occurrences: [] }));
  const chats = [];
  for (const file of files.filter((file) => extension(file.name) === ".txt")) {
    let text;
    try {
      // Preserve a leading BOM in the original text; header parsing removes it.
      text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(file.data);
    } catch {
      warnings.push(`Skipping non-UTF-8 chat text: ${file.path}`);
      continue;
    }
    const messages = parseChat(text);
    if (messages.length || file.name.toLowerCase() === "_chat.txt") {
      chats.push({ path: file.path, name: file.name, text, messages });
    }
  }

  const byName = new Map();
  for (const recording of audio) {
    const name = recording.name.normalize("NFC");
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push(recording);
  }
  if (!byName.size) return { chats, audio, warnings };
  const alternatives = [...byName.keys()].sort((a, b) => b.length - a.length || order(a, b)).map(escapeRegex);
  const filenames = new RegExp(
    String.raw`(?<![\p{L}\p{N}\p{M}_.-])(?:${alternatives.join("|")})(?![\p{L}\p{N}\p{M}_.-])`,
    "gu",
  );
  for (const chat of chats) {
    chat.messages.forEach((message, messageIndex) => {
      const names = new Set(message.text.normalize("NFC").match(filenames) || []);
      for (const name of [...names].sort(order)) {
        const candidates = byName.get(name);
        const nearby = candidates.filter((recording) => parent(recording.path) === parent(chat.path));
        const selected = nearby.length ? nearby : candidates;
        if (selected.length !== 1) {
          warnings.push(`Ambiguous attachment ${JSON.stringify(name)} in ${chat.path}; metadata left unset.`);
          continue;
        }
        const recording = selected[0];
        message.audio_paths.push(recording.path);
        recording.messages.push({ chat_file: chat.path, timestamp: message.timestamp, sender: message.sender });
        recording.occurrences.push({
          chat_file: chat.path,
          message_index: messageIndex,
          end_line: message.end_line,
          end_offset: message.end_offset,
        });
      }
    });
  }
  return { chats, audio, warnings };
}

function resultMap(results) {
  if (results instanceof Map) return results;
  if (Array.isArray(results)) return new Map(results.map((result) => [result.path ?? result.file, result]));
  return new Map(Object.entries(results || {}));
}

function resultStatus(result) {
  if (!result) return "pending";
  return result.status || (result.error ? "error" : typeof result.text === "string" ? "ok" : "pending");
}

/** Insert completed transcripts after full message bodies; original text stays intact. */
export function annotatedChat(chat, audioResults) {
  const results = resultMap(audioResults);
  const newline = chat.text.match(/\r\n|\r|\n/u)?.[0] || "\n";
  let annotated = "";
  let cursor = 0;
  for (const message of chat.messages) {
    const additions = [];
    for (const path of message.audio_paths || []) {
      const result = results.get(path);
      const status = resultStatus(result);
      if (status !== "ok" && status !== "error") continue;
      const text = result.text || (status === "error" ? `Error: ${result.error || "Transcription failed"}` : "No speech detected");
      additions.push(`[Voice message transcript: ${text}]${newline}`);
    }
    if (!additions.length) continue;
    const original = chat.text.slice(cursor, message.end_offset);
    annotated += original;
    if (!original.endsWith("\n") && !original.endsWith("\r")) annotated += newline;
    annotated += additions.join("");
    cursor = message.end_offset;
  }
  return annotated + chat.text.slice(cursor);
}

/**
 * Build a JSON-compatible report with every recording, ordered chat messages and
 * exact original text. Missing results remain pending for later transcription.
 */
export function createReport(index, audioResults, sourceName) {
  const results = resultMap(audioResults);
  return {
    schema_version: 1,
    source: sourceName || "WhatsApp export",
    sample_rate: 16000,
    chats: index.chats.map((chat) => ({
      file: chat.path,
      original_text: chat.text,
      annotated_text: annotatedChat(chat, results),
      messages: chat.messages.map((message) => ({ ...message, audio_paths: [...message.audio_paths] })),
    })),
    results: index.audio.map((recording) => {
      const result = results.get(recording.path);
      const entry = {
        file: recording.path,
        messages: recording.messages.map((message) => ({ ...message })),
        status: resultStatus(result),
        text: result?.text || "",
        languages: [...(result?.languages || [])],
        segments: (result?.segments || []).map((segment) => ({ ...segment })),
      };
      if (result?.error != null) entry.error = result.error;
      if (result?.duration_seconds != null) entry.duration_seconds = result.duration_seconds;
      if (result?.words) entry.words = result.words.map((word) => ({ ...word }));
      if (result?.model) entry.model = result.model;
      return entry;
    }),
    warnings: [...index.warnings],
  };
}
