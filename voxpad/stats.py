"""Count what a chat holds and write its viewer: the voxpad-stats command, and write_conversation for every other surface.

Works on an export with or without its recordings and on what earlier runs
left behind: a chat with transcripts inserted, a transcripts.json.
"""

import argparse
from collections.abc import Collection, Sequence
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unicodedata
import wave
import zipfile

from .analysis import BUCKETS, DATE_ORDERS, WORD, build_model, parse_events, totals
from .chat import parse_chat
from .export import Export, open_export, read_chats
from .figure import EVENT_STYLES, MAX_PEOPLE, chat_span, choose_people, require_matplotlib, split_events, write_figure
from .reports import TRANSCRIPT_PREFIX, load_previous_results, merged, without_transcripts
from .viewer import audio_link, write_viewer


AUDIO_MODES = ("auto", "link", "copy", "none")
ORDER_WORDS = {"DMY": "day/month/year", "MDY": "month/day/year", "YMD": "year/month/day"}
# The fixed part of an Ogg page header: capture pattern, version, flags, granule position, stream, sequence, checksum, segments.
OGG_PAGE = struct.Struct("<4sBBqIIIB")
# The last page lies within this many bytes of the end; a page holds at most 65307.
OGG_TAIL = 2 * 65307


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _ogg_page(data: bytes, start: int) -> tuple[int, int, int, int] | None:
    """The Ogg page at `start`: (granule position, stream, where its packets begin, where it ends), or None when none stands there whole."""
    if len(data) - start < OGG_PAGE.size:
        return None
    capture, version, flags, granule, stream, _, _, segments = OGG_PAGE.unpack_from(data, start)
    body = start + OGG_PAGE.size + segments
    if capture != b"OggS" or version or flags > 7 or body > len(data):
        return None
    end = body + sum(data[start + OGG_PAGE.size:body])
    return (granule, stream, body, end) if end <= len(data) else None


def opus_seconds(path: Path) -> float | None:
    """Return the length of an Ogg Opus recording read from its headers, or None when the file is something else.

    The last page says how many samples the stream holds at 48 kHz; the first
    says how many of them at the start are not played.
    """
    try:
        with path.open("rb") as recording:
            head = recording.read(4096)
            first = _ogg_page(head, 0)
            if first is None:
                return None
            _, stream, body, end = first
            if end - body < 19 or head[body:body + 8] != b"OpusHead":
                return None
            skipped = struct.unpack_from("<H", head, body + 10)[0]
            size = recording.seek(0, os.SEEK_END)
            start = max(0, size - OGG_TAIL)
            recording.seek(start)
            tail = recording.read()
    except OSError:
        return None
    at = len(tail)
    while True:
        at = tail.rfind(b"OggS", 0, at)
        # The first page only describes the stream: a file that ends there was cut off.
        if at < 0 or start + at == 0:
            return None
        page = _ogg_page(tail, at)
        # A page on which no packet ends has no position; the page before it has.
        if page and page[1] == stream and page[0] >= 0:
            seconds = max(0, page[0] - skipped) / 48000
            # Even silence takes more than a byte a second: a longer claim is a damaged position, not a length.
            return seconds if seconds <= size else None


def wav_seconds(path: Path) -> float | None:
    """Return the length of a WAV recording read from its header, or None when the standard library cannot read it."""
    try:
        with path.open("rb") as stream, wave.open(stream) as recording:
            rate = recording.getframerate()
            return recording.getnframes() / rate if rate else None
    except (OSError, EOFError, wave.Error):
        return None


def measure_seconds(path: Path, decoder=None) -> float | None:
    """Return how long a recording lasts, or None when that cannot be told.

    Ogg Opus and WAV say so in their headers. Anything else is decoded with
    `decoder`, the codecpod module, when one is given.
    """
    suffix = path.suffix.lower()
    seconds = opus_seconds(path) if suffix in {".opus", ".ogg", ".oga"} else wav_seconds(path) if suffix == ".wav" else None
    if seconds is None and decoder is not None:
        try:
            samples, rate = decoder.load(str(path), mono=True)
        except (decoder.CodecpodError, OSError, ValueError, RuntimeError):
            return None
        seconds = samples.shape[-1] / rate if rate else None
    return seconds


