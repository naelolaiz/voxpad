import { defineConfig } from 'vite';

// GitHub Pages cannot send response headers, so the policy ships inside the
// built page. It limits connections to this site and the model host, which
// backs the promise that an export is never uploaded. Workers started from
// blob: URLs inherit it; blob: scripts are needed to start the verified speech
// runtime. The development server injects styles inline, so builds only.
const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'self' blob: 'wasm-unsafe-eval'",
  'worker-src blob:',
  'child-src blob:',
  "style-src 'self'",
  "img-src 'self'",
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

export default defineConfig({
  base: './',
  build: { target: 'es2022' },
  plugins: [contentSecurityPolicy],
});
