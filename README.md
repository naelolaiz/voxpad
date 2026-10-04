# VoxPad

Local audio and WhatsApp transcription powered by Whistle, with real-time cross-platform dictation planned.

VoxPad currently transcribes audio files and exported WhatsApp voice messages using [Cactus Whistle](https://cactuscompute.com/blog/whistle). It accepts an export ZIP, an extracted folder, a chat `.txt` with nearby media, or an individual audio file. It associates voice notes with sender names and timestamps from common Android and iPhone chat exports.

Export your WhatsApp chat **with media included**. Text-only exports do not contain audio to transcribe. Supported audio formats include WhatsApp `.opus` and `.m4a` files, plus OGG, AAC, MP3, WAV, FLAC, AMR and AIFF. Videos are not transcribed.

## Setup

Use Python 3.10 or newer and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, create the environment with `py -m venv .venv` and activate it in PowerShell with `.venv\Scripts\Activate.ps1`.

The [codecpod](https://github.com/zhoukezi/codecpod) dependency decodes audio, mixes channels to mono and resamples to 16 kHz through Python. Its wheels include native audio codec libraries for Linux x86-64, macOS and Windows x86-64. No FFmpeg executable or subprocess is needed. The codec library embeds a reduced FFmpeg build and depends on NumPy, which is installed automatically. Platforms without a wheel require building native code; see the package documentation.

The first transcription downloads Whistle's model and platform runtime. Subsequent runs use cached files. Audio is processed locally; the script disables Needle's optional telemetry.

## Usage

```bash
voxpad "WhatsApp Chat.zip"
```

This saves `transcripts.json` and `transcripts.txt` in the current directory. JSON includes filenames, chat references, transcripts, detected languages, durations and chunk times. The text report is convenient to read. Reports name the input by its file or folder name only, so sharing one does not reveal where the export is stored. Existing report files are replaced; original chat and audio files are protected.

Force Spanish and save an annotated copy of the original chat:

```bash
voxpad "WhatsApp Chat.zip" \
  --language es \
  --output results/transcripts.json \
  --chat-output results/chat_with_transcripts.txt
```

Use an extracted export or its chat text:

```bash
voxpad "exported-chat/"
voxpad "exported-chat/_chat.txt"
```

A chat `.txt` input searches its parent folder recursively for audio. Keep unrelated chats in separate folders. If a folder contains multiple chats, select the desired `.txt` for `--chat-output`, which requires exactly one chat. Put generated annotated chats outside the source export folder.

Preview filenames and message metadata without downloading the model:

```bash
voxpad "WhatsApp Chat.zip" --dry-run
```

Transcribe one voice note and include word timestamps or names to favor:

```bash
voxpad "PTT-20261004-WA0001.opus" \
  --word-timestamps --keyword "María" --keyword "Cactus"
```

Whistle supports English (`en`), German (`de`), French (`fr`), Spanish (`es`), Italian (`it`), Dutch (`nl`) and Polish (`pl`). Omit `--language` to detect language per chunk. `--model /path/to/whistle.cact` uses local weights; the runtime must also be cached for fully offline use.

Whistle processes at most 30 seconds per call. The script decodes each recording to 16 kHz mono floating-point samples, then sends every frame in consecutive chunks, including the final partial chunk. A chunk that cannot hold the rest of the recording ends at the quietest moment of its last five seconds, so boundaries usually fall in a pause instead of inside a word. Long voice notes are fully processed; speech without a pause near a boundary can still be cut mid-word and may be less accurate there. `--chunk-seconds` selects a shorter maximum chunk length. Word timestamps are relative to the full voice note, while chat timestamps stay in their original format to avoid guessing the date locale. Decoded recordings are held in memory, using about 3.7 MiB per minute of audio.

Unrecognized chat formats still allow audio transcription, with empty message metadata. Missing or omitted media cannot be recovered. A failed audio file is recorded with its error and processing continues. Reports are saved after each recording. Exit status is `0` on success, `1` for failures and `130` for interruption.

You can also run `python -m voxpad` with the same arguments. For source-only use without installing the command, install `requirements.txt` and run the module from the repository root.

## Development

The `voxpad/` package contains the application, and `tests/` contains the test suite. `pyproject.toml` defines package metadata, dependencies and the `voxpad` command. Install the development tools in your virtual environment:

```bash
python -m pip install -e ".[dev]"
python scripts/dev.py test
python scripts/dev.py build
# Run both:
python scripts/dev.py check
```

These helpers work on Linux, macOS and Windows, including when called from another directory. Tests use generated audio, codecpod for decoding and a fake transcription model; they do not download Whistle. The test helper requires the audio dependencies so decoding tests cannot silently skip. You can also run `python -m unittest discover -v` directly.

Builds use the standard Python build frontend (`python -m build`) and produce a wheel and source archive in `dist/`. VoxPad contains no compiled extensions, so its `py3-none-any.whl` installs across supported platforms. Native dependencies are installed separately for the target platform. To install a wheel, run `python -m pip install dist/voxpad-0.1.0-py3-none-any.whl`. The source archive includes tests and development helpers.

GitHub Actions runs tests on Linux, Windows and both Intel and Apple Silicon macOS, builds distributions, and checks that the wheel and source archive install and expose the CLI. Download the `python-distributions` artifact from a successful workflow run. Package publishing is not configured.

## Browser app

`web/` contains a static browser app that does the same job without installing anything: drop a WhatsApp export ZIP, or a chat `.txt` with its audio files, and download the conversation with each voice message transcribed in place, as text and as JSON. It is published to GitHub Pages from `main`.

Everything runs in the browser tab. The export is read, decoded and transcribed locally with Whistle compiled to WebAssembly; no chat text, audio, filenames or transcripts are uploaded. The first transcription downloads the Whistle model and runtime (about 17 MB) from Hugging Face and caches them in the browser. Voice messages of any length are transcribed in full: as in the command-line tool, each recording is sent to Whistle in consecutive chunks of at most 30 seconds that end in a pause where there is one. Inputs are limited to 100 MiB, and ZIP64 and encrypted archives are unsupported.

Two safeguards back the privacy claim. The built page carries a Content-Security-Policy that allows connections only to its own site and to Hugging Face, and the speech worker is started so that the same policy binds it. The model and runtime are pinned to exact revisions and checked against their SHA-256 before they are run or cached; to update them, change the URLs and digests in both `web/src/whistle-worker.js` and `web/tests/browser/assets.js`. The policy needs a browser that supports `'wasm-unsafe-eval'` (Chrome 97, Firefox 102, Safari 16 or newer) and is not applied by the development server.

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

The browser tests run the real Whistle model on a synthetic recording, so their first run downloads the model and runtime into `web/node_modules/.cache/whistle`. Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to use an existing Chromium.

## Roadmap

Real-time microphone capture and dictation are planned, with desktop support across Linux, macOS and Windows as the initial target. Live dictation and typing into other applications are not implemented yet.
