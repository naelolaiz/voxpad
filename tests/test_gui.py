"""Tests for the desktop application's logic; no window is opened and no model is loaded."""

import io
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from voxpad import gui, whatsapp
from tests.test_whatsapp import FakeService, FakeWhisper, codecpod, http_error, np, write_wav

# The window tests need no display: Qt can draw offscreen.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6 import QtWidgets
except ImportError:
    QtWidgets = None


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

    def test_run_on_box_offers_this_computer_first_and_marks_every_upload(self):
        self.assertEqual(gui.PLACES[0], (gui.LOCAL, None))
        self.assertIsNone(gui.remote_service(gui.LOCAL))
        self.assertIn("Everything runs on this computer", gui.privacy_note(None))
        self.assertEqual([service for _, service in gui.PLACES[1:]], list(whatsapp.REMOTE_SERVICES))
        for label, service in gui.PLACES[1:]:
            self.assertIn("uploads the audio", label)
            self.assertEqual(gui.remote_service(label), service)
            self.assertIn(f"uploaded to {whatsapp.REMOTE_SERVICES[service]}", gui.privacy_note(service))
            self.assertNotIn("Everything runs on this computer", gui.privacy_note(service))

    def test_remote_choices_that_cannot_start_are_explained(self):
        # These checks run on the window's own thread, so they never load or call the hub client.
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(sys.modules, {"huggingface_hub": None}):
            # Nothing is checked when Whisper runs here.
            self.assertIsNone(gui.remote_problem(None, language="es", model=folder, token="not a token"))
            self.assertIn("cannot be told the language", gui.remote_problem("hf-inference", language="es", model="tiny", token="hf_x"))
            # A folder, a path or a web address would be used as the place to upload to.
            for model in (folder, "http://127.0.0.1:8000/elsewhere", "https://example.org/whisper", "some/deep/path"):
                with self.subTest(model=model):
                    self.assertIn("not a folder or a web address", gui.remote_problem("deepinfra", language="es", model=model, token="hf_x"))
            # A mispasted token is refused without being repeated.
            for token in ('"hf_quoted"', "HF_TOKEN=hf_pasted", "Bearer hf_pasted", "hf_two\nlines", "/home/ana/WhatsApp Chat.zip"):
                with self.subTest(token=token):
                    problem = gui.remote_problem("deepinfra", language="es", model="tiny", token=token)
                    self.assertIn("starts with hf_", problem)
                    self.assertNotIn(token, problem)
            self.assertIsNone(gui.remote_problem("deepinfra", language="es", model="openai/whisper-large-v3", token="hf_x"))
            # An empty box is left to HF_TOKEN or a saved login, which the job looks up.
            self.assertIsNone(gui.remote_problem("hf-inference", language=None, model="tiny", token=None))

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

    def test_every_documented_way_to_start_it_works(self):
        for arguments in (["-m", "voxpad.gui"], [gui.__file__]):
            with self.subTest(arguments=arguments):
                completed = subprocess.run([sys.executable, *arguments, "--help"], capture_output=True, text=True)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertIn("voxpad-app", completed.stdout)

    def test_missing_window_toolkit_is_explained(self):
        errors = io.StringIO()
        with mock.patch.dict("sys.modules", {"PySide6": None}), redirect_stderr(errors):
            self.assertEqual(gui.main([]), 1)
        self.assertIn("PySide6", errors.getvalue())
        self.assertIn("requirements.txt", errors.getvalue())


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
        self.assertIsNone(summary["refusal"])
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

    def test_remote_job_uploads_with_the_given_token_and_passes_on_a_refusal(self):
        service = FakeService(token=None, outcomes=["Hola", http_error(401, "Invalid token")])
        local = mock.Mock(side_effect=AssertionError("Whisper must not run on this computer"))
        job = gui.Job(self.export, self.folder, model="tiny", language="es", events=self.events,
                      remote="deepinfra", token="hf_typed")
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}), mock.patch.object(whatsapp, "Whisper", local):
            job.run()
        events = drain(self.events)
        local.assert_not_called()
        self.assertEqual((service.lookups[0]["token"], service.clients[0]["token"]), ("hf_typed", "hf_typed"))
        self.assertEqual([(call["model"], call["extra_body"]) for call in service.calls],
                         [("openai/whisper-tiny", {"language": "es"})] * 2)
        kind, summary, preview = events[-1]
        self.assertEqual(kind, "done")
        self.assertEqual((summary["total"], summary["failures"], summary["stopped"]), (2, 1, True))
        self.assertIn("Invalid token", summary["refusal"])
        self.assertIn("[Voice message transcript: Hola]", preview)
        # The token is used to connect and is shown or saved nowhere.
        saved = "".join(path.read_bytes().decode("utf-8") for path in self.folder.iterdir())
        self.assertNotIn("hf_typed", repr(events) + saved)

    def test_remote_job_that_cannot_start_says_why_and_writes_nothing(self):
        cases = (
            # No token typed, set or saved: the window has a box for one.
            (FakeService(token=None), "tiny", ("HF_TOKEN", "Token box")),
            (FakeService(offers=()), "tiny", ("does not offer openai/whisper-tiny",)),
        )
        for service, model, expected in cases:
            with self.subTest(expected=expected):
                job = gui.Job(self.export, self.folder, model=model, language="es", events=self.events, remote="deepinfra")
                with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}):
                    job.run()
                kind, message = drain(self.events)[-1]
                self.assertEqual(kind, "failed")
                for text in expected:
                    self.assertIn(text, message)
                self.assertEqual((service.clients, service.calls), ([], []))
                self.assertFalse(self.folder.exists())


