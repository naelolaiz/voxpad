"""VoxPad transcription of audio files and WhatsApp voice messages.

Requires faster-whisper and codecpod. Run with --help for examples and options.
"""

import argparse
import math
import os
from pathlib import Path
import re
import shlex
import sys
import zipfile

from . import stats, transcribe
from .engines import DEFAULT_MODEL, LANGUAGES, REMOTE_SERVICES, remote_conflict
from .export import open_export


FAILURES = (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voxpad",
        description="VoxPad: transcribe audio files and exported WhatsApp voice messages locally with OpenAI Whisper.",
        epilog="Example: voxpad chat.zip --language es -o transcripts.json. Run the same command again to continue a stopped run.",
    )
    parser.add_argument("source", type=Path, help="Export ZIP, extracted folder, chat .txt, or a single audio file")
    parser.add_argument("-o", "--output", type=Path, default=Path("transcripts.json"), help="JSON output; also writes a matching .txt report. A report already there is continued: what it holds is not transcribed again (default: transcripts.json)")
    parser.add_argument("--chat-output", type=Path, help="Write a copy of the chat with transcripts inserted (requires exactly one chat .txt)")
    parser.add_argument("--viewer", type=Path, metavar="HTML", help="Write a page to read the chat, play its voice messages and chart its activity, also for what is done when the run is stopped (requires exactly one chat .txt)")
    parser.add_argument("--events", type=Path, metavar="FILE", help="Dated events to show in the page, one per line: 2024-01-15, label (requires --viewer)")
    parser.add_argument("--audio", choices=stats.AUDIO_MODES, help="How the page reaches the recordings: link names them where they are, copy puts them beside the page, none leaves them out (requires --viewer; default: auto, which links recordings in the page's folder or below it and copies otherwise)")
    parser.add_argument("--reuse", type=Path, action="append", default=[], metavar="JSON", help="Take the transcripts of this earlier report, a transcripts.json or the browser app's JSON download, instead of transcribing those recordings again; may be repeated")
    parser.add_argument("--fresh", action="store_true", help="Transcribe everything again instead of continuing an existing --output")
    parser.add_argument("--language", choices=LANGUAGES, metavar="CODE", help="Force the spoken language, as a Whisper code such as es or en; default: auto-detect for each chunk")
    parser.add_argument("--keyword", action="append", default=[], help="Favor a name or phrase; may be repeated")
    parser.add_argument("--word-timestamps", action="store_true", help="Include word times and probabilities in JSON")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Whisper model name, or a folder with a converted model (default: {DEFAULT_MODEL})")
    parser.add_argument("--remote", choices=tuple(REMOTE_SERVICES), help="Upload the audio to this hosted service instead of running Whisper on this computer; needs a Hugging Face token in HF_TOKEN and is billed to that account")
    parser.add_argument("--chunk-seconds", type=float, default=30, help="Longest chunk, greater than 0 and at most 30 (default: 30)")
    parser.add_argument("--dry-run", action="store_true", help="List audio and associated messages, marking what is already transcribed, without loading Whisper or writing files")
    return parser


def shell_word(path: Path) -> str:
    """Write a path so that it can be pasted into a command line of this system."""
    text = str(path)
    if os.name != "nt":
        return shlex.quote(text)
    # A Windows path holds no double quote, so a pair of them is enough.
    return text if re.fullmatch(r"[\w.:/\\+-]+", text) else f'"{text}"'


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
    viewer = args.viewer.expanduser().resolve() if args.viewer else None
    events = args.events.expanduser().resolve() if args.events else None
    reuse = [path.expanduser().resolve() for path in args.reuse]
    if output.suffix.lower() != ".json":
        parser.error("--output must have a .json extension")
    if (events or args.audio) and not viewer:
        parser.error("--events and --audio need --viewer")
    if viewer and viewer.suffix.lower() != ".html":
        parser.error("--viewer must have a .html extension")
    destinations = {output, output.with_suffix(".txt").resolve()}
    if chat_output:
        if chat_output in destinations:
            parser.error("--chat-output must differ from the report output files")
        destinations.add(chat_output)
    if viewer:
        if viewer in destinations:
            parser.error("--viewer must differ from the other output files")
        destinations.add(viewer)
    if source in destinations:
        parser.error("Output paths must differ from the input")
    # Naming the report at --output is harmless, since it is continued anyway. Any other output would be read and then replaced.
    if (destinations - {output}) & set(reuse):
        parser.error("--reuse must differ from the files this run writes")
    if events in destinations:
        parser.error("--events must differ from the files this run writes")
    if not source.exists():
        parser.error(f"Input does not exist: {source}")
    for path in reuse:
        if not path.is_file():
            parser.error(f"--reuse file does not exist: {path}")
    if events and not events.is_file():
        parser.error(f"--events file does not exist: {events}")
    again = "Run the command again without --fresh to continue." if args.fresh else "Run the same command again to continue."
    reached = {}

    def view() -> bool:
        """Write the page from the report as it stands, whether the run finished or not."""
        try:
            conversation = stats.write_conversation(source, viewer, transcripts=[output], events=events, audio=args.audio or "auto",
                                                    excluded=destinations)
        except FAILURES as error:
            print(f"Error: {error}", file=sys.stderr)
            return False
        for line in stats.viewer_lines(conversation, command=False):
            print(line)
        return True

    try:
        if viewer:
            # Like --chat-output, the page needs one chat, and a run that cannot end with it should not start.
            with open_export(source, destinations) as export:
                try:
                    stats.choose_chat(export, source)
                except ValueError:
                    raise ValueError("--viewer requires exactly one readable UTF-8 chat .txt; use a chat .txt as the input to select it.") from None
        summary = transcribe.transcribe_export(
            source, output, chat_output, model=args.model, language=args.language, keywords=args.keyword,
            word_timestamps=args.word_timestamps, chunk_seconds=args.chunk_seconds, dry_run=args.dry_run,
            remote=args.remote, reuse=reuse, fresh=args.fresh,
            progress=lambda done, total: reached.update(done=done, total=total),
        )
    except KeyboardInterrupt:
        # Before the first recording there is nothing to count; cli() reports that interruption.
        if not reached:
            raise
        print(file=sys.stderr)
        if viewer:
            view()
        print(f"Stopped after {reached['done']} of {reached['total']}. {again}", file=sys.stderr)
        return 130
    except FAILURES as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    if summary is None:
        return 0
    saved = len(summary["report"]["results"])
    print(f"Saved {'1 transcript' if saved == 1 else f'{saved} transcripts'} ({summary['reused']} reused, {summary['failures']} failed) "
          f"to {output} and {output.with_suffix('.txt')}")
    if chat_output:
        print(f"Annotated chat: {chat_output}")
    viewed = view() if viewer else True
    # voxpad-stats reads one conversation; a lone recording has none.
    if not viewer and summary["chats"] == 1:
        print(f"View it: voxpad-stats {shell_word(args.source)} --transcripts {shell_word(args.output)}")
    if summary["stopped"]:
        print(f"Stopped after {summary['done']} of {summary['total']}. {again}", file=sys.stderr)
    return 1 if summary["failures"] or not viewed else 0


def cli() -> None:
    """Command-line entry point with a consistent interruption exit status."""
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Any completed transcripts have been saved.", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    cli()
