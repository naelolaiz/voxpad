"""Tests for writing the reports and the annotated chat, and for reading earlier reports."""

import codecs
import json
import tempfile
import unicodedata
import unittest
from unittest import mock

from voxpad.export import index_messages, scan_export
from voxpad.reports import (PreviousReport, atomic_write, load_previous_results, merged, without_transcripts,
                            write_annotated_chat, write_reports)
from tests.support import ExportTestCase


def strict_json(text):
    """Parse as a browser does, where NaN and Infinity are syntax errors."""
    def refuse(name):
        raise AssertionError(f"{name} is not JSON")
    return json.loads(text, parse_constant=refuse)


class ReportTests(ExportTestCase):
    def test_annotated_chat_inserts_after_complete_message_and_keeps_original_text(self):
        audio = self.touch("voice.opus")
        chat = self.root / "chat.txt"
        text = "01/10/26, 10:00 - Ana: voice.opus (attached)\r\nA continuation\r\n01/10/26, 10:01 - Bob: Bye"
        destination = self.root / "annotated.txt"
        write_annotated_chat(
            destination, chat, text, {(chat, 1): [audio]},
            {audio: {"status": "ok", "text": "Hola, ¿qué tal?"}},
        )
        result = destination.read_bytes().decode("utf-8")
        self.assertEqual(result, text.replace("A continuation\r\n", "A continuation\r\n[Voice message transcript: Hola, ¿qué tal?]\r\n"))

    def test_annotated_chat_keeps_line_separators_and_form_feeds_inside_messages(self):
        first = self.touch("first.opus")
        second = self.touch("second.opus")
        chat = self.root / "chat.txt"
        destination = self.root / "annotated.txt"
        # Neither character ends a line, so each transcript follows its whole message.
        write_annotated_chat(
            destination, chat, "01/10/26, 10:00 - Ana: one\u2028two\n01/10/26, 10:01 - Bob: x\x0cy",
            {(chat, 0): [first], (chat, 1): [second]},
            {first: {"status": "ok", "text": "uno"}, second: {"status": "ok", "text": "dos"}},
        )
        self.assertEqual(destination.read_bytes().decode("utf-8"), (
            "01/10/26, 10:00 - Ana: one\u2028two\n"
            "[Voice message transcript: uno]\n"
            "01/10/26, 10:01 - Bob: x\x0cy\n"
            "[Voice message transcript: dos]\n"
        ))

    def test_annotated_chat_numbers_lines_as_the_chat_parser_does(self):
        first, second, third = (self.touch(name) for name in ("first.opus", "second.opus", "third.opus"))
        chat = self.root / "chat.txt"
        # CR LF, CR and LF each end a line; nothing else str.splitlines() breaks at does.
        text = (
            "01/10/26, 10:00 - Ana: first.opus (attached)\u2029a caption\x0b\x1c\x1d\x1e\x85\r\n"
            "01/10/26, 10:01 - Bob: second.opus (attached)\r"
            "One more line\n"
            "01/10/26, 10:02 - Ana: third.opus (attached)\x0c"
        )
        chat.write_bytes(text.encode("utf-8"))
        _, texts, occurrences = index_messages(scan_export(self.root, set()))
        self.assertEqual(occurrences, {(chat, 0): [first], (chat, 2): [second], (chat, 3): [third]})
        destination = self.root / "annotated.txt"
        write_annotated_chat(destination, chat, texts[chat], occurrences,
                             {path: {"status": "ok", "text": path.stem} for path in (first, second, third)})
        self.assertEqual(destination.read_bytes().decode("utf-8"), (
            "01/10/26, 10:00 - Ana: first.opus (attached)\u2029a caption\x0b\x1c\x1d\x1e\x85\r\n"
            "[Voice message transcript: first]\r\n"
            "01/10/26, 10:01 - Bob: second.opus (attached)\r"
            "One more line\n"
            "[Voice message transcript: second]\r\n"
            "01/10/26, 10:02 - Ana: third.opus (attached)\x0c\r\n"
            "[Voice message transcript: third]\r\n"
        ))

    def test_atomic_write_preserves_mixed_line_endings_with_windows_text_defaults(self):
        destination = self.root / "report.txt"
        text = "José\r\nWindows line\nUnix line\rOld Mac line"
        named_temporary_file = tempfile.NamedTemporaryFile

        def windows_temporary_file(*args, **kwargs):
            if kwargs.get("newline") is None:
                kwargs["newline"] = "\r\n"
            return named_temporary_file(*args, **kwargs)

        with mock.patch.object(tempfile, "NamedTemporaryFile", side_effect=windows_temporary_file):
            atomic_write(destination, text)
        self.assertEqual(destination.read_bytes(), text.encode("utf-8"))
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_annotated_chat_replaces_the_transcripts_a_message_already_ends_with(self):
        first, second, third, fourth = (self.touch(f"{name}.opus") for name in ("first", "second", "third", "fourth"))
        chat = self.root / "chat.txt"
        # The annotated copy of an earlier run, given as the chat.
        text = (
            "01/10/26, 10:00 - Ana: first.opus (attached)\r\n"
            "[Voice message transcript: an earlier transcript]\r\n"
            "01/10/26, 10:01 - Bob: second.opus (attached)\r\n"
            "A caption [Voice message transcript: typed by hand]\r\n"
            "[Voice message transcript: Error: the decoder said\n"
            "two lines]\r\n"
            "01/10/26, 10:02 - Ana: third.opus (attached)\r\n"
            "[Voice message transcript: stays until this recording is reached]\r\n"
            "01/10/26, 10:03 - Bob: fourth.opus (attached)\r\n"
            "[Voice message transcript: not at the end of the message]\r\n"
            "because this line follows it\r\n"
            "01/10/26, 10:04 - Ana: [Voice message transcript: only quoted]\r\n"
        )
        chat.write_bytes(text.encode("utf-8"))
        _, texts, occurrences = index_messages(scan_export(self.root, set()))
        destination = self.root / "reports/annotated.txt"
        write_annotated_chat(destination, chat, texts[chat], occurrences, {
            first: {"status": "ok", "text": "uno"}, second: {"status": "ok", "text": ""}, fourth: {"status": "ok", "text": "cuatro"},
        })
        self.assertEqual(destination.read_bytes().decode("utf-8"), (
            "01/10/26, 10:00 - Ana: first.opus (attached)\r\n"
            "[Voice message transcript: uno]\r\n"
            "01/10/26, 10:01 - Bob: second.opus (attached)\r\n"
            "A caption [Voice message transcript: typed by hand]\r\n"
            "[Voice message transcript: No speech detected]\r\n"
            "01/10/26, 10:02 - Ana: third.opus (attached)\r\n"
            "[Voice message transcript: stays until this recording is reached]\r\n"
            "01/10/26, 10:03 - Bob: fourth.opus (attached)\r\n"
            "[Voice message transcript: not at the end of the message]\r\n"
            "because this line follows it\r\n"
            "[Voice message transcript: cuatro]\r\n"
            "01/10/26, 10:04 - Ana: [Voice message transcript: only quoted]\r\n"
        ))
        # Writing the same results over the copy changes nothing: no transcript appears twice.
        again = self.root / "reports/again.txt"
        _, texts, occurrences = index_messages(scan_export(self.root, {chat.resolve()}))
        write_annotated_chat(again, destination, texts[destination], occurrences, {
            first: {"status": "ok", "text": "uno"}, second: {"status": "ok", "text": ""}, fourth: {"status": "ok", "text": "cuatro"},
        })
        self.assertEqual(again.read_bytes(), destination.read_bytes())

    def test_taking_the_transcripts_out_gives_back_the_exported_chat(self):
        first, second = self.touch("first.opus"), self.touch("second.opus")
        chat = self.root / "chat.txt"
        bom = codecs.BOM_UTF8.decode("utf-8")
        for ending in ("\r\n", ""):
            with self.subTest(ending=ending):
                text = (f"{bom}01/10/26, 10:00 - Ana: first.opus (attached)\r\nA caption\r\n"
                        f"01/10/26, 10:01 - Bob: second.opus (attached){ending}")
                destination = self.root / "annotated.txt"
                write_annotated_chat(destination, chat, text, {(chat, 1): [first], (chat, 2): [second]}, {
                    first: {"status": "error", "text": "", "error": "Could not decode audio"}, second: {"status": "ok", "text": "dos"},
                })
                annotated = destination.read_bytes().decode("utf-8")
                self.assertIn("A caption\r\n[Voice message transcript: Error: Could not decode audio]\r\n", annotated)
                self.assertTrue(annotated.endswith("(attached)\r\n[Voice message transcript: dos]\r\n"))
                # A last line without an ending was given one before its transcript.
                self.assertEqual(without_transcripts(annotated), text + ("" if ending else "\r\n"))
                self.assertEqual(without_transcripts(text), text)
        self.assertEqual(without_transcripts(""), "")
        self.assertEqual(without_transcripts("[Voice message transcript: before any message]\n"), "[Voice message transcript: before any message]\n")

    def test_reports_are_json_that_every_reader_accepts(self):
        output = self.root / "transcripts.json"
        result = {"file": "voice.opus", "messages": [], "status": "ok", "text": "hola", "duration_seconds": float("inf"),
                  "words": [{"word": "hola", "start": 0.5, "end": 1.0, "probability": float("nan")}]}
        write_reports(output, {"source": "export", "results": [result]})
        written = strict_json(output.read_text(encoding="utf-8"))["results"][0]
        self.assertIsNone(written["duration_seconds"])
        self.assertEqual(written["words"], [{"word": "hola", "start": 0.5, "end": 1.0, "probability": None}])
        self.assertEqual(output.with_suffix(".txt").read_text(encoding="utf-8"), "voice.opus\nhola\n")


