/* Classic worker: runs Cactus Whistle in WebAssembly, off the main thread. */

const SAMPLE_RATE = 16000;
// Whistle processes at most 30 seconds per call.
const CHUNK_SAMPLES = 30 * SAMPLE_RATE;
const OUTPUT_BYTES = 1 << 18;
const CACHE_NAME = 'voxpad-whistle-v1';
// Pinned revisions; tests/browser/assets.js serves the same URLs.
const RUNTIME_URL = 'https://huggingface.co/Cactus-Compute/needle3/resolve/c7c415a3d1b3d929014bc6e866d51ebb971f7089/wasm/';
const MODEL_URL = 'https://huggingface.co/Cactus-Compute/whistle/resolve/b358ddadd89b7a713b5aa131f23032d3cca1b251/whistle.cact';
const LANGUAGES = new Set(['en', 'de', 'fr', 'es', 'it', 'nl', 'pl']);
const SPEECH_MODEL = 2;

let needle = null;
let loading = null;
let modelPointer = 0;
let transcribing = false;

function report(id, phase, message, fraction) {
  const progress = { phase, message };
  if (fraction !== undefined) progress.fraction = fraction;
  self.postMessage({ id, type: 'progress', progress });
}

/** Fetch a public engine/model asset, reusing the browser cache when allowed. */
async function download(url, label, id) {
  let cache = null;
  try {
    if (typeof caches !== 'undefined') cache = await caches.open(CACHE_NAME);
    const cached = cache && await cache.match(url);
    if (cached) {
      report(id, 'download', `Using cached ${label}.`, 1);
      return new Uint8Array(await cached.arrayBuffer());
    }
  } catch {
    cache = null;
  }
  report(id, 'download', `Downloading ${label}…`, 0);
  const response = await fetch(url, { mode: 'cors', credentials: 'omit' });
  if (!response.ok) throw new Error(`Could not download ${label} (${response.status}). Check your connection and try again.`);
  const total = Number(response.headers.get('Content-Length')) || 0;
  let bytes;
  if (response.body?.getReader) {
    const reader = response.body.getReader();
    const chunks = [];
    let received = 0;
    let reported = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      received += value.byteLength;
      const now = Date.now();
      if (now - reported >= 150) {
        report(id, 'download', `Downloading ${label}: ${(received / 1024 / 1024).toFixed(1)} MB…`, total ? Math.min(received / total, 1) : undefined);
        reported = now;
      }
    }
    bytes = new Uint8Array(received);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
  } else {
    bytes = new Uint8Array(await response.arrayBuffer());
  }
  if (!bytes.byteLength) throw new Error(`The downloaded ${label} is empty. Please try again.`);
  if (cache) {
    try {
      await cache.put(url, new Response(bytes, {
        headers: { 'Content-Type': response.headers.get('Content-Type') || 'application/octet-stream' },
      }));
    } catch { /* Caching is optional, e.g. in private browsing. */ }
  }
  report(id, 'download', `${label} ready.`, 1);
  return bytes;
}

function allocate(bytes) {
  const pointer = needle._malloc(bytes);
  if (!pointer) throw new Error('The browser ran out of memory while transcribing.');
  return pointer;
}

function lastError() {
  return needle.UTF8ToString(needle._needle_last_error()) || 'The speech engine could not process this recording.';
}

async function load(id) {
  if (needle) return;
  loading ||= (async () => {
    const runtime = await download(`${RUNTIME_URL}needle.js`, 'speech runtime', id);
    const wasmBinary = await download(`${RUNTIME_URL}needle.wasm`, 'speech engine', id);
    const model = await download(MODEL_URL, 'Whistle model (16.9 MB)', id);
    report(id, 'initialize', 'Starting the local speech engine…', 0);
    const script = URL.createObjectURL(new Blob([runtime], { type: 'text/javascript' }));
    try {
      importScripts(script);
    } finally {
      URL.revokeObjectURL(script);
    }
    if (typeof self.createNeedle !== 'function') throw new Error('The speech runtime could not start.');
    needle = await self.createNeedle({ wasmBinary });
    modelPointer = allocate(model.byteLength);
    needle.HEAPU8.set(model, modelPointer);
    if (needle._needle_load(modelPointer, BigInt(model.byteLength)) < 0) throw new Error(lastError());
    if (!(needle._needle_models() & SPEECH_MODEL)) throw new Error('The downloaded model does not contain Whistle speech recognition.');
    report(id, 'ready', 'Whistle is ready. Your recordings stay in this browser.', 1);
  })().catch((error) => {
    if (needle && modelPointer) needle._free(modelPointer);
    needle = null;
    modelPointer = 0;
    loading = null;
    throw error;
  });
  await loading;
}