def _conversation(text: str) -> tuple:
    """What a chat says, whichever copy of it this is: its messages without inserted transcripts, word by word."""
    return tuple((tuple(WORD.findall(message.timestamp)), message.sender and tuple(WORD.findall(message.sender)),
                  tuple(WORD.findall(message.text))) for message in parse_chat(without_transcripts(text)))


def choose_chat(export: Export, source: Path, *, notify=None) -> tuple[Path, str]:
    """Return the one chat of an export and its exact text, or raise ValueError when there is none or more than one.

    Files that hold the same conversation count as one chat, so a folder may keep
    the copy with transcripts beside the chat it was made from; the copy with the
    most transcripts is the one read.
    """
    chats = read_chats(export)
    if not chats:
        raise ValueError("No WhatsApp chat was found. Pass an export ZIP, its folder or the chat .txt.")
    if len(chats) == 1:
        return next(iter(chats.items()))
    names = [chat.relative_to(export.root).as_posix() for chat in chats]
    if len({_conversation(text) for text in chats.values()}) > 1:
        zipped = source.suffix.lower() == ".zip" and not source.is_dir()
        raise ValueError(
            f"Found {len(chats)} chats: {', '.join(names)}. " + (f"Extract {source.name} and pass" if zipped else "Pass")
            + f' the one to analyse, for example: voxpad-stats "{"FOLDER" if zipped else source.name}/{names[0]}"')
    # max() keeps the first of equals, and the export lists its chats in a fixed order.
    chosen = max(chats, key=lambda chat: chats[chat].count(TRANSCRIPT_PREFIX))
    if notify:
        notify(f"{', '.join(names)} hold the same conversation; reading {chosen.relative_to(export.root).as_posix()}.")
    return chosen, chats[chosen]


def _recordings(export: Export, chat: Path) -> dict[str, Path]:
    """The recording each file name stands for. A name shared by several is the one beside the chat, or nobody's."""
    by_name: dict[str, list[Path]] = {}
    for path in export.audio:
        by_name.setdefault(_nfc(path.name), []).append(path)
    chosen = {}
    for name, paths in by_name.items():
        selected = [path for path in paths if path.parent == chat.parent] or paths
        if len(selected) == 1:
            chosen[name] = selected[0]
    return chosen


def _candidates(source: Path, chat: Path) -> list[Path]:
    """Where a report of this chat is looked for when none is named, the likeliest place first."""
    if source.is_file() and source.suffix.lower() == ".zip":
        places = [source.parent / "transcripts.json"]
    else:
        places = [chat.parent / "transcripts.json", chat.with_suffix(".json")]
    places.append(Path.cwd() / "transcripts.json")
    found: dict[Path, Path] = {}
    for place in places:
        if place.is_file():
            found.setdefault(place.resolve(), place)
    return list(found.values())


def _voices(model: dict) -> list[dict]:
    return [message["voice"] for message in model["messages"] if message["kind"] == "voice" and message["sender"] is not None]


def _matched(model: dict, results: list[dict]) -> int:
    """How many voice messages of a model a report describes."""
    names = {_nfc(result["file"]).rsplit("/", 1)[-1] for result in results}
    return sum(voice["file"] in names for voice in _voices(model))


def _counted(count: int, noun: str) -> str:
    """A count with its noun: 1 recording, 2,000 recordings."""
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def _listed(names: list[str]) -> str:
    return " and ".join(filter(None, [", ".join(names[:-1]), names[-1]]))


def _size(count: int) -> str:
    return f"{max(1, round(count / 1024)):,} KiB" if count < 1024 ** 2 else f"{count / 1024 ** 2:,.0f} MiB"


