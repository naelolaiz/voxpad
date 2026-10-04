/** Decode a recording to the 16 kHz mono samples Whistle expects. */

const SAMPLE_RATE = 16000;
const OPUS_HEAD = [0x4f, 0x70, 0x75, 0x73, 0x48, 0x65, 0x61, 0x64];

function isOggOpus(bytes) {
  if (bytes.length < 12 || bytes[0] !== 0x4f || bytes[1] !== 0x67 || bytes[2] !== 0x67 || bytes[3] !== 0x53) return false;
  const last = Math.min(bytes.length - OPUS_HEAD.length, 4096);
  for (let offset = 4; offset <= last; offset += 1) {
    if (OPUS_HEAD.every((byte, index) => bytes[offset + index] === byte)) return true;
  }
  return false;
}

function downmix(channels, frames, sampleRate) {
  if (sampleRate !== SAMPLE_RATE) throw new Error(`Audio decoder returned an unsupported sample rate: ${sampleRate}.`);
  if (!Number.isSafeInteger(frames) || frames <= 0) throw new Error('The audio file contains no decoded audio.');
  if (!Array.isArray(channels) || channels.length === 0
      || channels.some((channel) => !(channel instanceof Float32Array) || channel.length !== frames)) {
    throw new Error('The audio decoder returned incomplete audio channels.');
  }
  const mono = new Float32Array(frames);
  const gain = 1 / channels.length;
  for (const channel of channels) {
    for (let frame = 0; frame < frames; frame += 1) {
      const sample = channel[frame];
      if (!Number.isFinite(sample)) throw new Error('The audio decoder returned an invalid audio sample.');
      mono[frame] += sample * gain;
    }
  }
  return mono;
}

async function decodeNative(bytes) {
  const Offline = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
  const Online = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!Offline && !Online) throw new Error('This browser does not provide an audio decoder.');
  // A context created at 16 kHz makes decodeAudioData resample for us.
  const context = Offline ? new Offline(1, 1, SAMPLE_RATE) : new Online({ sampleRate: SAMPLE_RATE });
  try {
    // decodeAudioData detaches its input. Copy the exact view so the caller's
    // bytes stay readable for the fallback decoder and for retries.
    const buffer = await context.decodeAudioData(new Uint8Array(bytes).buffer);
    if (!Number.isSafeInteger(buffer.numberOfChannels) || buffer.numberOfChannels <= 0) {
      throw new Error('The audio file contains no audio channels.');
    }
    const channels = Array.from({ length: buffer.numberOfChannels }, (_, channel) => buffer.getChannelData(channel));
    return downmix(channels, buffer.length, buffer.sampleRate);
  } finally {
    if (typeof context.close === 'function') await context.close().catch(() => {});
  }
}

async function decodeOggOpus(bytes) {
  const { OggOpusDecoder } = await import('ogg-opus-decoder');
  const decoder = new OggOpusDecoder({ sampleRate: SAMPLE_RATE });
  try {
    await decoder.ready;
    const decoded = await decoder.decodeFile(bytes);
    if (decoded.errors.length !== 0) throw new Error('The Ogg/Opus file contains damaged audio packets.');
    return downmix(decoded.channelData, decoded.samplesDecoded, decoded.sampleRate);
  } finally {
    decoder.free();
  }
}

/**
 * Decode audio bytes to a mono Float32Array at 16 kHz. The browser's decoder is
 * tried first; WhatsApp's Ogg/Opus falls back to WebAssembly where the browser
 * cannot decode it (notably Safari).
 */
export async function decodeAudio(bytes) {
  if (!(bytes instanceof Uint8Array)) throw new TypeError('Audio data must be a Uint8Array.');
  if (bytes.length === 0) throw new Error('The audio file is empty.');
  try {
    return await decodeNative(bytes);
  } catch (nativeError) {
    if (!isOggOpus(bytes)) {
      throw new Error('This browser could not decode this audio file. Try another browser or convert it to WAV.', { cause: nativeError });
    }
    try {
      return await decodeOggOpus(bytes);
    } catch (error) {
      throw new Error(`Could not decode this Ogg/Opus audio file: ${error.message}`, { cause: error });
    }
  }
}
