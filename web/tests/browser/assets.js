import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';

export const assetDirectory = fileURLToPath(new URL('../../node_modules/.cache/whistle/', import.meta.url));
// The same pinned revisions and SHA-256 digests as src/whistle-worker.js.
export const assets = [
  {
    name: 'needle.js',
    url: 'https://huggingface.co/Cactus-Compute/needle3/resolve/c7c415a3d1b3d929014bc6e866d51ebb971f7089/wasm/needle.js',
    sha256: 'f3f7366dcad9555b792ee519d2518f3c506038bcb2ffd179e76e850000749359',
  },
  {
    name: 'needle.wasm',
    url: 'https://huggingface.co/Cactus-Compute/needle3/resolve/c7c415a3d1b3d929014bc6e866d51ebb971f7089/wasm/needle.wasm',
    sha256: 'c19b9ddf9c7de4eb4f37e5f1811c5bbea9f099041d2a27284daf89789ee8523d',
  },
  {
    name: 'whistle.cact',
    url: 'https://huggingface.co/Cactus-Compute/whistle/resolve/b358ddadd89b7a713b5aa131f23032d3cca1b251/whistle.cact',
    sha256: 'b6e02f048568ac5d01a2042556c658061e699acbc0aa2a1439f52f3d461dffeb',
  },
];

export const assetHashes = () => Object.fromEntries(assets.map(({ url, sha256 }) => [url, sha256]));

const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

export default async function setup() {
  await mkdir(assetDirectory, { recursive: true });
  for (const { name, url, sha256 } of assets) {
    const path = `${assetDirectory}${name}`;
    // A stale or damaged copy, for example from a restored CI cache, is replaced.
    try { if (digest(await readFile(path)) === sha256) continue; } catch { /* Download public, pinned engine/model assets only. */ }
    const response = await fetch(url, { signal: AbortSignal.timeout(60000) });
    if (!response.ok) throw new Error(`Could not fetch test asset ${name}: ${response.status}`);
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (digest(bytes) !== sha256) throw new Error(`Test asset ${name} does not match its pinned SHA-256.`);
    await writeFile(path, bytes);
  }
}
