"""Transcribe a recording chunk by chunk, and every recording of an export into the reports."""

from collections import Counter
from collections.abc import Sequence
import os
from pathlib import Path
import sys
import unicodedata

from .chat import NEWLINE, parse_chat
from .engines import (DEFAULT_MODEL, REMOTE_SERVICES, SAMPLE_RATE, RemoteRefused, RemoteWhisper, Whisper,
                      remote_conflict, remote_model)
from .export import AUDIO_EXTENSIONS, Export, index_messages, open_export
from .reports import PreviousReport, load_previous_results, without_transcripts, write_annotated_chat, write_reports


# A chunk ends at the quietest tenth of a second within its last five seconds.
BOUNDARY_SEARCH_SECONDS = 5
BOUNDARY_WINDOW_SECONDS = 0.1


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


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def previous_candidates(files: Sequence[str], report: PreviousReport) -> list[dict | None]:
    """Find, for each recording of an export, the result of an earlier report that describes it.

    `files` are the recordings' paths inside the export, with forward slashes. A
    result describes the recording whose path it names; failing that, the one
    whose file name it shares, when that name stands for one recording of the
    export and one result of the report. Sender names, chat file names and
    timestamps take no part: two exports of one chat differ in them.
    """
    by_file: dict[str, dict] = {}
    by_name: dict[str, list[dict]] = {}
    for result in report.results:
        file = _nfc(result["file"])
        # Of results naming one file a transcript is preferred, then the later one.
        if result["status"] == "ok" or by_file.get(file, result)["status"] != "ok":
            by_file[file] = result
        by_name.setdefault(file.rsplit("/", 1)[-1], []).append(result)
    files = [_nfc(file) for file in files]
    names = Counter(file.rsplit("/", 1)[-1] for file in files)
    candidates = []
    for file in files:
        name = file.rsplit("/", 1)[-1]
        candidate = by_file.get(file)
        if candidate is None and names[name] == 1 and len(by_name.get(name, ())) == 1:
            candidate = by_name[name][0]
        candidates.append(candidate)
    return candidates


def reusable(candidate: dict | None, size: int | None, word_timestamps: bool = False) -> bool:
    """Whether an earlier result still stands for a recording of `size` bytes."""
    return (candidate is not None and candidate["status"] == "ok" and candidate.get("bytes", size) == size
            and (not word_timestamps or "words" in candidate))


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _result(file: str, size: int | None, messages: list[dict]) -> dict:
    """Start a report entry. The size lets a later run notice that the recording changed."""
    result = {"file": file}
    if size is not None:
        result["bytes"] = size
    result.update(messages=messages, status="ok", text="")
    return result


def _counted(count: int, one: str, many: str) -> str:
    return one if count == 1 else many.format(count)


def _listed(names: list[str]) -> str:
    """Join names as a sentence lists them: A, B and C."""
    return " and ".join(filter(None, [", ".join(names[:-1]), names[-1]]))


def _annotated_copy(path: Path, export: Export, destinations: set[Path]) -> bool:
    """Whether a chat standing where an output goes is the annotated copy an earlier run left there.

    It is when taking its transcripts out leaves the exact text of another chat
    of the export, so writing it again loses nothing.
    """
    try:
        plain = without_transcripts(path.read_bytes().decode("utf-8"))
    except UnicodeError:
        return False
    for chat in export.chats:
        if chat.resolve() in destinations:
            continue
        try:
            text = chat.read_bytes().decode("utf-8")
        except UnicodeError:
            continue
        accepted = [text]
        if text and not text.endswith(("\n", "\r")):
            # A transcript after a last line without an ending was put on a line of its own.
            ending = NEWLINE.search(text)
            accepted.append(text + (ending[0] if ending else "\n"))
        if plain in accepted and parse_chat(text):
            return True
    return False


