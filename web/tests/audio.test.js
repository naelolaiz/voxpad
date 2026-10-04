import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import { decodeAudio } from '../src/audio.js';

function useOfflineContext(t, Context) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'OfflineAudioContext');
  Object.defineProperty(globalThis, 'OfflineAudioContext', {
    value: Context,
    configurable: true,
    writable: true,
  });
  t.after(() => {
    if (previous) {
      Object.defineProperty(globalThis, 'OfflineAudioContext', previous);
    } else {
      delete globalThis.OfflineAudioContext;
    }
  });
}

function audioBuffer(channels, sampleRate = 16000) {
  return {
    numberOfChannels: channels.length,
    length: channels[0].length,
    sampleRate,
    getChannelData: (channel) => channels[channel],
  };
}

test('native decoding copies the exact byte view, survives detachment, and downmixes every frame', async (t) => {
  const frames = 40017;
  const left = new Float32Array(frames);
  const right = new Float32Array(frames);
  left[0] = 1;
  right[0] = -1;
  left[1] = 0.25;
  right[1] = 0.75;
  left[frames - 1] = 1;
  right[frames - 1] = 0.5;
  let attempts = 0;

  useOfflineContext(t, class {
    constructor(channels, length, sampleRate) {
      assert.deepEqual([channels, length, sampleRate], [1, 1, 16000]);
    }

    async decodeAudioData(input) {
      attempts += 1;
      assert.ok(input instanceof ArrayBuffer);
      assert.deepEqual(Array.from(new Uint8Array(input)), [10, 20, 30]);
      structuredClone(input, { transfer: [input] });
      assert.equal(input.byteLength, 0);
      return audioBuffer([left, right]);
    }
  });

  const backing = new Uint8Array([91, 92, 10, 20, 30, 93]);
  const view = backing.subarray(2, 5);
  const mono = await decodeAudio(view);

  assert.equal(attempts, 1);
  assert.ok(mono instanceof Float32Array);
  assert.equal(mono.length, frames);
  assert.equal(mono[0], 0);
  assert.equal(mono[1], 0.5);
  assert.equal(mono[frames - 1], 0.75);
  assert.ok(mono.every(Number.isFinite));
  assert.deepEqual(Array.from(backing), [91, 92, 10, 20, 30, 93]);
});

test('real Ogg/Opus fallback decodes the complete speech fixture after native detachment and failure', async (t) => {
  const base64 = await readFile(new URL('./fixtures/speech.opus.base64', import.meta.url), 'utf8');
  const bytes = new Uint8Array(Buffer.from(base64, 'base64'));
  const original = bytes.slice();
  let attempts = 0;

  useOfflineContext(t, class {
    async decodeAudioData(input) {
      attempts += 1;
      assert.deepEqual(new Uint8Array(input), original);
      structuredClone(input, { transfer: [input] });
      assert.equal(input.byteLength, 0);
      throw new DOMException('Ogg is unsupported by this native decoder.', 'EncodingError');
    }
  });

  const mono = await decodeAudio(bytes);

  assert.equal(attempts, 1);
  assert.ok(mono instanceof Float32Array);
  // The fixture's final Ogg granule is 197202, with a pre-skip of 312.
  // Its 196890 frames at 48 kHz become exactly 65630 frames at 16 kHz.
  assert.equal(mono.length, 65630);
  assert.ok(mono.every(Number.isFinite));
  assert.ok(mono.some((sample) => Math.abs(sample) > 0.01), 'decoded speech has an audible signal');
  assert.deepEqual(bytes, original);
});

test('empty and incorrectly typed input fail before creating a browser decoder', async (t) => {
  let attempts = 0;
  useOfflineContext(t, class {
    constructor() {
      attempts += 1;
      throw new Error('Input validation should run before decoder creation.');
    }
  });

  await assert.rejects(decodeAudio(new Uint8Array()), /audio file is empty/i);
  await assert.rejects(decodeAudio(new ArrayBuffer(4)), TypeError);
  await assert.rejects(decodeAudio([1, 2, 3]), /Uint8Array/);
  assert.equal(attempts, 0);
});

test('invalid non-Opus bytes produce a per-file error and preserve the native cause', async (t) => {
  const nativeError = new DOMException('The audio data is malformed.', 'EncodingError');
  useOfflineContext(t, class {
    async decodeAudioData(input) {
      structuredClone(input, { transfer: [input] });
      throw nativeError;
    }
  });

  const bytes = new Uint8Array([0, 1, 2, 3, 4]);
  await assert.rejects(decodeAudio(bytes), (error) => {
    assert.match(error.message, /browser could not decode this audio file/i);
    assert.equal(error.cause, nativeError);
    return true;
  });
  assert.deepEqual(Array.from(bytes), [0, 1, 2, 3, 4]);
});

test('native PCM validation rejects empty audio, unexpected rates, and non-finite samples', async (t) => {
  let decoded;
  useOfflineContext(t, class {
    async decodeAudioData() {
      return decoded;
    }
  });

  for (const [buffer, message] of [
    [audioBuffer([new Float32Array()]), /no decoded audio/i],
    [audioBuffer([new Float32Array([0.5])], 48000), /unsupported sample rate/i],
    [audioBuffer([new Float32Array([NaN])]), /invalid audio sample/i],
  ]) {
    decoded = buffer;
    await assert.rejects(decodeAudio(new Uint8Array([1, 2, 3])), (error) => {
      assert.match(error.cause.message, message);
      return true;
    });
  }
});
