/** Main-thread client for the Whistle worker. Recordings never leave the tab. */

const LANGUAGES = new Set(['en', 'de', 'fr', 'es', 'it', 'nl', 'pl']);

function checkLanguage(language) {
  if (language != null && !LANGUAGES.has(language)) {
    throw new Error('Choose English, German, French, Spanish, Italian, Dutch, or Polish.');
  }
}

function cancelled() {
  return new DOMException('Transcription cancelled.', 'AbortError');
}

export class Transcriber {
  constructor({ onProgress } = {}) {
    this.onProgress = onProgress;
    this.worker = null;
    this.requests = new Map();
    this.nextId = 1;
    this.initializing = null;
    this.ready = false;
    this.language = null;
    this.currentRun = null;
  }

  createWorker() {
    if (typeof Worker === 'undefined' || typeof WebAssembly === 'undefined') {
      throw new Error('This browser cannot run local transcription. Try a recent Chrome, Firefox, Edge, or Safari.');
    }
    const worker = new Worker(new URL('./whistle-worker.js', import.meta.url));
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
      this.stop(new Error(event.message || 'The speech engine stopped. Please try again.'));
    };
    worker.onerror = failed;
    worker.onmessageerror = failed;
    this.worker = worker;
    return worker;
  }

  request(type, payload = {}, transfer = []) {
    const worker = this.worker || this.createWorker();
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

  /** Download and start Whistle. An optional language becomes the default. */
  async init(language) {
    if (arguments.length > 0) {
      checkLanguage(language);
      this.language = language ?? null;
    }
    if (this.ready) return this;
    if (!this.initializing) {
      const worker = this.worker || this.createWorker();
      const initializing = this.request('init').then(() => {
        if (this.worker !== worker) throw cancelled();
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
    this.worker?.terminate();
    this.worker = null;
    this.ready = false;
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