def _copy(path: Path, target: Path) -> None:
    """Copy a recording so that the name appears only once all of it is there."""
    temporary = None
    try:
        with path.open("rb") as incoming, tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as outgoing:
            temporary = Path(outgoing.name)
            shutil.copyfileobj(incoming, outgoing)
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_conversation(source: Path, viewer: Path | None, *, transcripts: Sequence[Path] = (), events: Path | None = None,
                       date_order: str | None = None, audio: str = "auto", bucket: str | None = None,
                       excluded: Collection[Path] = (), optional: bool = False, discover: bool = False, check=None,
                       notify=None) -> dict | None:
    """Read one chat as a conversation model and write its viewer.

    `source` is an export ZIP, a folder or a chat .txt, with or without
    recordings. `viewer` is the HTML file to write, or None to only build the
    model. `transcripts` are reports of earlier runs; with `discover` and none
    given, a transcripts.json beside the chat is looked for. `events` is a file
    of dated events, `date_order` DMY, MDY or YMD when the dates are not to be
    worked out, `bucket` the day, week or month the viewer's charts open with.
    `audio` says how the page reaches the recordings: `link` names them where
    they are, which needs them in the page's folder or below it; `copy` puts
    them into "<page name>_audio" beside the page; `none` leaves them out; `auto`
    links where it can and copies otherwise. `excluded` are files this run wrote
    into the source and that are not to be read as chats. `check(model)` is
    called before anything is written and may raise to stop. `notify(text)`
    receives what was found and done.

    The source must hold exactly one conversation: otherwise ValueError, or None
    with `optional`. Returns {"model", "viewer", "chat", "transcripts", "matched",
    "recordings", "measured", "audio", "audio_folder", "copied", "copied_bytes",
    "event_warnings"}: the model (schema 1); the page written or None; the
    chat's file name; the file names of the reports used and how many voice
    messages they describe; how many voice messages have their recording in the
    source and how many lengths were measured from the files; `link`, `copy` or
    `none` as it turned out, the folder of the copies or None, their number and
    size; and the warnings about lines of the events file.
    """
    notify = notify or (lambda text: print(text, file=sys.stderr))
    if audio not in AUDIO_MODES:
        raise ValueError(f"Unknown audio mode: {audio}")
    source = Path(source)
    viewer = Path(viewer) if viewer else None
    zipped = source.is_file() and source.suffix.lower() == ".zip"
    marked, event_warnings = ([], []) if events is None else parse_events(Path(events).read_bytes().decode("utf-8", errors="replace"))
    for warning in event_warnings:
        notify(warning)
    with open_export(source, {Path(path).resolve() for path in excluded}) as export:
        try:
            chat, text = choose_chat(export, source, notify=notify)
        except ValueError:
            if optional:
                return None
            raise
        inputs = {path.resolve() for path in [source, *export.chats, *export.audio, *map(Path, transcripts), *([Path(events)] if events else [])]}
        # A message naming a recording of the export is a voice message, transcribed or not.
        known = [{"file": path.relative_to(export.root).as_posix(), "status": "pending"} for path in export.audio]

        def build(results: list[dict], **more) -> dict:
            return build_model(text, results=results + known, events=marked, date_order=date_order, **more)

        used, results, draft = [], [], None
        if transcripts:
            reports = load_previous_results(transcripts)
            used, results = [report.name for report in reports], merged(reports)
        elif discover:
            for place in _candidates(source, chat):
                try:
                    found = merged(load_previous_results([place]))
                except ValueError as error:
                    notify(f"{error} It is not used.")
                    continue
                trial = build(found)
                if _matched(trial, found):
                    used, results, draft = [place.name], found, trial
                    inputs.add(place.resolve())
                    break
                notify(f"{place.name} matches no voice message of this chat; it is not used.")
        draft = draft or build(results)
        voices = _voices(draft)
        matched = _matched(draft, results)
        if used:
            notify(f"Using {_listed(used)}: {matched:,} of {len(voices):,} voice messages matched")
        if viewer and viewer.resolve() in inputs:
            raise ValueError(f"The viewer must not replace {viewer.name}, which this run reads.")

        # The recordings the chat's voice messages name, in the order of the chat.
        present = _recordings(export, chat)
        wanted = {voice["file"]: present[voice["file"]] for voice in voices if voice["file"] in present}
        mode, folder, copies, sources = "none", None, {}, {}
        if viewer and wanted and audio != "none":
            links = None if zipped else {name: audio_link(path, viewer) for name, path in wanted.items()}
            folder = viewer.with_name(f"{viewer.stem}_audio")
            # Copies inside the export would be taken for recordings of the chat by the next run.
            inside = not zipped and export.root.resolve() in [folder.resolve(), *folder.resolve().parents]
            if audio == "link" and zipped:
                raise ValueError("--audio link cannot be used with a ZIP: its recordings exist only while it is read. Use --audio copy.")
            if audio == "link" and None in links.values():
                raise ValueError("--audio link needs every recording in the viewer's folder or below it: reaching one elsewhere takes "
                                 '"..", which would write the names of this computer\'s folders into the page. Write the viewer beside '
                                 "the export, or use --audio copy.")
            if audio == "copy" and inside:
                raise ValueError(f"--audio copy would put {folder.name}/ inside the export, where its files would be read as recordings "
                                 "of the chat. Write the viewer outside the export, or use --audio link.")
            if links and None not in links.values() and audio != "copy":
                mode, folder, sources = "link", None, links
            elif inside:
                folder = None
                notify(f"{viewer.name} is written without the recordings: from inside the export it can only play those in its own "
                       "folder or below it. Write the viewer into the export's top folder, or outside the export.")
            else:
                mode, taken = "copy", set()
                for name, path in wanted.items():
                    target, number = path.name, 1
                    # Two names may differ only in ways this file system does not tell apart.
                    while _nfc(target).casefold() in taken:
                        number += 1
                        target = f"{number}-{path.name}"
                    taken.add(_nfc(target).casefold())
                    copies[name] = target
                    sources[name] = audio_link(folder / target, viewer)
        # Recordings that are here but that no report times are measured, from their headers where those say it.
        untimed = sorted({voice["file"] for voice in voices if voice["seconds"] is None and voice["file"] in wanted})
        durations = {name: seconds for name in untimed if (seconds := measure_seconds(wanted[name])) is not None}
        if len(durations) < len(untimed):
            try:
                import codecpod as decoder
            except ImportError:
                decoder = None
            for name in untimed:
                if decoder and name not in durations and (seconds := measure_seconds(wanted[name], decoder)) is not None:
                    durations[name] = seconds
            if len(durations) < len(untimed):
                unread = len(untimed) - len(durations)
                notify(f"The length of {_counted(unread, 'recording')} could not be read"
                       + ("." if decoder else "; with codecpod installed it is decoded to measure it." if unread == 1
                          else "; with codecpod installed they are decoded to measure them."))
        if durations:
            notify(f"Measured the length of {_counted(len(durations), 'recording')} from {'its file' if len(durations) == 1 else 'their files'}.")
        model = build(results, audio_src=sources.get, durations=durations) if sources or durations else draft
        if check:
            check(model)
        copied = 0
        if copies:
            folder.mkdir(parents=True, exist_ok=True)
            for name, target in copies.items():
                if (folder / target).resolve() in inputs:
                    raise ValueError(f"The copy of a recording must not replace {target}, which this run reads.")
                _copy(wanted[name], folder / target)
                copied += wanted[name].stat().st_size
            notify(f"Copied {_counted(len(copies), 'voice message')} ({_size(copied)}) to {folder.name}/ so the viewer can play "
                   f"{'it' if len(copies) == 1 else 'them'}; --audio none skips this.")
        if viewer:
            write_viewer(viewer, model, bucket=bucket)
        return {
            "model": model, "viewer": viewer, "chat": chat.name, "transcripts": used, "matched": matched,
            "recordings": sum(voice["file"] in present for voice in voices), "measured": len(durations),
            "audio": mode, "audio_folder": folder if mode == "copy" else None, "copied": len(copies), "copied_bytes": copied,
            "event_warnings": event_warnings,
        }


