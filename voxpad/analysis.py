"""Read a chat as a conversation model and count what each person wrote and said.

The browser app applies the same rules in JavaScript and must reach the same numbers, so every rule
is spelled out here instead of being left to str.split(), int(), \\d or a platform date.
"""

from datetime import date, timedelta
import math
import re
import unicodedata

from .chat import CLOCK, DATE, NEWLINE, PERIOD, parse_chat
from .export import AUDIO_EXTENSIONS


DATE_ORDERS = ("DMY", "MDY", "YMD")
BUCKETS = ("day", "week", "month")
METRICS = ("messages", "words_typed", "words_spoken", "voice_notes", "voice_seconds", "voice_transcribed", "voice_timed")

# The zero of every set of decimal digits a timestamp may be written in. A digit is worth its distance from its zero.
DIGIT_ZEROS = (
    0x30, 0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66, 0xDE6, 0xE50, 0xED0, 0xF20, 0x1040, 0x1090,
    0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90, 0x1B50, 0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10,
)
# What separates words, listed by code point so that no invisible character has to survive an editor.
WHITESPACE = "".join(map(chr, (
    *range(0x09, 0x0E), *range(0x1C, 0x21), 0x85, 0xA0, 0x1680, *range(0x2000, 0x200B), 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
)))
WORD = re.compile("[^" + re.escape(WHITESPACE) + "]+")

# What WhatsApp writes in place of a message, lower-cased. tests/fixtures/analysis_cases.json holds the same lists for the browser app.
DELETED = (
    "this message was deleted", "you deleted this message", "se eliminó este mensaje", "eliminaste este mensaje",
    "diese nachricht wurde gelöscht", "du hast diese nachricht gelöscht", "mensagem apagada", "esta mensagem foi apagada",
    "você apagou esta mensagem", "apagou esta mensagem", "ce message a été supprimé", "vous avez supprimé ce message",
    "questo messaggio è stato eliminato", "hai eliminato questo messaggio",
)
EDITED = (
    "this message was edited", "se editó este mensaje", "diese nachricht wurde bearbeitet", "mensagem editada",
    "ce message a été modifié", "questo messaggio è stato modificato",
)
KNOWN_OMITTED = ("<media omitted>", "<multimedia omitido>")
OMITTED_NOUNS = (
    "image", "imagen", "immagine", "bild", "audio", "áudio", "video", "vídeo", "vidéo", "sticker", "gif", "document", "documento",
    "dokument", "contact", "tarjeta",
)
OMITTED_SUFFIXES = ("omitted", "omitido", "omitida", "weggelassen", "omis", "omise", "omesso", "omessa")
AUDIO_NOUNS = ("audio", "áudio")
PHRASES = {"deleted": DELETED, "edited": EDITED, "known_omitted": KNOWN_OMITTED, "omitted_nouns": OMITTED_NOUNS,
           "omitted_suffixes": OMITTED_SUFFIXES}

TRANSCRIPT_PREFIX = "[Voice message transcript: "
ATTACHMENT_EXTENSIONS = frozenset(extension[1:] for extension in AUDIO_EXTENSIONS) | frozenset(
    "jpg jpeg png heic gif webp mp4 mov 3gp mkv avi vcf pdf doc docx xls xlsx ppt pptx txt csv zip rar apk".split(" "))
# Names WhatsApp gives its files: IMG-20240304-WA0001.jpg on Android, 00000012-AUDIO-2024-03-04-10-00-00.opus on iOS.
GENERATED = re.compile(r"[A-Z]{3}-[0-9]{8}-WA[0-9]{4}\.[0-9A-Za-z]{1,5}|[0-9]{8}-[A-Z]+-[0-9]{4}(?:-[0-9]{2}){5}\.[0-9A-Za-z]{1,5}")
ANDROID_ATTACHMENT = re.compile(r"([^\n]+) \(([^()\n]{1,40})\)")
IOS_ATTACHMENT = re.compile(r"<([^<>:：\n]*)[:：]([^<>\n]*)>")
NAME_DATE = re.compile(r"(?<![0-9])(20(?:09|[1-9][0-9]))(-?)(0[1-9]|1[0-2])\2(0[1-9]|[12][0-9]|3[01])(?![0-9])")
# \w is a letter, a digit or "_" of any script here; the browser app writes the same class as [\p{L}\p{N}_.-].
JOINED = re.compile(r"[\w.-]+")
AUDIO_ENDINGS = tuple(sorted(extension.upper() for extension in AUDIO_EXTENSIONS))



