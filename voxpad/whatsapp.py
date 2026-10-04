"""VoxPad transcription of audio files and WhatsApp voice messages.

Requires cactus-needle and codecpod. Run with --help for examples and options.
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
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
import zipfile


AUDIO_EXTENSIONS = {".opus", ".ogg", ".oga", ".m4a", ".aac", ".mp3", ".wav", ".flac", ".amr", ".aif", ".aiff"}
LANGUAGES = ("en", "de", "fr", "es", "it", "nl", "pl")
SAMPLE_RATE = 16000
MAX_ZIP_BYTES = 8 * 1024**3
DATE = r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}"
TIME = r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:[aApP]\.?\s*[mM]\.?|[صم]|上午|下午|午前|午後))?"
HEADER = re.compile(
    rf"^(?:\[(?P<ios>{DATE},?\s+{TIME})\]\s*|(?P<android>{DATE},?\s+{TIME})\s+-\s+)(?P<body>.*)$"
)
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
            sender, separator, body = match["body"].partition(": ")
            messages.append(Message(
                match["ios"] or match["android"],
                sender if separator else None,
                body if separator else match["body"],
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
        with tempfile.TemporaryDirectory(prefix="whistle-export-") as temporary:
            root = Path(temporary)
            seen: set[str] = set()
            extracted_bytes = 0
            with zipfile.ZipFile(source) as archive:
                for member in archive.infolist():
                    relative = PurePosixPath(member.filename.replace("\\", "/"))
                    mode = member.external_attr >> 16
                    if (relative.is_absolute() or ".." in relative.parts
                            or (relative.parts and ":" in relative.parts[0])
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
            text = chat.read_text(encoding="utf-8-sig")
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
    for offset in range(0, total_frames, chunk_frames):
        frames = samples[offset:offset + chunk_frames]
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
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
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
    lines = []
    for number, line in enumerate(text.splitlines(keepends=True)):
        lines.append(line)
        attached = occurrences.get((chat, number), [])
        if attached and not line.endswith(("\n", "\r")):
            lines.append("\n")
        for path in attached:
            result = results[path]
            transcript = result["text"] or (f"Error: {result['error']}" if result["status"] == "error" else "No speech detected")
            lines.append(f"[Voice message transcript: {transcript}]\n")
    atomic_write(destination, "".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxpad",
        description="VoxPad: transcribe audio files and exported WhatsApp voice messages locally with Cactus Whistle.",
        epilog="Example: voxpad chat.zip --language es -o transcripts.json",
    )
    parser.add_argument("source", type=Path, help="Export ZIP, extracted folder, chat .txt, or a single audio file")
    parser.add_argument("-o", "--output", type=Path, default=Path("transcripts.json"), help="JSON output; also writes a matching .txt report (default: transcripts.json)")
    parser.add_argument("--chat-output", type=Path, help="Write a copy of the chat with transcripts inserted (requires exactly one chat .txt)")
    parser.add_argument("--language", choices=LANGUAGES, help="Force the spoken language; default: auto-detect for each chunk")
    parser.add_argument("--keyword", action="append", default=[], help="Favor a name or phrase; may be repeated")
    parser.add_argument("--word-timestamps", action="store_true", help="Include word times and probabilities in JSON")
    parser.add_argument("--model", type=Path, help="Use a local whistle.cact model instead of the default download")
    parser.add_argument("--chunk-seconds", type=float, default=30, help="Chunk length, greater than 0 and at most 30 (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="List audio and associated messages without loading Whistle or writing files")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.chunk_seconds) or not 0 < args.chunk_seconds <= 30:
        parser.error("--chunk-seconds must be greater than 0 and at most 30")
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
    if args.model and not args.model.expanduser().is_file():
        parser.error(f"Model does not exist: {args.model}")
    try:
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
                raise ValueError("--chat-output requires exactly one readable UTF-8 chat .txt; use a chat .txt as the input to select it.")
            if chat_output and any(chat_output == path.resolve() for path in export.chats + export.audio):
                raise ValueError("--chat-output must not overwrite an original chat or audio file.")
            if args.dry_run:
                for path in export.audio:
                    print(path.relative_to(export.root).as_posix())
                    for message in contexts[path]:
                        print(f"  {message['timestamp']} | {message['sender'] or '(system message)'}")
                return 0
            # Keep inference local and disable the runtime's optional usage telemetry.
            os.environ["NEEDLE_TELEMETRY"] = "0"
            os.environ["DO_NOT_TRACK"] = "1"
            try:
                import codecpod
                from needle import Whistle
            except ImportError as error:
                raise RuntimeError("Install the Python dependencies: python -m pip install -r requirements.txt") from error
            print("Loading Whistle (the first run downloads model/runtime files)...", file=sys.stderr)
            model = Whistle(weights=str(args.model.expanduser().resolve()) if args.model else None)
            report = {"source": str(source), "model": "Cactus Whistle", "sample_rate": SAMPLE_RATE,
                      "chunk_seconds": args.chunk_seconds, "results": []}
            results: dict[Path, dict] = {}
            for number, path in enumerate(export.audio, 1):
                filename = path.relative_to(export.root).as_posix()
                print(f"[{number}/{len(export.audio)}] {filename}", file=sys.stderr)
                result = {"file": filename, "messages": contexts[path], "status": "ok", "text": ""}
                try:
                    result.update(transcribe_audio(
                        path, model, chunk_seconds=args.chunk_seconds, decoder=codecpod,
                        language=args.language, keywords=args.keyword, word_timestamps=args.word_timestamps,
                    ))
                except (OSError, ValueError, RuntimeError, EOFError) as error:
                    result.update(status="error", error=str(error))
                    print(f"  Error: {error}", file=sys.stderr)
                report["results"].append(result)
                results[path] = result
                # Save after each recording so completed work survives interruption.
                write_reports(output, report)
            if chat_output:
                chat, text = next(iter(texts.items()))
                write_annotated_chat(chat_output, chat, text, occurrences, results)
            failures = sum(result["status"] == "error" for result in report["results"])
            print(f"Saved {len(report['results'])} transcripts ({failures} failed) to {output} and {output.with_suffix('.txt')}")
            if chat_output:
                print(f"Annotated chat: {chat_output}")
            return 1 if failures else 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


def cli() -> None:
    """Command-line entry point with a consistent interruption exit status."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Any completed transcripts have been saved.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    cli()
