"""Tests for the desktop application's logic; no window is opened and no model is loaded."""

import io
from contextlib import redirect_stderr, redirect_stdout
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest import mock

from voxpad import gui, whatsapp
from tests.test_whatsapp import FakeWhisper, codecpod, np, write_wav


def drain(events):
    collected = []
    while not events.empty():
        collected.append(events.get_nowait())
    return collected


class ChoiceTests(unittest.TestCase):
    def test_language_box_accepts_names_codes_and_automatic_detection(self):
        self.assertIsNone(gui.language_code(gui.AUTOMATIC))
        self.assertIsNone(gui.language_code("  "))
        self.assertEqual(gui.language_code("Spanish"), "es")
        self.assertEqual(gui.language_code("spanish "), "es")
        self.assertEqual(gui.language_code("ES"), "es")
        # Languages outside the short list can be typed as Whisper codes.
        self.assertEqual(gui.language_code("gl"), "gl")
        with self.assertRaisesRegex(ValueError, "Unknown language"):
            gui.language_code("Klingon")
        for code, _ in gui.LANGUAGES:
            self.assertIn(code, whatsapp.LANGUAGES)

    def test_model_box_maps_descriptions_to_names_and_keeps_typed_names(self):
        name, description = gui.MODELS[0]
        self.assertEqual(name, whatsapp.DEFAULT_MODEL)
        self.assertEqual(gui.model_name(f"{name} — {description}"), name)
        self.assertEqual(gui.model_name("small"), "small")
        self.assertEqual(gui.model_name(" /models/custom "), "/models/custom")
        self.assertEqual(gui.model_name(""), whatsapp.DEFAULT_MODEL)

    def test_default_output_folder_is_beside_the_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "WhatsApp Chat.zip"
            archive.write_bytes(b"")
            folder = root / "export"
            folder.mkdir()
            self.assertEqual(gui.default_output_folder(archive), root / "WhatsApp Chat transcripts")
            self.assertEqual(gui.default_output_folder(folder), root / "export transcripts")

    def test_summary_mentions_failures_and_early_stops(self):
        summary = {"report": {"results": [{}, {}, {}]}, "total": 5, "failures": 1, "stopped": True, "output": Path("out/transcripts.json")}
        self.assertEqual(gui.summary_text(summary), "Transcribed 2 of 5 voice messages, 1 failed (stopped early). Saved in out")
        summary = {"report": {"results": [{}]}, "total": 1, "failures": 0, "stopped": False, "output": Path("out/transcripts.json")}
        self.assertEqual(gui.summary_text(summary), "Transcribed 1 of 1 voice messages. Saved in out")

    def test_help_does_not_open_a_window(self):
        with self.assertRaises(SystemExit) as exit_, redirect_stdout(io.StringIO()) as output:
            gui.main(["--help"])
        self.assertEqual(exit_.exception.code, 0)
        self.assertIn("voxpad-app", output.getvalue())

    def test_missing_tkinter_is_explained(self):
        errors = io.StringIO()
        with mock.patch.dict("sys.modules", {"tkinter": None}), redirect_stderr(errors):
            self.assertEqual(gui.main([]), 1)
        self.assertIn("Tkinter", errors.getvalue())


@unittest.skipUnless(codecpod is not None and np is not None, "Install codecpod and NumPy for audio conversion tests")
class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.export = self.root / "export"
        self.export.mkdir()
        for name in ("first.wav", "second.wav"):
            write_wav(self.export / name)
        self.chat = "01/10/26, 10:00 - José: first.wav (archivo adjunto)\r\n01/10/26, 10:01 - Ana: second.wav (attached)\r\n"
        (self.export / "chat.txt").write_bytes(self.chat.encode("utf-8"))
        self.folder = self.root / "export transcripts"
        self.events = queue.Queue()

    def run_job(self, source=None, *, stop_first=False, **options):
        constructor = mock.Mock(return_value=FakeWhisper())
        job = gui.Job(source or self.export, self.folder, model="tiny", language="es", events=self.events, **options)
        if stop_first:
            job.stop()
        with mock.patch.object(whatsapp, "Whisper", constructor):
            job.run()
        return constructor, drain(self.events)

    def test_job_writes_reports_and_shows_the_annotated_chat(self):
        constructor, events = self.run_job()
        constructor.assert_called_once_with("tiny")
        self.assertEqual([event for event in events if event[0] == "progress"], [("progress", 0, 2), ("progress", 1, 2), ("progress", 2, 2)])
        self.assertIn(("status", "[2/2] second.wav"), events)
        kind, summary, preview = events[-1]
        self.assertEqual(kind, "done")
        self.assertEqual((summary["total"], summary["failures"], summary["stopped"]), (2, 0, False))
        self.assertEqual(preview, self.chat.replace(
            "(archivo adjunto)\r\n", "(archivo adjunto)\r\n[Voice message transcript: part 1]\r\n",
        ).replace("(attached)\r\n", "(attached)\r\n[Voice message transcript: part 2]\r\n"))
        self.assertEqual((self.folder / gui.CHAT_NAME).read_bytes().decode("utf-8"), preview)
        report = json.loads((self.folder / gui.REPORT_NAME).read_text(encoding="utf-8"))
        self.assertEqual([result["text"] for result in report["results"]], ["part 1", "part 2"])
        self.assertEqual(report["source"], "export")
        # The export itself is untouched.
        self.assertEqual(sorted(path.name for path in self.export.iterdir()), ["chat.txt", "first.wav", "second.wav"])

    def test_recording_without_a_chat_shows_the_plain_report(self):
        _, events = self.run_job(self.export / "first.wav")
        kind, summary, preview = events[-1]
        self.assertEqual(kind, "done")
        self.assertIsNone(summary["chat_output"])
        self.assertFalse((self.folder / gui.CHAT_NAME).exists())
        self.assertEqual(preview, (self.folder / "transcripts.txt").read_text(encoding="utf-8"))
        self.assertIn("part 1", preview)

    def test_stopped_job_keeps_what_was_done(self):
        _, events = self.run_job(stop_first=True)
        kind, summary, preview = events[-1]
        self.assertEqual(kind, "done")
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["report"]["results"], [])
        self.assertEqual(preview, self.chat)
        self.assertIn("stopped early", gui.summary_text(summary))

    def test_failures_are_reported_instead_of_raised(self):
        empty = self.root / "empty"
        empty.mkdir()
        _, events = self.run_job(empty)
        self.assertEqual(events[-1][0], "failed")
        self.assertIn("No audio files found", events[-1][1])
        with mock.patch.object(whatsapp, "transcribe_export", side_effect=KeyError("boom")):
            gui.Job(self.export, self.folder, model="tiny", language=None, events=self.events).run()
        self.assertEqual(drain(self.events)[-1], ("failed", "Unexpected error: 'boom'"))


if __name__ == "__main__":
    unittest.main()
