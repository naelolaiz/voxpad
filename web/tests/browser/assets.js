import { mkdir, readFile, rename, stat, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

import { fileUrl, MODELS } from '../../src/models.js';

// The tests choose Whisper tiny: its files are small enough to serve from disk.
const model = MODELS.tiny;

export const assetDirectory = fileURLToPath(new URL('../../node_modules/.cache/whisper/', import.meta.url));
export const assets = Object.entries(model.files).map(([file, { size, sha256 }]) => ({
  name: sha256,
  url: fileUrl(model, file),
  size,
  sha256,
}));

export const assetHashes = () => Object.fromEntries(assets.map(({ url, sha256 }) => [url, sha256]));

const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

/** Fetch with a few retries: the model host rate-limits shared CI addresses. */
async function download(url) {
  for (let attempt = 1; ; attempt += 1) {
    const response = await fetch(url, { signal: AbortSignal.timeout(600000) });
    if (response.ok) return new Uint8Array(await response.arrayBuffer());
    if ((response.status !== 429 && response.status < 500) || attempt === 4) {
      throw new Error(`Could not fetch test asset ${url}: ${response.status}`);
    }
    const seconds = Math.min(Number(response.headers.get('Retry-After')) || 2 ** attempt, 60);
    await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
  }
}

export default async function setup() {
  await mkdir(assetDirectory, { recursive: true });
  for (const { name, url, size, sha256 } of assets) {
    const path = `${assetDirectory}${name}`;
    // Files are named by their digest and written atomically, so a present file of the right size is intact.
    try { if ((await stat(path)).size === size) continue; } catch { /* Download public, pinned model files only. */ }
    const bytes = await download(url);
    if (digest(bytes) !== sha256) throw new Error(`Test asset ${url} does not match its pinned SHA-256.`);
    await writeFile(`${path}.partial`, bytes);
    await rename(`${path}.partial`, path);
  }
}

export const readAsset = ({ name }) => readFile(`${assetDirectory}${name}`);
