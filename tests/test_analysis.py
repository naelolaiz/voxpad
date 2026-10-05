"""Tests for reading a chat into the conversation model and counting it.

tests/fixtures/analysis_cases.json was written by hand from the rules and is also run by the browser
app's tests, so both programs have to reach the numbers in it.
"""

import ast
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import unicodedata
import unittest

from voxpad import analysis
from voxpad.analysis import (BUCKETS, DATE_ORDERS, METRICS, aggregate, build_model, count_words, date_hints, filename_date,
                             infer_date_order, is_attachment_name, media_type, parse_events, parse_timestamp, totals)
from voxpad.chat import parse_chat


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "analysis_cases.json").read_text(encoding="utf-8"))
VOICE_KEYS = {"file", "src", "seconds", "status", "text", "words"}


def model_of(case):
    events = None if case["events_text"] is None else parse_events(case["events_text"])[0]
    return build_model(case["chat"], results=case["results"], events=events, date_order=case["date_order"], durations=case["durations"])


def recording(file, text="hola", **more):
    return {"file": file, "messages": [], "status": "ok", "text": text, **more}


class FixtureTests(unittest.TestCase):
    maxDiff = None

    def assertCounts(self, actual, expected):
        """Compare the metrics of one participant: seconds within a millionth, everything else exactly."""
        self.assertEqual(set(actual), set(METRICS))
        for metric in METRICS:
            if metric != "voice_seconds":
                self.assertEqual(actual[metric], expected[metric], metric)
            elif isinstance(expected[metric], list):
                self.assertEqual(len(actual[metric]), len(expected[metric]), metric)
                for value, wanted in zip(actual[metric], expected[metric]):
                    self.assertAlmostEqual(value, wanted, delta=1e-6)
            else:
                self.assertAlmostEqual(actual[metric], expected[metric], delta=1e-6)

    def test_the_fixture_holds_every_section_and_names_its_cases_once(self):
        self.assertEqual(set(FIXTURE), {"phrases", "timestamps", "date_orders", "filename_dates", "words", "events", "cases"})
        names = [case["name"] for case in FIXTURE["cases"]]
        self.assertEqual(len(names), len(set(names)))
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(set(case), {"name", "chat", "results", "events_text", "date_order", "durations", "model", "aggregates", "totals"})
                self.assertEqual(set(case["aggregates"]), set(BUCKETS))

    def test_phrase_lists_are_the_ones_the_browser_app_checks(self):
        self.assertEqual(set(FIXTURE["phrases"]), set(analysis.PHRASES))
        for name, phrases in FIXTURE["phrases"].items():
            with self.subTest(name=name):
                self.assertEqual(list(analysis.PHRASES[name]), phrases)
                # They are compared with lower-cased text, composed as WhatsApp writes it.
                self.assertEqual([unicodedata.normalize("NFC", phrase.lower()) for phrase in phrases], phrases)

    def test_timestamps_become_local_times(self):
        for entry in FIXTURE["timestamps"]:
            with self.subTest(raw=ascii(entry["raw"]), order=entry["order"], forced=entry["forced"]):
                self.assertEqual(parse_timestamp(entry["raw"], entry["order"], forced=entry["forced"]), entry["time"])

    def test_date_order_is_decided_once_for_a_chat(self):
        for entry in FIXTURE["date_orders"]:
            with self.subTest(timestamps=ascii(entry["timestamps"]), hints=entry["hints"]):
                self.assertEqual(infer_date_order(entry["timestamps"], entry["hints"]), (entry["order"], entry["ambiguous"]))

    def test_attachment_names_give_their_date(self):
        for entry in FIXTURE["filename_dates"]:
            with self.subTest(name=entry["name"]):
                self.assertEqual(filename_date(entry["name"]), entry["date"])

    def test_words_are_runs_between_the_listed_whitespace(self):
        for entry in FIXTURE["words"]:
            with self.subTest(text=ascii(entry["text"])):
                self.assertEqual(count_words(entry["text"]), entry["count"])

    def test_events_files_are_read_line_by_line(self):
        for entry in FIXTURE["events"]:
            with self.subTest(text=ascii(entry["text"][:40])):
                self.assertEqual(parse_events(entry["text"]), (entry["events"], entry["warnings"]))

    def test_chats_become_the_models_written_by_hand(self):
        for case in FIXTURE["cases"]:
            with self.subTest(case=case["name"]):
                model = model_of(case)
                self.assertEqual(model, case["model"])
                # The model describes the parsed chat message by message and is plain JSON.
                self.assertEqual(len(model["messages"]), len(parse_chat(case["chat"])))
                self.assertEqual(json.loads(json.dumps(model, allow_nan=False)), model)

    def test_models_are_counted_per_day_week_and_month(self):
        for case in FIXTURE["cases"]:
            model = model_of(case)
            for bucket in BUCKETS:
                with self.subTest(case=case["name"], bucket=bucket):
                    counted, expected = aggregate(model, bucket), case["aggregates"][bucket]
                    self.assertEqual(set(counted), {"bucket", "buckets", "series"})
                    self.assertEqual((counted["bucket"], counted["buckets"]), (bucket, expected["buckets"]))
                    self.assertEqual(len(counted["series"]), len(expected["series"]))
                    for row, wanted in zip(counted["series"], expected["series"]):
                        self.assertCounts(row, wanted)
            with self.subTest(case=case["name"], bucket="none"):
                counted = totals(model)
                self.assertEqual(len(counted), len(case["totals"]))
                for row, wanted in zip(counted, case["totals"]):
                    self.assertCounts(row, wanted)
            self.assertEqual(aggregate(model), aggregate(model, "day"))

    def test_buckets_add_up_to_the_totals(self):
        for case in FIXTURE["cases"]:
            model = case["model"]
            dated = any(message["time"] for message in model["messages"])
            for bucket in BUCKETS:
                with self.subTest(case=case["name"], bucket=bucket):
                    counted = aggregate(model, bucket)
                    for row, total in zip(counted["series"], totals(model)):
                        for metric in METRICS:
                            self.assertEqual(len(row[metric]), len(counted["buckets"]))
                            if dated:
                                self.assertAlmostEqual(sum(row[metric]), total[metric], delta=1e-6)

    def test_every_model_keeps_the_keys_the_viewer_relies_on(self):
        for case in FIXTURE["cases"]:
            for model in (case["model"], model_of(case)):
                with self.subTest(case=case["name"]):
                    self.assertEqual(set(model), {"schema", "title", "date_order", "date_order_ambiguous", "participants", "messages", "events", "warnings"})
                    for message in model["messages"]:
                        self.assertEqual(set(message) - {"edited", "media", "voice"}, {"time", "sender", "kind", "text", "words"})
                        self.assertIn(message["kind"], ("text", "voice", "media", "deleted", "system"))
                        self.assertEqual(message["sender"] is None, message["kind"] == "system")
                        self.assertEqual("media" in message, message["kind"] == "media")
                        self.assertEqual("voice" in message, message["kind"] == "voice")
                        self.assertIs(message.get("edited", True), True)
                        if "media" in message:
                            self.assertEqual(set(message["media"]), {"type", "file"})
                            self.assertEqual(message["media"]["file"] is None, message["media"]["type"] == "omitted")
                        if "voice" in message:
                            voice = message["voice"]
                            self.assertEqual(set(voice) - {"languages", "error"}, VOICE_KEYS)
                            self.assertIn(voice["status"], ("ok", "empty", "error", "pending", "missing"))
                            self.assertEqual(voice["file"] is None, voice["status"] == "missing")
                            self.assertEqual("error" in voice, voice["status"] == "error")
                            self.assertTrue(voice.get("languages", ["es"]))
                            self.assertEqual(voice["words"], count_words(voice["text"]))