def _span(seconds: float) -> str:
    """A length of time in words, as the viewer writes it: 42 s, 12 min 5 s, 3 h 12 min."""
    total = max(0, round(seconds))
    if total < 60:
        return f"{total} s"
    if total < 3600:
        return f"{total // 60} min {total % 60} s"
    return f"{total // 3600} h {total % 3600 // 60} min"


def coverage_lines(model: dict) -> list[str]:
    """The sentences that keep a chat transcribed in part from reading as one that said little; the viewer shows the same."""
    voices = _voices(model)
    counts = {status: sum(voice["status"] == status for voice in voices) for status in ("ok", "empty", "error", "pending", "missing")}
    transcribed, timed = counts["ok"] + counts["empty"], sum(voice["seconds"] is not None for voice in voices)
    lines = []
    if transcribed < len(voices):
        lines.append(f"Spoken words cover {transcribed:,} of {len(voices):,} voice messages ({counts['pending']:,} not transcribed, "
                     f"{counts['error']:,} failed, {counts['missing']:,} without a recording).")
    if timed < len(voices):
        lines.append(f"Voice time covers {timed:,} of {len(voices):,} voice messages.")
    return lines


def _table(model: dict) -> list[str]:
    """What each person wrote and said, in columns."""
    rows = [(name, row["messages"], row["words_typed"], row["words_spoken"], row["words_typed"] + row["words_spoken"],
             row["voice_notes"], row["voice_seconds"]) for name, row in zip(model["participants"], totals(model))]
    if len(rows) > 1:
        rows.append(("Total", *(sum(row[column] for row in rows) for column in range(1, 7))))
    cells = [("", "Messages", "Typed words", "Spoken words", "Total words", "Voice notes", "Voice time")]
    cells += [(row[0], *(f"{value:,}" for value in row[1:6]), _span(row[6])) for row in rows]
    widths = [max(len(line[column]) for line in cells) for column in range(7)]
    return ["  ".join([line[0].ljust(widths[0])] + [cell.rjust(width) for cell, width in zip(line[1:], widths[1:])]).rstrip()
            for line in cells]


