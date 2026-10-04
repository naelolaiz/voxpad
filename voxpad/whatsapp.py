"""VoxPad transcription of audio files and WhatsApp voice messages.

Requires faster-whisper and codecpod. Run with --help for examples and options.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import wave
import zipfile


AUDIO_EXTENSIONS = {".opus", ".ogg", ".oga", ".m4a", ".aac", ".mp3", ".wav", ".flac", ".amr", ".aif", ".aiff"}
LANGUAGES = tuple((
    "af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl gu haw ha he hi hr ht hu hy id is "
    "it ja jw ka kk km kn ko la lb ln lo lt lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk "
    "sl sn so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh"
).split())
DEFAULT_MODEL = "large-v3-turbo"
# Hosted services that can run Whisper instead of this computer, and who receives the audio.
REMOTE_SERVICES = {"deepinfra": "DeepInfra through Hugging Face", "hf-inference": "Hugging Face"}
# Of those, the services that can be told the spoken language; the others always detect it.
REMOTE_LANGUAGE_SERVICES = {"deepinfra"}
REMOTE_TIMEOUT_SECONDS = 120
SAMPLE_RATE = 16000
# A chunk ends at the quietest tenth of a second within its last five seconds.
BOUNDARY_SEARCH_SECONDS = 5
BOUNDARY_WINDOW_SECONDS = 0.1
MAX_ZIP_BYTES = 8 * 1024**3
DATE = r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}"
PERIOD = r"(?:[aApP]\.?\s*[mM]\.?|[صم]|上午|下午|午前|午後)"
CLOCK = r"\d{1,2}[:：]\d{2}(?:[:：]\d{2})?"
TIME = rf"(?:{PERIOD}\s*)?{CLOCK}(?:\s*{PERIOD})?"
HEADER = re.compile(
    rf"^(?:\[(?P<ios>{DATE}[,،]?\s*{TIME})\]\s*|(?P<android>{DATE}[,،]?\s+{TIME})\s+-\s+)(?P<body>.*)$"
)
SENDER = re.compile(r"[:：]\s")
NEWLINE = re.compile(r"\r\n|\r|\n")
INVISIBLE = str.maketrans("", "", "\ufeff\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


@dataclass
class Message:
    timestamp: str
    sender: str | None
    text: str
    end_line: int


@dataclass
class Export:
    root: Path
    audio: list[Path]
    chats: list[Path]


def parse_chat(text: str) -> list[Message]:
    """Read common iOS/Android headers without guessing the date locale."""
    messages: list[Message] = []
    for line_number, line in enumerate(text.splitlines()):
        clean = line.translate(INVISIBLE)
        match = HEADER.match(clean)
        if match:
            body = match["body"]
            separator = SENDER.search(body)
            messages.append(Message(
                match["ios"] or match["android"],
                body[:separator.start()] if separator else None,
                body[separator.end():] if separator else body,
                line_number,
            ))
        elif messages:
            messages[-1].text += "\n" + clean
            messages[-1].end_line = line_number
    return messages


def scan_export(root: Path, excluded: set[Path]) -> Export:
    audio, chats = [], []
    for directory, folders, filenames in os.walk(root, followlinks=False):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.is_symlink() or path.resolve() in excluded:
                continue
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                audio.append(path)
            elif path.suffix.lower() == ".txt":
                chats.append(path)
    return Export(root, sorted(audio), sorted(chats))


@contextmanager
def open_export(source: Path, excluded: set[Path] | None = None):
    """Yield media and chat paths; ZIP media exist only in a temporary folder."""
    excluded = excluded or set()
    if source.is_dir():
        yield scan_export(source, excluded)
    elif source.suffix.lower() == ".zip":
        with tempfile.TemporaryDirectory(prefix="voxpad-export-") as temporary:
            root = Path(temporary)
            seen: set[str] = set()
            extracted_bytes = 0
            with zipfile.ZipFile(source) as archive:
                for member in archive.infolist():
                    relative = PurePosixPath(member.filename.replace("\\", "/"))
                    mode = member.external_attr >> 16
                    if (relative.is_absolute() or ".." in relative.parts
                            # On Windows a drive-like component anywhere resets the path.
                            or any(":" in part for part in relative.parts)
                            or stat.S_ISLNK(mode)):
                        raise ValueError(f"Unsafe ZIP entry: {member.filename}")
                    if member.is_dir() or relative.suffix.lower() not in AUDIO_EXTENSIONS | {".txt"}:
                        continue
                    normalized = str(relative)
                    if normalized in seen:
                        raise ValueError(f"Duplicate ZIP entry: {member.filename}")
                    seen.add(normalized)
                    extracted_bytes += member.file_size
                    if extracted_bytes > MAX_ZIP_BYTES:
                        raise ValueError("ZIP media exceed 8 GiB. Extract the export yourself and use the folder as input.")
                    target = root.joinpath(*relative.parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as incoming, target.open("wb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
            yield scan_export(root, set())
    elif source.suffix.lower() == ".txt":
        export = scan_export(source.parent, excluded)
        export.chats = [source]
        yield export
    elif source.suffix.lower() in AUDIO_EXTENSIONS:
        yield Export(source.parent, [source], [])
    else:
        raise ValueError("Input must be an export ZIP, folder, chat .txt, or audio file.")


def index_messages(export: Export) -> tuple[dict[Path, list[dict]], dict[Path, str], dict[tuple[Path, int], list[Path]]]:
    """Associate known audio filenames with messages, independent of attachment labels."""
    contexts: dict[Path, list[dict]] = {path: [] for path in export.audio}
    texts: dict[Path, str] = {}
    occurrences: dict[tuple[Path, int], list[Path]] = {}
    by_name: dict[str, list[Path]] = {}
    for path in export.audio:
        key = unicodedata.normalize("NFC", path.name)
        by_name.setdefault(key, []).append(path)
    if not by_name:
        return contexts, texts, occurrences
    filenames = re.compile(
        r"(?<![\w.-])(?:" + "|".join(re.escape(name) for name in sorted(by_name, key=len, reverse=True)) + r")(?![\w.-])"
    )
    for chat in export.chats:
        try:
            # Decode the bytes directly: text mode would rewrite CRLF line endings
            # and drop a leading BOM, and the annotated copy must keep both.
            text = chat.read_bytes().decode("utf-8")
        except UnicodeError:
            print(f"Warning: skipping non-UTF-8 chat text: {chat.name}", file=sys.stderr)
            continue
        messages = parse_chat(text)
        if not messages and chat.name.lower() != "_chat.txt":
            continue
        texts[chat] = text
        for message in messages:
            names = set(filenames.findall(unicodedata.normalize("NFC", message.text)))
            for name in sorted(names):
                candidates = by_name[name]
                # Prefer media beside this chat when different chats share a filename.
                nearby = [path for path in candidates if path.parent == chat.parent]
                selected = nearby or candidates
                if len(selected) != 1:
                    print(f"Warning: ambiguous attachment {name!r} in {chat.name}; leaving its metadata unset.", file=sys.stderr)
                    continue
                path = selected[0]
                contexts[path].append({
                    "chat_file": chat.relative_to(export.root).as_posix(),
                    "timestamp": message.timestamp,
                    "sender": message.sender,
                })
                occurrences.setdefault((chat, message.end_line), []).append(path)
    return contexts, texts, occurrences


class Whisper:
    """Run a Whisper model through faster-whisper, one chunk of audio at a time."""

    def __init__(self, model: str = DEFAULT_MODEL):
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            raise RuntimeError("Install the Python dependencies: python -m pip install -r requirements.txt") from error
        # Uses a GPU when one is available, and the fastest number format the device supports.
        self._model = WhisperModel(model, device="auto", compute_type="auto")

    def transcribe(self, samples, *, language: str | None = None, keywords: list[str] | None = None,
                   word_timestamps: bool = False) -> dict:
        segments, info = self._model.transcribe(
            samples, language=language, hotwords=" ".join(keywords) if keywords else None,
            word_timestamps=word_timestamps,
            # Chunks are independent; earlier text must not steer or repeat into later ones.
            condition_on_previous_text=False,
        )
        segments = list(segments)
        result = {"text": "".join(segment.text for segment in segments).strip(), "language": info.language}
        if word_timestamps:
            result["words"] = [
                {"word": word.word.strip(), "start": word.start, "end": word.end, "probability": word.probability}
                for segment in segments for word in segment.words or []
            ]
        return result


class RemoteRefused(RuntimeError):
    """A hosted service turned a request down for a reason the next recording would meet too."""


def remote_model(model: str) -> str:
    """Name a Whisper size as its Hugging Face repository; a repository is used as given."""
    return model if "/" in model else f"openai/whisper-{model}"


def remote_conflict(remote: str | None, *, model: str, language: str | None, keywords: list[str] | None,
                    word_timestamps: bool) -> str | None:
    """Say which option the chosen hosted service cannot honor, if any."""
    if not remote:
        return None
    if remote not in REMOTE_SERVICES:
        return f"--remote must be one of: {', '.join(REMOTE_SERVICES)}"
    if keywords:
        return "--keyword is not available with --remote"
    if word_timestamps:
        return "--word-timestamps is not available with --remote"
    if language and remote not in REMOTE_LANGUAGE_SERVICES:
        return f"--language is not available with --remote {remote}; use --remote deepinfra, or let Whisper detect the language"
    if Path(model).is_dir():
        return "--remote needs a Whisper size or a Hugging Face repository as --model, not a folder"
    return None


def wav_bytes(samples) -> bytes:
    """Encode 16 kHz mono samples as a 16-bit WAV file held in memory."""
    import numpy
    pcm = numpy.rint(numpy.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(SAMPLE_RATE)
        recording.writeframes(pcm.tobytes())
    return buffer.getvalue()


class RemoteWhisper:
    """Run Whisper on a hosted service through Hugging Face, uploading one chunk of audio at a time."""

    def __init__(self, model: str = DEFAULT_MODEL, service: str = "deepinfra", token: str | None = None):
        try:
            from huggingface_hub import InferenceClient, get_token
        except ImportError as error:
            raise RuntimeError("Install the Python dependencies: python -m pip install -r requirements.txt") from error
        # A token given here wins over HF_TOKEN and a saved login.
        token = token or get_token()
        if not token:
            raise RuntimeError("Remote transcription needs a Hugging Face access token that may call Inference "
                               "Providers: set HF_TOKEN or run `hf auth login`.")
        self.model = remote_model(model)
        self.service = REMOTE_SERVICES[service]
        # Hugging Face's own service tells raw audio apart by this header; DeepInfra receives a form upload.
        headers = {"Content-Type": "audio/wav"} if service == "hf-inference" else None
        self._client = InferenceClient(provider=service, token=token, timeout=REMOTE_TIMEOUT_SECONDS, headers=headers)

    def transcribe(self, samples, *, language: str | None = None, keywords: list[str] | None = None,
                   word_timestamps: bool = False) -> dict:
        audio = wav_bytes(samples)
        try:
            output = self._client.automatic_speech_recognition(
                audio, model=self.model, extra_body={"language": language} if language else None)
        except Exception as error:  # Network, service and response failures arrive as unrelated types.
            status = getattr(getattr(error, "response", None), "status_code", None)
            reason = getattr(error, "server_message", None) or str(error) or type(error).__name__
            message = f"{self.service} did not transcribe the audio: {' '.join(reason.split())}"
            # A rejected token, used-up credits or a model the service lacks fail every recording alike.
            if isinstance(error, ValueError) or (status is not None and 400 <= status < 500 and status not in (408, 429)):
                raise RemoteRefused(message) from error
            raise RuntimeError(message) from error
        # The service does not say which language it heard.
        return {"text": (output.text or "").strip(), "language": language or ""}


def chunk_end(samples, start: int, chunk_frames: int) -> int:
    """Return where the chunk starting at `start` ends.

    A chunk that cannot hold the rest of the recording ends at its quietest
    moment, so fewer words are cut.
    """
    limit = start + chunk_frames
    if limit >= len(samples):
        return len(samples)
    window = int(BOUNDARY_WINDOW_SECONDS * SAMPLE_RATE)
    search = min(int(BOUNDARY_SEARCH_SECONDS * SAMPLE_RATE), chunk_frames // 2)
    if search <= window:
        return limit
    first = limit - search
    energy = (samples[first:limit].astype("float64") ** 2).cumsum()
    energy = energy[window:] - energy[:-window]
    # Of equally quiet windows take the latest, which keeps chunks long.
    quietest = len(energy) - 1 - int(energy[::-1].argmin())
    return first + quietest + 1 + window // 2


def transcribe_audio(path: Path, model, *, chunk_seconds: float,
                     language: str | None, keywords: list[str], word_timestamps: bool,
                     decoder=None) -> dict:
    """Decode to mono samples, then transcribe every frame in contiguous chunks."""
    if decoder is None:
        try:
            import codecpod as decoder
        except ImportError as error:
            raise RuntimeError("Install the dependencies: python -m pip install -r requirements.txt") from error
    try:
        samples, rate = decoder.load(str(path), sample_rate=SAMPLE_RATE, mono=True)
    except decoder.CodecpodError as error:
        raise RuntimeError(f"Could not decode audio: {error}") from error
    if rate != SAMPLE_RATE or samples.ndim != 1:
        raise RuntimeError("Decoder did not produce 16 kHz mono audio.")
    total_frames = len(samples)
    if not total_frames:
        raise ValueError("Audio file contains no samples.")
    segments, words = [], []
    chunk_frames = max(1, int(chunk_seconds * SAMPLE_RATE))
    offset = 0
    while offset < total_frames:
        frames = samples[offset:chunk_end(samples, offset, chunk_frames)]
        result = model.transcribe(
            frames, language=language, keywords=keywords or None,
            word_timestamps=word_timestamps,
        )
        start = offset / SAMPLE_RATE
        end = (offset + len(frames)) / SAMPLE_RATE
        segments.append({"start": start, "end": end,
                         "text": result["text"].strip(), "language": result.get("language", "")})
        if word_timestamps:
            for word in result.get("words", []):
                words.append({**word, "start": word["start"] + start, "end": word["end"] + start})
        offset += len(frames)
    detected = list(dict.fromkeys(segment["language"] for segment in segments if segment["language"]))
    transcript = {
        "text": " ".join(segment["text"] for segment in segments if segment["text"]),
        "languages": detected, "duration_seconds": total_frames / SAMPLE_RATE, "segments": segments,
    }
    if word_timestamps:
        transcript["words"] = words
    return transcript


def atomic_write(destination: Path, text: str) -> None:
    """Replace a report only after its new contents have been written in full."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=destination.parent,
                                         prefix=f".{destination.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_reports(output: Path, report: dict) -> None:
    atomic_write(output, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    lines = []
    for result in report["results"]:
        lines.append(result["file"])
        for message in result["messages"]:
            lines.append(f"  {message['timestamp']} | {message['sender'] or '(system message)'}")
        lines.append(result["text"] or (f"[ERROR: {result['error']}]" if result["status"] == "error" else "[No speech detected]"))
        lines.append("")
    atomic_write(output.with_suffix(".txt"), "\n".join(lines))


def write_annotated_chat(destination: Path, chat: Path, text: str,
                         occurrences: dict, results: dict[Path, dict]) -> None:
    # Inserted lines follow the chat's own line endings.
    newline = NEWLINE.search(text)
    newline = newline[0] if newline else "\n"
    lines = []
    for number, line in enumerate(text.splitlines(keepends=True)):
        lines.append(line)
        # A stopped run has no result yet for the recordings it did not reach.
        attached = [results[path] for path in occurrences.get((chat, number), []) if path in results]
        if attached and not line.endswith(("\n", "\r")):
            lines.append(newline)
        for result in attached:
            transcript = result["text"] or (f"Error: {result['error']}" if result["status"] == "error" else "No speech detected")
            lines.append(f"[Voice message transcript: {transcript}]{newline}")
    atomic_write(destination, "".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxpad",
        description="VoxPad: transcribe audio files and exported WhatsApp voice messages locally with OpenAI Whisper.",
        epilog="Example: voxpad chat.zip --language es -o transcripts.json",
    )
    parser.add_argument("source", type=Path, help="Export ZIP, extracted folder, chat .txt, or a single audio file")
    parser.add_argument("-o", "--output", type=Path, default=Path("transcripts.json"), help="JSON output; also writes a matching .txt report (default: transcripts.json)")
    parser.add_argument("--chat-output", type=Path, help="Write a copy of the chat with transcripts inserted (requires exactly one chat .txt)")
    parser.add_argument("--language", choices=LANGUAGES, metavar="CODE", help="Force the spoken language, as a Whisper code such as es or en; default: auto-detect for each chunk")
    parser.add_argument("--keyword", action="append", default=[], help="Favor a name or phrase; may be repeated")
    parser.add_argument("--word-timestamps", action="store_true", help="Include word times and probabilities in JSON")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Whisper model name, or a folder with a converted model (default: {DEFAULT_MODEL})")
    parser.add_argument("--remote", choices=tuple(REMOTE_SERVICES), help="Upload the audio to this hosted service instead of running Whisper on this computer; needs a Hugging Face token in HF_TOKEN and is billed to that account")
    parser.add_argument("--chunk-seconds", type=float, default=30, help="Longest chunk, greater than 0 and at most 30 (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="List audio and associated messages without loading Whisper or writing files")
    return parser


def transcribe_export(source: Path, output: Path, chat_output: Path | None = None, *,
                      model: str = DEFAULT_MODEL, language: str | None = None, keywords: list[str] | None = None,
                      word_timestamps: bool = False, chunk_seconds: float = 30, dry_run: bool = False,
                      remote: str | None = None, remote_token: str | None = None, chat_output_optional: bool = False,
                      notify=None, progress=None, should_stop=None) -> dict | None:
    """Transcribe every recording of an export and write the reports.

    `notify(text)` receives status lines, `progress(done, total)` is called before
    each recording and once at the end, and `should_stop()` is asked before each
    recording. With `chat_output_optional`, an export without exactly one chat
    skips the annotated chat instead of failing. `remote` names a hosted service
    that receives the audio instead of Whisper running here, and `remote_token`
    is the access token for it when the saved one is not to be used. Returns a
    summary, whose `refusal` says why a hosted service ended the run early, or
    None for a dry run.
    """
    notify = notify or (lambda text: print(text, file=sys.stderr))
    conflict = remote_conflict(remote, model=model, language=language, keywords=keywords, word_timestamps=word_timestamps)
    if conflict:
        raise ValueError(conflict)
    destinations = {output, output.with_suffix(".txt").resolve()}
    if chat_output:
        destinations.add(chat_output)
    with open_export(source) as export:
        # Check original inputs before hiding any output from discovery.
        for path in export.audio + export.chats:
            if path.resolve() not in destinations:
                continue
            is_original = path.suffix.lower() in AUDIO_EXTENSIONS
            if not is_original:
                try:
                    is_original = bool(parse_chat(path.read_text(encoding="utf-8-sig"))) or path.name.lower() == "_chat.txt"
                except UnicodeError:
                    is_original = True
            if is_original:
                raise ValueError(f"Output must not overwrite an original chat or audio file: {path}")
        export.audio = [path for path in export.audio if path.resolve() not in destinations]
        export.chats = [path for path in export.chats if path.resolve() not in destinations]
        if not export.audio:
            raise ValueError("No audio files found. Export the WhatsApp chat with media included and keep the voice-note attachments.")
        contexts, texts, occurrences = index_messages(export)
        if chat_output and len(texts) != 1:
            if not chat_output_optional:
                raise ValueError("--chat-output requires exactly one readable UTF-8 chat .txt; use a chat .txt as the input to select it.")
            chat_output = None
        if chat_output and any(chat_output == path.resolve() for path in export.chats + export.audio):
            raise ValueError("--chat-output must not overwrite an original chat or audio file.")
        if dry_run:
            for path in export.audio:
                print(path.relative_to(export.root).as_posix())
                for message in contexts[path]:
                    print(f"  {message['timestamp']} | {message['sender'] or '(system message)'}")
            return None
        # Disable the model hub's optional usage telemetry. Inference stays local unless a remote service was chosen.
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["DO_NOT_TRACK"] = "1"
        try:
            import codecpod
        except ImportError as error:
            raise RuntimeError("Install the Python dependencies: python -m pip install -r requirements.txt") from error
        if remote:
            engine = RemoteWhisper(model, remote, remote_token)
            notify(f"Uploading the audio to {engine.service} to transcribe it with {engine.model}...")
            model_name = f"{engine.model} on {engine.service}"
        else:
            notify(f"Loading Whisper {model} (the first run downloads the model)...")
            engine = Whisper(model)
            model_name = f"Whisper {Path(model).name if Path(model).is_dir() else model}"
        # Reports get shared: name the input without revealing where it is stored.
        report = {"source": source.name, "model": model_name, "sample_rate": SAMPLE_RATE,
                  "chunk_seconds": chunk_seconds, "results": []}
        results: dict[Path, dict] = {}
        stopped = False
        refusal = None
        for number, path in enumerate(export.audio, 1):
            if should_stop and should_stop():
                stopped = True
                break
            filename = path.relative_to(export.root).as_posix()
            if progress:
                progress(number - 1, len(export.audio))
            notify(f"[{number}/{len(export.audio)}] {filename}")
            result = {"file": filename, "messages": contexts[path], "status": "ok", "text": ""}
            try:
                result.update(transcribe_audio(
                    path, engine, chunk_seconds=chunk_seconds, decoder=codecpod,
                    language=language, keywords=keywords or [], word_timestamps=word_timestamps,
                ))
            except RemoteRefused as error:
                notify(f"  Error: {error}")
                # The remaining recordings would be refused alike, so keep what is done and stop.
                refusal = str(error)
                result.update(status="error", error=refusal)
            except (OSError, ValueError, RuntimeError, EOFError) as error:
                notify(f"  Error: {error}")
                # Decoder and system messages may quote the full local path.
                result.update(status="error", error=str(error).replace(str(path), filename))
            report["results"].append(result)
            results[path] = result
            # Save after each recording so completed work survives interruption.
            write_reports(output, report)
            if refusal:
                stopped = True
                break
        if chat_output:
            chat, text = next(iter(texts.items()))
            write_annotated_chat(chat_output, chat, text, occurrences, results)
        if progress:
            progress(len(report["results"]), len(export.audio))
        return {
            "report": report, "total": len(export.audio), "stopped": stopped, "refusal": refusal,
            "failures": sum(result["status"] == "error" for result in report["results"]),
            "output": output, "chat_output": chat_output,
        }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.chunk_seconds) or not 0 < args.chunk_seconds <= 30:
        parser.error("--chunk-seconds must be greater than 0 and at most 30")
    conflict = remote_conflict(args.remote, model=args.model, language=args.language, keywords=args.keyword,
                               word_timestamps=args.word_timestamps)
    if conflict:
        parser.error(conflict)
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    chat_output = args.chat_output.expanduser().resolve() if args.chat_output else None
    if output.suffix.lower() != ".json":
        parser.error("--output must have a .json extension")
    destinations = {output, output.with_suffix(".txt").resolve()}
    if chat_output:
        if chat_output in destinations:
            parser.error("--chat-output must differ from the report output files")
        destinations.add(chat_output)
    if source in destinations:
        parser.error("Output paths must differ from the input")
    if not source.exists():
        parser.error(f"Input does not exist: {source}")
    try:
        summary = transcribe_export(
            source, output, chat_output, model=args.model, language=args.language, keywords=args.keyword,
            word_timestamps=args.word_timestamps, chunk_seconds=args.chunk_seconds, dry_run=args.dry_run,
            remote=args.remote,
        )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if summary is None:
        return 0
    print(f"Saved {len(summary['report']['results'])} transcripts ({summary['failures']} failed) to {output} and {output.with_suffix('.txt')}")
    if chat_output:
        print(f"Annotated chat: {chat_output}")
    if summary["stopped"]:
        print("Stopped early: the remaining recordings were not transcribed.", file=sys.stderr)
    return 1 if summary["failures"] else 0


def cli() -> None:
    """Command-line entry point with a consistent interruption exit status."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Any completed transcripts have been saved.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    cli()
