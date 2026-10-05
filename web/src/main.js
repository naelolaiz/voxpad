import './style.css';
import '../../voxpad/viewer/viewer.css';
import { mountViewer } from '../../voxpad/viewer/viewer.js';
import { MAX_EVENTS_BYTES, MAX_REPORT_BYTES, ZIP_ALONE, readCompanion, readFiles, splitSelection } from './archive.js';
import { applyResult, buildModel } from './analysis.js';
import { indexMessages, annotatedChat, createReport, reportResults } from './chat.js';
import { chatEntries, matchReports, readEventsFile, readReport } from './previous.js';
import { decodeAudio } from './audio.js';
import { Transcriber } from './transcriber.js';
import { LANGUAGES } from './models.js';

const elements = Object.fromEntries([
  'files', 'dropzone', 'selection', 'source-name', 'source-summary', 'view-link', 'clear',
  'language', 'model', 'start', 'cancel', 'progress-panel', 'progress-message', 'progress-count',
  'progress', 'error', 'warnings', 'results', 'result-summary', 'chat-choice',
  'chat-select', 'date-order-choice', 'date-order', 'viewer', 'plain-text', 'chat-preview', 'audio-list',
  'download-log', 'download-json',
].map((id) => [id, document.getElementById(id)]));

// What the browser is told a recording is, so that it tries to play it.
const MEDIA_TYPES = {
  opus: 'audio/ogg', ogg: 'audio/ogg', oga: 'audio/ogg', m4a: 'audio/mp4', aac: 'audio/aac', mp3: 'audio/mpeg',
  wav: 'audio/wav', flac: 'audio/flac', amr: 'audio/amr', aif: 'audio/aiff', aiff: 'audio/aiff',
};

let index = null;
let sourceName = '';
let results = new Map();
// Results of earlier reports whose recording is not in the selection: shown in the conversation, kept in the JSON.
let kept = [];
// From an events file in the selection, or loaded in the viewer. Like everything else, in memory only.
let events = null;
let model = null;
let viewer = null;
// The date order chosen by hand for the chat on show; null while it is inferred.
let dateOrder = null;
// The one object URL the viewer's player may hold, and a count that outdates a recording still being prepared.
let playing = null;
let asked = 0;
let attempted = false;
let busy = false;
let generation = 0;
let currentAudio = 0;
let stage = '';
let usedModel = null;

