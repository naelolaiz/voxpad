import assert from 'node:assert/strict';
import test from 'node:test';

import { chunkEnd, CHUNK_SAMPLES, SAMPLE_RATE } from '../src/chunks.js';
import { downloadBytes, fileUrl, LANGUAGES, MODELS } from '../src/models.js';

function tone(seconds, silences = []) {
  const samples = new Float32Array(seconds * SAMPLE_RATE);
  // The phase keeps the tone away from zero where the silences below end.
  for (let index = 0; index < samples.length; index += 1) samples[index] = 0.2 * Math.sin(2 * Math.PI * 440 * index / SAMPLE_RATE + 1);
  for (const [from, to] of silences) samples.fill(0, Math.round(from * SAMPLE_RATE), Math.round(to * SAMPLE_RATE));
  return samples;
}

test('every model file is pinned to a revision, a size and a SHA-256', () => {
  assert.deepEqual(Object.keys(MODELS), ['turbo', 'small', 'tiny']);
  for (const model of Object.values(MODELS)) {
    assert.match(model.revision, /^[0-9a-f]{40}$/);
    assert.ok(['webgpu', 'wasm'].includes(model.device));
    const files = Object.entries(model.files);
    assert.ok(files.some(([name]) => name.includes('encoder_model')) && files.some(([name]) => name.includes('decoder_model_merged')));
    for (const [name, { size, sha256 }] of files) {
      assert.ok(Number.isSafeInteger(size) && size > 0, name);
      assert.match(sha256, /^[0-9a-f]{64}$/, name);
      assert.equal(fileUrl(model, name), `https://huggingface.co/${model.repo}/resolve/${model.revision}/${name}`);
    }
    assert.equal(downloadBytes(model), files.reduce((total, [, { size }]) => total + size, 0));
  }
  assert.ok(downloadBytes(MODELS.small) < downloadBytes(MODELS.turbo));
  assert.equal(new Set(LANGUAGES).size, LANGUAGES.length);
  assert.ok(['en', 'es', 'de', 'zh'].every((code) => LANGUAGES.includes(code)));
});

test('recordings that fit in one chunk are not split', () => {
  assert.equal(CHUNK_SAMPLES, 30 * SAMPLE_RATE);
  assert.equal(chunkEnd(tone(4), 0), 4 * SAMPLE_RATE);
  assert.equal(chunkEnd(tone(30), 0), 30 * SAMPLE_RATE);
  assert.equal(chunkEnd(tone(40), 20 * SAMPLE_RATE), 40 * SAMPLE_RATE);
});

test('long recordings are cut inside a pause near the 30-second limit', () => {
  const samples = tone(61, [[27, 27.5], [55.2, 55.6]]);
  const first = chunkEnd(samples, 0);
  // The last fully silent tenth of a second ends at 27.5 s; the cut is its middle, 27.45 s.
  assert.equal(first, 439200);
  const second = chunkEnd(samples, first);
  assert.equal(second, 888800);
  assert.equal(chunkEnd(samples, second), samples.length);
});

test('speech without a pause is still cut within the last five seconds', () => {
  const samples = tone(45);
  const end = chunkEnd(samples, 0);
  assert.ok(end > 25 * SAMPLE_RATE && end <= 30 * SAMPLE_RATE, `cut at ${end / SAMPLE_RATE} s`);
  // A pause before the search window is not used: chunks stay close to 30 s.
  const early = chunkEnd(tone(45, [[10, 12]]), 0);
  assert.ok(early > 25 * SAMPLE_RATE && early <= 30 * SAMPLE_RATE, `cut at ${early / SAMPLE_RATE} s`);
});
