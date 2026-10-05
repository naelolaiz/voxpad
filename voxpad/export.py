"""Find the recordings and chats of a WhatsApp export and link them to each other."""

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
import zipfile

from .chat import parse_chat


AUDIO_EXTENSIONS = {".opus", ".ogg", ".oga", ".m4a", ".aac", ".mp3", ".wav", ".flac", ".amr", ".aif", ".aiff"}
MAX_ZIP_BYTES = 8 * 1024**3


@dataclass
class Export:
    root: Path
    audio: list[Path]
    chats: list[Path]


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


def _chats(export: Export):
    """Yield each chat of an export with its exact text and its messages."""
    for chat in export.chats:
        try:
            # Decode the bytes directly: text mode would rewrite CRLF line endings
            # and drop a leading BOM, and the annotated copy must keep both.
            text = chat.read_bytes().decode("utf-8")
        except UnicodeError:
            print(f"Warning: skipping non-UTF-8 chat text: {chat.name}", file=sys.stderr)
            continue
        messages = parse_chat(text)
        # Other text files of the folder, such as an earlier report, are not chats.
        if messages or chat.name.lower() == "_chat.txt":
            yield chat, text, messages


def read_chats(export: Export) -> dict[Path, str]:
    """Return the exact text of every chat in an export, whether or not it has recordings."""
    return {chat: text for chat, text, _ in _chats(export)}


def index_messages(export: Export) -> tuple[dict[Path, list[dict]], dict[Path, str], dict[tuple[Path, int], list[Path]]]:
    """Associate known audio filenames with messages, independent of attachment labels."""
    contexts: dict[Path, list[dict]] = {path: [] for path in export.audio}
    texts: dict[Path, str] = {}
    occurrences: dict[tuple[Path, int], list[Path]] = {}
    by_name: dict[str, list[Path]] = {}
    for path in export.audio:
        key = unicodedata.normalize("NFC", path.name)
        by_name.setdefault(key, []).append(path)
    filenames = re.compile(
        r"(?<![\w.-])(?:" + "|".join(re.escape(name) for name in sorted(by_name, key=len, reverse=True)) + r")(?![\w.-])"
    )
    for chat, text, messages in _chats(export):
        texts[chat] = text
        # Without recordings there is nothing to link, and the pattern would match everywhere.
        if not by_name:
            continue
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
