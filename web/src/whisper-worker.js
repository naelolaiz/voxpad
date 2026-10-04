/** Module worker: runs Whisper with Transformers.js, off the main thread. */
import { env, pipeline, Tensor } from '@huggingface/transformers';

import { chunkEnd, SAMPLE_RATE } from './chunks.js';
import { downloadBytes, fileUrl, LANGUAGES, MODELS } from './models.js';

const CACHE_NAME = 'voxpad-whisper-v1';
// Cached copies up to this size are verified again on every start.
const REVERIFY_BYTES = 32 * 1024 * 1024;
// A chunk this quiet holds no speech; Whisper tends to invent text for silence.
const SILENCE_PEAK = 0.003;

// Model files reach Transformers.js only through verifiedFetch below.
env.allowLocalModels = false;
env.useBrowserCache = false;
env.useWasmCache = false;
env.fetch = verifiedFetch;

let asr = null;
let active = null;
let loading = null;
let transcribing = false;
let pinned = new Map();
let progress = null;

function report(id, phase, message, fraction) {
  const update = { phase, message };
  if (fraction !== undefined) update.fraction = fraction;
  self.postMessage({ id, type: 'progress', progress: update });
}

async function digest(bytes) {
  if (!self.crypto?.subtle) throw new Error('Open this page over HTTPS so the speech model can be verified.');
  const hash = await self.crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

const megabytes = (bytes) => (bytes / 1024 / 1024).toFixed(0);

/**
 * Serve one pinned model file: from the browser cache when present, otherwise
 * downloaded, checked against its SHA-256, and only then cached and used.
 * Anything that is not pinned does not exist as far as the model loader knows.
 */
async function verifiedFetch(input) {
  const url = typeof input === 'string' ? input : input.url;
  const file = pinned.get(url);
  if (!file) return new Response('Not a pinned model file.', { status: 404 });
  const headers = { 'Content-Type': 'application/octet-stream', 'Content-Length': String(file.size) };
  let cache = null;
  try {
    if (typeof caches !== 'undefined') cache = await caches.open(CACHE_NAME);
    const cached = cache && await cache.match(url);
    if (cached) {
      const blob = await cached.blob();
      // Entries are only stored after verification; small ones are cheap to recheck.
      if (blob.size === file.size && (file.size > REVERIFY_BYTES || await digest(await blob.arrayBuffer()) === file.sha256)) {
        progress?.add(file.size);
        return new Response(blob, { headers });
      }
      await cache.delete(url);
    }
  } catch {
    cache = null;
  }
  const response = await fetch(url, { mode: 'cors', credentials: 'omit' });
  if (!response.ok) throw new Error(`Could not download the speech model (${response.status}). Check your connection and try again.`);
  const chunks = [];
  if (response.body?.getReader) {
    const reader = response.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
      progress?.add(value.byteLength);
    }
  } else {
    chunks.push(await response.arrayBuffer());
    progress?.add(file.size);
  }
  const blob = new Blob(chunks, { type: 'application/octet-stream' });
  if (blob.size !== file.size || await digest(await blob.arrayBuffer()) !== file.sha256) {
    throw new Error('The downloaded speech model failed its integrity check and was not used. Please try again later.');
  }
  if (cache) {
    try {
      await cache.put(url, new Response(blob, { headers }));
    } catch { /* Caching is optional, e.g. in private browsing or when storage is full. */ }
  }
  return new Response(blob, { headers });
}

/** The best model needs WebGPU with 16-bit floats; everything else runs Whisper small. */
async function chooseModel(preference) {
  if (preference === 'small' || preference === 'tiny') return preference;
  try {
    const adapter = await self.navigator?.gpu?.requestAdapter();
    if (adapter?.features.has('shader-f16')) return 'turbo';
  } catch { /* No usable GPU. */ }
  return 'small';
}

async function start(key, id) {
  const model = MODELS[key];
  // Every request goes to the pinned revision, whatever the loader would default to.
  env.remotePathTemplate = `{model}/resolve/${model.revision}/`;
  pinned = new Map(Object.entries(model.files).map(([file, details]) => [fileUrl(model, file), details]));
  const total = downloadBytes(model);
  let done = 0;
  let reported = 0;
  progress = {
    add(bytes) {
      done += bytes;
      const now = Date.now();
      if (now - reported < 150 && done < total) return;
      reported = now;
      report(id, 'download', `Getting ${model.name}: ${megabytes(done)} of ${megabytes(total)} MB…`, Math.min(done / total, 1));
    },
  };
  report(id, 'download', `Getting ${model.name} (${megabytes(total)} MB the first time)…`, 0);
  try {
    asr = await pipeline('automatic-speech-recognition', model.repo, {
      revision: model.revision,
      device: model.device,
      dtype: model.dtype,
    });
  } finally {
    progress = null;
  }
  // Let generation accept a precomputed audio encoding (see recognize below).
  const accepted = asr.model.forward_params;
  if (Array.isArray(accepted) && !accepted.includes('encoder_outputs')) asr.model.forward_params = [...accepted, 'encoder_outputs'];
  active = key;
}