async function transcribe(samples, language, id) {
  if (!(samples instanceof Float32Array) || samples.length === 0) throw new Error('The recording contains no decoded audio.');
  if (language != null && !LANGUAGES.has(language)) throw new Error('The selected spoken language is unsupported.');
  if (transcribing) throw new Error('A recording is already being transcribed.');
  transcribing = true;
  let input = 0;
  let output = 0;
  let languagePointer = 0;
  try {
    await load(id);
    input = allocate(Math.min(samples.length, CHUNK_SAMPLES) * 4);
    output = allocate(OUTPUT_BYTES);
    if (language) {
      const encoded = new TextEncoder().encode(`${language}\0`);
      languagePointer = allocate(encoded.length);
      needle.HEAPU8.set(encoded, languagePointer);
    }
    const segments = [];
    const languages = [];
    const parts = Math.ceil(samples.length / CHUNK_SAMPLES);
    // Every frame is sent, in consecutive chunks, including the final partial one.
    for (let start = 0; start < samples.length; start += CHUNK_SAMPLES) {
      const length = Math.min(CHUNK_SAMPLES, samples.length - start);
      const part = segments.length + 1;
      report(id, 'transcribe', `Transcribing part ${part} of ${parts}…`, (part - 1) / parts);
      // Re-create the view each time: the WebAssembly heap may have grown.
      const heap = new Float32Array(needle.HEAPU8.buffer, input, length);
      for (let index = 0; index < length; index += 1) {
        const sample = samples[start + index];
        if (!Number.isFinite(sample)) throw new Error('The recording contains invalid audio samples.');
        heap[index] = Math.max(-1, Math.min(1, sample));
      }
      needle.HEAPU8[output] = 0;
      if (needle._needle_transcribe(input, length, languagePointer, 0, 0, output, OUTPUT_BYTES) < 0) throw new Error(lastError());
      const parsed = JSON.parse(needle.UTF8ToString(output));
      if (typeof parsed.text !== 'string') throw new Error('The speech engine returned an invalid transcript.');
      const detected = parsed.language || '';
      segments.push({ start: start / SAMPLE_RATE, end: (start + length) / SAMPLE_RATE, text: parsed.text.trim(), language: detected });
      if (detected && !languages.includes(detected)) languages.push(detected);
      report(id, 'transcribe', `Finished part ${part} of ${parts}.`, part / parts);
      await new Promise((resolve) => setTimeout(resolve, 0));
    }
    return {
      text: segments.map((segment) => segment.text).filter(Boolean).join(' '),
      languages,
      duration_seconds: samples.length / SAMPLE_RATE,
      segments,
    };
  } finally {
    if (needle) {
      if (languagePointer) needle._free(languagePointer);
      if (output) needle._free(output);
      if (input) needle._free(input);
    }
    transcribing = false;
  }
}

self.onmessage = async ({ data }) => {
  const { id, type } = data;
  try {
    let result;
    if (type === 'init') {
      await load(id);
      result = { ready: true };
    } else if (type === 'transcribe') {
      result = await transcribe(data.samples, data.language, id);
    } else {
      throw new Error('Unknown speech engine request.');
    }
    self.postMessage({ id, type: 'result', result });
  } catch (error) {
    self.postMessage({ id, type: 'error', error: error.message || 'Local transcription failed.' });
  }
};