class RuleTests(unittest.TestCase):
    maxDiff = None

    def test_words_are_counted_as_split_counts_them_once_the_byte_order_mark_is_a_space(self):
        self.assertEqual([ord(character) for character in analysis.WHITESPACE], [
            0x9, 0xA, 0xB, 0xC, 0xD, 0x1C, 0x1D, 0x1E, 0x1F, 0x20, 0x85, 0xA0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
            0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
        ])
        different = []
        for code in range(0x110000):
            if 0xD800 <= code <= 0xDFFF:
                continue
            text = f"a{chr(code)}b {chr(code)}"
            if count_words(text) != len(text.replace("﻿", " ").split()):
                different.append(hex(code))
        self.assertEqual(different, [])
        self.assertEqual(count_words(None), 0)

    def test_the_timestamp_pattern_reads_whatever_the_chat_header_accepts(self):
        self.assertEqual(analysis.STAMP.groups, 8)
        cases = (
            ("10/4/26, 9:10 PM - José: Hola", ("10", "4", "26", None, "9", "10", None, "PM")),
            ("2026-10-4, 9:10 p. m. - José: Hola", ("2026", "10", "4", None, "9", "10", None, "p. m.")),
            ("[٤/١٠/٢٠٢٦، ٩:١٠ م] José: Hola", ("٤", "١٠", "٢٠٢٦", None, "٩", "١٠", None, "م")),
            ("[2026/10/4, 上午9:10:05] José: Hola", ("2026", "10", "4", "上午", "9", "10", "05", None)),
            ("[2026/10/4, 9:10 午後] José: Hola", ("2026", "10", "4", None, "9", "10", None, "午後")),
            ("[04.10.2026, 09：10] José： Hola", ("04", "10", "2026", None, "09", "10", None, None)),
            ("[04.10.2026 09:10:11]José: Hola", ("04", "10", "2026", None, "09", "10", "11", None)),
        )
        for line, fields in cases:
            with self.subTest(line=line):
                message, = parse_chat(line)
                self.assertEqual(analysis.STAMP.fullmatch(message.timestamp).groups(), fields)

    def test_digits_of_every_listed_set_have_their_value_and_no_others_are_read(self):
        self.assertEqual(analysis.DIGIT_ZEROS, (
            0x30, 0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66, 0xDE6, 0xE50, 0xED0, 0xF20, 0x1040,
            0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90, 0x1B50, 0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0,
            0xAA50, 0xABF0, 0xFF10,
        ))
        for zero in analysis.DIGIT_ZEROS:
            with self.subTest(zero=hex(zero)):
                digits = [chr(zero + value) for value in range(10)]
                self.assertEqual([unicodedata.decimal(digit) for digit in digits], list(range(10)))
                day, year = digits[2] + digits[9], digits[1] + digits[9] + digits[8] + digits[7]
                stamp = f"{day}/{digits[0]}{digits[3]}/{year}, {digits[1]}{digits[4]}:{digits[5]}{digits[6]}"
                self.assertEqual(parse_timestamp(stamp, "DMY"), "1987-03-29T14:56:00")
        # A digit the header accepts but the table does not hold: no value is guessed.
        self.assertIsNone(parse_timestamp("\U0001D7D0\U0001D7D7/03/1987, 14:56", "DMY"))

    def test_dates_are_real_days_of_the_calendar(self):
        day = date(1896, 1, 1)
        while day.year < 1905:
            self.assertEqual(analysis._ordinal(day.year, day.month, day.day), day.toordinal())
            day += timedelta(days=1)
        for year in (1, 1900, 2000, 2023, 2024, 2100, 9999):
            self.assertEqual(analysis._ordinal(year, 12, 31), date(year, 12, 31).toordinal())
            for month in range(1, 13):
                for number in range(0, 33):
                    try:
                        expected = date(year, month, number).isoformat() + "T10:00:00"
                    except ValueError:
                        expected = None
                    self.assertEqual(parse_timestamp(f"{number}/{month}/{year:04d}, 10:00", "DMY"), expected)

    def test_an_order_or_a_bucket_that_does_not_exist_is_refused(self):
        self.assertEqual((DATE_ORDERS, BUCKETS), (("DMY", "MDY", "YMD"), ("day", "week", "month")))
        for call in (lambda: parse_timestamp("18/01/26, 21:45", "DYM"), lambda: build_model("", date_order="dmy"),
                     lambda: aggregate(build_model(""), "year")):
            with self.assertRaises(ValueError):
                call()

    def test_a_chosen_order_is_used_as_given(self):
        chat = "3/4/24, 9:10 PM - Ana: one\n13/4/24, 9:11 PM - José: two\n"
        self.assertEqual((build_model(chat)["date_order"], [message["time"] for message in build_model(chat)["messages"]]),
                         ("DMY", ["2024-04-03T21:10:00", "2024-04-13T21:11:00"]))
        model = build_model(chat, date_order="MDY")
        self.assertEqual((model["date_order"], model["date_order_ambiguous"]), ("MDY", False))
        self.assertEqual([message["time"] for message in model["messages"]], ["2024-03-04T21:10:00", None])

    def test_title_events_and_playable_sources_come_from_the_caller(self):
        asked = []

        def audio_src(filename):
            asked.append(filename)
            return {"PTT-20260110-WA0001.opus": "./audio/PTT-20260110-WA0001.opus", "PTT-20260110-WA0002.opus": "", "PTT-20260110-WA0003.opus": 3}.get(filename)

        chat = "".join(f"10/01/26, 08:0{number} - Ana: PTT-20260110-WA000{number}.opus (file attached)\n" for number in range(1, 5))
        events = [{"date": "2026-01-10", "label": "Day one", "extra": "left out"}]
        model = build_model(chat + "10/01/26, 08:05 - Ana: audio omitted\n", title="Trip", events=events, audio_src=audio_src)
        self.assertEqual((model["title"], model["events"]), ("Trip", [{"date": "2026-01-10", "label": "Day one"}]))
        self.assertEqual(asked, [f"PTT-20260110-WA000{number}.opus" for number in range(1, 5)])
        self.assertEqual([message["voice"]["src"] for message in model["messages"]], ["./audio/PTT-20260110-WA0001.opus", None, None, None, None])
        self.assertEqual(build_model("10/01/26, 08:00 - Ana: a\n10/01/26, 08:00 - Li: b\n10/01/26, 08:00 - José: c\n")["title"], "Ana · José · Li")

    def test_recordings_are_matched_after_unicode_normalisation(self):
        composed, decomposed = (unicodedata.normalize(form, "canción.opus") for form in ("NFC", "NFD"))
        self.assertNotEqual(composed, decomposed)
        for written, reported in ((composed, decomposed), (decomposed, composed)):
            with self.subTest(written=ascii(written)):
                model = build_model(f"10/01/26, 08:00 - Ana: {written} (file attached)\n10/01/26, 08:01 - Ana: {written} (file attached)\n",
                                    results=[recording(f"media/{reported}", "la la")], durations={reported: 12.5})
                self.assertEqual(model["messages"][0]["voice"], {"file": composed, "src": None, "seconds": 12.5, "status": "ok", "text": "la la", "words": 2})
        # The same recording written both ways in two reports is one recording: the transcript that worked stays.
        results = [recording(composed, "first"), {**recording(decomposed, ""), "status": "error", "error": "failed later"}]
        self.assertEqual(build_model(f"10/01/26, 08:00 - Ana: {composed} (file attached)\n", results=results)["messages"][0]["voice"]["text"], "first")

    def test_recordings_named_by_a_report_are_found_as_whole_tokens(self):
        results = [recording("voice.opus"), recording("my voice.opus"), recording("my voice.opus (1).opus")]

        def voice_of(text):
            message = build_model(f"10/01/26, 08:00 - Ana: {text}\n", results=results)["messages"][0]
            return (message["voice"]["file"], message["text"]) if message["kind"] == "voice" else message["kind"]

        self.assertEqual(voice_of("escucha voice.opus ya"), ("voice.opus", "escucha  ya"))
        self.assertEqual(voice_of("(voice.opus)"), ("voice.opus", "()"))
        self.assertEqual(voice_of("«voice.opus»"), ("voice.opus", "«»"))
        # The longest name that fits is the recording, and one that runs into other text is none.
        self.assertEqual(voice_of("es my voice.opus, sí"), ("my voice.opus", "es , sí"))
        self.assertEqual(voice_of("es my voice.opus (1).opus"), ("my voice.opus (1).opus", "es"))
        self.assertEqual(voice_of("es my voice.opus (1).opusx"), ("my voice.opus", "es  (1).opusx"))
        for neighbour in ("a", "é", "中", "7", "٧", "_", ".", "-"):
            with self.subTest(neighbour=neighbour):
                self.assertEqual(voice_of(f"escucha {neighbour}voice.opus ya"), "text")
                self.assertEqual(voice_of(f"escucha voice.opus{neighbour} ya"), "text")
        # \w stands for letters, digits and the underscore: the class the browser app spells out.
        for code in range(0x10000):
            character = chr(code)
            listed = character == "_" or unicodedata.category(character)[0] in "LN"
            if bool(analysis.JOINED.fullmatch(character)) != (listed or character in ".-"):
                self.fail(f"U+{code:04X} joins names differently from [\\p{{L}}\\p{{N}}_.-]")

    def test_entries_that_are_not_results_and_fields_of_the_wrong_type_are_ignored(self):
        chat = "10/01/26, 08:00 - Ana: voice.opus (file attached)\n"
        pending = {"file": "voice.opus", "src": None, "seconds": None, "status": "pending", "text": "", "words": 0}
        for results in (None, [], [None, "voice.opus", 3], [{"status": "ok", "text": "no file"}], [{"file": 3, "status": "ok"}],
                        [{"file": "voice.opus", "status": "pending", "text": "not finished"}], [{"file": "voice.opus", "text": "no status"}]):
            with self.subTest(results=results):
                self.assertEqual(build_model(chat, results=results)["messages"][0]["voice"], pending)
        for field, value, kept in (
            ("duration_seconds", float("nan"), None), ("duration_seconds", float("inf"), None), ("duration_seconds", True, None),
            ("duration_seconds", "3", None), ("duration_seconds", 10 ** 400, None), ("duration_seconds", 0, 0), ("duration_seconds", 2, 2),
        ):
            with self.subTest(field=field, value=value):
                voice = build_model(chat, results=[recording("voice.opus", **{field: value})])["messages"][0]["voice"]
                self.assertEqual(voice["seconds"], kept)
                self.assertNotIsInstance(voice["seconds"], bool)
        voice = build_model(chat, results=[{"file": "voice.opus", "status": "ok", "text": None, "languages": "es", "messages": "none"}])["messages"][0]["voice"]
        self.assertEqual(voice, {**pending, "status": "empty"})
        voice = build_model(chat, results=[recording("voice.opus", languages=["es", 3, "en"])])["messages"][0]["voice"]
        self.assertEqual(voice["languages"], ["es", "en"])
        voice = build_model(chat, results=[{"file": "voice.opus", "status": "error", "text": ""}], durations={"voice.opus": float("nan")})["messages"][0]["voice"]
        # A failure without a reason keeps an empty one: the viewer then says only that it failed.
        self.assertEqual(voice, {**pending, "status": "error", "error": ""})
        voice = build_model(chat + "[Voice message transcript: Error: ]\n")["messages"][0]["voice"]
        self.assertEqual(voice, {**pending, "status": "error", "error": ""})

    def test_one_warning_counts_the_transcript_lines_that_belong_to_no_recording(self):
        chat = "10/01/26, 08:00 - Ana: hola\n[Voice message transcript: suelto]\n"
        self.assertEqual(build_model(chat)["warnings"], ["1 transcript line in the chat belongs to no voice message and was left out."])
        chat += "10/01/26, 08:01 - Ana: audio omitted\n[Voice message transcript: sin grabación]\n[Voice message transcript: otra]\n"
        model = build_model(chat)
        self.assertEqual(model["warnings"], ["3 transcript lines in the chat belong to no voice message and were left out."])
        self.assertEqual([(message["kind"], message["text"]) for message in model["messages"]], [("text", "hola"), ("voice", "")])
        self.assertEqual(model["messages"][1]["voice"]["status"], "missing")
        # Both warnings, in this order.
        chat += "10/01/26, 08:02 - Ana: voice.opus (file attached)\n"
        elsewhere = recording("voice.opus", messages=[{"chat_file": "other.txt", "timestamp": "1/10/26, 8:02 AM", "sender": "Ana"}])
        self.assertEqual(build_model(chat, results=[elsewhere])["warnings"], [
            "3 transcript lines in the chat belong to no voice message and were left out.",
            "1 transcript was matched by file name only; the report seems to come from a different export of this chat.",
        ])

    def test_attachment_names_and_their_kinds(self):
        for name, kind in (
            ("PTT-20240101-WA0001.opus", "audio"), ("AUD-20240101-WA0001.m4a", "audio"), ("00000012-AUDIO-2024-03-04-10-00-00.opus", "audio"),
            ("song.MP3", "audio"), ("PTT-20240101-WA0001.xyz", "audio"), ("GIF-20240101-WA0001.mp4", "gif"), ("funny.gif", "gif"),
            ("STK-20240101-WA0001.webp", "sticker"), ("x.webp", "sticker"), ("00000012-STICKER-2024-03-04-10-00-00.webp", "sticker"),
            ("IMG-20240101-WA0001.jpg", "image"), ("photo.JPEG", "image"), ("shot.png", "image"), ("x.heic", "image"),
            ("00000012-PHOTO-2024-03-04-10-00-00.jpg", "image"), ("VID-20240101-WA0001.mp4", "video"), ("clip.mov", "video"), ("x.3gp", "video"),
            ("x.mkv", "video"), ("x.avi", "video"), ("00000012-VIDEO-2024-03-04-10-00-00.mp4", "video"), ("Ana.vcf", "contact"),
            ("DOC-20240101-WA0001.pdf", "document"), ("Informe final.docx", "document"), ("notes.txt", "document"),
        ):
            with self.subTest(name=name):
                self.assertTrue(is_attachment_name(name))
                self.assertEqual(media_type(name), kind)
        for name in ("alle 20.30", "I paid 12.50", "www.example.com", "v1.2", "Dr.Who", "report", "", ".", "img-20240101-wa0001.xyz",
                     "IMG-2024011-WA0001.xyz", "00000012-AUDIO-2024-03-04-10-00.xyz", "x.jpg ", "x.toolong"):
            with self.subTest(name=name):
                self.assertFalse(is_attachment_name(name))

    def test_date_hints_name_the_first_dated_attachment_of_each_message(self):
        messages = parse_chat(
            "3/4/24, 9:10 PM - Group created\n"
            "3/4/24, 9:11 PM - Ana: IMG-20240304-WA0001.jpg (file attached)\n"
            "3/4/24, 9:12 PM - Ana: <attached: Informe.pdf> <attached: 00000012-PHOTO-2024-03-05-10-00-00.jpg>\n"
            "3/4/24, 9:13 PM - Ana: sent IMG-20240306-WA0002.jpg and voice-2024-03-07.opus\n"
            "3/4/24, 9:14 PM - Ana: This message was deleted\n"
        )
        self.assertEqual(date_hints(messages), [None, "2024-03-04", "2024-03-05", None, None])
        self.assertEqual(date_hints(messages, [recording("media/voice-2024-03-07.opus")]), [None, "2024-03-04", "2024-03-05", "2024-03-07", None])

    def test_voice_seconds_are_added_one_by_one_in_message_order(self):
        seconds = [0.1, 0.2, 0.3, 1e16, 1.0, -1e16, 0.7]
        chat = "".join(f"10/01/26, 08:0{number} - Ana: {number}.opus (file attached)\n" for number in range(len(seconds)))
        model = build_model(chat, durations={f"{number}.opus": value for number, value in enumerate(seconds)})
        expected = 0
        for value in seconds:
            expected += value
        self.assertNotEqual(expected, sum(sorted(seconds)))
        self.assertEqual(totals(model)[0]["voice_seconds"], expected)
        self.assertEqual(aggregate(model, "month")["series"][0]["voice_seconds"], [expected])

    def test_buckets_run_from_the_earliest_day_to_the_latest_whatever_the_order_of_the_messages(self):
        def message(time, words=1):
            return {"time": time, "sender": 0, "kind": "text", "text": "x", "words": words}

        model = {"participants": ["Ana"], "messages": [
            message("2024-03-01T10:00:00"), message("2024-02-27T10:00:00", 2), message(None, 4), message("not a time", 8), message("2024-02-30T10:00:00", 16),
        ]}
        counted = aggregate(model)
        self.assertEqual(counted["buckets"], ["2024-02-27", "2024-02-28", "2024-02-29", "2024-03-01"])
        # A message without a real time is counted on the day of the message before it.
        self.assertEqual(counted["series"][0]["words_typed"], [30, 0, 0, 1])
        self.assertEqual(aggregate(model, "week")["buckets"], ["2024-02-26"])
        self.assertEqual(aggregate(model, "month")["buckets"], ["2024-02-01", "2024-03-01"])
        # The calendar has a first and a last day.
        edges = {"participants": ["Ana"], "messages": [message("0001-01-01T00:00:00"), message("0001-01-09T00:00:00")]}
        self.assertEqual(aggregate(edges, "week")["buckets"], ["0001-01-01", "0001-01-08"])
        edges = {"participants": ["Ana"], "messages": [message("9999-11-30T00:00:00"), message("9999-12-31T23:59:59")]}
        self.assertEqual(aggregate(edges, "month")["buckets"], ["9999-11-01", "9999-12-01"])
        self.assertEqual(len(aggregate(edges, "day")["buckets"]), 32)
        self.assertEqual(aggregate(edges, "week")["buckets"][-1], "9999-12-27")

    def test_the_module_needs_nothing_but_the_standard_library(self):
        tree = ast.parse(Path(analysis.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * node.level + (node.module or ""))
        self.assertEqual({name for name in imported if name not in sys.stdlib_module_names}, {".chat", ".export"})


if __name__ == "__main__":
    unittest.main()