const fileName = (path) => path.slice(path.lastIndexOf('/') + 1);
const counted = (count, one, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;
const listed = (names) => [names.slice(0, -1).join(', '), names[names.length - 1]].filter(Boolean).join(' and ');

// Offer every language Whisper recognizes, by name where the browser knows it.
const languageNames = new Intl.DisplayNames(['en'], { type: 'language' });
const languageName = (code) => {
  try {
    const name = languageNames.of(code === 'jw' ? 'jv' : code);
    return name && name !== code ? name : code;
  } catch {
    return code;
  }
};
elements.language.append(...LANGUAGES.map((code) => [languageName(code), code])
  .sort(([left], [right]) => left.localeCompare(right))
  .map(([name, code]) => {
    const option = document.createElement('option');
    option.value = code;
    option.textContent = name;
    return option;
  }));

const transcriber = new Transcriber({ onProgress: ({ message, fraction }) => {
  if (!busy) return;
  elements['progress-message'].textContent = message;
  if (Number.isFinite(fraction)) {
    elements.progress.value = stage === 'audio'
      ? (currentAudio + fraction) / Math.max(index.audio.length, 1)
      : fraction;
  } else {
    elements.progress.removeAttribute('value');
  }
} });

function setBusy(value) {
  busy = value;
  for (const id of ['files', 'clear', 'language', 'model']) elements[id].disabled = value;
  elements.start.disabled = value || !index;
  elements.dropzone.classList.toggle('busy', value);
  elements.cancel.hidden = !value || stage === 'import';
}

function showError(error) {
  elements.error.textContent = error?.message || String(error);
  elements.error.hidden = false;
}

function notice(text) {
  const item = document.createElement('li');
  item.textContent = text;
  elements.warnings.append(item);
  elements.warnings.hidden = false;
}

function releaseAudio() {
  asked += 1;
  if (playing) URL.revokeObjectURL(playing);
  playing = null;
}

/** Samples at 16 kHz as a 16-bit PCM WAV file, which every browser plays. */
function wav(samples) {
  const view = new DataView(new ArrayBuffer(44 + samples.length * 2));
  const write = (offset, text) => { for (let at = 0; at < text.length; at += 1) view.setUint8(offset + at, text.charCodeAt(at)); };
  write(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  write(8, 'WAVEfmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true);
  view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  for (let at = 0; at < samples.length; at += 1) view.setInt16(44 + at * 2, Math.round(Math.max(-1, Math.min(1, samples[at])) * 32767), true);
  return view.buffer;
}

/**
 * Give the viewer one recording to play, as an object URL of bytes held in this tab; `src` is its path
 * in the selection. The URL handed out before is released first, so one recording at a time is
 * playable. `decoded` asks for a WAV copy, for a browser that cannot play the recording as it is.
 */
async function resolveAudio(src, { decoded }) {
  releaseAudio();
  const request = asked;
  const recording = index?.audio.find((audio) => audio.path === src);
  if (!recording) return null;
  let content = recording.data;
  if (decoded) {
    content = wav(await decodeAudio(recording.data));
    // Another recording was chosen meanwhile, and the viewer no longer waits for this one.
    if (request !== asked) return null;
  }
  const type = decoded ? 'audio/wav' : MEDIA_TYPES[src.slice(src.lastIndexOf('.') + 1).toLowerCase()] || '';
  playing = URL.createObjectURL(new Blob([content], { type }));
  return playing;
}

function closeViewer() {
  try {
    viewer?.destroy();
  } catch { /* A viewer that cannot close is let go all the same. */ }
  viewer = null;
  model = null;
  releaseAudio();
}

/** Go on without the viewer: the plain text and the downloads never depend on it. */
function dropViewer(error) {
  console.error('Could not show the conversation view:', error);
  closeViewer();
  // A fresh element, so that nothing a half-built viewer attached stays behind.
  const fresh = document.createElement('div');
  fresh.id = 'viewer';
  fresh.className = 'viewer';
  fresh.hidden = true;
  elements.viewer.replaceWith(fresh);
  elements.viewer = fresh;
  elements['date-order-choice'].hidden = true;
  elements['plain-text'].open = true;
  notice('The conversation view could not be shown. The plain text and both downloads are complete.');
}

function reset() {
  generation += 1;
  transcriber.cancel();
  closeViewer();
  index = null;
  results = new Map();
  kept = [];
  events = null;
  dateOrder = null;
  attempted = false;
  usedModel = null;
  sourceName = '';
  elements.files.value = '';
  for (const id of ['selection', 'view-link', 'results', 'viewer', 'date-order-choice', 'progress-panel', 'error', 'warnings']) elements[id].hidden = true;
  elements.warnings.replaceChildren();
  elements['chat-preview'].textContent = '';
  elements['audio-list'].replaceChildren();
  setBusy(false);
}

/** Read a report of an earlier run selected with the export; one that is something else stops the import. */
async function earlierReport(file) {
  const problem = `${file.name} is not a VoxPad transcript report.`;
  const data = await readCompanion(file, MAX_REPORT_BYTES);
  if (!data) throw new Error(problem);
  let text;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(data);
  } catch {
    throw new Error(problem);
  }
  return readReport(text, file.name);
}

/**
 * What to read when reports were selected without an export: the chats inside them, of two with one
 * path the later report's, and whatever else was selected with them.
 */
function reportChats(reports, entries) {
  const chats = new Map(reports.flatMap(chatEntries).map((entry) => [entry.path.normalize('NFC'), entry]));
  // Only this app's download carries the chat; the command's and the desktop application's reports do not.
  if (!chats.size) {
    throw new Error(reports.length === 1 ? 'This report has no chat text. Select it together with the export ZIP or the chat .txt.'
      : 'These reports have no chat text. Select them together with the export ZIP or the chat .txt.');
  }
  return [...entries.filter((entry) => !chats.has(entry.path)), ...chats.values()];
}

/** Say what the reports of earlier runs gave this selection. */
function importNotices(reports, match) {
  if (!reports.length) return;
  const names = listed(reports.map((report) => report.name));
  const imported = match.reused.size + kept.filter((result) => result.status === 'ok').length;
  notice(imported
    ? `${counted(imported, 'earlier transcript')} imported from ${names}; ${index.audio.length - match.reused.size} left to transcribe.`
    : `No earlier transcripts were found in ${names}.`);
  if (match.byNameOnly) notice(`${counted(match.byNameOnly, 'earlier transcript was', 'earlier transcripts were')} matched by file name only.`);
  if (match.changed) notice(`${counted(match.changed, 'recording differs', 'recordings differ')} in size from what was transcribed earlier and will be transcribed again.`);
  if (kept.length && index.audio.length) {
    notice(`${counted(kept.length, 'earlier result has', 'earlier results have')} no recording in this selection. The conversation shows them, and Download JSON keeps them.`);
  }
}

async function load(files) {
  if (busy || files.length === 0) return;
  reset();
  stage = 'import';
  setBusy(true);
  const run = generation;
  elements['progress-panel'].hidden = false;
  elements['progress-message'].textContent = 'Opening your export…';
  elements['progress-count'].textContent = '';
  elements.progress.removeAttribute('value');
  try {
    const selection = splitSelection(files);
    const reports = [];
    for (const file of selection.reports) reports.push(await earlierReport(file));
    let found = null;
    for (const file of selection.texts) {
      const data = await readCompanion(file, MAX_EVENTS_BYTES);
      const read = data && readEventsFile(file.name, data);
      if (!read) throw new Error(ZIP_ALONE);
      found ??= { name: file.name, beside: true, ...read };
    }
    let entries = selection.files.length ? await readFiles(selection.files) : [];
    if (generation !== run) return;
    index = indexMessages(entries);
    // Reports without an export: this app's download holds the chats it was made from.
    const reopened = !index.chats.length && !index.audio.length && reports.length > 0;
    if (reopened) {
      entries = reportChats(reports, entries);
      index = indexMessages(entries);
    }
    if (!index.chats.length && !index.audio.length) throw new Error('No chat text or supported voice recordings were found. Export your chat with media included.');
    // A text that is no chat may be a list of events.
    const chats = new Set(index.chats.map((chat) => chat.path));
    for (const entry of entries) {
      if (found) break;
      const read = entry.path.toLowerCase().endsWith('.txt') && !chats.has(entry.path) ? readEventsFile(entry.path, entry.data) : null;
      if (read) found = { name: fileName(entry.path), beside: reopened, ...read };
    }
    events = found ? found.events : null;
    const match = matchReports(reports, index.audio);
    kept = match.kept;
    for (const audio of index.audio) results.set(audio.path, match.reused.get(audio.path) ?? { status: 'pending' });
    // The export gives the downloads their name; what was selected with it is named beside it.
    const exported = reopened ? selection.reports : selection.files;
    sourceName = exported.length === 1 ? exported[0].name : `${exported.length} selected files`;
    const beside = [...(reopened ? [] : selection.reports.map((file) => file.name)), ...(found?.beside ? [found.name] : [])];
    elements['source-name'].textContent = [sourceName, ...beside].join(' + ');
    elements['source-summary'].textContent = `${index.chats.length} chat file${index.chats.length === 1 ? '' : 's'} · ${index.audio.length} voice message${index.audio.length === 1 ? '' : 's'}`;
    elements.selection.hidden = false;
    for (const warning of index.warnings) notice(warning);
    importNotices(reports, match);
    if (found) {
      const skipped = found.warnings.length ? ` (${counted(found.warnings.length, 'line')} without a date ${found.warnings.length === 1 ? 'was' : 'were'} skipped)` : '';
      notice(`Read ${counted(found.events.length, 'event')} from ${found.name}${skipped}.`);
    }
    if (!index.chats.length) notice('No chat TXT was included. The text download will list recordings; include the exported chat to place transcripts in the conversation.');
    if (!index.audio.length && !kept.length) notice('No voice recordings were included. You can still download the chat and its JSON. To transcribe voice messages, export with media.');
    if (index.chats.some((chat) => chat.messages.length === 0)) notice('A chat format was not recognized. Its original text is preserved, and recordings remain available in JSON.');
    if (index.audio.some((audio) => audio.messages.length === 0)) notice('Some recordings could not be linked to chat messages. They will be listed separately at the end of the text download.');
    elements['chat-select'].replaceChildren(...index.chats.map((chat, number) => {
      const option = document.createElement('option');
      option.value = String(number);
      option.textContent = chat.path;
      return option;
    }));
    elements['chat-choice'].hidden = index.chats.length <= 1;
    elements.start.textContent = startLabel();
    elements['progress-panel'].hidden = true;
    showChat();
    // A chat is shown at once; recordings alone have nothing to show before they are transcribed.
    if (currentChat()) renderResults();
    elements['view-link'].hidden = !currentChat();
  } catch (error) {
    if (generation !== run) return;
    reset();
    showError(error);
  } finally {
    if (generation === run) setBusy(false);
  }
}

function currentChat() {
  return index?.chats[Number(elements['chat-select'].value) || 0];
}

function conversationModel(chat) {
  return buildModel({
    text: chat.text,
    results: reportResults(index, results, kept),
    events,
    dateOrder,
    // A recording that is loaded is named by its path, which the viewer hands back to resolveAudio.
    audioSrc: (name, at) => chat.messages[at].audio_paths.find((path) => fileName(path) === name) ?? null,
  });
}

/**
 * Show the selected chat in the viewer, or nothing when the selection has no chat. Nothing here may
 * stop the page: without the viewer the plain text stands open in its place.
 */
function showChat() {
  closeViewer();
  dateOrder = null;
  const chat = currentChat();
  if (chat) {
    // Shown before mounting, so that the viewer can measure itself.
    elements.results.hidden = false;
    elements.viewer.hidden = false;
    try {
      model = conversationModel(chat);
      viewer = mountViewer(elements.viewer, model, { theme: 'light', resolveAudio, onEvents: (list) => { events = list; } });
    } catch (error) {
      dropViewer(error);
    }
  }
  elements.viewer.hidden = !viewer;
  // Offered only where the dates leave a choice; it stays while the reader tries the other orders.
  elements['date-order-choice'].hidden = !viewer || !model.date_order_ambiguous;
  if (viewer) elements['date-order'].value = model.date_order;
  elements['plain-text'].open = !viewer;
}

/** Read the chat again, for a change that reaches every message, and keep the viewer where it is. */
function rebuild() {
  if (!viewer) return;
  try {
    model = conversationModel(currentChat());
    viewer.update(model);
  } catch (error) {
    dropViewer(error);
  }
}

/** Bring the voice messages of one recording up to date in the viewer, without reading the chat again. */
function showResult(audio) {
  if (!viewer) return;
  const chat = currentChat();
  const places = audio.occurrences.filter((occurrence) => occurrence.chat_file === chat.path).map((occurrence) => occurrence.message_index);
  try {
    const changed = applyResult(model, places, { file: audio.path, ...results.get(audio.path) });
    if (changed.length) viewer.update(model, changed);
  } catch (error) {
    dropViewer(error);
  }
}

/**
 * The chat with its transcripts in place. Those of loaded recordings are placed by annotatedChat;
 * those an earlier report gives for a recording the selection lacks are placed where the conversation
 * shows them.
 */
function annotated(chat) {
  if (!kept.length) return annotatedChat(chat, results);
  let described;
  try {
    described = chat === currentChat() && model ? model : buildModel({ text: chat.text, results: reportResults(index, results, kept) });
  } catch {
    return annotatedChat(chat, results);
  }
  const names = new Set(kept.map((result) => fileName(result.file.normalize('NFC'))));
  const placed = new Map(results);
  const messages = chat.messages.map((message, at) => {
    const voice = described.messages[at]?.voice;
    if (!voice?.file || voice.status === 'pending' || !names.has(voice.file)
        || message.audio_paths.some((path) => fileName(path) === voice.file)) return message;
    // An annotated chat may hold the transcript of a later, better run: a failure never takes its place.
    if (voice.status === 'error' && message.text.includes('\n[Voice message transcript: ')) return message;
    // No path of the selection starts with a slash, so this key stands for the absent recording alone.
    const key = `/${at}`;
    placed.set(key, { status: voice.status === 'error' ? 'error' : 'ok', text: voice.text, error: voice.error });
    return { ...message, audio_paths: [...message.audio_paths, key] };
  });
  return annotatedChat({ ...chat, messages }, placed);
}

function plainAudio(audio) {
  const result = results.get(audio.path);
  const content = result?.status === 'ok' ? result.text || '[No speech detected]'
    // An earlier report read without a reason carries these two words as its error.
    : result?.status === 'error' ? (result.error && result.error !== 'Transcription failed' ? `[Transcription failed: ${result.error}]` : '[Transcription failed]')
      : '[Not transcribed]';
  return `${audio.path}\n${content}`;
}

function logText() {
  const chat = currentChat();
  if (!chat) return index.audio.map(plainAudio).join('\n\n') + '\n';
  const text = annotated(chat);
  const unlinked = index.audio.filter((audio) => audio.messages.length === 0);
  return unlinked.length
    ? `${text}${text.endsWith('\n') ? '' : '\n'}\n[Voice messages not linked to the chat]\n\n${unlinked.map(plainAudio).join('\n\n')}\n`
    : text;
}

function startLabel() {
  if (!index.audio.length) return 'Prepare downloads →';
  const done = index.audio.filter((audio) => results.get(audio.path)?.status === 'ok').length;
  if (done === index.audio.length) return 'Transcribe again →';
  return done || attempted ? 'Continue / retry transcription →' : 'Transcribe voice messages →';
}

function renderResults() {
  if (!index) return;
  elements.results.hidden = false;
  const success = [...results.values()].filter((result) => result.status === 'ok').length;
  const failed = [...results.values()].filter((result) => result.status === 'error').length;
  const pending = results.size - success - failed;
  const earlier = kept.filter((result) => result.status === 'ok').length;
  elements['result-summary'].textContent = `${success} transcribed${failed ? ` · ${failed} failed` : ''}${pending ? ` · ${pending} pending` : ''}. Originals preserved.`
    + `${earlier ? ` ${counted(earlier, 'earlier transcript')} shown without ${earlier === 1 ? 'its recording' : 'their recordings'}.` : ''}`
    + `${usedModel ? ` Transcribed with ${usedModel.name}.` : ''}`;
  const text = logText();
  const limit = 60000;
  elements['chat-preview'].textContent = text.length > limit ? `${text.slice(0, limit)}\n\n[Preview shortened. Downloads contain the complete chat.]` : text;
  elements['audio-list'].replaceChildren(...index.audio.map((audio) => {
    const item = document.createElement('li');
    const name = document.createElement('strong');
    name.textContent = audio.path;
    const content = document.createElement('p');
    content.textContent = plainAudio(audio).slice(audio.path.length + 1);
    item.append(name, content);
    return item;
  }));
  elements.start.textContent = startLabel();
}

async function transcribe() {
  if (!index || busy) return;
  const run = ++generation;
  attempted = true;
  const allDone = index.audio.every((audio) => results.get(audio.path)?.status === 'ok');
  if (allDone && index.audio.length) {
    for (const audio of index.audio) results.set(audio.path, { status: 'pending' });
    rebuild();
  }
  const language = elements.language.value || undefined;
  elements.error.hidden = true;
  stage = 'model';
  setBusy(true);
  elements['progress-panel'].hidden = false;
  elements['progress-message'].textContent = index.audio.length ? 'Preparing Whisper…' : 'Preparing your downloads…';
  elements['progress-count'].textContent = '';
  elements.progress.value = 0;
  try {
    if (index.audio.length) {
      await transcriber.init(language, { model: elements.model.value || undefined });
      usedModel = transcriber.model;
    }
    for (let number = 0; number < index.audio.length; number += 1) {
      if (generation !== run) return;
      const audio = index.audio[number];
      if (results.get(audio.path)?.status === 'ok') continue;
      currentAudio = number;
      stage = 'audio';
      elements['progress-count'].textContent = `${number + 1} / ${index.audio.length}`;
      elements['progress-message'].textContent = `Listening to ${audio.name}…`;
      elements.progress.value = number / index.audio.length;
      try {
        const samples = await decodeAudio(audio.data);
        if (generation !== run) return;
        const transcript = await transcriber.transcribe(samples, { language });
        if (generation !== run) return;
        results.set(audio.path, { status: 'ok', ...transcript });
      } catch (error) {
        if (generation !== run) return;
        results.set(audio.path, { status: 'error', error: error.message || String(error) });
      }
      showResult(audio);
      renderResults();
    }
    renderResults();
    elements['progress-message'].textContent = 'Done. Your downloads are ready.';
    elements['progress-count'].textContent = '';
    elements.progress.value = 1;
  } catch (error) {
    if (generation === run) {
      showError(error);
      elements['progress-message'].textContent = 'Could not start transcription. You can retry or download the available results.';
      renderResults();
    }
  } finally {
    if (generation === run) setBusy(false);
  }
}

function download(content, extension, type) {
  const stem = (currentChat()?.name || sourceName || 'conversation').replace(/\.[^.]+$/, '').replace(/[\\/:*?"<>|]/g, '_');
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `${stem}_transcribed.${extension}`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

elements.files.addEventListener('change', () => load([...elements.files.files]));
elements.clear.addEventListener('click', reset);
elements.start.addEventListener('click', transcribe);
elements.cancel.addEventListener('click', () => {
  generation += 1;
  transcriber.cancel();
  setBusy(false);
  elements['progress-message'].textContent = 'Stopped. Completed transcripts are available; continue when ready. Download JSON to continue another day: select it together with the export.';
  renderResults();
});
elements['chat-select'].addEventListener('change', () => {
  showChat();
  renderResults();
});
elements['date-order'].addEventListener('change', () => {
  dateOrder = elements['date-order'].value;
  rebuild();
});
elements['download-log'].addEventListener('click', () => download(logText(), 'txt', 'text/plain;charset=utf-8'));
elements['download-json'].addEventListener('click', () => {
  const report = createReport(index, results, sourceName, kept);
  // createReport places the transcripts of loaded recordings only.
  if (kept.length) report.chats.forEach((entry, at) => { entry.annotated_text = annotated(index.chats[at]); });
  report.model = usedModel || { name: 'Whisper' };
  // A transcript taken over from an earlier report names its model only when that is another one.
  for (const result of report.results.slice(0, index.audio.length)) {
    if (result.model === report.model.name) delete result.model;
  }
  download(JSON.stringify(report, null, 2) + '\n', 'json', 'application/json;charset=utf-8');
});
for (const event of ['dragenter', 'dragover']) elements.dropzone.addEventListener(event, (e) => {
  e.preventDefault();
  if (!busy) elements.dropzone.classList.add('dragging');
});
for (const event of ['dragleave', 'drop']) elements.dropzone.addEventListener(event, (e) => {
  e.preventDefault();
  elements.dropzone.classList.remove('dragging');
});
elements.dropzone.addEventListener('drop', (e) => load([...e.dataTransfer.files]));
// A file dropped beside the dropzone would otherwise replace the page and its results.
for (const event of ['dragover', 'drop']) window.addEventListener(event, (e) => e.preventDefault());
window.addEventListener('pagehide', () => {
  transcriber.dispose();
  releaseAudio();
});
