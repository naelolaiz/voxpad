# VoxPad

Local audio and WhatsApp transcription powered by Whisper, with real-time cross-platform dictation planned.

VoxPad currently transcribes audio files and exported WhatsApp voice messages using [OpenAI Whisper](https://github.com/openai/whisper), run locally through [faster-whisper](https://github.com/SYSTRAN/faster-whisper). It accepts an export ZIP, an extracted folder, a chat `.txt` with nearby media, or an individual audio file. It associates voice notes with sender names and timestamps from common Android and iPhone chat exports.

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

The first transcription downloads the Whisper model from Hugging Face, about 1.6 GB for the default `large-v3-turbo`. Subsequent runs use the cached files. Audio is processed locally, on a GPU when one is available, unless you choose [remote transcription](#remote-transcription); the script disables the model hub's optional telemetry.

On Intel Macs use Python 3.13 or older: one of faster-whisper's dependencies has no Intel macOS build for Python 3.14.

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
  --word-timestamps --keyword "María" --keyword "VoxPad"
```

Whisper recognizes about a hundred languages; pass one as its code, such as `--language es`, or omit `--language` to detect it per chunk. Naming the language is more reliable for short voice notes. `--model` selects another Whisper size, for example `small` or `medium` on a slower computer, or a folder containing a converted model for offline use.

The script sends Whisper at most 30 seconds per call. The script decodes each recording to 16 kHz mono floating-point samples, then sends every frame in consecutive chunks, including the final partial chunk. A chunk that cannot hold the rest of the recording ends at the quietest moment of its last five seconds, so boundaries usually fall in a pause instead of inside a word. Long voice notes are fully processed; speech without a pause near a boundary can still be cut mid-word and may be less accurate there. `--chunk-seconds` selects a shorter maximum chunk length. Word timestamps are relative to the full voice note, while chat timestamps stay in their original format to avoid guessing the date locale. Decoded recordings are held in memory, using about 3.7 MiB per minute of audio.

Unrecognized chat formats still allow audio transcription, with empty message metadata. Missing or omitted media cannot be recovered. A failed audio file is recorded with its error and processing continues. Reports are saved after each recording. Exit status is `0` on success, `1` for failures and `130` for interruption.

You can also run `python -m voxpad` with the same arguments. For source-only use without installing the command, install `requirements.txt` and run the module from the repository root.

## Remote transcription

Whisper's larger models are slow without a GPU. `--remote` runs the model on a hosted service instead of your computer, through [Hugging Face Inference Providers](https://huggingface.co/docs/inference-providers):

```bash
export HF_TOKEN=hf_...
voxpad "WhatsApp Chat.zip" --language es --remote deepinfra
```

**This uploads your audio.** Each voice message is decoded on your computer and sent, in chunks of at most 30 seconds, to the service you name. Chat text, sender names, filenames and transcripts are not sent. Nothing is uploaded unless you pass `--remote` or choose a hosted service in the desktop application, and the browser app never uploads.

| Service | Audio goes to | `--language` |
|---|---|---|
| `deepinfra` | DeepInfra, routed through Hugging Face | Supported |
| `hf-inference` | Hugging Face | Not available; Whisper detects the language |

Create an access token that may make calls to Inference Providers in your [Hugging Face settings](https://huggingface.co/settings/tokens) and put it in `HF_TOKEN`, or run `hf auth login`. Usage is billed to that account at the service's rate, after the monthly credits the account includes; see [Hugging Face's pricing](https://huggingface.co/docs/inference-providers/pricing).

`--model` names a Whisper size, which selects `openai/whisper-<size>`, or a full repository name. The service must offer the model; the default, `openai/whisper-large-v3-turbo`, is offered by both. `--keyword` and `--word-timestamps` are not available remotely, and the detected language is not reported. Each chunk is uploaded as 16-bit WAV, about 1 MB for 30 seconds.

A recording that fails is recorded with its error and the run continues, as it does locally. A rejected token, used-up credits or a model the service does not offer stops the run instead and keeps the transcripts already saved.

## Desktop application

`voxpad-app` opens a window that does the same without the command line: choose an export ZIP, folder, chat `.txt` or audio file, or drop it on the window, pick the language and model, and press Transcribe. It shows the conversation with each voice message transcribed in place and saves `transcripts.json`, `transcripts.txt` and `chat_with_transcripts.txt` in a new folder beside the export, which you can change. Stop finishes the current voice message and keeps what is done.

Whisper runs on this computer unless you pick a hosted service under **Run on**. Each choice that uploads the audio says so, and the window then states where the voice messages go; [Remote transcription](#remote-transcription) describes the services, what is sent and what it costs. The access token comes from `HF_TOKEN` or a saved `hf auth login`, or you can paste one in the Token box. A pasted token is used until the window closes and is not saved. Keywords and word timestamps are not offered in the window.

The application uses the same local Whisper models as the command. Its window is built with Qt through [PySide6](https://pypi.org/project/PySide6/), which is installed with the other dependencies; nothing has to be installed separately on a desktop system. A minimal Linux installation without a desktop may lack the system libraries Qt loads, such as `libEGL`, `libGL`, `libxkbcommon`, `fontconfig` and `dbus`. You can also start it with `python -m voxpad.gui` or `python voxpad/gui.py`, optionally followed by the export to open.

If you use uv and a dependency seems to be missing after pulling changes, run `uv sync`. Dependencies are declared in `requirements.txt`, and the project tells uv to watch that file.

## Development

The `voxpad/` package contains the application, and `tests/` contains the test suite. `pyproject.toml` defines package metadata, dependencies and the `voxpad` command. Install the development tools in your virtual environment:

```bash
python -m pip install -e ".[dev]"
python scripts/dev.py test
python scripts/dev.py build
# Run both:
python scripts/dev.py check
```

These helpers work on Linux, macOS and Windows, including when called from another directory. Tests use generated audio, codecpod for decoding, and fakes for the transcription model and the hosted service; they do not download Whisper or connect anywhere. The test helper requires the audio dependencies so decoding tests cannot silently skip. You can also run `python -m unittest discover -v` directly.

Builds use the standard Python build frontend (`python -m build`) and produce a wheel and source archive in `dist/`. VoxPad contains no compiled extensions, so its `py3-none-any.whl` installs across supported platforms. Native dependencies are installed separately for the target platform. To install a wheel, run `python -m pip install dist/voxpad-0.1.0-py3-none-any.whl`. The source archive includes tests and development helpers.

GitHub Actions runs tests on Linux, Windows and both Intel and Apple Silicon macOS, builds distributions, and checks that the wheel and source archive install and expose the CLI. Download the `python-distributions` artifact from a successful workflow run. Package publishing is not configured.

Security checks run in GitHub Actions as well. CodeQL analyses the Python, JavaScript and workflow code. Dependency review, pip-audit and npm audit check dependencies for known vulnerabilities, and Dependabot proposes weekly updates. zizmor checks the workflows, gitleaks scans the history for secrets, and OpenSSF Scorecard reports on the repository setup from `main`. The browser tests assert that the app contacts only its own site and the model host.

## Browser app

`web/` contains a static browser app that does the same job without installing anything: drop a WhatsApp export ZIP, or a chat `.txt` with its audio files, and download the conversation with each voice message transcribed in place, as text and as JSON. It is published to GitHub Pages from `main`.

Everything runs in the browser tab. The export is read, decoded and transcribed locally with Whisper through [Transformers.js](https://github.com/huggingface/transformers.js); no chat text, audio, filenames or transcripts are uploaded. The first transcription downloads the model from Hugging Face and caches it in the browser. Voice messages of any length are transcribed in full: as in the command-line tool, each recording is sent to Whisper in consecutive chunks of at most 30 seconds that end in a pause where there is one. An export ZIP is read by byte ranges, so photos and videos in it are skipped without being loaded and the archive itself may be several gigabytes. The chat text and recordings, which are held in memory, may total 512 MiB. ZIP64 archives (4 GiB or more, or over 65,534 files) and encrypted archives are unsupported.

The app picks the model for the device:

| Model | Download | Used when |
|---|---|---|
| Whisper large-v3-turbo | 1.4 GB | The browser offers WebGPU with 16-bit float support. Most accurate. |
| Whisper small | 240 MB | Everywhere else, on WebAssembly. Also selectable to save download size. |
| Whisper tiny | 42 MB | Only when selected. Fastest and least accurate; the browser tests use it. |

Whisper small on WebAssembly runs on a single thread, because a static host cannot enable the browser isolation that threads need: expect roughly ten to fifteen seconds per voice message on a desktop computer. If the larger model cannot start, the app falls back to Whisper small. Without a chosen language, the language of each voice message is detected from its first audible part.

Two safeguards back the privacy claim. The built page carries a Content-Security-Policy that allows connections only to its own site and to Hugging Face, and the speech worker is started so that the same policy binds it. The speech runtime's WebAssembly files are published with the app instead of being loaded from a CDN. Every model file is pinned to an exact revision and checked against its SHA-256 before it is used or cached; to update a model, change its revision, sizes and digests in `web/src/models.js`. The policy needs a browser that supports `'wasm-unsafe-eval'` (Chrome 97, Firefox 102, Safari 16 or newer) and is not applied by the development server.

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

The browser tests run the real Whisper tiny model on a synthetic recording, so their first run downloads it into `web/node_modules/.cache/whisper`. The WebGPU path cannot run in a headless test browser and is not covered by them. Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to use an existing Chromium.

## Roadmap

Real-time microphone capture and dictation are planned, with desktop support across Linux, macOS and Windows as the initial target. Live dictation and typing into other applications are not implemented yet.
