import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

export const assetDirectory = fileURLToPath(new URL('../../node_modules/.cache/whistle/', import.meta.url));
export const assets = [
  { name: 'needle.js', url: 'https://huggingface.co/Cactus-Compute/needle3/resolve/c7c415a3d1b3d929014bc6e866d51ebb971f7089/wasm/needle.js' },
  { name: 'needle.wasm', url: 'https://huggingface.co/Cactus-Compute/needle3/resolve/c7c415a3d1b3d929014bc6e866d51ebb971f7089/wasm/needle.wasm' },
  { name: 'whistle.cact', url: 'https://huggingface.co/Cactus-Compute/whistle/resolve/b358ddadd89b7a713b5aa131f23032d3cca1b251/whistle.cact' },
];

export async function assetHashes() {
  return Object.fromEntries(await Promise.all(assets.map(async ({ name, url }) => [
    url, createHash('sha256').update(await readFile(`${assetDirectory}${name}`)).digest('hex'),
  ])));
}

export default async function setup() {
  await mkdir(assetDirectory, { recursive: true });
  for (const { name, url } of assets) {
    const path = `${assetDirectory}${name}`;
    try { if ((await readFile(path)).length) continue; } catch { /* Download public, pinned engine/model assets only. */ }
    const response = await fetch(url, { signal: AbortSignal.timeout(60000) });
    if (!response.ok) throw new Error(`Could not fetch test asset ${name}: ${response.status}`);
    await writeFile(path, new Uint8Array(await response.arrayBuffer()));
  }
}
