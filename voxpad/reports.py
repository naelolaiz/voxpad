"""Write the transcript reports and the copy of the chat with transcripts inserted, and read earlier reports."""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import tempfile
import unicodedata

from .chat import HEADER, INVISIBLE, NEWLINE


# A line with its own ending, or the last line when the text does not end with one.
# Lines end where parse_chat ends them, so its line numbers count the same lines.
LINE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+")
# How a transcript inserted after a message begins; it ends with "]" at the end of a line.
TRANSCRIPT_PREFIX = "[Voice message transcript: "


@dataclass
class PreviousReport:
    """What an earlier run found out: the report's file name, the model it names and its finished results."""
    name: str
    model: str | None
    results: list[dict]


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


def _finite(value):
    """The same data with null in place of every number that JSON cannot hold."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(item) for item in value]
    return value


def write_reports(output: Path, report: dict) -> None:
    try:
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    except ValueError:
        # NaN and Infinity are not JSON: the browser app and the viewer could not read the report.
        text = json.dumps(_finite(report), ensure_ascii=False, indent=2, allow_nan=False)
    atomic_write(output, text + "\n")
    lines = []
    for result in report["results"]:
        lines.append(result["file"])
        for message in result["messages"]:
            lines.append(f"  {message['timestamp']} | {message['sender'] or '(system message)'}")
        lines.append(result["text"] or (f"[ERROR: {result['error']}]" if result["status"] == "error" else "[No speech detected]"))
        lines.append("")
    atomic_write(output.with_suffix(".txt"), "\n".join(lines))


def _transcript_tail(lines: list[str]) -> int:
    """Count the lines at the end of a message that are transcripts inserted by an earlier run.

    `lines` are the message's lines after its header, without their endings. A
    transcript that contains line breaks runs on to the first line ending in "]".
    """
    tail = 0
    number = 0
    while number < len(lines):
        end = number
        if lines[number].startswith(TRANSCRIPT_PREFIX):
            while end < len(lines) and not lines[end].endswith("]"):
                end += 1
        else:
            end = len(lines)
        if end < len(lines):
            tail += end - number + 1
            number = end + 1
        else:
            # Anything typed after a transcript means the message does not end with it.
            tail = 0
            number += 1
    return tail


def _as_parsed(lines: list[str]) -> list[str]:
    """Each line as parse_chat reads it: without its ending and without invisible marks."""
    return [line.translate(INVISIBLE).rstrip("\r\n") for line in lines]


def without_transcripts(text: str) -> str:
    """Return a chat without the transcripts its messages end with: the chat as it was exported."""
    lines = LINE.findall(text)
    parsed = _as_parsed(lines)
    kept = []
    start = None
    for number in range(len(lines) + 1):
        if number == len(lines) or HEADER.match(parsed[number]):
            if start is not None:
                tail = _transcript_tail(parsed[start + 1:number])
                if tail:
                    del kept[-tail:]
            start = number
        if number < len(lines):
            kept.append(lines[number])
    return "".join(kept)


def write_annotated_chat(destination: Path, chat: Path, text: str,
                         occurrences: dict, results: dict[Path, dict]) -> None:
    # Inserted lines follow the chat's own line endings.
    newline = NEWLINE.search(text)
    newline = newline[0] if newline else "\n"
    lines = []
    original = LINE.findall(text)
    parsed = _as_parsed(original)
    start = None
    for number, line in enumerate(original):
        if HEADER.match(parsed[number]):
            start = number
        lines.append(line)
        # A stopped run has no result yet for the recordings it did not reach.
        attached = [results[path] for path in occurrences.get((chat, number), []) if path in results]
        if attached and start is not None:
            # The chat may be an annotated copy itself: its transcripts are replaced, not repeated.
            tail = _transcript_tail(parsed[start + 1:number + 1])
            if tail:
                del lines[-tail:]
        if attached and not lines[-1].endswith(("\n", "\r")):
            lines.append(newline)
        for result in attached:
            transcript = result["text"] or (f"Error: {result['error']}" if result["status"] == "error" else "No speech detected")
            lines.append(f"[Voice message transcript: {transcript}]{newline}")
    atomic_write(destination, "".join(lines))


def _number(text: str) -> float | None:
    value = float(text)
    return value if math.isfinite(value) else None


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_duration(value) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _objects(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def _model(value) -> str | None:
    # The browser app describes its model as an object with a name.
    if isinstance(value, dict):
        value = value.get("name")
    return value if isinstance(value, str) and value else None


def _previous_result(entry: dict) -> dict:
    """Copy the fields VoxPad knows from a result; one of the wrong type is left out."""
    result = {"file": entry["file"], "status": entry["status"], "text": entry.get("text", "")}
    if result["status"] == "error":
        error = entry.get("error")
        result["error"] = error if isinstance(error, str) and error else "Transcription failed"
    if _objects(entry.get("messages")):
        result["messages"] = entry["messages"]
    languages = entry.get("languages")
    if isinstance(languages, list) and all(isinstance(language, str) for language in languages):
        result["languages"] = languages
    if _is_duration(entry.get("duration_seconds")):
        result["duration_seconds"] = entry["duration_seconds"]
    for key in ("segments", "words"):
        if _objects(entry.get(key)):
            result[key] = entry[key]
    if _is_count(entry.get("bytes")):
        result["bytes"] = entry["bytes"]
    if _model(entry.get("model")):
        result["model"] = _model(entry["model"])
    return result


def load_previous_results(paths) -> list[PreviousReport]:
    """Read reports of earlier runs: the command's, the desktop application's or the browser app's download.

    Each keeps its finished results, transcribed or failed, with the fields VoxPad
    writes. A file that is not such a report raises ValueError naming the file
    only, never where it is stored.
    """
    reports = []
    for path in paths:
        path = Path(path)
        problem = f"{path.name} is not a VoxPad transcript report."
        try:
            # Numbers JSON cannot hold are read as null, so they never reach a report written later.
            data = json.loads(path.read_bytes().decode("utf-8-sig"), parse_float=_number, parse_constant=lambda name: None)
        except (ValueError, RecursionError):
            raise ValueError(problem) from None
        entries = data.get("results") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ValueError(problem)
        results = []
        for entry in entries:
            if not (isinstance(entry, dict) and isinstance(entry.get("file"), str) and isinstance(entry.get("status"), str)
                    and isinstance(entry.get("text", ""), str)):
                raise ValueError(problem)
            # The browser app also lists the recordings it has not transcribed yet.
            if entry["status"] in ("ok", "error"):
                results.append(_previous_result(entry))
        reports.append(PreviousReport(path.name, _model(data.get("model")), results))
    return reports


def merged(reports) -> list[dict]:
    """Return one result per recording from several reports: a later report wins, and a transcript wins over an error."""
    chosen: dict[str, dict] = {}
    for report in reports:
        for result in report.results:
            file = unicodedata.normalize("NFC", result["file"])
            if result["status"] == "ok" or chosen.get(file, result)["status"] != "ok":
                chosen[file] = result
    return [dict(result) for result in chosen.values()]
