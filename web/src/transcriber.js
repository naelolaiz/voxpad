/** Main-thread client for the Whisper worker. Recordings never leave the tab. */
import workerUrl from './whisper-worker.js?worker&url';
import { LANGUAGES } from './models.js';

// The speech runtime's WebAssembly files are served by this site, not a CDN.
const RUNTIME_PATH = import.meta.env.DEV
  ? new URL('/node_modules/onnxruntime-web/dist/', location.href).href
  : new URL('./ort/', document.baseURI).href;

let blobUrl = null;

/**
 * The built worker starts from a blob: URL so that it inherits this page's
 * Content-Security-Policy. A worker loaded by URL would not be bound by it.
 */
async function startWorker() {
  if (typeof Worker === 'undefined' || typeof WebAssembly === 'undefined') {
    throw new Error('This browser cannot run local transcription. Try a recent Chrome, Firefox, Edge, or Safari.');
  }
  if (import.meta.env.DEV) return new Worker(workerUrl, { type: 'module' });
  if (!blobUrl) {
    const response = await fetch(workerUrl);
    if (!response.ok) throw new Error('Could not load the transcription engine. Reload the page and try again.');
    blobUrl = URL.createObjectURL(new Blob([await response.text()], { type: 'text/javascript' }));
  }
  return new Worker(blobUrl, { type: 'module' });
}

function checkLanguage(language) {
  if (language != null && !LANGUAGES.includes(language)) throw new Error('Choose one of the listed languages.');
}

function cancelled() {
  return new DOMException('Transcription cancelled.', 'AbortError');
}

export class Transcriber {
  constructor({ onProgress } = {}) {
    this.onProgress = onProgress;
    this.worker = null;
    this.starting = null;
    this.epoch = 0;
    this.requests = new Map();
    this.nextId = 1;
    this.initializing = null;
    this.ready = false;
    this.language = null;
    this.model = null;
    this.currentRun = null;
  }

  async ensureWorker() {
    if (this.worker) return this.worker;
    if (!this.starting) {
      const epoch = this.epoch;
      const starting = startWorker().then((worker) => {
        if (epoch !== this.epoch) {
          worker.terminate();
          throw cancelled();
        }
        this.attach(worker);
        return worker;
      }).finally(() => {
        if (this.starting === starting) this.starting = null;
      });
      this.starting = starting;
    }
    return this.starting;
  }

  attach(worker) {
    worker.onmessage = ({ data }) => {
      if (this.worker !== worker) return;
      const request = this.requests.get(data.id);
      if (!request) return;
      if (data.type === 'progress') {
        if (typeof this.onProgress === 'function') {
          try {
            this.onProgress(data.progress);
          } catch (error) {
            console.error('Could not display transcription progress:', error);
          }
        }
        return;
      }
      this.requests.delete(data.id);
      if (data.type === 'result') request.resolve(data.result);
      else request.reject(new Error(data.error || 'Local transcription failed.'));
    };
    const failed = (event) => {
      if (this.worker !== worker) return;
      this.stop(new Error(event.message || 'The speech model stopped. Please try again.'));
    };
    worker.onerror = failed;
    worker.onmessageerror = failed;
    this.worker = worker;
  }

  async request(type, payload = {}, transfer = []) {
    const worker = await this.ensureWorker();
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.requests.set(id, { resolve, reject });
      try {
        worker.postMessage({ id, type, ...payload }, transfer);
      } catch (error) {
        this.requests.delete(id);
        reject(error);
      }
    });
  }

  /**
   * Download and start Whisper. An optional language becomes the default, and
   * `model` may be "small" to skip the larger model.
   */
  async init(language, { model } = {}) {
    if (arguments.length > 0) {
      checkLanguage(language);
      this.language = language ?? null;
    }
    if (this.ready) return this;
    if (!this.initializing) {
      const epoch = this.epoch;
      const initializing = this.request('init', { model, runtimePath: RUNTIME_PATH }).then((result) => {
        if (epoch !== this.epoch) throw cancelled();
        this.model = result.model;
        this.ready = true;
        return this;
      }).finally(() => {
        if (this.initializing === initializing) this.initializing = null;
      });
      this.initializing = initializing;
    }
    return this.initializing;
  }

  /** Transcribe 16 kHz mono samples. The caller's samples are left intact. */
  async transcribe(samples, options = {}) {
    const language = Object.hasOwn(options, 'language') ? options.language : this.language;
    checkLanguage(language);
    if (!(samples instanceof Float32Array) || samples.length === 0) throw new Error('The recording contains no decoded audio.');
    if (this.currentRun) throw new Error('A recording is already being transcribed.');
    const run = {};
    this.currentRun = run;
    try {
      await this.init();
      if (this.currentRun !== run) throw cancelled();
      const copy = samples.slice();
      return await this.request('transcribe', { samples: copy, language }, [copy.buffer]);
    } finally {
      if (this.currentRun === run) this.currentRun = null;
    }
  }

  stop(error) {
    this.epoch += 1;
    this.worker?.terminate();
    this.worker = null;
    this.ready = false;
    this.model = null;
    this.initializing = null;
    this.currentRun = null;
    for (const request of this.requests.values()) request.reject(error);
    this.requests.clear();
  }

  cancel() {
    this.stop(cancelled());
  }

  dispose() {
    this.cancel();
  }
}
