/** Split long recordings for Whisper, which processes at most 30 seconds per call. */

export const SAMPLE_RATE = 16000;
export const CHUNK_SAMPLES = 30 * SAMPLE_RATE;
// A chunk ends at the quietest tenth of a second within its last five seconds.
const BOUNDARY_SEARCH_SAMPLES = 5 * SAMPLE_RATE;
const BOUNDARY_WINDOW_SAMPLES = SAMPLE_RATE / 10;

/**
 * Return where the chunk starting at `start` ends. A chunk that cannot hold the
 * rest of the recording ends at its quietest moment, so fewer words are cut.
 */
export function chunkEnd(samples, start) {
  const limit = start + CHUNK_SAMPLES;
  if (limit >= samples.length) return samples.length;
  const first = limit - BOUNDARY_SEARCH_SAMPLES;
  const energy = new Float64Array(BOUNDARY_SEARCH_SAMPLES);
  let total = 0;
  for (let index = 0; index < BOUNDARY_SEARCH_SAMPLES; index += 1) {
    total += samples[first + index] ** 2;
    energy[index] = total;
  }
  let end = limit;
  let quietest = Infinity;
  for (let index = BOUNDARY_WINDOW_SAMPLES; index < BOUNDARY_SEARCH_SAMPLES; index += 1) {
    const windowEnergy = energy[index] - energy[index - BOUNDARY_WINDOW_SAMPLES];
    // Of equally quiet windows take the latest, which keeps chunks long.
    if (windowEnergy <= quietest) {
      quietest = windowEnergy;
      end = first + index + 1 - BOUNDARY_WINDOW_SAMPLES / 2;
    }
  }
  return end;
}