def summary_lines(conversation: dict, *, command: bool = True) -> list[str]:
    """Return what voxpad-stats prints about a conversation, for any surface that has write_conversation's result.

    `command` says the reader is at a command line: without it, the two
    sentences that name options of voxpad-stats are worded without them.
    """
    model = conversation["model"]
    voices = _voices(model)
    span = chat_span(model)
    sent = sum(message["kind"] != "system" and message["sender"] is not None for message in model["messages"])
    lines = [f"Parsed {sent:,} messages from {span[0].isoformat()} to {span[1].isoformat()}" if span
             else f"Parsed {sent:,} messages; none has a date that can be read"]
    lines.append(f"Dates are read as {ORDER_WORDS[model['date_order']]} ({model['date_order']}).")
    if model["date_order_ambiguous"]:
        lines.append("Warning: the dates of this chat can also be read in another order."
                     + (" If the days look wrong, pass --date-order DMY, MDY or YMD." if command else ""))
    undated = sum(message["time"] is None for message in model["messages"])
    if span and undated:
        lines.append(f"{undated:,} messages have no date that can be read; they are counted on the day of the message before them.")
    lines += [f"Warning: {warning}" for warning in model["warnings"]]
    if model["participants"]:
        lines += ["", *_table(model), ""]
    timed = sum(voice["seconds"] is not None for voice in voices)
    lines.append(f"Voice messages: {len(voices):,} detected, {conversation['recordings']:,} with a recording, {timed:,} with a duration.")
    lines += coverage_lines(model)
    if not voices:
        lines.append("Warning: the chat names no voice message. An export made without media shows every attachment as "
                     "<Media omitted>; export the chat again with media to count voice messages per person.")
    waiting = sum(voice["status"] in ("pending", "error") for voice in voices)
    if waiting:
        it, its = ("it", "its") if waiting == 1 else ("them", "their")
        lines.append(f"{_counted(waiting, 'voice message')} {'has' if waiting == 1 else 'have'} no transcript: "
                     + (f"pass --transcripts FILE, or transcribe {it} with voxpad" if command else f"transcribe {it} to count {its} words"))
    if model["events"]:
        near, far = split_events(model["events"], span)
        lines.append(f"Events: {len(near):,} shown" + (f"; {len(far):,} far outside {span[0].isoformat()}–{span[1].isoformat()} not plotted"
                                                       if far and span else f"; {len(far):,} not plotted" if far else ""))
    return lines