def _grouped(pattern: str) -> str:
    """One of HEADER's patterns with a group around each of its numbers."""
    return re.sub(r"(\\d\{[0-9,]+\})", r"(\1)", pattern)


# HEADER's own date and clock, so that whatever parse_chat took for a timestamp is read field by
# field and nothing is split by hand. Groups: the three date fields, a marker, hour, minute, second, a marker.
STAMP = re.compile(rf"{_grouped(DATE)}[,،]?\s*(?:({PERIOD})\s*)?{_grouped(CLOCK)}(?:\s*({PERIOD}))?")
PM_MARKERS = ("م", "下午", "午後")
MONTH_DAYS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
MODEL_DAY = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
EVENT_DATE = re.compile(r"([0-9]{1,4})[-/.]([0-9]{1,2})[-/.]([0-9]{1,4})")


def count_words(text: str) -> int:
    """Count the words of a text: its longest runs of characters that are not whitespace."""
    return len(WORD.findall(text)) if isinstance(text, str) else 0


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _number(digits: str) -> int | None:
    """The value of a run of digits of any set, or None when one of them is in no set."""
    value = 0
    for character in digits:
        code = ord(character)
        for zero in DIGIT_ZEROS:
            if zero <= code <= zero + 9:
                value = value * 10 + code - zero
                break
        else:
            return None
    return value


def _month_length(year: int, month: int) -> int:
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    return 29 if month == 2 and leap else MONTH_DAYS[month - 1]


def _real_date(year: int | None, month: int | None, day: int | None) -> tuple[int, int, int] | None:
    if year is None or month is None or day is None:
        return None
    if not (1 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= _month_length(year, month)):
        return None
    return year, month, day


def _ordinal(year: int, month: int, day: int) -> int:
    """Days since the year 1 began, as datetime.date.toordinal counts them, without building a date."""
    before = year - 1
    days = before * 365 + before // 4 - before // 100 + before // 400
    for earlier in range(1, month):
        days += _month_length(year, earlier)
    return days + day


def _iso_day(day: tuple[int, int, int]) -> str:
    return "%04d-%02d-%02d" % day


def _stamp_date(match: re.Match, order: str, forced: bool = False) -> tuple[int, int, int] | None:
    """The date a timestamp gives under an order. The number of digits decides which fields can be a year."""
    first, second, third = match.group(1, 2, 3)
    if order == "YMD":
        # A short year comes first only when the user says so: on its own such a date reads day-first.
        if len(first) != 4 and not (forced and len(first) <= 2):
            return None
        year = _number(first)
        if year is not None and len(first) <= 2:
            year += 2000
        return _real_date(year, _number(second), _number(third))
    if len(first) > 2 or len(third) not in (2, 4):
        return None
    year = _number(third)
    if year is not None and len(third) == 2:
        year += 2000
    if order == "DMY":
        return _real_date(year, _number(second), _number(first))
    return _real_date(year, _number(first), _number(second))


