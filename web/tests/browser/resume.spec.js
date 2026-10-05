import { test, expect } from '@playwright/test';
import { zipSync, strToU8 } from 'fflate';
import { MODEL_HOST } from '../../src/models.js';
import { assetHashes } from './assets.js';
import {
  CHAT, FIRST, SECOND, SPOKEN, bubble, downloadText, expectLocalRequests, file, json, notices, play, record, serveAssets, speechFixture, stored, tab, watch,
} from './support.js';

// What may be selected with an export, or in its place: reports of earlier runs and a list of events.
const EARLIER = 'EARLIER-TRANSCRIPT-417 kept as it was';
const ZIP_ALONE = 'Choose one ZIP alone, or select the extracted chat and audio files together.';
const said = (timestamp, sender) => [{ chat_file: '_chat.txt', timestamp, sender }];
const problems = new WeakMap();

test.use({ actionTimeout: 15000 });

test.beforeEach(async ({ page }) => {
  problems.set(page, await watch(page));
});

test.afterEach(async ({ page }) => {
  expect(await problems.get(page)()).toEqual([]);
});

/** The export, with both recordings beside the chat. */
async function exportZip() {
  const speech = await speechFixture();
  return file('export.zip', zipSync({ '_chat.txt': strToU8(CHAT), [FIRST]: speech, [SECOND]: speech, 'photo.jpg': new Uint8Array([1, 2, 3]) }), 'application/zip');
}

/** What this app's Download JSON holds after a run that was stopped behind the first recording. */
function stoppedReport(bytes) {
  return {
    schema_version: 1, source: 'export.zip', sample_rate: 16000,
    chats: [{ file: '_chat.txt', original_text: CHAT, annotated_text: CHAT, messages: [] }],
    results: [
      { file: FIRST, bytes, messages: said('05/01/26, 09:05', 'Ana'), status: 'ok', text: EARLIER, languages: ['en'], segments: [{ start: 0, end: 4.1, text: EARLIER, language: 'en' }], duration_seconds: 4.1 },
      { file: SECOND, bytes, messages: said('06/01/26, 10:01', 'José'), status: 'pending', text: '', languages: [], segments: [] },
    ],
    warnings: [],
    model: { name: 'Whisper small', repository: 'onnx-community/whisper-small', revision: '36050c46d777d46dc4b5f43f6d90574fc38f8732', runtime: 'WebAssembly' },
  };
}

