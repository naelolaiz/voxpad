# VoxPad

Local transcription of audio and WhatsApp voice messages powered by Whisper, with a viewer and activity statistics for the conversation. Real-time cross-platform dictation is planned.

VoxPad does three things with an exported WhatsApp chat:

- **Transcribe.** It transcribes audio files and exported WhatsApp voice messages using [OpenAI Whisper](https://github.com/openai/whisper), run locally through [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and associates voice notes with sender names and timestamps from common Android and iPhone chat exports.
- **View.** It writes the conversation as one HTML file to read, search and listen to, with every voice message in its place beside its transcript.
- **Analyse.** It counts what each person wrote and said per day, week or month: messages, typed words, words spoken in voice messages, voice notes and minutes of audio, with dated events of your own marked on the charts.

| Surface | What it is for |
|---|---|
| [`voxpad`](#transcribing-with-voxpad) | Transcribes an export ZIP, an extracted folder, a chat `.txt` with nearby media, or an individual audio file. |
| [`voxpad-stats`](#activity-analysis-with-voxpad-stats) | Counts and charts one conversation and writes its [viewer](#conversation-viewer), from an export or from what an earlier run left. It does not transcribe. |
| [`voxpad-app`](#desktop-application) | A window that transcribes an export and writes its viewer. |
| [Browser app](#browser-app) | A static page that does the same in a browser tab, with nothing to install. |

Export your WhatsApp chat **with media included**. Text-only exports do not contain audio to transcribe, and on Android they do not say which attachments were voice messages. Supported audio formats include WhatsApp `.opus` and `.m4a` files, plus OGG, AAC, MP3, WAV, FLAC, AMR and AIFF. Videos are not transcribed.

## Setup

Use Python 3.10 or newer and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, create the environment with `py -m venv .venv` and activate it in PowerShell with `.venv\Scripts\Activate.ps1`.

This installs the commands `voxpad`, `voxpad-stats` and `voxpad-app`. They can also be run as `python -m voxpad`, `python -m voxpad.stats` and `python -m voxpad.gui` with the same arguments. For source-only use without installing the commands, install `requirements.txt` and run the modules from the repository root.

The [codecpod](https://github.com/zhoukezi/codecpod) dependency decodes audio, mixes channels to mono and resamples to 16 kHz through Python. Its wheels include native audio codec libraries for Linux x86-64, macOS and Windows x86-64. No FFmpeg executable or subprocess is needed. The codec library embeds a reduced FFmpeg build and depends on NumPy, which is installed automatically. Platforms without a wheel require building native code; see the package documentation.

The first transcription downloads the Whisper model from Hugging Face, about 1.6 GB for the default `large-v3-turbo`. Subsequent runs use the cached files. Audio is processed locally, on a GPU when one is available, unless you choose [remote transcription](#remote-transcription); the script disables the model hub's optional telemetry.

The image that `voxpad-stats --png` draws needs [matplotlib](https://matplotlib.org/), which is an optional extra. Nothing else uses it:

```bash
python -m pip install -e ".[plot]"
```

On Intel Macs use Python 3.13 or older: one of faster-whisper's dependencies has no Intel macOS build for Python 3.14.

If you use uv and a dependency seems to be missing after pulling changes, run `uv sync`. Dependencies are declared in `requirements.txt`, and the project tells uv to watch that file.

## Transcribing with `voxpad`

```bash
voxpad "WhatsApp Chat.zip"
```

This saves `transcripts.json` and `transcripts.txt` in the current directory. JSON includes filenames, file sizes, chat references, transcripts, detected languages, durations and chunk times. The text report is convenient to read. Reports name the input by its file or folder name only, so sharing one does not reveal where the export is stored. Reports are saved after each recording, and a report that is already there is [continued](#stopping-and-continuing); original chat and audio files are protected. When the input holds one chat and no `--viewer` was asked for, the run ends by naming the `voxpad-stats` command that shows the result in the [viewer](#conversation-viewer).

Force Spanish, save an annotated copy of the original chat and write the viewer:

```bash
voxpad "WhatsApp Chat.zip" \
  --language es \
  --output results/transcripts.json \
  --chat-output results/chat_with_transcripts.txt \
  --viewer results/conversation.html
```

`--chat-output` writes a copy of the chat in which every voice message is followed by a line `[Voice message transcript: …]`. `--viewer` writes the [conversation viewer](#conversation-viewer), a page to read the chat, play its voice messages and chart its activity; its name must end in `.html`. With it, `--events FILE` marks [dated events](#events-file) in the page and `--audio` chooses [how the page reaches the recordings](#voice-messages-in-the-page). Both outputs require exactly one chat in the input, and both are also written for what is done when a run is stopped.

Use an extracted export or its chat text:

```bash
voxpad "exported-chat/"
voxpad "exported-chat/_chat.txt"
```

A chat `.txt` input searches its parent folder recursively for audio. Keep unrelated chats in separate folders. If a folder contains multiple chats, select the desired `.txt` as the input for `--chat-output` and `--viewer`, which require exactly one chat. Put generated files outside the source export folder where you can: an annotated chat inside it is one more chat `.txt` for the next run. Running the same command again replaces it, and the viewer reads it and the original as one conversation, but a `--chat-output` to another place then finds two chats and stops.

Preview filenames and message metadata without downloading the model; recordings that already have a transcript are marked `(already transcribed)`:

```bash
voxpad "WhatsApp Chat.zip" --dry-run
```

Transcribe one voice note into a report of its own and include word timestamps or names to favor:

```bash
voxpad "PTT-20261004-WA0001.opus" --output voice-note.json \
  --word-timestamps --keyword "María" --keyword "VoxPad"
```

Whisper recognizes about a hundred languages; pass one as its code, such as `--language es`, or omit `--language` to detect it per chunk. Naming the language is more reliable for short voice notes. `--model` selects another Whisper size, for example `small` or `medium` on a slower computer, or a folder containing a converted model for offline use.

The script sends Whisper at most 30 seconds per call. The script decodes each recording to 16 kHz mono floating-point samples, then sends every frame in consecutive chunks, including the final partial chunk. A chunk that cannot hold the rest of the recording ends at the quietest moment of its last five seconds, so boundaries usually fall in a pause instead of inside a word. Long voice notes are fully processed; speech without a pause near a boundary can still be cut mid-word and may be less accurate there. `--chunk-seconds` selects a shorter maximum chunk length. Word timestamps are relative to the full voice note, while chat timestamps stay in their original format in the reports. Decoded recordings are held in memory, using about 3.7 MiB per minute of audio.

Unrecognized chat formats still allow audio transcription, with empty message metadata. Missing or omitted media cannot be recovered. A failed audio file is recorded with its error and processing continues. Exit status is `0` on success, `1` for failures and `130` for interruption.

### Stopping and continuing

Ctrl-C stops a run and keeps what is done:

```text
Stopped after 120 of 205. Run the same command again to continue.
```

Running the same command again continues it: `voxpad` reads the report that is already at `--output` and transcribes only the recordings it holds no transcript for. Recordings that failed are tried again. It says what it reuses:

```text
Reusing 120 transcripts from transcripts.json (made with Whisper large-v3-turbo); 85 left to transcribe. --fresh transcribes everything again.
```

When nothing is left to transcribe, the reports, the annotated chat and the viewer are written from the earlier transcripts without loading Whisper and, with `--remote`, without a token or an upload.

`--fresh` transcribes everything again and replaces the report at `--output`:

```bash
voxpad "WhatsApp Chat.zip" --fresh
```

`--reuse` takes the transcripts of another report, a `transcripts.json` or the browser app's JSON download, and may be repeated:

```bash
voxpad "WhatsApp Chat.zip" --reuse earlier/transcripts.json --output results/transcripts.json
```

[Reusing earlier transcripts and resuming](#reusing-earlier-transcripts-and-resuming) says how a transcript finds its recording. Give each export its own `--output`: a report there that holds transcripts of recordings that are not in this export is not replaced, and the run stops and asks for another `--output` or for `--fresh`. A file there that is not a VoxPad report is replaced, with a notice.

## Remote transcription

Whisper's larger models are slow without a GPU. `--remote` runs the model on a hosted service instead of your computer, through [Hugging Face Inference Providers](https://huggingface.co/docs/inference-providers):

```bash
export HF_TOKEN=hf_...
voxpad "WhatsApp Chat.zip" --language es --remote deepinfra
```

**This uploads your audio.** Each voice message is decoded on your computer and sent, in chunks of at most 30 seconds, to the service you name. Chat text, sender names, filenames and transcripts are not sent, and a recording whose transcript is reused from an earlier report is not uploaded again. Nothing is uploaded unless you pass `--remote` or choose a hosted service in the desktop application, and the browser app never uploads.

| Service | Audio goes to | `--language` |
|---|---|---|
| `deepinfra` | DeepInfra, routed through Hugging Face | Supported |
| `hf-inference` | Hugging Face | Not available; Whisper detects the language |

Create an access token that may make calls to Inference Providers in your [Hugging Face settings](https://huggingface.co/settings/tokens) and put it in `HF_TOKEN`, or run `hf auth login`. Use the token itself, which starts with `hf_`, without quotes: anything else is refused before it is sent anywhere. Usage is billed to that account at the service's rate, after the monthly credits the account includes; see [Hugging Face's pricing](https://huggingface.co/docs/inference-providers/pricing).

`--model` names a Whisper size, which selects `openai/whisper-<size>`, or a full repository name; a folder, path or URL is not accepted. The service must offer the model, and VoxPad asks Hugging Face about that before anything is uploaded or any earlier report is replaced. Both services offer the default, `openai/whisper-large-v3-turbo`, and `openai/whisper-large-v3`; neither currently offers the smaller sizes. `--keyword` and `--word-timestamps` are not available remotely, and the detected language is not reported. Each chunk is uploaded as 16-bit WAV, about 1 MB for 30 seconds.

A recording that fails is recorded with its error and the run continues, as it does locally. A request the service turns down outright, such as one with a rejected token or used-up credits, stops the run instead and keeps the transcripts already saved.

## Conversation viewer

The viewer is one HTML file, `conversation.html` unless you name it otherwise, that shows a conversation with its voice messages. `voxpad --viewer`, `voxpad-stats` and the desktop application write it, and the browser app shows the same viewer in its page.

Open the file in a browser, for example with a double click. It needs no server and no connection. The file's Content-Security-Policy allows only the script and the stylesheet inside it, by their hashes, and audio from where the page itself was opened; the page requests nothing from the network, stores nothing in the browser, and loads a recording only when you press play. It follows the system's light or dark theme and fits a phone screen.

### The four views

- **Conversation** shows the messages as bubbles under their dates. In a chat of two people each has a side, and "Viewing as" swaps them; in a group every sender has a colour. A voice message has a play button, a seek bar, its length, its transcript (a long one is folded under "Show more") and its count of spoken words, or says "Not transcribed yet", "No speech detected", "Transcription failed" with the reason, or "Recording not included". Photos, videos, stickers and documents appear as a small label, since the page holds no media; edited and deleted messages are marked. The bar above searches typed text, captions and transcripts, filters by sender or to voice messages only, and jumps to a date. Events appear as numbered banners on their day.
- **Voice messages** lists every voice message with its sender, date, length, word count and transcript. The list can be sorted by date, length or number of words, filtered by sender and searched; "Play in sequence" plays one after the other, the player has a 1×, 1.5× and 2× speed control, and "Show in conversation" goes to the message. Above the list stand each person's voice notes, voice time, spoken words and words per minute.
- **Activity** has a table per participant (messages, typed words, spoken words, total words, voice notes, how many of them are transcribed, voice time) and four charts over one time axis: messages, words with spoken words hatched on top of typed ones, voice notes, and voice minutes. "Group by" switches between day, week and month; a conversation longer than 730 days opens by week. Weekends are shaded in the day view. The legend hides and shows participants. Pointing at a bar shows that day's exact values, and clicking it, or Enter after moving along the chart with the arrow keys, opens the day in Conversation. Events are numbered markers under the date axis; choosing one pins its full label under the charts.
- **Events** is a timeline of the events with their full labels, each with that day's messages, words and voice time per person and links to the day in Conversation and in Activity. "Load events file…" shows another [events file](#events-file) in the open page; the HTML file is not changed by it.

### Voice messages in the page

The recordings are not inside the HTML file. `--audio` chooses how the page reaches them:

| `--audio` | What the page plays |
|---|---|
| `link` | The recordings where they are, named by their address relative to the page. Every recording must lie in the page's folder or below it: reaching one elsewhere would write the names of your computer's folders into the page. Not possible for a ZIP, whose recordings exist only while it is read. |
| `copy` | Copies in a folder beside the page that is named after it, `conversation_audio/` for `conversation.html`. Refused when that folder would lie inside the export, where its files would be taken for recordings of the chat. |
| `none` | Nothing. Transcripts and lengths are shown without a play button. |
| `auto` | The default: `link` where that is possible, otherwise `copy`, and always `copy` for a ZIP. |

Copying is announced with its size, for example `Copied 205 voice messages (28 MiB) to conversation_audio/ so the viewer can play them; --audio none skips this.` Where `auto` can do neither, because the page lies inside the export but not above its recordings, it writes the page without them and says so. A page that links or copies must be opened from its folder: leave a linking page where it was written, and keep `conversation_audio/` beside a copying one when you move or send it. Moved on its own, the page still shows every transcript and plays nothing. A browser that cannot decode a recording says so on that message and keeps its transcript.

### What the file contains

`conversation.html` contains the whole conversation: every message with its sender, time and text, the file names of attachments, every transcript and the events. Whoever has the file can read all of it, so treat it as you treat the export itself. To show only the charts, share the image that `voxpad-stats --png` draws. Photos, videos, stickers, documents and the recordings are not in the file, and it names nothing above its own folder: no user name and no place on your computer.

## Activity analysis with `voxpad-stats`

```bash
voxpad-stats "WhatsApp Chat.zip"
```

This reads the chat, prints a summary and writes the [viewer](#conversation-viewer) as `conversation.html` in the current directory. It does not transcribe and needs neither Whisper nor a connection; the words spoken in voice messages come from transcripts made earlier. The summary goes to standard output and the notices before it to standard error:

```text
Using transcripts.json: 2 of 4 voice messages matched
Measured the length of 2 recordings from their files.
Copied 4 voice messages (177 KiB) to conversation_audio/ so the viewer can play them; --audio none skips this.
Parsed 11 messages from 2026-01-05 to 2026-01-14
Dates are read as day/month/year (DMY).

       Messages  Typed words  Spoken words  Total words  Voice notes  Voice time
Ana           6            5            24           29            3        12 s
José          5           22             0           22            1         4 s
Total        11           27            24           51            4        16 s

Voice messages: 4 detected, 4 with a recording, 4 with a duration.
Spoken words cover 2 of 4 voice messages (2 not transcribed, 0 failed, 0 without a recording).
2 voice messages have no transcript: pass --transcripts FILE, or transcribe them with voxpad
Viewer: /home/ana/chats/conversation.html
Open it in a browser; it needs no connection.
Keep conversation_audio/ beside it: the voice messages play from there.
conversation.html contains the whole conversation and its transcripts; share the PNG (--png) when you only want the charts.
```

`--viewer PATH` writes the page elsewhere, `--no-viewer` leaves it out, and `--audio` chooses [how it reaches the recordings](#voice-messages-in-the-page). No output may replace an input. Exit status is `0` on success, `1` for an error and `130` for interruption. The command imports nothing outside the standard library unless it draws the image or has to decode a recording to measure it, so `python -m voxpad.stats` also runs from the repository root of a checkout without any dependency installed.

### What it reads

The input is an export ZIP, an extracted folder or a chat `.txt`. The `.txt` may be the exported chat or a copy with transcripts inserted: the `chat_with_transcripts.txt` of an earlier run, or the browser app's text download. Recordings are not needed.

```bash
voxpad-stats "exported-chat/"
voxpad-stats results/chat_with_transcripts.txt --no-viewer
```

The input must hold exactly one conversation. A chat and its annotated copy in one folder count as one, and the copy with the most transcripts is read. With different chats the command stops and names them; pass the `.txt` to analyse.

Transcripts come from these places, the first that knows a voice message winning:

1. Reports named with `--transcripts JSON`: a `transcripts.json` of `voxpad` or the desktop application, or the browser app's JSON download. The option may be repeated; where two reports know the same recording, the later one wins, and a transcript wins over a failure.
2. Without `--transcripts`, a report that is found by itself: `transcripts.json` or a `.json` named like the chat beside the chat, `transcripts.json` beside a ZIP, then `transcripts.json` in the current directory. The first that is a VoxPad report and matches at least one voice message is used and announced. `--no-transcripts` turns this search off.
3. The `[Voice message transcript: …]` lines of an annotated chat.

A report describes a voice message when it names the recording's file name. The sender names and timestamps it recorded need not match, so a report made from another export of the same chat fits too; the summary then warns that transcripts were matched by file name only.

The length of a voice message is taken from the report. A recording that is present and has no length there is measured: Ogg Opus, which is WhatsApp's `.opus`, and WAV from their headers, other formats by decoding them when codecpod is installed.

### What is counted

Every number is counted per person and per day. `--bucket week` or `--bucket month` sums them per week or month in the image and in the charts the page opens with; the page's own switch changes that at any time. A week runs from Monday to Sunday and is labelled by its Monday; a month is labelled by its first day. A message whose time cannot be read is counted on the day of the message before it.

| Number | What it counts |
|---|---|
| Messages | Every message that has a sender, whatever it holds: text, a voice message, a photo, a deleted message. Lines WhatsApp writes itself, such as the encryption notice, are not counted. |
| Typed words | The words of typed text and of captions. The file name of an attachment, a placeholder for omitted media, the notice that stands for a deleted message, the mark of an edited one and inserted transcript lines are not typed text. |
| Spoken words | The words of the transcripts of voice messages. |
| Total words | Typed and spoken words together. |
| Voice notes | Voice messages, transcribed or not, including those an export without media only mentions. |
| Voice time | The sum of the lengths that are known. |

A word is a run of characters without whitespace, so `12.50`, `:)` and a lone emoji are one word each. The notices for deleted and edited messages and for omitted media are recognised in English, Spanish, German, Portuguese, French and Italian exports; in another language they may be counted as typed text. A chat that names no voice message at all gets a warning: an Android export made without media writes `<Media omitted>` for every attachment, so its voice messages cannot be told from its photos.

### Coverage

A chat that is transcribed in part must not read as one in which little was said. Whenever some voice messages have no transcript or no known length, every place that shows spoken words or voice time says how much the numbers cover:

- The summary prints `Spoken words cover 120 of 205 voice messages (84 not transcribed, 1 failed, 0 without a recording).` and `Voice time covers 120 of 205 voice messages.`
- The viewer shows the same sentences above the Activity charts and the list of voice messages, has a "Transcribed" column in its table ("120 of 205"), and marks the days whose voice messages are not all transcribed in the words chart and those not all timed in the voice minutes chart.
- The image adds `(120 of 205 voice notes transcribed)` and `(120 of 205 timed)` to the titles of its words and minutes panels.

### Date order

WhatsApp writes dates in the order of the phone's region, and an export does not say which that was. VoxPad decides once per chat between day/month/year, month/day/year and year/month/day. It takes the order under which every timestamp is a real date, the dates agree with those in attachment file names such as `IMG-20260304-WA0001.jpg`, and the messages do not go back in time. The summary names the order it used. When more than one order fits equally well, it warns, and `--date-order DMY`, `--date-order MDY` or `--date-order YMD` settles it:

```bash
voxpad-stats "WhatsApp Chat.zip" --date-order DMY
```

An export that writes a two-digit year first is read as day first unless `--date-order YMD` is given. Times are kept as the chat writes them, as local times; 12-hour clocks with their AM and PM marks and digits of other scripts are read.

### The figure

```bash
voxpad-stats "WhatsApp Chat.zip" --events events.txt --png activity.png
```

`--png` also draws the charts as one image, whose name must end in `.png`: a panel each for messages, words (spoken words hatched on top of typed ones), voice notes and minutes of audio over a shared date axis, with each person's totals in the panel titles and the [events](#events-file) below. A panel that has nothing to show is left out, which is said (`[skip] no data for voice notes.`). Weekends are shaded when counting per day in a chat of up to 250 days. `--bucket` sets what a bar covers.

The image draws the two people who sent the most messages and says so when others wrote too. `--person` chooses who is drawn, by the exact name the chat uses, from one to six people:

```bash
voxpad-stats "WhatsApp Chat.zip" --png activity.png --bucket week --person "Ana" --person "José"
```

`--event-style list`, the default, writes every event in full under its date. `--event-style key` puts numbered marks on the date axis and a numbered key below, which suits many or long events.

The image needs matplotlib, the `plot` extra of [Setup](#setup). Without it the command says so and stops before writing anything.

## Events file

An events file marks dates of your own in the viewer and in the image. It is a text file with one event per line, the date first:

```text
# Lines starting with # and blank lines are skipped
2026-01-06, Trip to Lisbon
13/01/2026 | Back home
01.03.26   First day at the new job
2026-01-10
```

- A date whose first field has four digits is year-month-day; `-`, `/` and `.` all separate. Any other date is read day first (`13/01/2026`, `01.03.26`), and month first only when day first gives no real date. A two-digit year is 2000 to 2099.
- After the date come an optional `,` or `|`, a tab or spaces, and the label. An event without a label is labelled by its date.
- A line that does not start with a real date is skipped with a warning that gives the number of the line and not its text.
- Events are numbered in date order. The same number marks an event in the banners of Conversation, on the Activity charts, on the Events tab and in the key of the image.
- A label may be long. The Events tab and the strip under the charts show it in full, a chart's tooltip cuts it at 140 characters, and the image wraps it; `--event-style key` keeps a crowded image readable.
- An event far outside the conversation would squeeze the chat on the time axis. Events more than 21 days, or 15 % of the conversation's length when that is more, before its first or after its last day are left off the charts and the image. The summary counts them (`Events: 3 shown; 1 far outside 2026-01-05–2026-01-14 not plotted`) and the Events tab lists them, flagged.

Pass the file with `--events` to `voxpad-stats`, or to `voxpad` together with `--viewer`:

```bash
voxpad-stats "WhatsApp Chat.zip" --events events.txt
voxpad "WhatsApp Chat.zip" --viewer results/conversation.html --events events.txt
```

The desktop application has an "Events" row, the browser app takes the file together with the export, and every viewer has "Load events file…" on its Events tab.

## Reusing earlier transcripts and resuming

Transcribing is the slow part, so no surface repeats it when an earlier result is at hand. An earlier result is a report: the `transcripts.json` that `voxpad` and the desktop application write, or the JSON downloaded from the browser app. Every surface reads all three.

| Surface | Continues from | Takes other reports from | Starts over with |
|---|---|---|---|
| `voxpad` | The report at `--output` | `--reuse JSON`, repeatable | `--fresh` |
| Desktop application | `transcripts.json` in the "Save in" folder | "Earlier transcripts" | "Start over" |
| Browser app | A report selected together with the export | The same selection; several reports are allowed | "Transcribe again", offered once every recording has a transcript; before that, select the export without the report |
| `voxpad-stats` | Transcribes nothing; reads `transcripts.json` beside the chat or in the current directory | `--transcripts JSON`, repeatable | `--no-transcripts` to read none |

A transcript finds its recording the same way everywhere that transcribes:

- By the recording's path inside the export. Failing that, by its file name, when that name stands for exactly one recording of the export and exactly one result of the report.
- Only a transcript is reused; a recording that failed is transcribed again. So is one whose size differs from the size the report recorded, and, with `--word-timestamps`, one whose transcript has no word times. The report at `--output` keeps the transcript it has for such a recording until the new one is made, so a run that stops before reaching it loses nothing.
- Sender names, chat file names and timestamps take no part, so the report of another export of the same chat is reused too. A report that records no sizes can only be trusted by name, and VoxPad says how many transcripts "were matched by file name only".
- Of several reports, the last one given that has a transcript for a recording supplies it. For `voxpad` the report at `--output` counts as given last, and `--fresh` sets only that one aside.

A reused transcript is written into the report with this export's path for the recording and, when another model made it, with that model's name.

The transcript lines of a `chat_with_transcripts.txt` are shown and counted by `voxpad-stats` and the viewer, but they are not a source for continuing: `voxpad`, the desktop application and the browser app reuse reports only. Keep the JSON.

## Desktop application

`voxpad-app` opens a window that does the same without the command line: choose an export ZIP, folder, chat `.txt` or audio file, or drop it on the window, pick the language and model, and press Transcribe. It shows the conversation with each voice message transcribed in place and saves `transcripts.json`, `transcripts.txt`, `chat_with_transcripts.txt` and the viewer `conversation.html` in a new folder beside the export, which you can change. Stop finishes the current voice message and keeps what is done.

- **Open viewer** opens the folder's `conversation.html` in your browser. The window itself shows text.
- **Continuing.** When the folder under "Save in" already holds transcripts, the window says so ("This folder already holds 120 transcripts; they will be reused.") and Transcribe goes on from them. Tick "Start over" to transcribe everything again; the box applies to one run.
- **Earlier transcripts** takes a report from elsewhere, a `transcripts.json` or the browser app's JSON download, whose transcripts are [reused](#reusing-earlier-transcripts-and-resuming).
- **Events** takes an [events file](#events-file) for the viewer.
- **Include voice messages for playback** lets the viewer play the recordings. It names them where they are when they lie in the output folder or below it, and otherwise copies them to `conversation_audio/` beside the page, always for a ZIP. Untick it for a page without recordings.
- **View without transcribing** writes the viewer from the export, the transcripts that are already there and the events, without loading Whisper and without a token or an upload, and shows the summary of `voxpad-stats` in the window. It reads the report chosen under "Earlier transcripts" and the folder's `transcripts.json`; with neither, it looks for a `transcripts.json` where `voxpad-stats` looks: beside the chat or the ZIP, then in the directory the application was started from.

The annotated chat and the viewer need one conversation, so an export with recordings only, or with several chats, gets the two reports alone.

Whisper runs on this computer unless you pick a hosted service under **Run on**. Each choice that uploads the audio says so, and the window then states where the voice messages go; [Remote transcription](#remote-transcription) describes the services, what is sent and what it costs. The access token comes from `HF_TOKEN` or a saved `hf auth login`, or you can paste one in the Token box. A pasted token is used until the window closes and is not saved. Of the models in the list, hosted services currently offer only `large-v3-turbo`; choosing another is explained before anything is uploaded. Keywords and word timestamps are not offered in the window.

The application uses the same local Whisper models as the command. Its window is built with Qt through [PySide6](https://pypi.org/project/PySide6/), which is installed with the other dependencies; nothing has to be installed separately on a desktop system. A minimal Linux installation without a desktop may lack the system libraries Qt loads, such as `libEGL`, `libGL`, `libxkbcommon`, `fontconfig` and `dbus`. You can also start it with `python -m voxpad.gui` or `python voxpad/gui.py`, optionally followed by the export to open.

## Browser app

`web/` contains a static browser app that does the same job without installing anything: drop a WhatsApp export ZIP, or a chat `.txt` with its audio files, read and play the conversation, transcribe its voice messages, and download the conversation with each voice message transcribed in place, as text and as JSON. It is published to GitHub Pages from `main`.

The conversation appears in the [viewer](#conversation-viewer) as soon as the selection is read. Voice messages say "Not transcribed yet" and fill in one by one while Whisper works, and a recording can be played at any time. A selection of recordings without a chat has no conversation to show, and its results are listed as plain text.

Everything runs in the browser tab. The export is read, decoded and transcribed locally with Whisper through [Transformers.js](https://github.com/huggingface/transformers.js); no chat text, audio, filenames or transcripts are uploaded. Voice messages play from the tab's memory, and a browser that cannot play a recording as it is gets a WAV copy decoded in the tab. The first transcription downloads the model from Hugging Face and caches it in the browser. Voice messages of any length are transcribed in full: as in the command-line tool, each recording is sent to Whisper in consecutive chunks of at most 30 seconds that end in a pause where there is one. An export ZIP is read by byte ranges, so photos and videos in it are skipped without being loaded and the archive itself may be several gigabytes. The chat text and recordings, which are held in memory, may total 512 MiB. ZIP64 archives (4 GiB or more, or over 65,534 files) and encrypted archives are unsupported.

Files selected together with the export add to it:

- **A report**, the JSON downloaded from the app or a `transcripts.json` of the command or the desktop application, up to 64 MiB. Its transcripts are [taken over](#reusing-earlier-transcripts-and-resuming), the page says how many, and Whisper listens to the rest only. After Stop, download the JSON to continue another day.
- **A chat without its audio.** A chat `.txt`, the original or an annotated copy, selected with a report shows the transcripts, lengths and spoken words with nothing to play.
- **A report on its own.** The app's JSON download holds the chat and reopens the conversation without audio. A `transcripts.json` of the command or the desktop application holds no chat text and has to be selected with the export or the chat.
- **An [events file](#events-file)**, up to 1 MiB. Events stay in the tab's memory and are in neither download.

When the dates of a chat can be read in more than one [order](#date-order), a selector above the conversation chooses it.

The app picks the model for the device:

| Model | Download | Used when |
|---|---|---|
| Whisper large-v3-turbo | 1.4 GB | The browser offers WebGPU with 16-bit float support. Most accurate. |
| Whisper small | 240 MB | Everywhere else, on WebAssembly. Also selectable to save download size. |
| Whisper tiny | 42 MB | Only when selected. Fastest and least accurate; the browser tests use it. |

Whisper small on WebAssembly runs on a single thread, because a static host cannot enable the browser isolation that threads need: expect roughly ten to fifteen seconds per voice message on a desktop computer. If the larger model cannot start, the app falls back to Whisper small. Without a chosen language, the language of each voice message is detected from its first audible part.

Two safeguards back the privacy claim. The built page carries a Content-Security-Policy that allows connections only to its own site and to Hugging Face and media only from the tab's own memory, and the speech worker is started so that the same policy binds it. The speech runtime's WebAssembly files are published with the app instead of being loaded from a CDN. Every model file is pinned to an exact revision and checked against its SHA-256 before it is used or cached; to update a model, change its revision, sizes and digests in `web/src/models.js`. The policy needs a browser that supports `'wasm-unsafe-eval'` (Chrome 97, Firefox 102, Safari 16 or newer) and is not applied by the development server.

Develop it with Node.js 22.12 or newer:

```bash
cd web
npm ci
npm run dev          # local development server
npm test             # unit tests
npm run build        # production build in web/dist
npx playwright install chromium
npm run test:browser # end-to-end tests against the production build
```

The browser tests run the real Whisper tiny model on a synthetic recording, so their first run downloads it into `web/node_modules/.cache/whisper`. The WebGPU path cannot run in a headless test browser and is not covered by them. Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to use an existing Chromium. The browser tests of the page that Python writes run `python3` from the repository root and open the result as a file; they need Python 3.10 or newer on the path and no Python package.

## Development

The `voxpad/` package contains the application, and `tests/` contains the test suite. `pyproject.toml` defines package metadata, dependencies and the commands. Install the development tools in your virtual environment:

```bash
python -m pip install -e ".[dev]"
python scripts/dev.py test
python scripts/dev.py build
# Run both:
python scripts/dev.py check
```

The commands are entry points that an install records. After pulling a change that adds a command or moves the module behind one, an existing editable install still starts the old entry point and fails with `ModuleNotFoundError` or `command not found`: run `python -m pip install -e ".[dev]"` again.

### Layout

| Module | What it holds |
|---|---|
| `voxpad/chat.py` | Parsing a chat export into messages. |
| `voxpad/export.py` | Finding the chats and recordings of a ZIP, a folder or a file, and linking recordings to messages. |
| `voxpad/engines.py` | Whisper on this computer and on the hosted services. |
| `voxpad/transcribe.py` | Chunking a recording, transcribing an export, and matching earlier reports to its recordings. |
| `voxpad/reports.py` | Writing the JSON and text reports and the annotated chat, and reading earlier reports. |
| `voxpad/analysis.py` | Timestamps and date order, words, kinds of message, events, the conversation model and its sums per day, week and month. Standard library only. |
| `voxpad/figure.py` | The image. matplotlib is imported when one is drawn. |
| `voxpad/stats.py` | The `voxpad-stats` command, and `write_conversation`, through which every surface writes the viewer. |
| `voxpad/viewer/` | The viewer, `viewer.js` and `viewer.css`, and the Python that writes them into one HTML file. |
| `voxpad/cli.py` | The `voxpad` command. |
| `voxpad/gui.py` | The desktop application. |
| `tests/` | The `unittest` suite, one file per module. `tests/support.py` holds what the files share and `tests/fixtures/` the analysis cases. |
| `scripts/dev.py` | Runs the tests and the build. |
| `web/` | The browser app, with its unit tests in `web/tests/` and its browser tests in `web/tests/browser/`. |

### The shared viewer and fixture

`voxpad/viewer/viewer.js` and `viewer.css` are the only implementation of the viewer. The browser app bundles them straight from that folder, and Python copies the same two files into the HTML it writes; they are package data of the wheel and the source archive. `viewer.js` is one ES module without imports that ends with its single `export` line, which Python removes to run it as a plain script, and it must not build markup from text, store anything or reach the network. Tests on both sides hold it to that.

The analysis exists twice, in `voxpad/analysis.py` and in `web/src/analysis.js`, and the two must describe a chat identically. `tests/fixtures/analysis_cases.json` holds cases written by hand from the rules, with the expected timestamps, word counts, events, conversation models and sums, and both the Python tests and the browser app's unit tests run it. A rule changed on one side is changed on the other and in the fixture. `aggregate`, `totals`, `parseEvents` and `countWords` exist once in JavaScript, in `viewer.js`. Reading earlier reports and matching them to recordings also exists twice, in `voxpad/reports.py` and `voxpad/transcribe.py` and in `web/src/previous.js`, under the same rules and with tests of its own on each side.

### Tests and builds

The helpers above work on Linux, macOS and Windows, including when called from another directory. Tests use generated audio, codecpod for decoding, and fakes for the transcription model and the hosted service; they do not download Whisper or connect anywhere. The test helper requires the audio dependencies, Qt and matplotlib so decoding, window and figure tests cannot silently skip or fail for a missing package. You can also run `python -m unittest discover -v` directly.

Builds use the standard Python build frontend (`python -m build`) and produce a wheel and source archive in `dist/`. VoxPad contains no compiled extensions, so its `py3-none-any.whl` installs across supported platforms. Native dependencies are installed separately for the target platform. To install a wheel, run `python -m pip install dist/voxpad-0.1.0-py3-none-any.whl`. The source archive includes tests and development helpers.

GitHub Actions runs tests on Linux, Windows and both Intel and Apple Silicon macOS, builds distributions, and checks that the wheel and source archive install, expose the commands and write a viewer. Download the `python-distributions` artifact from a successful workflow run. Package publishing is not configured.

Security checks run in GitHub Actions as well. CodeQL analyses the Python, JavaScript and workflow code. Dependency review, pip-audit and npm audit check dependencies for known vulnerabilities, and Dependabot proposes weekly updates. zizmor checks the workflows, gitleaks scans the history for secrets, and OpenSSF Scorecard reports on the repository setup from `main`. The browser tests assert that the app contacts only its own site and the model host.

## Roadmap

A tool for dictation and notes is planned: real-time microphone capture, with desktop support across Linux, macOS and Windows as the initial target. Live dictation, note-taking and typing into other applications are not implemented yet.