def _stamp_clock(match: re.Match) -> tuple[int, int, int] | None:
    hour, minute = _number(match[5]), _number(match[6])
    second = _number(match[7]) if match[7] else 0
    if hour is None or minute is None or second is None:
        return None
    # Of a marker on each side of the clock, the one after it counts.
    marker = match[8] or match[4]
    if marker:
        hour = hour % 12 + (12 if marker[0] in "pP" or marker in PM_MARKERS else 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    return hour, minute, second


def _check_order(order: str) -> None:
    if order not in DATE_ORDERS:
        raise ValueError(f"Unknown date order: {order}")


def parse_timestamp(raw: str, order: str, *, forced: bool = False) -> str | None:
    """Turn an exported timestamp into YYYY-MM-DDTHH:MM:SS, or None when it is no real date and time.

    `order` is DMY, MDY or YMD. `forced` says the user chose it, which lets YMD read a one- or
    two-digit first field as a year of this century.
    """
    _check_order(order)
    match = STAMP.fullmatch(raw)
    if not match:
        return None
    day, clock = _stamp_date(match, order, forced), _stamp_clock(match)
    if day is None or clock is None:
        return None
    return "%04d-%02d-%02dT%02d:%02d:%02d" % (day + clock)


def infer_date_order(timestamps, hints=None) -> tuple[str, bool]:
    """Decide once for a chat how its dates are written: (order, ambiguous).

    `hints[i]` is the date (YYYY-MM-DD) named by an attachment of message i, or None. The order that
    leaves the fewest timestamps without a date wins; then the one more hints agree with, the one in
    which fewer messages go back in time, day-first for dotted and dashed dates, and the shorter chat.
    """
    if not timestamps:
        return "DMY", False
    matches = [STAMP.fullmatch(raw) for raw in timestamps]
    # Month-first dates are written with slashes.
    dotted = any(match and (raw[match.end(1)] in ".-" or raw[match.end(2)] in ".-") for raw, match in zip(timestamps, matches))
    keys, readings = {}, {}
    for rank, order in enumerate(DATE_ORDERS):
        days = [_stamp_date(match, order) if match else None for match in matches]
        invalid = agree = back = 0
        previous = earliest = latest = None
        for index, day in enumerate(days):
            if day is None:
                invalid += 1
                continue
            if hints and index < len(hints) and hints[index] is not None and hints[index] == _iso_day(day):
                agree += 1
            number = _ordinal(*day)
            if previous is None:
                earliest = latest = number
            else:
                back += number < previous
                earliest, latest = min(earliest, number), max(latest, number)
            previous = number
        span = 0 if previous is None else latest - earliest
        keys[order] = (invalid, -agree, back, int(order == "MDY" and dotted), span, rank)
        readings[order] = days
    best = min(DATE_ORDERS, key=keys.get)
    ambiguous = any(order != best and keys[order][:4] == keys[best][:4] and readings[order] != readings[best] for order in DATE_ORDERS)
    return best, ambiguous


def is_attachment_name(name: str) -> bool:
    """Whether a name is one WhatsApp gives its files, or ends in an extension attachments have."""
    if GENERATED.fullmatch(name):
        return True
    _, dot, extension = name.rpartition(".")
    return bool(dot) and extension.lower() in ATTACHMENT_EXTENSIONS


def media_type(name: str) -> str:
    """What an attachment is, from its name: audio, gif, sticker, image, video, contact or document."""
    upper = name.upper()
    if upper.endswith(AUDIO_ENDINGS) or upper.startswith(("PTT-", "AUD-")) or "-AUDIO-" in upper:
        return "audio"
    if "GIF" in upper:
        return "gif"
    if upper.endswith(".WEBP") or upper.startswith("STK-") or "-STICKER-" in upper:
        return "sticker"
    if upper.endswith((".JPG", ".JPEG", ".PNG", ".HEIC")) or upper.startswith("IMG-") or "-PHOTO-" in upper:
        return "image"
    if upper.endswith((".MP4", ".MOV", ".3GP", ".MKV", ".AVI")) or upper.startswith("VID-") or "-VIDEO-" in upper:
        return "video"
    if upper.endswith(".VCF"):
        return "contact"
    return "document"


def filename_date(name: str) -> str | None:
    """The first date an attachment name carries, as YYYY-MM-DD: 20240304 or 2024-03-04, from 2009 to 2099, with no digit beside it."""
    for match in NAME_DATE.finditer(name):
        if _number(match[4]) <= _month_length(_number(match[1]), _number(match[3])):
            return f"{match[1]}-{match[3]}-{match[4]}"
    return None


def _split_transcripts(text: str) -> tuple[str, list[str]]:
    """Take the transcript lines VoxPad inserted out of a message: (the rest, the transcripts).

    They are lines of their own below the message, so one quoted on the message's first line is typed text.
    """
    first, *lines = text.split("\n")
    kept, transcripts, current = [first], [], None
    for line in lines:
        if current is not None:
            current.append(line)
        elif line.startswith(TRANSCRIPT_PREFIX):
            current = [line]
        else:
            kept.append(line)
        # A transcript with line breaks in it runs on to the line that closes the bracket.
        if current is not None and line.endswith("]"):
            transcripts.append("\n".join(current)[len(TRANSCRIPT_PREFIX):-1])
            current = None
    if current is not None:
        # Never closed, so it was typed by a person.
        kept.extend(current)
    return "\n".join(kept), transcripts


def _split_edited(text: str) -> tuple[str, bool]:
    """Take a closing <This message was edited> out of a text: (the rest, whether it was there)."""
    start = text.rfind("<")
    if start < 0 or not text.endswith(">"):
        return text, False
    phrase = text[start + 1:-1].lower()
    if phrase not in EDITED and not (phrase.endswith(".") and phrase[:-1] in EDITED):
        return text, False
    return text[:start], True


def _results(results) -> list[dict]:
    return [result for result in results or () if isinstance(result, dict) and isinstance(result.get("file"), str)]


def _basename(result: dict) -> str:
    return _nfc(result["file"]).rsplit("/", 1)[-1]


def _recording_names(results: list[dict]):
    """Prepare the search for the recordings the results name: (names that are one run of joined characters, a pattern for the rest)."""
    names = {_basename(result) for result in results} - {""}
    runs = {name for name in names if JOINED.fullmatch(name)}
    # Longest first, as index_messages looks for them: a name may be the start of another.
    others = sorted(names - runs, key=lambda name: (-len(name), name))
    pattern = re.compile(r"(?<![\w.-])(?:" + "|".join(map(re.escape, others)) + r")(?![\w.-])") if others else None
    return runs, pattern


def _recordings(text: str, names) -> list[tuple[int, int, str]]:
    """Known recordings named in a text as whole tokens: not next to a letter, a digit, "_", "." or "-"."""
    runs, pattern = names
    # Nearly every name is one run of such characters, and then a run of the text either is the name or is not.
    found = [(match.start(), match.end(), match[0]) for match in JOINED.finditer(text) if match[0] in runs] if runs else []
    if not pattern:
        return found
    found += [(match.start(), match.end(), match[0]) for match in pattern.finditer(text)]
    kept: list[tuple[int, int, str]] = []
    for token in sorted(found, key=lambda token: (token[0], -token[1])):
        if not kept or token[0] >= kept[-1][1]:
            kept.append(token)
    return kept


def _attachments(text: str, names) -> list[tuple[int, int, str]]:
    """Attachment tokens of a text as (start, end, filename), in the order of the text."""
    found = []
    first = text.split("\n", 1)[0]
    android = ANDROID_ATTACHMENT.fullmatch(first)
    if android and is_attachment_name(android[1]):
        found.append((0, len(first), android[1]))
    candidates = []
    for match in IOS_ATTACHMENT.finditer(text):
        label, name = match[1].rstrip(WHITESPACE), match[2].lstrip(WHITESPACE)
        if 1 <= len(label) <= 40 and is_attachment_name(name):
            candidates.append((match.start(), match.end(), name))
    # A name inside an attachment line or bracket is that attachment, not another one.
    for token in candidates + _recordings(text, names):
        if all(token[1] <= other[0] or token[0] >= other[1] for other in found):
            found.append(token)
    return sorted(found)


def _classify(text: str, whole: str, names, repeated: dict) -> dict:
    """What a message is once transcripts and the edited mark are out: kind, typed text, media, recording and date hint."""
    low = whole.lower()
    if low in DELETED or (low.endswith(".") and low[:-1] in DELETED):
        return {"kind": "deleted", "text": ""}
    tokens = _attachments(text, names)
    if tokens:
        files = [_nfc(name) for _, _, name in tokens]
        audio = [media_type(name) == "audio" for name in files]
        voice = audio.index(True) if True in audio else None
        # A message holds one voice note; the names of further recordings stay in its text.
        rest, position = [], 0
        for index, (start, end, _) in enumerate(tokens):
            if index == voice or not audio[index]:
                rest.append(text[position:start])
                position = end
        rest.append(text[position:])
        reading = {"kind": "media", "text": "".join(rest).strip(WHITESPACE), "hint": next(filter(None, map(filename_date, files)), None)}
        if voice is None:
            reading["media"] = {"type": media_type(files[0]), "file": files[0]}
        else:
            reading.update(kind="voice", file=files[voice])
        return reading
    words = WORD.findall(whole)
    if len(words) > 4:
        return {"kind": "text", "text": whole}
    # What an export without its media leaves of an attachment: <Media omitted>, image omitted, null.
    words = [word.lower() for word in words]
    bracketed = (len(whole) > 2 and whole[0] == "<" and whole[-1] == ">" and "<" not in whole[1:-1] and ">" not in whole[1:-1]
                 and (low in KNOWN_OMITTED or repeated[whole] > 1))
    bare = len(words) in (2, 3) and words[0] in OMITTED_NOUNS and words[-1] in OMITTED_SUFFIXES
    if bare and words[0] in AUDIO_NOUNS:
        return {"kind": "voice", "text": "", "file": None}
    if bracketed or bare or whole == "null":
        return {"kind": "media", "text": "", "media": {"type": "omitted", "file": None}}
    return {"kind": "text", "text": whole}


def _read(messages, results: list[dict]) -> list[dict | None]:
    """Read every message that has a sender; None stands for a system message."""
    names = _recording_names(results)
    drafts, repeated = [], {}
    for message in messages:
        if message.sender is None:
            drafts.append(None)
            continue
        text, transcripts = _split_transcripts(message.text)
        text, edited = _split_edited(text)
        whole = text.strip(WHITESPACE)
        # A bracketed placeholder in a language not listed here still repeats word for word.
        repeated[whole] = repeated.get(whole, 0) + 1
        drafts.append((text, whole, transcripts, edited))
    readings = []
    for draft in drafts:
        if draft is None:
            readings.append(None)
            continue
        text, whole, transcripts, edited = draft
        readings.append({**_classify(text, whole, names, repeated), "transcripts": transcripts, "edited": edited})
    return readings


def date_hints(messages, results=None) -> list[str | None]:
    """For each message of parse_chat, the date its first dated attachment name carries (YYYY-MM-DD), or None."""
    return [reading.get("hint") if reading else None for reading in _read(messages, _results(results))]


def _seconds(value) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _latest_results(results: list[dict]) -> dict[str, list[dict]]:
    """One result per file, the last that worked or else the last that failed, listed under the name of the file."""
    latest: dict[str, dict] = {}
    for result in results:
        if result.get("status") not in ("ok", "error"):
            continue
        file = _nfc(result["file"])
        if file not in latest or result["status"] == "ok" or latest[file]["status"] == "error":
            latest[file] = result
    by_name: dict[str, list[dict]] = {}
    for result in latest.values():
        by_name.setdefault(_basename(result), []).append(result)
    return by_name


def _transcript(line: str) -> dict:
    """What a transcript line of an annotated chat says, in the shape of a result."""
    if line == "No speech detected":
        return {"status": "ok", "text": ""}
    if line.startswith("Error: "):
        return {"status": "error", "text": "", "error": line[len("Error: "):]}
    return {"status": "ok", "text": line}


def _voice(name: str, message, transcripts: list[str], by_name: dict, durations: dict, audio_src) -> tuple[dict, bool]:
    """The voice item of a message that names a recording, and whether its transcript was found by file name alone."""
    candidates = by_name.get(name, ())
    if len(candidates) > 1:
        # The same name in two folders of an export: the report says which message each belongs to.
        candidates = [result for result in candidates if any(
            isinstance(context, dict) and context.get("timestamp") == message.timestamp and context.get("sender") == message.sender
            for context in (result.get("messages") if isinstance(result.get("messages"), list) else ()))]
        # Reports of one export laid out differently, a ZIP and its folder, both name the recording:
        # a transcript is preferred to a failure, then the later report.
        candidates = ([result for result in candidates if result["status"] == "ok"] or candidates)[-1:]
    result = candidates[0] if len(candidates) == 1 else None
    line = _transcript(transcripts[0]) if transcripts else None
    by_name_only = False
    if result is not None and result["status"] == "error" and line and line["status"] == "ok":
        # A failure in a report does not take away the transcript the chat itself carries.
        result = line
    elif result is not None:
        contexts = result.get("messages")
        by_name_only = isinstance(contexts, list) and bool(contexts) and not any(
            isinstance(context, dict) and context.get("timestamp") == message.timestamp for context in contexts)
    elif line:
        result = line
    source = audio_src(name) if audio_src else None
    voice = {"file": name, "src": source if isinstance(source, str) and source else None, "seconds": None, "status": "pending", "text": "", "words": 0}
    if result is not None:
        text = result.get("text")
        text = text.strip(WHITESPACE) if isinstance(text, str) else ""
        voice.update(seconds=_seconds(result.get("duration_seconds")), text=text, words=count_words(text),
                     status="error" if result["status"] == "error" else "ok" if text else "empty")
        languages = result.get("languages")
        languages = [language for language in languages if isinstance(language, str)] if isinstance(languages, list) else []
        if languages:
            voice["languages"] = languages
        if voice["status"] == "error":
            voice["error"] = result["error"] if isinstance(result.get("error"), str) else ""
    if voice["seconds"] is None:
        voice["seconds"] = _seconds(durations.get(name))
    return voice, by_name_only


def _counted(count: int, one: str, many: str) -> str:
    return one if count == 1 else many.format(count)


def build_model(chat_text: str, *, title: str = "", results=None, events=None, date_order: str | None = None,
                audio_src=None, durations=None) -> dict:
    """Build the conversation model of a chat; `messages[i]` describes `parse_chat(chat_text)[i]`.

    `results` are the entries of transcript reports, `events` a list as parse_events returns it,
    `date_order` DMY, MDY or YMD when the user chose it, `audio_src(filename)` gives what the viewer
    can play for a recording (or None), and `durations` maps recording names to seconds for
    recordings whose result has none.
    """
    if date_order is not None:
        _check_order(date_order)
    messages = parse_chat(chat_text)
    results = _results(results)
    readings = _read(messages, results)
    if date_order is None:
        order, ambiguous = infer_date_order([message.timestamp for message in messages],
                                            [reading.get("hint") if reading else None for reading in readings])
    else:
        order, ambiguous = date_order, False
    by_name = _latest_results(results)
    durations = {_nfc(name): seconds for name, seconds in (durations or {}).items() if isinstance(name, str)}
    sent: dict[str, int] = {}
    for message in messages:
        if message.sender is not None:
            sent[message.sender] = sent.get(message.sender, 0) + 1
    # Python compares strings by code point, which is the order the browser app has to rebuild.
    participants = sorted(sent, key=lambda name: (-sent[name], name))
    places = {name: place for place, name in enumerate(participants)}
    entries, unused, by_name_only = [], 0, 0
    for message, reading in zip(messages, readings):
        entry = {"time": parse_timestamp(message.timestamp, order, forced=date_order is not None),
                 "sender": None, "kind": "system", "text": message.text, "words": 0}
        entries.append(entry)
        if reading is None:
            continue
        entry.update(sender=places[message.sender], kind=reading["kind"], text=reading["text"], words=count_words(reading["text"]))
        if reading["edited"]:
            entry["edited"] = True
        transcripts = reading["transcripts"]
        if reading["kind"] == "media":
            entry["media"] = reading["media"]
        elif reading["kind"] == "voice" and reading["file"] is None:
            entry["voice"] = {"file": None, "src": None, "seconds": None, "status": "missing", "text": "", "words": 0}
        elif reading["kind"] == "voice":
            entry["voice"], name_only = _voice(reading["file"], message, transcripts, by_name, durations, audio_src)
            by_name_only += name_only
            # The first transcript line is this recording's, whether or not a report replaced it.
            transcripts = transcripts[1:]
        unused += len(transcripts)
    warnings = []
    if unused:
        warnings.append(_counted(unused, "1 transcript line in the chat belongs to no voice message and was left out.",
                                 "{} transcript lines in the chat belong to no voice message and were left out."))
    if by_name_only:
        warnings.append(_counted(by_name_only, "1 transcript was matched", "{} transcripts were matched")
                        + " by file name only; the report seems to come from a different export of this chat.")
    return {
        "schema": 1,
        "title": title or " · ".join(participants[:3]) + (f" +{len(participants) - 3}" if len(participants) > 3 else ""),
        "date_order": order, "date_order_ambiguous": ambiguous,
        "participants": participants,
        "messages": entries,
        "events": [{"date": event["date"], "label": event["label"]} for event in events or ()],
        "warnings": warnings,
    }


def _event_date(first: str, second: str, third: str) -> str | None:
    """Year first when the first field has four digits, otherwise day first, and month first when only that is a date."""
    if len(first) == 4:
        day = _real_date(_number(first), _number(second), _number(third))
    else:
        year = _number(third) + (2000 if len(third) <= 2 else 0)
        day = _real_date(year, _number(second), _number(first)) or _real_date(year, _number(first), _number(second))
    return _iso_day(day) if day else None


def parse_events(text: str) -> tuple[list[dict], list[str]]:
    """Read an events file, one `date label` per line: (events sorted by date, warnings).

    Blank lines and lines starting with # are skipped. A warning names its line by number only: the
    line itself may be private.
    """
    events, warnings = [], []
    for number, raw in enumerate(NEWLINE.split(text), 1):
        line = raw.strip(WHITESPACE)
        if not line or line.startswith("#"):
            continue
        match = EVENT_DATE.match(line)
        day = _event_date(*match.groups()) if match else None
        if day is None:
            warnings.append(f"Events line {number} has no valid date and was skipped.")
            continue
        label = line[match.end():].lstrip(" ,|\t").strip(WHITESPACE)
        events.append({"date": day, "label": label or day})
    events.sort(key=lambda event: event["date"])
    return events, warnings


def _message_days(messages: list[dict]) -> list[date | None]:
    """The day of each message. One without a time takes the day of the message before it, or else of the first message that has one."""
    days, known, read = [], None, {}
    for message in messages:
        time = message.get("time")
        key = time[:10] if isinstance(time, str) else None
        if key is not None:
            if key not in read:
                match = MODEL_DAY.fullmatch(key)
                parts = _real_date(*map(_number, match.groups())) if match else None
                read[key] = date(*parts) if parts else None
            known = read[key] or known
        days.append(known)
    first = next((day for day in days if day is not None), None)
    return [day or first for day in days]


def _bucket_start(day: date, bucket: str) -> date:
    """The first day of a day's bucket: the day, the Monday on or before it, or the first of its month."""
    if bucket == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1) if bucket == "month" else day