@unittest.skipUnless(QtWidgets is not None and codecpod is not None and np is not None,
                     "Install PySide6, codecpod and NumPy for the window tests")
class WindowTests(unittest.TestCase):
    """Drive the real window on Qt's offscreen platform, with a fake model."""

    @classmethod
    def setUpClass(cls):
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.export = self.root / "export"
        self.export.mkdir()
        write_wav(self.export / "voice.wav")
        self.chat = "01/10/26, 10:00 - José: voice.wav (archivo adjunto)\r\n01/10/26, 10:01 - Ana: ¡Perfecto!\r\n"
        (self.export / "chat.txt").write_bytes(self.chat.encode("utf-8"))
        self.window = gui.create_window()
        self.addCleanup(self.window.deleteLater)

    def finish(self):
        deadline = time.monotonic() + 30
        while self.window.job is not None and time.monotonic() < deadline:
            self.application.processEvents()
            self.window.poll()
            time.sleep(0.01)
        self.assertIsNone(self.window.job, "the transcription did not finish")

    def test_window_starts_idle_and_enables_transcribing_once_an_export_is_chosen(self):
        window = self.window
        self.assertEqual(window.windowTitle(), "VoxPad")
        self.assertFalse(window.start_button.isEnabled())
        self.assertFalse(window.stop_button.isEnabled())
        self.assertEqual(window.language.currentText(), gui.AUTOMATIC)
        self.assertTrue(window.model.currentText().startswith(whatsapp.DEFAULT_MODEL))
        window.set_source(self.export)
        self.assertTrue(window.start_button.isEnabled())
        self.assertEqual(window.source_box.text(), str(self.export))
        self.assertEqual(window.folder_box.text(), str(self.root / "export transcripts"))

    def test_transcribing_shows_the_conversation_and_saves_the_reports(self):
        window = self.window
        window.set_source(self.export)
        window.language.setCurrentText("Spanish")
        window.model.setCurrentText("tiny")
        constructor = mock.Mock(return_value=FakeWhisper())
        with mock.patch.object(whatsapp, "Whisper", constructor):
            window.start_button.click()
            self.assertTrue(window.stop_button.isEnabled())
            self.assertFalse(window.start_button.isEnabled())
            self.assertFalse(window.model.isEnabled())
            self.finish()
        constructor.assert_called_once_with("tiny")
        self.assertEqual(window.text.toPlainText(), self.chat.replace("\r\n", "\n").replace(
            "(archivo adjunto)\n", "(archivo adjunto)\n[Voice message transcript: part 1]\n"))
        self.assertIn("Transcribed 1 of 1 voice messages", window.status.text())
        self.assertEqual((window.progress.value(), window.progress.maximum()), (1, 1))
        self.assertTrue(window.start_button.isEnabled())
        self.assertFalse(window.stop_button.isEnabled())
        self.assertTrue(window.open_button.isEnabled())
        folder = self.root / "export transcripts"
        self.assertEqual(sorted(path.name for path in folder.iterdir()), [gui.CHAT_NAME, gui.REPORT_NAME, "transcripts.txt"])
        # The saved conversation keeps the export's own line endings.
        self.assertIn(b"(archivo adjunto)\r\n[Voice message transcript: part 1]\r\n", (folder / gui.CHAT_NAME).read_bytes())

    def test_problems_are_shown_in_a_dialog_and_leave_the_window_usable(self):
        window = self.window
        window.set_source(self.export)
        window.language.setCurrentText("Klingon")
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog:
            window.start_button.click()
        self.assertIn("Unknown language", dialog.call_args.args[2])
        self.assertIsNone(window.job)
        empty = self.root / "empty"
        empty.mkdir()
        window.set_source(empty)
        window.language.setCurrentText(gui.AUTOMATIC)
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog, mock.patch.object(whatsapp, "Whisper", mock.Mock()):
            window.start_button.click()
            self.finish()
        self.assertIn("No audio files found", dialog.call_args.args[2])
        self.assertTrue(window.start_button.isEnabled())
        self.assertFalse(window.open_button.isEnabled())

    def choose_service(self, service):
        self.window.place.setCurrentText(next(label for label, key in gui.PLACES if key == service))

    def test_running_elsewhere_is_an_explicit_choice_that_the_window_spells_out(self):
        window = self.window
        self.assertEqual(window.place.currentText(), gui.LOCAL)
        self.assertFalse(window.place.isEditable())
        self.assertFalse(window.token.isEnabled())
        self.assertEqual(window.token.echoMode(), QtWidgets.QLineEdit.EchoMode.Password)
        # An export dropped on the masked box must not be taken for a token.
        self.assertFalse(window.token.acceptDrops())
        self.assertIn("Everything runs on this computer", window.subtitle.text())
        self.choose_service("deepinfra")
        self.assertTrue(window.token.isEnabled())
        self.assertIn("uploaded to DeepInfra through Hugging Face", window.subtitle.text())
        self.assertNotIn("Everything runs on this computer", window.subtitle.text())
        window.place.setCurrentText(gui.LOCAL)
        self.assertFalse(window.token.isEnabled())
        self.assertIn("Everything runs on this computer", window.subtitle.text())

    def test_remote_transcription_needs_a_token_uses_the_typed_one_and_explains_a_refusal(self):
        window = self.window
        window.set_source(self.export)
        window.language.setCurrentText("Spanish")
        self.choose_service("deepinfra")
        service = FakeService(token=None, outcomes=[http_error(402, "Credits used up")])
        local = mock.Mock(side_effect=AssertionError("Whisper must not run on this computer"))
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}), \
                mock.patch.object(whatsapp, "Whisper", local), \
                mock.patch.object(QtWidgets.QMessageBox, "critical") as problem, \
                mock.patch.object(QtWidgets.QMessageBox, "warning") as refusal:
            # Without a typed or saved token the job ends before anything is asked or uploaded.
            window.start_button.click()
            self.finish()
            self.assertIn("Token box", problem.call_args.args[2])
            # A mispasted token is turned down in the window, without starting a job.
            window.token.setText("HF_TOKEN=hf_typed")
            window.start_button.click()
            self.assertIn("starts with hf_", problem.call_args.args[2])
            self.assertIsNone(window.job)
            self.assertEqual((service.lookups, service.clients), ([], []))
            window.token.setText(" hf_typed ")
            window.start_button.click()
            self.assertFalse(window.place.isEnabled())
            self.assertFalse(window.token.isEnabled())
            self.finish()
        local.assert_not_called()
        self.assertEqual(service.clients[0]["token"], "hf_typed")
        self.assertEqual((service.calls[0]["model"], service.calls[0]["extra_body"]),
                         ("openai/whisper-large-v3-turbo", {"language": "es"}))
        self.assertIn("Credits used up", refusal.call_args.args[2])
        self.assertIn("stopped early", window.status.text())
        self.assertTrue(window.place.isEnabled())
        self.assertTrue(window.token.isEnabled())
        self.assertNotIn("hf_typed", window.text.toPlainText() + window.status.text())


if __name__ == "__main__":
    unittest.main()