class PreviousReportTests(ExportTestCase):
    def save(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
        return path

    def test_a_report_of_the_command_is_read_with_its_model_and_finished_results(self):
        transcribed = {
            "file": "media/voice.opus", "bytes": 5, "status": "ok", "text": "hola", "languages": ["es"], "duration_seconds": 1.5,
            "messages": [{"chat_file": "chat.txt", "timestamp": "01/10/26, 10:00", "sender": "Ana"}],
            "segments": [{"start": 0, "end": 1.5, "text": "hola", "language": "es"}],
            "words": [{"word": "hola", "start": 0.1, "end": 0.4, "probability": 0.9}], "model": "Whisper tiny",
        }
        failed = {"file": "broken.opus", "messages": [], "status": "error", "text": "", "error": "Could not decode audio"}
        report = {"source": "export", "model": "Whisper small", "sample_rate": 16000, "chunk_seconds": 30, "results": [transcribed, failed]}
        first = self.save("earlier/transcripts.json", report)
        # A report saved again by an editor that puts a byte order mark first.
        second = self.save("with mark.json", codecs.BOM_UTF8 + json.dumps({"results": []}).encode("utf-8"))
        self.assertEqual(load_previous_results([first, second]), [
            PreviousReport("transcripts.json", "Whisper small", [transcribed, failed]), PreviousReport("with mark.json", None, []),
        ])
        self.assertEqual(load_previous_results([]), [])

    def test_a_download_of_the_browser_app_is_read_without_its_pending_recordings(self):
        message = {"chat_file": "chat.txt", "timestamp": "01/10/26, 10:00", "sender": "Ana"}
        path = self.save("voxpad.json", {
            "schema_version": 1, "source": "export.zip", "sample_rate": 16000,
            "chats": [{"file": "chat.txt", "original_text": "01/10/26, 10:00 - Ana: a.opus (attached)\n", "messages": []}],
            "results": [
                {"file": "a.opus", "messages": [message], "status": "ok", "text": "hola", "languages": ["es"],
                 "segments": [{"start": 0, "end": 2, "text": "hola"}], "duration_seconds": 2, "model": {"name": "Whisper base"}},
                {"file": "b.opus", "messages": [], "status": "pending", "text": "", "languages": [], "segments": []},
                {"file": "c.opus", "messages": [], "status": "error", "text": "", "languages": [], "segments": [], "error": "Out of memory"},
            ],
            "warnings": [],
            # The browser app describes its model with more than a name.
            "model": {"name": "Whisper small", "repository": "onnx-community/whisper-small", "runtime": "WebGPU"},
        })
        [report] = load_previous_results([path])
        self.assertEqual((report.name, report.model), ("voxpad.json", "Whisper small"))
        self.assertEqual(report.results, [
            {"file": "a.opus", "messages": [message], "status": "ok", "text": "hola", "languages": ["es"],
             "segments": [{"start": 0, "end": 2, "text": "hola"}], "duration_seconds": 2, "model": "Whisper base"},
            {"file": "c.opus", "messages": [], "status": "error", "text": "", "languages": [], "segments": [], "error": "Out of memory"},
        ])

    def test_fields_of_the_wrong_type_are_left_out_of_a_result(self):
        for key, value in (
            ("languages", ["es", 3]), ("languages", "es"), ("duration_seconds", -1), ("duration_seconds", True),
            ("duration_seconds", "3"), ("segments", [{"start": 0}, "text"]), ("words", {"word": "hola"}), ("bytes", 2.5),
            ("bytes", -1), ("bytes", True), ("messages", "none"), ("messages", ["none"]), ("model", 7), ("model", {"name": 7}),
            ("error", "left out of a transcript"), ("something else", "ignored"),
        ):
            with self.subTest(key=key, value=value):
                path = self.save("report.json", {"results": [{"file": "voice.opus", "status": "ok", "text": "hola", key: value}]})
                self.assertEqual(load_previous_results([path])[0].results, [{"file": "voice.opus", "status": "ok", "text": "hola"}])
        # Python reads and writes numbers that are not JSON. None may reach a report written later.
        path = self.save("report.json", '{"results": [{"file": "voice.opus", "status": "ok", "duration_seconds": NaN, '
                                        '"segments": [{"start": -Infinity, "end": 1e999, "text": "hola"}]}]}')
        self.assertEqual(load_previous_results([path])[0].results, [
            {"file": "voice.opus", "status": "ok", "text": "", "segments": [{"start": None, "end": None, "text": "hola"}]},
        ])
        # A failure always says something, as the reports and the annotated chat print it.
        path = self.save("report.json", {"results": [{"file": "voice.opus", "status": "error"}]})
        self.assertEqual(load_previous_results([path])[0].results, [
            {"file": "voice.opus", "status": "error", "text": "", "error": "Transcription failed"},
        ])

    def test_a_file_that_is_not_a_report_is_named_without_its_folder(self):
        result = {"file": "voice.opus", "status": "ok", "text": "hola"}
        for content in (
            "an earlier report", "", "[]", "3", '{"results": {}}', '{"model": "Whisper small"}', '{"results": ["voice.opus"]}',
            {"results": [{**result, "file": 3}]}, {"results": [{**result, "status": None}]}, {"results": [{"status": "ok"}]},
            {"results": [{"file": "voice.opus"}]}, {"results": [{**result, "text": None}]}, {"results": [result, None]},
            b"\xff\xfe not UTF-8", "[" * 100000,
        ):
            with self.subTest(content=content if len(content) < 100 else "deeply nested"):
                path = self.save("private folder/notes.json", content)
                with self.assertRaises(ValueError) as error:
                    load_previous_results([self.save("fine.json", {"results": [result]}), path])
                self.assertEqual(str(error.exception), "notes.json is not a VoxPad transcript report.")

    def test_merging_keeps_one_result_per_recording_preferring_later_reports_and_transcripts(self):
        def result(file, status, text):
            return {"file": file, "status": status, "text": text}

        composed, decomposed = (unicodedata.normalize(form, "café.opus") for form in ("NFC", "NFD"))
        self.assertNotEqual(composed, decomposed)
        first = PreviousReport("first.json", None, [
            result("a.opus", "ok", "first a"), result("b.opus", "ok", "first b"), result(composed, "error", "failed"),
        ])
        second = PreviousReport("second.json", None, [
            result("b.opus", "error", "second b failed"), result("a.opus", "ok", "second a"),
            result(decomposed, "error", "failed again"), result("c.opus", "error", "c failed"),
        ])
        self.assertEqual([(entry["file"], entry["text"]) for entry in merged([first, second])], [
            ("a.opus", "second a"), ("b.opus", "first b"), (decomposed, "failed again"), ("c.opus", "c failed"),
        ])
        self.assertEqual(merged([]), [])
        # The reports themselves stay as they were read.
        merged([first])[0]["text"] = "changed"
        self.assertEqual(first.results[0]["text"], "first a")


if __name__ == "__main__":
    unittest.main()
