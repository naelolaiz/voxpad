import './style.css';
import { readFiles } from './archive.js';
import { indexMessages, annotatedChat, createReport } from './chat.js';
import { decodeAudio } from './audio.js';
import { Transcriber } from './transcriber.js';

const elements = Object.fromEntries([
  'files', 'dropzone', 'selection', 'source-name', 'source-summary', 'clear',
  'language', 'start', 'cancel', 'progress-panel', 'progress-message', 'progress-count',
  'progress', 'error', 'warnings', 'results', 'result-summary', 'chat-choice',
  'chat-select', 'chat-preview', 'audio-list', 'download-log', 'download-json',
].map((id) => [id, document.getElementById(id)]));

let index = null;
let sourceName = '';
let results = new Map();
let busy = false;
let generation = 0;
let currentAudio = 0;
let stage = '';

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
  for (const id of ['files', 'clear', 'language']) elements[id].disabled = value;
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

function reset() {
  generation += 1;
  transcriber.cancel();
  index = null;
  results = new Map();
  sourceName = '';
  elements.files.value = '';
  for (const id of ['selection', 'results', 'progress-panel', 'error', 'warnings']) elements[id].hidden = true;
  elements.warnings.replaceChildren();
  elements['chat-preview'].textContent = '';
  elements['audio-list'].replaceChildren();
  setBusy(false);
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
    const entries = await readFiles(files);
    if (generation !== run) return;
    index = indexMessages(entries);
    if (!index.chats.length && !index.audio.length) throw new Error('No chat text or supported voice recordings were found. Export your chat with media included.');
    sourceName = files.length === 1 ? files[0].name : `${files.length} selected files`;
    for (const audio of index.audio) results.set(audio.path, { status: 'pending' });
    elements['source-name'].textContent = sourceName;
    elements['source-summary'].textContent = `${index.chats.length} chat file${index.chats.length === 1 ? '' : 's'} · ${index.audio.length} voice message${index.audio.length === 1 ? '' : 's'}`;
    elements.selection.hidden = false;
    for (const warning of index.warnings) notice(warning);
    if (!index.chats.length) notice('No chat TXT was included. The text download will list recordings; include the exported chat to place transcripts in the conversation.');
    if (!index.audio.length) notice('No voice recordings were included. You can still download the chat and its JSON. To transcribe voice messages, export with media.');
    if (index.chats.some((chat) => chat.messages.length === 0)) notice('A chat format was not recognized. Its original text is preserved, and recordings remain available in JSON.');
    if (index.audio.some((audio) => audio.messages.length === 0)) notice('Some recordings could not be linked to chat messages. They will be listed separately at the end of the text download.');
    elements['chat-select'].replaceChildren(...index.chats.map((chat, number) => {
      const option = document.createElement('option');
      option.value = String(number);
      option.textContent = chat.path;
      return option;
    }));
    elements['chat-choice'].hidden = index.chats.length <= 1;
    elements.start.textContent = index.audio.length ? 'Transcribe voice messages →' : 'Prepare downloads →';
    elements['progress-panel'].hidden = true;
  } catch (error) {
    index = null;
    elements['progress-panel'].hidden = true;
    showError(error);
  } finally {
    if (generation === run) setBusy(false);
  }
}

function currentChat() {
  return index?.chats[Number(elements['chat-select'].value) || 0];
}

function plainAudio(audio) {
  const result = results.get(audio.path);
  const content = result?.status === 'ok' ? result.text || '[No speech detected]'
    : result?.status === 'error' ? `[Transcription failed: ${result.error}]`
      : '[Not transcribed]';
  return `${audio.path}\n${content}`;
}

function logText() {
  const chat = currentChat();
  if (!chat) return index.audio.map(plainAudio).join('\n\n') + '\n';
  const text = annotatedChat(chat, results);
  const unlinked = index.audio.filter((audio) => audio.messages.length === 0);
  return unlinked.length
    ? `${text}${text.endsWith('\n') ? '' : '\n'}\n[Voice messages not linked to the chat]\n\n${unlinked.map(plainAudio).join('\n\n')}\n`
    : text;
}

function renderResults() {
  if (!index) return;
  elements.results.hidden = false;
  const success = [...results.values()].filter((result) => result.status === 'ok').length;
  const failed = [...results.values()].filter((result) => result.status === 'error').length;
  const pending = results.size - success - failed;
  elements['result-summary'].textContent = `${success} transcribed${failed ? ` · ${failed} failed` : ''}${pending ? ` · ${pending} pending` : ''}. Originals preserved.`;
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
  elements.start.textContent = !index.audio.length ? 'Prepare downloads →'
    : pending || failed ? 'Continue / retry transcription →' : 'Transcribe again →';
}

async function transcribe() {
  if (!index || busy) return;
  const run = ++generation;
  const allDone = index.audio.every((audio) => results.get(audio.path)?.status === 'ok');
  if (allDone) for (const audio of index.audio) results.set(audio.path, { status: 'pending' });
  const language = elements.language.value || undefined;
  elements.error.hidden = true;
  stage = 'model';
  setBusy(true);
  elements['progress-panel'].hidden = false;
  elements['progress-message'].textContent = index.audio.length ? 'Preparing Whistle…' : 'Preparing your downloads…';
  elements['progress-count'].textContent = '';
  elements.progress.value = 0;
  try {
    if (index.audio.length) await transcriber.init(language);
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
  elements['progress-message'].textContent = 'Stopped. Completed transcripts are available; continue when ready.';
  renderResults();
});
elements['chat-select'].addEventListener('change', renderResults);
elements['download-log'].addEventListener('click', () => download(logText(), 'txt', 'text/plain;charset=utf-8'));
elements['download-json'].addEventListener('click', () => {
  const report = createReport(index, results, sourceName);
  report.model = { name: 'Whistle', runtime: 'WebAssembly' };
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
window.addEventListener('pagehide', () => transcriber.dispose());
