import { cp, mkdir, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

// GitHub Pages cannot send response headers, so the policy ships inside the
// built page. It limits connections to this site and the model host, which
// backs the promise that an export is never uploaded. Workers started from
// blob: URLs inherit it. Voice messages play from object URLs of bytes held
// in the tab, so media may come from blob: and from nowhere else. The
// development server injects styles inline, so builds only.
const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'self' blob: 'wasm-unsafe-eval'",
  'worker-src blob:',
  'child-src blob:',
  "style-src 'self'",
  "img-src 'self'",
  'media-src blob:',
  "connect-src 'self' https://huggingface.co https://*.huggingface.co https://*.hf.co",
  "base-uri 'none'",
  "form-action 'none'",
].join('; ');

const contentSecurityPolicy = {
  name: 'voxpad-content-security-policy',
  apply: 'build',
  transformIndexHtml: () => [{
    tag: 'meta',
    attrs: { 'http-equiv': 'Content-Security-Policy', content: CONTENT_SECURITY_POLICY },
    injectTo: 'head-prepend',
  }],
};

// The speech runtime's WebAssembly files are published with the app, so the
// page never loads code from a CDN.
const speechRuntime = {
  name: 'voxpad-speech-runtime',
  apply: 'build',
  async closeBundle() {
    const source = new URL('./node_modules/onnxruntime-web/dist/', import.meta.url);
    const target = new URL('./dist/ort/', import.meta.url);
    await mkdir(target, { recursive: true });
    for (const name of await readdir(source)) {
      if (name.startsWith('ort-wasm-simd-threaded')) await cp(new URL(name, source), new URL(name, target));
    }
  },
};

export default defineConfig({
  base: './',
  build: { target: 'es2022' },
  // The viewer is shared with the Python package and lives outside this folder. The development
  // server may read it there, and nothing else of the repository.
  server: { fs: { allow: ['.', '../voxpad/viewer'].map((path) => fileURLToPath(new URL(path, import.meta.url))) } },
  // One self-contained module, so the worker can be started from a blob: URL.
  worker: { format: 'es', rollupOptions: { output: { inlineDynamicImports: true } } },
  plugins: [contentSecurityPolicy, speechRuntime],
});
