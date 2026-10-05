/**
 * Read the reports of earlier runs and decide which of their transcripts an export can reuse: the
 * twin of load_previous_results, merged, previous_candidates and reusable in the command. A report's
 * `file` values are only ever compared with the paths of loaded recordings.
 */

import { parseEvents } from "../../voxpad/viewer/viewer.js";
import { parseChat } from "./chat.js";

const EVENTS_FILE_LIMIT = 1024 * 1024;
const BYTE_ORDER_MARK = String.fromCodePoint(0xfeff);

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const objects = (value) => Array.isArray(value) && value.every(isObject);
const nfc = (text) => text.normalize("NFC");
const basename = (path) => path.slice(path.lastIndexOf("/") + 1);
// Numbers JSON cannot hold are read as null, so they never reach a report written later.
const finite = (key, value) => (typeof value === "number" && !Number.isFinite(value) ? null : value);

function parse(text) {
  const json = text.startsWith(BYTE_ORDER_MARK) ? text.slice(1) : text;
  try {
    return JSON.parse(json, finite);
  } catch (error) {
    // The command used to write NaN and Infinity, which Python reads back and JSON.parse does not.
    const repaired = json.replace(/"(?:[^"\\]|\\.)*"|-?(?:NaN|Infinity)/g, (token) => (token.startsWith('"') ? token : "null"));
    if (repaired === json) throw error;
    return JSON.parse(repaired, finite);
  }
}

// The browser app describes its model as an object with a name.
function modelName(value) {
  const name = isObject(value) ? value.name : value;
  return typeof name === "string" && name ? name : null;
}

/** Copy the fields VoxPad knows from a result; one of the wrong type is left out. */
function previousResult(entry) {
  const result = { file: entry.file, status: entry.status, text: entry.text ?? "" };
  if (result.status === "error") result.error = typeof entry.error === "string" && entry.error ? entry.error : "Transcription failed";
  if (objects(entry.messages)) result.messages = entry.messages;
  if (Array.isArray(entry.languages) && entry.languages.every((language) => typeof language === "string")) result.languages = entry.languages;
  if (typeof entry.duration_seconds === "number" && Number.isFinite(entry.duration_seconds) && entry.duration_seconds >= 0) {
    result.duration_seconds = entry.duration_seconds;
  }
  for (const key of ["segments", "words"]) {
    if (objects(entry[key])) result[key] = entry[key];
  }
  if (Number.isSafeInteger(entry.bytes) && entry.bytes >= 0) result.bytes = entry.bytes;
  if (modelName(entry.model)) result.model = modelName(entry.model);
  return result;
}

/**
 * Read a report of an earlier run from its JSON text: the command's, the desktop application's or
 * this app's download. Returns { name, model, results, chats }: the model the report names (or
 * null), its finished results, transcribed or failed, with the fields VoxPad writes, and the chats
 * it holds as { path, text }, which only this app's download does. A text that is not such a report
 * throws an Error naming the file.
 */
export function readReport(text, name) {
  const problem = `${name} is not a VoxPad transcript report.`;
  let data;
  try {
    data = parse(text);
  } catch {
    throw new Error(problem);
  }
  const entries = isObject(data) ? data.results : null;
  if (!Array.isArray(entries)) throw new Error(problem);
  const results = [];
  for (const entry of entries) {
    if (!(isObject(entry) && typeof entry.file === "string" && typeof entry.status === "string"
      && (entry.text === undefined || typeof entry.text === "string"))) throw new Error(problem);
    // This app also lists the recordings it has not transcribed yet.
    if (entry.status === "ok" || entry.status === "error") results.push(previousResult(entry));
  }
  const chats = (Array.isArray(data.chats) ? data.chats : [])
    .filter((chat) => isObject(chat) && typeof chat.file === "string" && chat.file && typeof chat.original_text === "string")
    .map((chat) => ({ path: chat.file, text: chat.original_text }));
  return { name, model: modelName(data.model), results, chats };
}

/** The chats of a report as entries for indexMessages, so that a report opened alone shows its conversation. */
export function chatEntries(report) {
  return report.chats.map(({ path, text }) => ({ path, data: new TextEncoder().encode(text) }));
}

/** One result per recording from several reports: a later report wins, and a transcript wins over an error. */
export function merged(reports) {
  const chosen = new Map();
  for (const report of reports) {
    for (const result of report.results) {
      const file = nfc(result.file);
      if (result.status === "ok" || (chosen.get(file) ?? result).status !== "ok") chosen.set(file, result);
    }
  }
  return [...chosen.values()].map((result) => ({ ...result }));
}