test('an export selected with the JSON of a stopped run continues where it stopped, and reopens complete', async ({ page, context }) => {
  const requests = record(context);
  await serveAssets(context);
  const speech = await speechFixture();
  await page.goto('/');
  await page.locator('#files').setInputFiles([await exportZip(), json('export_transcribed.json', stoppedReport(speech.length))]);
  await expect(page.locator('#source-name')).toHaveText('export.zip + export_transcribed.json');
  await expect(page.locator('#source-summary')).toHaveText('1 chat file · 2 voice messages');
  await expect(notices(page)).toHaveText(['1 earlier transcript imported from export_transcribed.json; 1 left to transcribe.']);
  await expect(page.locator('#result-summary')).toHaveText('1 transcribed · 1 pending. Originals preserved.');
  await expect(page.locator('#start')).toHaveText('Continue / retry transcription →');
  // The earlier transcript is in the conversation before anything is transcribed.
  await expect(bubble(page, 2).locator('.vp-transcript')).toHaveText(EARLIER);
  await expect(bubble(page, 2).locator('.vp-voice-status')).toHaveText('5 spoken words · en');
  await expect(bubble(page, 4).locator('.vp-voice-status')).toHaveText('Not transcribed yet');
  await page.evaluate(() => {
    window.__heard = [];
    const message = document.getElementById('progress-message');
    new MutationObserver(() => window.__heard.push(message.textContent)).observe(message, { childList: true, characterData: true, subtree: true });
  });
  await page.locator('#language').selectOption('en');
  await page.locator('#model').selectOption('tiny');
  await page.locator('#start').click();
  await expect(page.locator('#progress-message')).toHaveText('Done. Your downloads are ready.', { timeout: 300000 });
  // Whisper listened to the second recording only.
  expect((await page.evaluate(() => window.__heard)).filter((text) => text.startsWith('Listening to'))).toEqual([`Listening to ${SECOND}…`]);
  await expect(page.locator('#result-summary')).toHaveText('2 transcribed. Originals preserved. Transcribed with Whisper tiny.');
  await expect(page.locator('#start')).toHaveText('Transcribe again →');
  await expect(bubble(page, 2).locator('.vp-transcript')).toHaveText(EARLIER);
  await expect(bubble(page, 4).locator('.vp-transcript')).toContainText(/tomorrow/i);
  await expect(bubble(page, 4).locator('.vp-voice-time')).toHaveText('0:04');
  await tab(page, 'Activity').click();
  await expect(page.locator('#viewer').getByRole('table', { name: 'Activity per participant' }).locator('tbody tr').last()).toContainText('2 of 2');

  const text = await downloadText(page, 'Download JSON');
  const report = JSON.parse(text);
  const [first, second] = report.results;
  expect(report.model.name).toBe('Whisper tiny');
  // The transcript taken over says what made it; the new one was made by this run. Both record the recording's size.
  expect(first).toMatchObject({ file: FIRST, bytes: speech.length, status: 'ok', text: EARLIER, duration_seconds: 4.1, model: 'Whisper small', messages: said('05/01/26, 09:05', 'Ana') });
  expect(first.segments).toEqual([{ start: 0, end: 4.1, text: EARLIER, language: 'en' }]);
  expect(second).toMatchObject({ file: SECOND, bytes: speech.length, status: 'ok', messages: said('06/01/26, 10:01', 'José') });
  expect(second.text.toLowerCase()).toContain('tomorrow');
  expect(second).not.toHaveProperty('model');
  const log = await downloadText(page, 'Download text');
  expect(log).toContain(`${FIRST} (file attached)\r\n[Voice message transcript: ${EARLIER}]\r\n`);
  expect(log).toContain(`${SECOND} (file attached)\r\n[Voice message transcript: ${second.text}]\r\n`);
  expect(report.chats[0].annotated_text).toBe(log);
  // The browser keeps the speech model for next time, and nothing else.
  expect((await stored(page)).entries).toEqual(await assetHashes());

  // Another day: the same export with the JSON just downloaded. Nothing is left to transcribe, and no model is fetched.
  await page.reload();
  const before = requests.length;
  await page.locator('#files').setInputFiles([await exportZip(), file('export_transcribed.json', text, 'application/json')]);
  await expect(notices(page)).toHaveText(['2 earlier transcripts imported from export_transcribed.json; 0 left to transcribe.']);
  await expect(page.locator('#result-summary')).toHaveText('2 transcribed. Originals preserved.');
  await expect(page.locator('#start')).toHaveText('Transcribe again →');
  await expect(bubble(page, 2).locator('.vp-transcript')).toHaveText(EARLIER);
  await expect(bubble(page, 4).locator('.vp-transcript')).toHaveText(second.text);
  // The recordings are there as well: what was transcribed earlier can be played.
  expect((await play(page, 4)).error).toBe(0);
  const again = JSON.parse(await downloadText(page, 'Download JSON'));
  expect(again.results[0]).toEqual(first);
  expect(again.results[1]).toEqual({ ...second, model: 'Whisper tiny' });
  expect(await downloadText(page, 'Download text')).toBe(log);
  const later = requests.slice(before);
  expect(later.length).toBeGreaterThan(0);
  expect(later.filter((request) => !request.url.startsWith('blob:')).every((request) => new URL(request.url).hostname === '127.0.0.1')).toBe(true);

  // Throughout, the page sent nothing and stored nothing but the speech model (which a browser may drop on reload).
  expectLocalRequests(requests, { marker: FIRST });
  const kept = await stored(page);
  expect(await assetHashes()).toMatchObject(kept.entries);
  expect([kept.local, kept.session, kept.databases, kept.cookies]).toEqual([0, 0, [], '']);
});

test('transcripts taken over name their model only when it is another one, and Transcribe again starts from nothing', async ({ page, context }) => {
  // The model never arrives: this test stops before Whisper would start.
  await context.route(`${MODEL_HOST}**`, () => {});
  const earlier = stoppedReport((await speechFixture()).length);
  // What this page downloads when it transcribed nothing itself: one transcript says what made it, the report does not.
  earlier.model = { name: 'Whisper' };
  earlier.results[0].model = 'Whisper small';
  Object.assign(earlier.results[1], { status: 'ok', text: 'Segundo', languages: ['es'], duration_seconds: 4.1 });
  await page.goto('/');
  await page.locator('#files').setInputFiles([await exportZip(), json('export_transcribed.json', earlier)]);
  await expect(notices(page)).toHaveText(['2 earlier transcripts imported from export_transcribed.json; 0 left to transcribe.']);
  const report = JSON.parse(await downloadText(page, 'Download JSON'));
  expect(report.model).toEqual({ name: 'Whisper' });
  expect(report.results[0]).toMatchObject({ status: 'ok', text: EARLIER, model: 'Whisper small' });
  expect(report.results[1]).toMatchObject({ status: 'ok', text: 'Segundo' });
  expect(report.results[1]).not.toHaveProperty('model');
  await expect(bubble(page, 4).locator('.vp-transcript')).toHaveText('Segundo');
  await expect(page.locator('#start')).toHaveText('Transcribe again →');
  await page.locator('#model').selectOption('tiny');
  await page.locator('#start').click();
  // Every transcript is given up at once, in the conversation as in the summary.
  await expect(bubble(page, 4).locator('.vp-voice-status')).toHaveText('Not transcribed yet');
  await expect(bubble(page, 4).locator('.vp-transcript')).toHaveCount(0);
  await expect(bubble(page, 2).locator('.vp-voice-status')).toHaveText('Not transcribed yet');
  await page.locator('#cancel').click();
  await expect(page.locator('#result-summary')).toHaveText('0 transcribed · 2 pending. Originals preserved.');
  await expect(page.locator('#start')).toHaveText('Continue / retry transcription →');
});

