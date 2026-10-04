import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

import { assets } from './browser/assets.js';

const SAMPLE_RATE = 16000;
const source = await readFile(new URL('../src/whistle-worker.js', import.meta.url), 'utf8');

/** The worker is a classic script; run it in a sandbox to reach its functions. */
function loadWorker() {
  const context = vm.createContext({ self: {} });
  vm.runInContext(source, context);
  return context;
}

function tone(seconds, silences = []) {
  const samples = new Float32Array(seconds * SAMPLE_RATE);
  // The phase keeps the tone away from zero where the silences below end.
  for (let index = 0; index < samples.length; index += 1) samples[index] = 0.2 * Math.sin(2 * Math.PI * 440 * index / SAMPLE_RATE + 1);
  for (const [from, to] of silences) samples.fill(0, Math.round(from * SAMPLE_RATE), Math.round(to * SAMPLE_RATE));
  return samples;
}

test('the worker pins the same asset revisions and digests as the browser tests', () => {
  assert.equal(assets.length, 3);
  for (const { url, sha256 } of assets) {
    assert.match(sha256, /^[0-9a-f]{64}$/);
    assert.ok(source.includes(`'${url}'`), `worker pins ${url}`);
    assert.ok(source.includes(`'${sha256}'`), `worker pins the digest of ${url}`);
  }
});

test('recordings that fit in one chunk are not split', () => {
  const { chunkEnd } = loadWorker();
  assert.equal(chunkEnd(tone(4), 0), 4 * SAMPLE_RATE);
  assert.equal(chunkEnd(tone(30), 0), 30 * SAMPLE_RATE);
  assert.equal(chunkEnd(tone(40), 20 * SAMPLE_RATE), 40 * SAMPLE_RATE);
});

test('long recordings are cut inside a pause near the 30-second limit', () => {
  const { chunkEnd } = loadWorker();
  const samples = tone(61, [[27, 27.5], [55.2, 55.6]]);
  const first = chunkEnd(samples, 0);
  // The last fully silent tenth of a second ends at 27.5 s; the cut is its middle, 27.45 s.
  assert.equal(first, 439200);
  const second = chunkEnd(samples, first);
  assert.equal(second, 888800);
  assert.equal(chunkEnd(samples, second), samples.length);
});

test('speech without a pause is still cut within the last five seconds', () => {
  const { chunkEnd } = loadWorker();
  const samples = tone(45);
  const end = chunkEnd(samples, 0);
  assert.ok(end > 25 * SAMPLE_RATE && end <= 30 * SAMPLE_RATE, `cut at ${end / SAMPLE_RATE} s`);
  // A pause before the search window is not used: chunks stay close to 30 s.
  const early = chunkEnd(tone(45, [[10, 12]]), 0);
  assert.ok(early > 25 * SAMPLE_RATE && early <= 30 * SAMPLE_RATE, `cut at ${early / SAMPLE_RATE} s`);
});