def _rows(model: dict, length: int) -> list[dict]:
    return [{metric: [0] * length for metric in METRICS} for _ in model["participants"]]


def _tally(row: dict, place: int, message: dict) -> None:
    """Add one message to a participant's metrics. Buckets and totals both count here, so their sums agree."""
    row["messages"][place] += 1
    row["words_typed"][place] += message["words"]
    voice = message.get("voice") if message["kind"] == "voice" else None
    if not voice:
        return
    row["voice_notes"][place] += 1
    row["words_spoken"][place] += voice["words"]
    if voice["status"] in ("ok", "empty"):
        row["voice_transcribed"][place] += 1
    if voice["seconds"] is not None:
        row["voice_timed"][place] += 1
        # Added one by one in message order, as the browser app does: sum() rounds differently.
        row["voice_seconds"][place] += voice["seconds"]


def aggregate(model: dict, bucket: str = "day") -> dict:
    """Count a model per participant and per day, week or month.

    Returns {"bucket", "buckets", "series"}: the buckets are every day, Monday or first of a month
    from the earliest message to the latest, as ISO dates, and `series[i]` holds for participant i
    a list per metric with one number per bucket.
    """
    if bucket not in BUCKETS:
        raise ValueError(f"Unknown bucket: {bucket}")
    days = _message_days(model["messages"])
    if not days or days[0] is None:
        return {"bucket": bucket, "buckets": [], "series": _rows(model, 0)}
    start, end = _bucket_start(min(days), bucket), _bucket_start(max(days), bucket)
    places = {}
    while True:
        places[start] = len(places)
        if start >= end:
            break
        if bucket == "month":
            start = date(start.year + start.month // 12, start.month % 12 + 1, 1)
        else:
            start += timedelta(days=7 if bucket == "week" else 1)
    series = _rows(model, len(places))
    for message, day in zip(model["messages"], days):
        if message["kind"] != "system" and message["sender"] is not None:
            _tally(series[message["sender"]], places[_bucket_start(day, bucket)], message)
    return {"bucket": bucket, "buckets": [start.isoformat() for start in places], "series": series}


def totals(model: dict) -> list[dict]:
    """The metrics of each participant as single numbers, over every message with a sender, dated or not."""
    rows = _rows(model, 1)
    for message in model["messages"]:
        if message["kind"] != "system" and message["sender"] is not None:
            _tally(rows[message["sender"]], 0, message)
    return [{metric: row[metric][0] for metric in METRICS} for row in rows]