async function load(id, options = {}) {
  if (asr) return;
  loading ||= (async () => {
    if (options.runtimePath) env.backends.onnx.wasm.wasmPaths = options.runtimePath;
    // Threads need cross-origin isolation, which a static host cannot provide.
    env.backends.onnx.wasm.numThreads = 1;
    const key = await chooseModel(options.model);
    try {
      await start(key, id);
    } catch (error) {
      if (key !== 'turbo') throw error;
      // The larger model can fail on GPUs with too little memory.
      report(id, 'download', 'The larger model could not start on this device. Switching to Whisper small…', 0);
      await asr?.dispose?.().catch(() => {});
      asr = null;
      await start('small', id);
    }
    report(id, 'ready', `${MODELS[active].name} is ready. Your recordings stay in this browser.`, 1);
  })().catch((error) => {
    asr = null;
    active = null;
    loading = null;
    throw error;
  });
  await loading;
}

/**
 * Transcribe one chunk of at most 30 seconds. Without a language, Whisper is
 * first asked which one it hears (Transformers.js does not do this itself), and
 * the audio encoding from that step is reused for the transcript.
 */
async function recognize(samples, language) {
  const config = asr.model.generation_config;
  const { input_features: features } = await asr.processor(samples);
  const inputs = { inputs: features };
  let spoken = language;
  if (!spoken) {
    const first = new Tensor('int64', BigInt64Array.from([BigInt(config.decoder_start_token_id)]), [1, 1]);
    try {
      // Encoding is the slow step; this internal helper lets both steps share it.
      inputs.encoder_outputs = (await asr.model._prepare_encoder_decoder_kwargs_for_generation({
        inputs_tensor: features,
        model_inputs: { input_features: features },
        model_input_name: 'input_features',
        generation_config: { guidance_scale: null },
      })).encoder_outputs;
    } catch { /* Encode twice if the helper changes in a later library version. */ }
    const output = await asr.model(inputs.encoder_outputs
      ? { encoder_outputs: inputs.encoder_outputs, decoder_input_ids: first }
      : { input_features: features, decoder_input_ids: first });
    const vocabulary = output.logits.dims.at(-1);
    const scores = output.logits.data.subarray(output.logits.data.length - vocabulary);
    let bestScore = -Infinity;
    for (const [token, tokenId] of Object.entries(config.lang_to_id)) {
      if (scores[tokenId] > bestScore) {
        bestScore = scores[tokenId];
        spoken = token.slice(2, -2);
      }
    }
  }
  const tokens = await asr.model.generate({ ...inputs, language: spoken, task: 'transcribe' });
  const [text] = asr.tokenizer.batch_decode(tokens, { skip_special_tokens: true });
  if (typeof text !== 'string') throw new Error('The speech model returned an invalid transcript.');
  return { text: text.trim(), language: spoken };
}

function isSilent(samples) {
  for (let index = 0; index < samples.length; index += 1) {
    if (Math.abs(samples[index]) > SILENCE_PEAK) return false;
  }
  return true;
}

async function transcribe(samples, language, id) {
  if (!(samples instanceof Float32Array) || samples.length === 0) throw new Error('The recording contains no decoded audio.');
  if (language != null && !LANGUAGES.includes(language)) throw new Error('The selected spoken language is unsupported.');
  if (transcribing) throw new Error('A recording is already being transcribed.');
  transcribing = true;
  try {
    await load(id);
    const segments = [];
    const languages = [];
    let spoken = language;
    // Every frame is sent, in consecutive chunks, including the final partial one.
    for (let start = 0, end; start < samples.length; start = end) {
      end = chunkEnd(samples, start);
      const part = segments.length + 1;
      report(id, 'transcribe', `Transcribing part ${part}…`, start / samples.length);
      const chunk = samples.slice(start, end);
      for (let index = 0; index < chunk.length; index += 1) {
        if (!Number.isFinite(chunk[index])) throw new Error('The recording contains invalid audio samples.');
      }
      let text = '';
      if (!isSilent(chunk)) {
        // The language of a recording is detected once, on its first audible part.
        ({ text, language: spoken } = await recognize(chunk, spoken));
        if (!languages.includes(spoken)) languages.push(spoken);
      }
      segments.push({ start: start / SAMPLE_RATE, end: end / SAMPLE_RATE, text, language: text ? spoken : '' });
      report(id, 'transcribe', `Finished part ${part}.`, end / samples.length);
    }
    return {
      text: segments.map((segment) => segment.text).filter(Boolean).join(' '),
      languages,
      duration_seconds: samples.length / SAMPLE_RATE,
      segments,
    };
  } finally {
    transcribing = false;
  }
}

self.onmessage = async ({ data }) => {
  const { id, type } = data;
  try {
    let result;
    if (type === 'init') {
      await load(id, data);
      result = { ready: true, model: { name: MODELS[active].name, repository: MODELS[active].repo, revision: MODELS[active].revision, runtime: MODELS[active].device === 'webgpu' ? 'WebGPU' : 'WebAssembly' } };
    } else if (type === 'transcribe') {
      result = await transcribe(data.samples, data.language, id);
    } else {
      throw new Error('Unknown speech model request.');
    }
    self.postMessage({ id, type: 'result', result });
  } catch (error) {
    self.postMessage({ id, type: 'error', error: error.message || 'Local transcription failed.' });
  }
};