def transcribe_export(source: Path, output: Path, chat_output: Path | None = None, *,
                      model: str = DEFAULT_MODEL, language: str | None = None, keywords: list[str] | None = None,
                      word_timestamps: bool = False, chunk_seconds: float = 30, dry_run: bool = False,
                      remote: str | None = None, remote_token: str | None = None, chat_output_optional: bool = False,
                      reuse: Sequence[Path] = (), fresh: bool = False,
                      notify=None, progress=None, should_stop=None) -> dict | None:
    """Transcribe every recording of an export and write the reports.

    A recording that an earlier report holds a transcript for is not transcribed
    again. Those reports are the ones named in `reuse` and, unless `fresh`, the
    one already standing at `output`, which is how a stopped run continues.
    `notify(text)` receives status lines, `progress(done, total)` is called before
    each recording and once at the end, counting reused recordings as done, and
    `should_stop()` is asked before each recording. With `chat_output_optional`,
    an export without exactly one chat skips the annotated chat instead of
    failing. `remote` names a hosted service that receives the audio instead of
    Whisper running here, and `remote_token` is the access token for it when the
    saved one is not to be used. Returns a summary, whose `reused` counts the
    transcripts taken from earlier reports, whose `done` adds the recordings
    this run transcribed, whose `chats` counts the chats read and whose
    `refusal` says why a hosted service ended the run early, or None for a dry
    run.
    """
    notify = notify or (lambda text: print(text, file=sys.stderr))
    conflict = remote_conflict(remote, model=model, language=language, keywords=keywords, word_timestamps=word_timestamps)
    if conflict:
        raise ValueError(conflict)
    destinations = {output, output.with_suffix(".txt").resolve()}
    if chat_output:
        destinations.add(chat_output)
    # A report read here and written over afterwards would be lost. Only the output itself is continued.
    if any(path.resolve() in destinations - {output} for path in reuse):
        raise ValueError("--reuse must differ from the files this run writes")
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
                # The same command run again finds the annotated chat that its first run wrote.
                is_original = is_original and not _annotated_copy(path, export, destinations)
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
        total = len(export.audio)
        files = [path.relative_to(export.root).as_posix() for path in export.audio]
        sizes = {path: _size(path) for path in export.audio}
        # The engine's own name for the model, known without loading it.
        if remote:
            model_name = f"{remote_model(model)} on {REMOTE_SERVICES[remote]}"
        else:
            model_name = f"Whisper {Path(model).name if Path(model).is_dir() else model}"

        # Earlier reports, the least preferred first. The report standing at the output comes last.
        given = {path.resolve(): path for path in reuse}
        if not fresh:
            given.pop(output.resolve(), None)
        sources = list(given)
        earlier = load_previous_results(given.values())
        continued = None
        if not fresh and output.is_file():
            try:
                continued = load_previous_results([output])[0]
            except ValueError as error:
                if not dry_run:
                    notify(f"{error} It will be replaced.")
            else:
                sources.append(output.resolve())
                earlier.append(continued)
        candidates = [previous_candidates(files, report) for report in earlier]
        if continued:
            # This run rewrites the output, and must not drop from it what it cannot place.
            placed = {_nfc(candidate["file"]) for candidate in candidates[-1] if candidate}
            orphans = {_nfc(result["file"]) for result in continued.results if result["status"] == "ok"} - placed
            if orphans:
                held = _counted(len(orphans), "1 transcript of a recording that is", "{} transcripts of recordings that are")
                raise ValueError(f"{output.name} holds {held} not in this export. Choose another --output, or pass --fresh to replace it.")
        results: dict[Path, dict] = {}
        # Transcripts of the output's report that this run replaces, kept in it until it does.
        kept: dict[Path, dict] = {}
        origins: Counter = Counter()
        models: dict[int, dict[str, None]] = {}
        by_name_only = changed = without_words = 0

        def taken(index: int, position: int, size: int | None) -> tuple[dict, str | None]:
            """The entry that carries an earlier transcript, and the model that made it."""
            candidate = candidates[position][index]
            result = _result(files[index], size, contexts[export.audio[index]])
            result.update({key: candidate[key] for key in ("text", "languages", "duration_seconds", "segments", "words")
                           if key in candidate})
            # A report that took transcripts over from others says of each what made it.
            made_with = candidate.get("model") or earlier[position].model
            if made_with and made_with != model_name:
                result["model"] = made_with
            return result, made_with

        for index, path in enumerate(export.audio):
            for position in reversed(range(len(earlier))):
                candidate = candidates[position][index]
                if not reusable(candidate, sizes[path], word_timestamps):
                    continue
                results[path], made_with = taken(index, position, sizes[path])
                if made_with:
                    models.setdefault(position, {})[made_with] = None
                origins[position] += 1
                by_name_only += "bytes" not in candidate
                break
            else:
                # Say why a recording that has a transcript is transcribed again.
                found = [column[index] for column in candidates if column[index] and column[index]["status"] == "ok"]
                if any(reusable(candidate, sizes[path]) for candidate in found):
                    without_words += 1
                elif found:
                    changed += 1
                # A run that stops before reaching the recording leaves the output the transcript it had,
                # with the size it was made from, so that the next run still sees what changed.
                if continued and candidates[-1][index] and candidates[-1][index]["status"] == "ok":
                    kept[path], _ = taken(index, len(earlier) - 1, candidates[-1][index].get("bytes"))
        reused = len(results)
        if reused:
            parts = []
            names = Counter(report.name for report in earlier)
            for position, count in sorted(origins.items()):
                name = earlier[position].name
                if names[name] > 1:
                    # Reports of one name are told apart by their folders.
                    name = f"{sources[position].parent.name}/{name}"
                made_with = f" (made with {_listed(list(models[position]))})" if position in models else ""
                parts.append(f"{count} from {name}{made_with}" if len(origins) > 1 else f"from {name}{made_with}")
            line = (f"Reusing {_counted(reused, '1 transcript', '{} transcripts')}{':' if len(origins) > 1 else ''} {', '.join(parts)}; "
                    f"{total - reused} left to transcribe.")
            # Only the report at the output is set aside by that option.
            if continued and origins[len(earlier) - 1]:
                line += " --fresh transcribes everything again."
            notify(line)
        if by_name_only:
            notify(_counted(by_name_only, "1 earlier transcript was", "{} earlier transcripts were") + " matched by file name only.")
        if changed:
            notify(_counted(changed, "1 recording changed since it was transcribed; it is transcribed again.",
                            "{} recordings changed since they were transcribed; they are transcribed again."))
        if without_words:
            notify(_counted(without_words, "1 earlier transcript has no word timestamps; that recording is transcribed again.",
                            "{} earlier transcripts have no word timestamps; those recordings are transcribed again."))

        if dry_run:
            for path, file in zip(export.audio, files):
                print(file)
                for message in contexts[path]:
                    print(f"  {message['timestamp']} | {message['sender'] or '(system message)'}")
                if path in results:
                    print("  (already transcribed)")
            return None

        # Nothing has been written so far: a run that cannot start leaves earlier reports as they were.
        codecpod = engine = None
        if reused < total:
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
            else:
                notify(f"Loading Whisper {model} (the first run downloads the model)...")
                engine = Whisper(model)
        # Reports get shared: name the input without revealing where it is stored.
        report = {"source": source.name, "model": model_name, "sample_rate": SAMPLE_RATE,
                  "chunk_seconds": chunk_seconds, "results": []}

        def save() -> None:
            # Recordings keep the export's order, whichever run transcribed them.
            report["results"] = [results.get(path) or kept[path] for path in export.audio if path in results or path in kept]
            write_reports(output, report)

        def annotate() -> None:
            if chat_output:
                chat, text = next(iter(texts.items()))
                write_annotated_chat(chat_output, chat, text, occurrences, {**kept, **results})

        # From the first write on the report holds every reused transcript, however early the run ends.
        save()
        stopped = False
        refusal = None
        try:
            for number, path in enumerate(export.audio, 1):
                if path in results:
                    continue
                if should_stop and should_stop():
                    stopped = True
                    break
                filename = files[number - 1]
                if progress:
                    progress(len(results), total)
                notify(f"[{number}/{total}] {filename}")
                result = _result(filename, sizes[path], contexts[path])
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
                results[path] = result
                # Save after each recording so completed work survives interruption.
                save()
                if refusal:
                    stopped = True
                    break
        except KeyboardInterrupt:
            # Interrupted by hand: keep what is done, so that the same command continues from it.
            save()
            annotate()
            if progress:
                progress(len(results), total)
            raise
        annotate()
        if progress:
            progress(len(results), total)
        return {
            "report": report, "total": total, "stopped": stopped, "refusal": refusal,
            "failures": sum(result["status"] == "error" for result in report["results"]),
            # `done` leaves out the transcripts a stopped run kept for recordings it did not reach again.
            "reused": reused, "done": len(results), "chats": len(texts), "output": output, "chat_output": chat_output,
        }