/**
 * Find, for each recording of an export, the result of an earlier report that describes it, or null.
 * `files` are the recordings' paths inside the export. A result describes the recording whose path
 * it names; failing that, the one whose file name it shares, when that name stands for one recording
 * of the export and one result of the report. Sender names, chat file names and timestamps take no
 * part: two exports of one chat differ in them.
 */
export function previousCandidates(files, report) {
  const byFile = new Map();
  const byName = new Map();
  for (const result of report.results) {
    const file = nfc(result.file);
    // Of results naming one file a transcript is preferred, then the later one.
    if (result.status === "ok" || (byFile.get(file) ?? result).status !== "ok") byFile.set(file, result);
    const name = basename(file);
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push(result);
  }
  const paths = files.map(nfc);
  const names = new Map();
  for (const path of paths) names.set(basename(path), (names.get(basename(path)) || 0) + 1);
  return paths.map((path) => {
    const name = basename(path);
    const named = byName.get(name) || [];
    return byFile.get(path) ?? (names.get(name) === 1 && named.length === 1 ? named[0] : null);
  });
}

/** Whether an earlier result still stands for a recording of `size` bytes. */
export function reusable(candidate, size, wordTimestamps = false) {
  return Boolean(candidate) && candidate.status === "ok" && (candidate.bytes === undefined || candidate.bytes === size)
    && (!wordTimestamps || candidate.words !== undefined);
}

/**
 * Decide what earlier reports give an export. `reports` come in the order they were selected, the
 * preferred one last, and each is tried on its own; `recordings` are the export's, each with its
 * `path` and its bytes as `data`. Returns
 *   reused: Map of recording path to { status: "ok", text, … } for the app's own results,
 *   kept: the results that stand for no loaded recording, one per file, for the model and the next download,
 *   sources: [{ name, reused }] per report, and the counts byNameOnly (reused without a recorded
 *   size), changed (the recording's size differs) and withoutWords.
 */
export function matchReports(reports, recordings, { wordTimestamps = false } = {}) {
  const candidates = reports.map((report) => previousCandidates(recordings.map((recording) => recording.path), report));
  const reused = new Map();
  const sources = reports.map((report) => ({ name: report.name, reused: 0 }));
  let [byNameOnly, changed, withoutWords] = [0, 0, 0];
  recordings.forEach((recording, index) => {
    const size = recording.data?.length ?? null;
    for (let position = reports.length - 1; position >= 0; position -= 1) {
      const candidate = candidates[position][index];
      if (!reusable(candidate, size, wordTimestamps)) continue;
      const result = { status: "ok" };
      for (const key of ["text", "languages", "duration_seconds", "segments", "words"]) {
        if (candidate[key] !== undefined) result[key] = candidate[key];
      }
      // A report that took transcripts over from others says of each what made it.
      const madeWith = candidate.model || reports[position].model;
      if (madeWith) result.model = madeWith;
      reused.set(recording.path, result);
      sources[position].reused += 1;
      if (candidate.bytes === undefined) byNameOnly += 1;
      return;
    }
    // Why a recording that has a transcript is transcribed again.
    const found = candidates.map((column) => column[index]).filter((candidate) => candidate?.status === "ok");
    if (found.some((candidate) => reusable(candidate, size))) withoutWords += 1;
    else if (found.length) changed += 1;
  });
  const kept = merged(reports.map((report, position) => {
    const placed = new Set(candidates[position].filter(Boolean).map((candidate) => nfc(candidate.file)));
    return {
      results: report.results.filter((result) => !placed.has(nfc(result.file)))
        .map((result) => (result.model || !report.model ? result : { ...result, model: report.model })),
    };
  }));
  return { reused, kept, sources, byNameOnly, changed, withoutWords };
}

/**
 * The events of a text file that is an events file, or null. `name` is its file name and `data` its
 * bytes. It is one when it is not a chat, holds at most 1 MiB, and gives at least one event from at
 * least four in five of its lines that are neither blank nor comments.
 */
export function readEventsFile(name, data) {
  if (data.length > EVENTS_FILE_LIMIT || basename(name).toLowerCase() === "_chat.txt") return null;
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(data);
  } catch {
    return null;
  }
  // A chat is never taken for a list of events, however many of its lines start with a date.
  if (parseChat(text).length) return null;
  const { events, warnings } = parseEvents(text);
  // Every line that is neither blank nor a comment gave an event or a warning.
  return events.length && events.length * 5 >= (events.length + warnings.length) * 4 ? { events, warnings } : null;
}
