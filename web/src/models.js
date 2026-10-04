/**
 * The Whisper models the app can run, pinned to exact revisions. Every file is
 * checked against its SHA-256 before it is used or cached.
 */

export const MODEL_HOST = 'https://huggingface.co/';

export const MODELS = {
  // Best accuracy. Needs WebGPU with 16-bit float support.
  turbo: {
    name: 'Whisper large-v3-turbo',
    repo: 'onnx-community/whisper-large-v3-turbo',
    revision: '360ebcde2559d60bb474678be3c1de9ef347d01a',
    device: 'webgpu',
    dtype: { encoder_model: 'fp16', decoder_model_merged: 'q4f16' },
    files: {
      'config.json': { size: 1332, sha256: '35cd83669f75bc2867f3b3a4461850392d5e308cd6ea951c3700539883c28df1' },
      'generation_config.json': { size: 3897, sha256: '16f95291d2f47c944d3c2b19390bba7965666555c1ea2a0bdc850d1fab45612f' },
      'preprocessor_config.json': { size: 340, sha256: '7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711' },
      'tokenizer.json': { size: 2480617, sha256: '6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca' },
      'tokenizer_config.json': { size: 282843, sha256: '844b642c73a91359722f47b35705f7174686df33d252695d8572cf9ac03a6389' },
      'onnx/encoder_model_fp16.onnx': { size: 1274342603, sha256: 'fdadc70836e6b028fd5e580417c312208dad073d2d01e509e2d127c1373399d8' },
      'onnx/decoder_model_merged_q4f16.onnx': { size: 193505017, sha256: '45981cdd958a4c8e1447839850d2e6e27e30974ccbe31b4a1e5ebe9ad8965a5f' },
    },
  },
  // Runs everywhere on WebAssembly.
  small: {
    name: 'Whisper small',
    repo: 'onnx-community/whisper-small',
    revision: '36050c46d777d46dc4b5f43f6d90574fc38f8732',
    device: 'wasm',
    dtype: 'q8',
    files: {
      'config.json': { size: 2227, sha256: '457854d452f17661e197d74aee12b8e74fb75ba30ebfaa7426d0d61ea1e08a18' },
      'generation_config.json': { size: 3893, sha256: 'f538b28220c6a6d6f1af1458d4141cacb4ef4963df3de98a19490440c412ddf0' },
      'preprocessor_config.json': { size: 339, sha256: 'a6a76d28c93edb273669eb9e0b0636a2bddbb1272c3261e47b7ca6dfdbac1b8d' },
      'tokenizer.json': { size: 2480466, sha256: '27fc476bfe7f17299480be2273fc0608e4d5a99aba2ab5dec5374b4482d1a566' },
      'tokenizer_config.json': { size: 282683, sha256: '2a4c4281cf9f51ac6ccc406fdc711a087afe6530f671fa7b80953edc498275ce' },
      'onnx/encoder_model_quantized.onnx': { size: 92326160, sha256: 'a43a83f3c5361cd591cfa7c36f14b43cf7cb22f47a415cc14a8d557be800fa92' },
      'onnx/decoder_model_merged_quantized.onnx': { size: 156750845, sha256: 'ec07c3cbb64172c39791e26ee870a65ac22b458c36722bfe2776b3dbf741e0c9' },
    },
  },
  // Fastest and least accurate; only used when chosen. The browser tests run it.
  tiny: {
    name: 'Whisper tiny',
    repo: 'onnx-community/whisper-tiny',
    revision: 'ff4177021cc41f7db950912b73ea4fdf7d01d8e7',
    device: 'wasm',
    dtype: 'q8',
    files: {
      'config.json': { size: 2243, sha256: '46aeea0a406afbeb563fc8e59ca10609203df4299af6a83f73752fef369efd2d' },
      'generation_config.json': { size: 3772, sha256: 'f5c67e5a4f7102f8cb4d058bc95da276bbc19eeec997267c3bb0f25ef68facd1' },
      'preprocessor_config.json': { size: 339, sha256: 'a6a76d28c93edb273669eb9e0b0636a2bddbb1272c3261e47b7ca6dfdbac1b8d' },
      'tokenizer.json': { size: 2480466, sha256: '27fc476bfe7f17299480be2273fc0608e4d5a99aba2ab5dec5374b4482d1a566' },
      'tokenizer_config.json': { size: 282683, sha256: '2a4c4281cf9f51ac6ccc406fdc711a087afe6530f671fa7b80953edc498275ce' },
      'onnx/encoder_model_quantized.onnx': { size: 10124990, sha256: '2af4a414ca47aa30f61246017e5fe82b0a8d229281d1255ba666a2a7f6b84d19' },
      'onnx/decoder_model_merged_quantized.onnx': { size: 30719241, sha256: '25e807a962b6349356d0ea5d0dfe530b7e5bf0e2a484aeca0359d03143faddd3' },
    },
  },
};

/** The spoken languages Whisper recognizes, as its language codes. */
export const LANGUAGES = 'af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl gu haw ha he hi hr ht hu hy id is it ja jw ka kk km kn ko la lb ln lo lt lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk sl sn so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh'.split(' ');

export const fileUrl = (model, file) => `${MODEL_HOST}${model.repo}/resolve/${model.revision}/${file}`;

export const downloadBytes = (model) => Object.values(model.files).reduce((total, file) => total + file.size, 0);