def viewer_lines(conversation: dict, *, command: bool = True) -> list[str]:
    """Return the closing lines about the page that was written: where it is, how to open it, and what it holds."""
    viewer = conversation["viewer"]
    if viewer is None:
        return []
    lines = [f"Viewer: {viewer}", "Open it in a browser; it needs no connection."]
    if conversation["audio"] == "copy":
        lines.append(f"Keep {conversation['audio_folder'].name}/ beside it: the voice messages play from there.")
    elif conversation["audio"] == "link":
        lines.append("Leave it where it is: the voice messages play from the recordings beside it.")
    lines.append(f"{viewer.name} contains the whole conversation and its transcripts"
                 + ("; share the PNG (--png) when you only want the charts." if command else "."))
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxpad-stats",
        description="VoxPad: count what each person wrote and said in a WhatsApp chat, per day, and write a page to read, hear and chart it.",
        epilog="Example: voxpad-stats chat.zip --transcripts transcripts.json --events events.txt --png activity.png",
    )
    parser.add_argument("source", type=Path, help="Export ZIP, extracted folder, or chat .txt: the exported chat or a copy with transcripts inserted")
    parser.add_argument("--transcripts", type=Path, action="append", default=[], metavar="JSON", help="Transcript report of an earlier run, a transcripts.json or the browser app's JSON download; may be repeated (default: a transcripts.json beside the chat or in the current folder)")
    parser.add_argument("--no-transcripts", action="store_true", help="Do not look for a transcript report")
    parser.add_argument("--events", type=Path, metavar="FILE", help="Dated events to show, one per line: 2024-01-15, label. Lines starting with # are skipped")
    parser.add_argument("--date-order", choices=DATE_ORDERS, help="How the chat writes its dates, when working it out goes wrong: DMY, MDY or YMD. A two-digit year written first is read as a day unless YMD is given")
    parser.add_argument("--viewer", type=Path, metavar="HTML", help="Where to write the page (default: conversation.html in the current folder)")
    parser.add_argument("--no-viewer", action="store_true", help="Do not write the page")
    parser.add_argument("--audio", choices=AUDIO_MODES, help="How the page reaches the recordings: link names them where they are, copy puts them beside the page, none leaves them out (default: auto, which links recordings in the page's folder or below it and copies otherwise)")
    parser.add_argument("--bucket", choices=BUCKETS, help="Count per day, week or month in the figure and when the page opens (default: day)")
    parser.add_argument("--png", type=Path, metavar="PNG", help="Also draw the charts as an image; needs matplotlib")
    parser.add_argument("--person", action="append", default=[], metavar="NAME", help=f"A participant to draw in the image, by exact name; may be repeated up to {MAX_PEOPLE} times (default: the two most active)")
    parser.add_argument("--event-style", choices=EVENT_STYLES, help="Events in the image: list writes each in full below its date, key numbers them and adds a legend (default: auto, which is list)")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.viewer and args.no_viewer:
        parser.error("--viewer and --no-viewer cannot be given together")
    if args.audio and args.no_viewer:
        parser.error("--audio needs the page, which --no-viewer leaves out")
    if args.transcripts and args.no_transcripts:
        parser.error("--transcripts and --no-transcripts cannot be given together")
    if (args.person or args.event_style) and not args.png:
        parser.error("--person and --event-style need --png")
    if len(args.person) > MAX_PEOPLE:
        parser.error(f"--person can be given at most {MAX_PEOPLE} times")
    source = args.source.expanduser().resolve()
    viewer = None if args.no_viewer else (args.viewer or Path("conversation.html")).expanduser().resolve()
    png = args.png.expanduser().resolve() if args.png else None
    transcripts = [path.expanduser().resolve() for path in args.transcripts]
    events = args.events.expanduser().resolve() if args.events else None
    if args.viewer and viewer.suffix.lower() != ".html":
        parser.error("--viewer must have a .html extension")
    if png and png.suffix.lower() != ".png":
        parser.error("--png must have a .png extension")
    if not source.exists():
        parser.error(f"Input does not exist: {source}")
    for path in transcripts:
        if not path.is_file():
            parser.error(f"--transcripts file does not exist: {path}")
    if events and not events.is_file():
        parser.error(f"--events file does not exist: {events}")
    if {viewer, png} & {source, events, *transcripts} - {None}:
        parser.error("Output paths must differ from the input")
    failures = (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile)
    try:
        if png:
            # Before anything is written: a page without the image that was asked for is half a result.
            require_matplotlib()
        conversation = write_conversation(
            source, viewer, transcripts=transcripts, discover=not args.no_transcripts, events=events, date_order=args.date_order,
            audio=args.audio or "auto", bucket=args.bucket,
            check=(lambda model: choose_people(model, args.person)) if png else None,
        )
    except failures as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    for line in summary_lines(conversation):
        print(line)
    code = 0
    if png:
        try:
            write_figure(png, conversation["model"], bucket=args.bucket or "day", people=args.person,
                         event_style=args.event_style or "auto", notify=print)
            print(f"Figure: {png}")
        except failures as error:
            print(f"Error: {error}", file=sys.stderr)
            code = 1
    for line in viewer_lines(conversation):
        print(line)
    return code


def cli() -> None:
    """Command-line entry point with a consistent interruption exit status."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    cli()