test('a chat with transcripts and the report of the command show durations and spoken words without any audio', async ({ page, context }) => {
  const requests = record(context);
  // The annotated chat of a run that stopped behind the first recording, and the report of the run that finished.
  const lines = CHAT.split('\r\n');
  lines.splice(3, 0, `[Voice message transcript: ${SPOKEN}]`);
  const annotated = lines.join('\r\n');
  const results = [
    { file: FIRST, bytes: 9710, messages: said('05/01/26, 09:05', 'Ana'), status: 'ok', text: SPOKEN, languages: ['en'], duration_seconds: 73.9, segments: [{ start: 0, end: 73.9, text: SPOKEN, language: 'en' }] },
    { file: `media/${SECOND}`, bytes: 4410, messages: said('06/01/26, 10:01', 'José'), status: 'ok', text: 'Dos palabras', languages: ['es'], duration_seconds: 5.4, segments: [] },
  ];
  await page.goto('/');
  await page.locator('#files').setInputFiles([
    file('chat_with_transcripts.txt', annotated),
    json('transcripts.json', { source: 'export', model: 'large-v3-turbo', sample_rate: 16000, chunk_seconds: 30, results }),
  ]);
  await expect(page.locator('#source-name')).toHaveText('chat_with_transcripts.txt + transcripts.json');
  await expect(page.locator('#source-summary')).toHaveText('1 chat file · 0 voice messages');
  await expect(notices(page)).toHaveText(['2 earlier transcripts imported from transcripts.json; 0 left to transcribe.']);
  await expect(page.locator('#start')).toHaveText('Prepare downloads →');
  await expect(page.locator('#result-summary')).toHaveText('0 transcribed. Originals preserved. 2 earlier transcripts shown without their recordings.');
  // Length, transcript and spoken words of both voice messages, with nothing to play.
  await expect(bubble(page, 2).locator('.vp-voice-time')).toHaveText('1:14');
  await expect(bubble(page, 2).locator('.vp-transcript')).toHaveText(SPOKEN);
  await expect(bubble(page, 2).locator('.vp-voice-status')).toHaveText('10 spoken words · en');
  await expect(bubble(page, 4).locator('.vp-voice-time')).toHaveText('0:05');
  await expect(bubble(page, 4).locator('.vp-transcript')).toHaveText('Dos palabras');
  await expect(bubble(page, 4).locator('.vp-voice-status')).toHaveText('2 spoken words · es');
  expect(await page.locator('#viewer .vp-msg .vp-play').count()).toBe(0);
  await tab(page, 'Voice messages').click();
  await expect(page.locator('#viewer .vp-voice-head')).toContainText('Recordings are not included in this view.');
  await tab(page, 'Activity').click();
  const rows = page.locator('#viewer').getByRole('table', { name: 'Activity per participant' }).locator('tbody tr');
  // José, then Ana: messages, typed, spoken and total words, voice notes, transcribed, voice time.
  await expect(rows.nth(0).locator('td')).toHaveText(['3', '6', '2', '8', '1', '1 of 1', '5 s']);
  await expect(rows.nth(1).locator('td')).toHaveText(['2', '3', '10', '13', '1', '1 of 1', '1 min 14 s']);
  // Every voice message has its transcript and its length, so nothing is said to be missing.
  await expect(page.locator('#viewer .vp-panel-activity .vp-coverage')).toBeHidden();

  // The text download has every transcript once, in place; the JSON keeps the results as they were read.
  const log = await downloadText(page, 'Download text');
  const expected = CHAT.split('\r\n');
  expected.splice(5, 0, '[Voice message transcript: Dos palabras]');
  expected.splice(3, 0, `[Voice message transcript: ${SPOKEN}]`);
  expect(log).toBe(expected.join('\r\n'));
  const report = JSON.parse(await downloadText(page, 'Download JSON'));
  expect(report.results).toEqual(results.map((result) => ({ ...result, model: 'large-v3-turbo' })));
  expect(report.chats[0].original_text).toBe(annotated);
  expect(report.chats[0].annotated_text).toBe(log);
  expectLocalRequests(requests);
  expect(requests.every((request) => new URL(request.url).hostname === '127.0.0.1')).toBe(true);
});

test('a failure in an earlier report is written into the text, but never over a transcript the chat already has', async ({ page }) => {
  const failed = (name) => ({ file: name, messages: [], status: 'error', text: '', error: 'Could not decode audio' });
  const report = { source: 'export', model: 'large-v3-turbo', sample_rate: 16000, chunk_seconds: 30, results: [failed(FIRST), failed(SECOND)] };
  // The chat was annotated by a later run, which did transcribe the first recording.
  const annotated = CHAT.replace(`${FIRST} (file attached)\r\n`, `${FIRST} (file attached)\r\n[Voice message transcript: ${SPOKEN}]\r\n`);
  await page.goto('/');
  await page.locator('#files').setInputFiles([file('chat_with_transcripts.txt', annotated), json('transcripts.json', report)]);
  await expect(notices(page)).toHaveText(['No earlier transcripts were found in transcripts.json.']);
  await expect(bubble(page, 4).locator('.vp-voice-status')).toHaveText('Transcription failed: Could not decode audio');
  expect(await downloadText(page, 'Download text')).toBe(annotated.replace(`${SECOND} (file attached)\r\n`, `${SECOND} (file attached)\r\n[Voice message transcript: Error: Could not decode audio]\r\n`));
});

test('a report of the command alone says what to add, and the page stays usable', async ({ page }) => {
  const report = { source: 'export', model: 'large-v3-turbo', sample_rate: 16000, chunk_seconds: 30, results: [{ file: FIRST, messages: [], status: 'ok', text: SPOKEN }] };
  await page.goto('/');
  await page.locator('#files').setInputFiles(json('transcripts.json', report));
  await expect(page.locator('#error')).toHaveText('This report has no chat text. Select it together with the export ZIP or the chat .txt.');
  await expect(page.locator('#start')).toBeDisabled();
  for (const id of ['#selection', '#results', '#progress-panel', '#warnings']) await expect(page.locator(id)).toBeHidden();
  // What else is selected does not change that, as long as there is no chat.
  await page.locator('#files').setInputFiles([json('transcripts.json', report), file('events.txt', '2026-01-06 Dinner with Li\n'), file('photo.jpg', 'image', 'image/jpeg')]);
  await expect(page.locator('#error')).toHaveText('This report has no chat text. Select it together with the export ZIP or the chat .txt.');
  // A JSON that is no report is named, whatever it was selected with, and nothing of the selection is loaded.
  await page.locator('#files').setInputFiles([file('_chat.txt', CHAT), json('notes.json', { hello: 'world' })]);
  await expect(page.locator('#error')).toHaveText('notes.json is not a VoxPad transcript report.');
  await expect(page.locator('#results')).toBeHidden();
  // With its chat the same report is welcome.
  await page.locator('#files').setInputFiles([json('transcripts.json', report), file('_chat.txt', CHAT)]);
  await expect(page.locator('#error')).toBeHidden();
  await expect(bubble(page, 2).locator('.vp-transcript')).toHaveText(SPOKEN);
  await expect(bubble(page, 4).locator('.vp-voice-status')).toHaveText('Not transcribed yet');
  await expect(notices(page)).toHaveText(['1 earlier transcript imported from transcripts.json; 0 left to transcribe.']);
});

test('a JSON downloaded from this page reopens its conversation by itself, without the audio', async ({ page }) => {
  await page.goto('/');
  await page.locator('#files').setInputFiles(json('export_transcribed.json', stoppedReport(9710)));
  await expect(page.locator('#source-name')).toHaveText('export_transcribed.json');
  await expect(page.locator('#source-summary')).toHaveText('1 chat file · 0 voice messages');
  await expect(notices(page)).toHaveText(['1 earlier transcript imported from export_transcribed.json; 0 left to transcribe.']);
  await expect(page.locator('#viewer .vp-title')).toHaveText('José · Ana');
  await expect(bubble(page, 1).locator('.vp-text')).toHaveText('Buenos días, Ana');
  await expect(bubble(page, 2).locator('.vp-transcript')).toHaveText(EARLIER);
  await expect(bubble(page, 2).locator('.vp-voice-time')).toHaveText('0:04');
  await expect(bubble(page, 4).locator('.vp-voice-status')).toHaveText('Not transcribed yet');
  expect(await page.locator('#viewer .vp-msg .vp-play').count()).toBe(0);
  await expect(page.locator('#start')).toHaveText('Prepare downloads →');
  // The downloads carry the earlier work on: the transcript in its place, and the result for another day.
  const log = await downloadText(page, 'Download text');
  expect(log).toBe(CHAT.replace(`${FIRST} (file attached)\r\n`, `${FIRST} (file attached)\r\n[Voice message transcript: ${EARLIER}]\r\n`));
  const report = JSON.parse(await downloadText(page, 'Download JSON'));
  expect(report.chats).toHaveLength(1);
  expect(report.chats[0]).toMatchObject({ file: '_chat.txt', original_text: CHAT, annotated_text: log });
  expect(report.results).toEqual([{ ...stoppedReport(9710).results[0], model: 'Whisper small' }]);
  // A list of events may come with it, as with an export.
  await page.locator('#files').setInputFiles([file('events.txt', '2026-01-06 Dinner with Li\n'), json('export_transcribed.json', stoppedReport(9710))]);
  await expect(page.locator('#source-name')).toHaveText('export_transcribed.json + events.txt');
  await expect(notices(page)).toHaveText(['1 earlier transcript imported from export_transcribed.json; 0 left to transcribe.', 'Read 1 event from events.txt.']);
  await expect(page.locator('#viewer .vp-banner .vp-banner-label')).toHaveText(['Dinner with Li']);
  await expect(bubble(page, 2).locator('.vp-transcript')).toHaveText(EARLIER);
});

test('an events file selected with the export marks its days in the conversation', async ({ page }) => {
  const events = file('events.txt', '# What happened\r\n2026-01-05 First day back\r\n06/01/2026, Dinner with Li\r\nnot a date\r\n2026-01-06 | Li calls\r\n2026-01-20 Later\r\n');
  await page.goto('/');
  await page.locator('#files').setInputFiles([events, await exportZip()]);
  await expect(page.locator('#source-name')).toHaveText('export.zip + events.txt');
  await expect(page.locator('#source-summary')).toHaveText('1 chat file · 2 voice messages');
  await expect(notices(page)).toHaveText(['Read 4 events from events.txt (1 line without a date was skipped).']);
  await expect(tab(page, 'Events').locator('.vp-tab-count')).toHaveText('4');
  await expect(page.locator('#viewer .vp-banner .vp-banner-label')).toContainText(['First day back', 'Dinner with Li', 'Li calls']);
  await tab(page, 'Events').click();
  await expect(page.locator('#viewer .vp-event-card')).toHaveCount(4);
  await expect(page.locator('#viewer .vp-event-card').first()).toContainText('First day back');
  // Neither download carries the events: they are not part of the chat.
  expect(await downloadText(page, 'Download text')).toBe(CHAT);
  expect(await downloadText(page, 'Download JSON')).not.toContain('First day back');

  // Among loose files the chat is a text file as well; the one that is no chat is the list of events.
  await page.locator('#files').setInputFiles([file('notes.txt', '2026-01-06 Dinner with Li\n'), file('_chat.txt', CHAT)]);
  await expect(page.locator('#source-name')).toHaveText('2 selected files');
  await expect(page.locator('#source-summary')).toHaveText('1 chat file · 0 voice messages');
  await expect(notices(page)).toContainText(['Read 1 event from notes.txt.']);
  await expect(page.locator('#viewer .vp-banner .vp-banner-label')).toHaveText(['Dinner with Li']);

  // Beside a ZIP a text file can only be a list of events: a chat or a note there is refused as before.
  for (const stray of [file('_chat.txt', CHAT), file('notes.txt', 'remember the tickets\n'), file('chat.txt', '2026-01-05 First\n05/01/26, 09:02 - José: Hola\n')]) {
    await page.locator('#files').setInputFiles([await exportZip(), stray]);
    await expect(page.locator('#error')).toHaveText(ZIP_ALONE);
    await expect(page.locator('#results')).toBeHidden();
    await expect(page.locator('#start')).toBeDisabled();
  }
  // So is a recording beside a ZIP, whatever else is selected.
  await page.locator('#files').setInputFiles([await exportZip(), events, { name: 'voice.opus', mimeType: 'audio/ogg', buffer: await speechFixture() }]);
  await expect(page.locator('#error')).toHaveText(ZIP_ALONE);
});
