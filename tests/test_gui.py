"""Tests for the desktop application's logic; no window is opened and no model is loaded."""

import io
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from voxpad import engines, gui, stats, transcribe
from tests.support import FakeService, FakeWhisper, codecpod, http_error, np, write_wav

# The window tests need no display: Qt can draw offscreen.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6 import QtCore, QtGui, QtWidgets
except ImportError:
    QtCore = QtGui = QtWidgets = None

# What only a command line understands: an option, or the name of a command to type.
COMMAND_WORDS = re.compile(r"--[a-z]|voxpad-stats|\bvoxpad [A-Za-z]")
COPIED = (r"Copied {} voice messages \(\d+ KiB\) to conversation_audio/ so the viewer can play them; "
          r"untick “Include voice messages for playback” to skip this\.")
OTHER_CHAT = "20/03/26, 18:00 - Li: Another conversation\n20/03/26, 18:01 - Ana: Yes\n"
TABLE_HEAD = ["Messages", "Typed", "words", "Spoken", "words", "Total", "words", "Voice", "notes", "Voice", "time"]


def stay_in(case, folder):
    """Work in `folder` for one test: a view also looks for a report in the current folder, wherever the tests are run from."""
    previous = os.getcwd()
    os.chdir(folder)
    case.addCleanup(os.chdir, previous)


def drain(events):
    collected = []
    while not events.empty():
        collected.append(events.get_nowait())
    return collected


def notices(events):
    return [event[1] for event in events if event[0] == "status"]


def result(file, text, status="ok", **more):
    return {"file": file, "messages": [], "status": status, "text": text, **more}


def save_report(path, results, model="Whisper small"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": "export", "model": model, "results": results}), encoding="utf-8")
    return path


def model_in(page):
    return json.loads(re.search(r'<script type="application/json" id="voxpad-model">(.*?)</script>', page.read_text(encoding="utf-8"))[1])


def voices(model):
    return [message["voice"] for message in model["messages"] if message["kind"] == "voice"]


def names(folder):
    return sorted(path.name for path in folder.iterdir())


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
            self.assertIn(code, engines.LANGUAGES)

    def test_model_box_maps_descriptions_to_names_and_keeps_typed_names(self):
        name, description = gui.MODELS[0]
        self.assertEqual(name, engines.DEFAULT_MODEL)
        self.assertEqual(gui.model_name(f"{name} — {description}"), name)
        self.assertEqual(gui.model_name("small"), "small")
        self.assertEqual(gui.model_name(" /models/custom "), "/models/custom")
        self.assertEqual(gui.model_name(""), engines.DEFAULT_MODEL)

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
        self.assertEqual([service for _, service in gui.PLACES[1:]], list(engines.REMOTE_SERVICES))
        for label, service in gui.PLACES[1:]:
            self.assertIn("uploads the audio", label)
            self.assertEqual(gui.remote_service(label), service)
            self.assertIn(f"uploaded to {engines.REMOTE_SERVICES[service]}", gui.privacy_note(service))
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

    def test_summary_mentions_reuse_failures_and_early_stops(self):
        summary = {"report": {"results": [{}, {}, {}]}, "total": 5, "failures": 1, "reused": 0, "stopped": True,
                   "output": Path("out/transcripts.json")}
        self.assertEqual(gui.summary_text(summary), "Transcribed 2 of 5 voice messages, 1 failed (stopped early). Saved in out")
        summary["reused"] = 2
        self.assertEqual(gui.summary_text(summary), "Transcribed 2 of 5 voice messages, 2 reused, 1 failed (stopped early). Saved in out")
        summary = {"report": {"results": [{}]}, "total": 1, "failures": 0, "reused": 0, "stopped": False, "output": Path("out/transcripts.json")}
        self.assertEqual(gui.summary_text(summary), "Transcribed 1 of 1 voice messages. Saved in out")

    def test_output_folder_note_counts_the_transcripts_a_run_would_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            self.assertEqual(gui.saved_transcripts(folder / "not there yet"), 0)
            self.assertEqual(gui.saved_transcripts(folder), 0)
            # Failures and recordings the browser app has not reached are transcribed by the next run, so they do not count.
            save_report(folder / gui.REPORT_NAME, [
                result("a.opus", "uno"), result("b.opus", "", status="error", error="Could not decode audio"),
                result("c.opus", ""), {"file": "d.opus", "status": "pending"},
            ])
            self.assertEqual(gui.saved_transcripts(folder), 2)
            self.assertEqual(gui.transcripts_in(folder / gui.REPORT_NAME), 2)
            for text in ("[1, 2]", "not JSON", json.dumps({"results": [{"file": 1}]})):
                with self.subTest(text=text):
                    (folder / gui.REPORT_NAME).write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "transcripts.json is not a VoxPad transcript report"):
                        gui.saved_transcripts(folder)
        self.assertEqual(gui.folder_note(0), "")
        self.assertEqual(gui.folder_note(1), "This folder already holds 1 transcript; it will be reused.")
        self.assertEqual(gui.folder_note(144), "This folder already holds 144 transcripts; they will be reused.")
        # Ticking the box changes what happens to them, and the note says so.
        self.assertEqual(gui.folder_note(144, fresh=True), "This folder already holds 144 transcripts; “Start over” replaces them.")
        self.assertEqual(gui.folder_note(1, fresh=True), "This folder already holds 1 transcript; “Start over” replaces it.")
        self.assertEqual(gui.folder_note(None), "transcripts.json in this folder is not a VoxPad transcript report; it will be replaced.")
        self.assertEqual(gui.folder_note(None, fresh=True), gui.folder_note(None))

    def test_window_names_its_own_controls_where_the_commands_name_options(self):
        for text in ("[1/2] first.wav", "  Error: Could not decode audio", "Loading Whisper tiny (the first run downloads the model)..."):
            self.assertEqual(gui.window_words(text), text)
        for _, words in gui.WORDING:
            self.assertNotRegex(words, COMMAND_WORDS)
        # The controls are named as the window labels them.
        reworded = gui.window_words("Reusing 1 transcript from a.json; 0 left to transcribe. --fresh transcribes everything again.")
        self.assertIn(f"“{gui.START_OVER}”", reworded)
        self.assertEqual((gui.START_OVER, gui.PLAYBACK), ("Start over", "Include voice messages for playback"))

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
        self.page = self.folder.resolve() / gui.VIEWER_NAME
        self.events = queue.Queue()
        stay_in(self, self.root)

    def run_job(self, source=None, *, folder=None, stop_first=False, **options):
        constructor = mock.Mock(return_value=FakeWhisper())
        job = gui.Job(source or self.export, folder or self.folder, model="tiny", language="es", events=self.events, **options)
        if stop_first:
            job.stop()
        with mock.patch.object(transcribe, "Whisper", constructor):
            job.run()
        return constructor, drain(self.events)

    def view(self, source=None, *, folder=None, **options):
        """Run a job that only writes the viewer; it fails the test if anything is transcribed or a model is asked for."""
        job = gui.Job(source or self.export, folder or self.folder, events=self.events, view_only=True, **options)
        with mock.patch.object(transcribe, "transcribe_export", side_effect=AssertionError("Nothing may be transcribed")) as run, \
                mock.patch.object(transcribe, "Whisper", side_effect=AssertionError("No model may be loaded")) as constructor:
            job.run()
        run.assert_not_called()
        constructor.assert_not_called()
        return drain(self.events)

    def assertWindowWords(self, events):
        for event in events:
            for text in event[1:]:
                if isinstance(text, str):
                    self.assertNotRegex(text, COMMAND_WORDS)

    def test_job_writes_reports_and_shows_the_annotated_chat(self):
        constructor, events = self.run_job()
        constructor.assert_called_once_with("tiny")
        self.assertEqual([event for event in events if event[0] == "progress"], [("progress", 0, 2), ("progress", 1, 2), ("progress", 2, 2)])
        self.assertIn(("status", "[2/2] second.wav"), events)
        kind, summary, preview = events[-1]
        self.assertEqual(kind, "done")
        self.assertEqual((summary["total"], summary["failures"], summary["stopped"], summary["reused"]), (2, 0, False, 0))
        self.assertIsNone(summary["refusal"])
        self.assertEqual(preview, self.chat.replace(
            "(archivo adjunto)\r\n", "(archivo adjunto)\r\n[Voice message transcript: part 1]\r\n",
        ).replace("(attached)\r\n", "(attached)\r\n[Voice message transcript: part 2]\r\n"))
        self.assertEqual((self.folder / gui.CHAT_NAME).read_bytes().decode("utf-8"), preview)
        report = json.loads((self.folder / gui.REPORT_NAME).read_text(encoding="utf-8"))
        self.assertEqual([result["text"] for result in report["results"]], ["part 1", "part 2"])
        self.assertEqual(report["source"], "export")
        # The export itself is untouched.
        self.assertEqual(names(self.export), ["chat.txt", "first.wav", "second.wav"])
        self.assertWindowWords(events)

    def test_job_writes_the_viewer_with_the_transcripts_and_the_recordings(self):
        _, events = self.run_job()
        summary = events[-1][1]
        self.assertEqual((summary["viewer"], summary["viewer_problem"]), (self.page, None))
        self.assertEqual(names(self.folder), [gui.CHAT_NAME, "conversation.html", "conversation_audio", gui.REPORT_NAME, "transcripts.txt"])
        # The viewer comes last, when every transcript is saved.
        self.assertLess(notices(events).index("[2/2] second.wav"), notices(events).index("Writing the viewer…"))
        model = model_in(self.page)
        self.assertEqual(model["participants"], ["Ana", "José"])
        self.assertEqual([(voice["status"], voice["text"], voice["src"]) for voice in voices(model)],
                         [("ok", "part 1", "./conversation_audio/first.wav"), ("ok", "part 2", "./conversation_audio/second.wav")])
        # The export's folder is beside the output folder, so the page gets copies it can reach.
        for name in ("first.wav", "second.wav"):
            self.assertEqual((self.folder / "conversation_audio" / name).read_bytes(), (self.export / name).read_bytes())
        self.assertRegex(notices(events)[-1], COPIED.format(2))
        # Unticked, the page is written without them.
        _, events = self.run_job(playback=False, fresh=True)
        self.assertEqual([voice["src"] for voice in voices(model_in(self.page))], [None, None])
        self.assertFalse(any(notice.startswith("Copied") for notice in notices(events)))

    def test_saving_into_the_export_folder_links_the_recordings_and_can_be_done_again(self):
        page = self.export.resolve() / gui.VIEWER_NAME
        for reused in (0, 2):
            _, events = self.run_job(folder=self.export)
            kind, summary, preview = events[-1]
            self.assertEqual((kind, summary["reused"], summary["viewer"], summary["viewer_problem"]), ("done", reused, page, None))
            # The copy of the chat that the run wrote there is not read as a second chat.
            self.assertFalse(any("hold the same conversation" in notice for notice in notices(events)), notices(events))
            # The page lies beside the recordings and plays them where they are.
            self.assertEqual([(voice["text"], voice["src"]) for voice in voices(model_in(page))],
                             [("part 1", "./first.wav"), ("part 2", "./second.wav")])
        self.assertEqual(names(self.export),
                         ["chat.txt", gui.CHAT_NAME, "conversation.html", "first.wav", "second.wav", gui.REPORT_NAME, "transcripts.txt"])
        kind, conversation, text = self.view(folder=self.export)[-1]
        self.assertEqual((kind, conversation["audio"], conversation["matched"]), ("viewed", "link", 2))
        self.assertIn("Leave it where it is: the voice messages play from the recordings beside it.", text.splitlines())

    def test_recording_without_a_chat_shows_the_plain_report(self):
        _, events = self.run_job(self.export / "first.wav")
        kind, summary, preview = events[-1]
        self.assertEqual(kind, "done")
        self.assertIsNone(summary["chat_output"])
        self.assertFalse((self.folder / gui.CHAT_NAME).exists())
        self.assertEqual(preview, (self.folder / "transcripts.txt").read_text(encoding="utf-8"))
        self.assertIn("part 1", preview)
        # Like the annotated chat, the viewer is left out: there is no conversation to show.
        self.assertEqual((summary["viewer"], summary["viewer_problem"]), (None, None))
        self.assertEqual(names(self.folder), [gui.REPORT_NAME, "transcripts.txt"])
        self.assertNotIn("Writing the viewer…", notices(events))

    def test_export_with_two_chats_gets_the_reports_without_annotated_chat_or_viewer(self):
        (self.export / "other.txt").write_text(OTHER_CHAT, encoding="utf-8")
        _, events = self.run_job()
        kind, summary, preview = events[-1]
        # Which of them to show is not the run's to guess; it is not a problem to report either.
        self.assertEqual((kind, summary["chat_output"], summary["viewer"], summary["viewer_problem"]), ("done", None, None, None))
        self.assertEqual(names(self.folder), [gui.REPORT_NAME, "transcripts.txt"])
        self.assertEqual(preview, (self.folder / "transcripts.txt").read_text(encoding="utf-8"))

    def test_stopped_job_keeps_what_was_done(self):
        _, events = self.run_job(stop_first=True)
        kind, summary, preview = events[-1]
        self.assertEqual(kind, "done")
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["report"]["results"], [])
        self.assertEqual(preview, self.chat)
        self.assertIn("stopped early", gui.summary_text(summary))
        # The viewer shows the conversation as far as it got.
        self.assertEqual(summary["viewer"], self.page)
        self.assertEqual([voice["status"] for voice in voices(model_in(self.page))], ["pending", "pending"])

    def test_stopped_job_is_continued_by_the_next_one(self):
        engine = FakeWhisper()
        job = gui.Job(self.export, self.folder, model="tiny", language="es", events=self.events)

        def transcribe_then_stop(samples, **options):
            # Stop is pressed while the first voice message is being transcribed.
            job.stop()
            return FakeWhisper.transcribe(engine, samples, **options)

        with mock.patch.object(transcribe, "Whisper", return_value=mock.Mock(transcribe=transcribe_then_stop)):
            job.run()
        summary = drain(self.events)[-1][1]
        self.assertEqual(([result["file"] for result in summary["report"]["results"]], summary["stopped"]), (["first.wav"], True))
        self.assertEqual([voice["status"] for voice in voices(model_in(self.page))], ["ok", "pending"])
        self.assertEqual(gui.saved_transcripts(self.folder), 1)
        constructor, events = self.run_job()
        kind, summary, preview = events[-1]
        self.assertEqual((kind, summary["total"], summary["reused"], summary["stopped"]), ("done", 2, 1, False))
        # Only the voice message that was left is transcribed, and it counts from where the run stopped.
        self.assertEqual(len(constructor.return_value.calls), 1)
        self.assertEqual([event for event in events if event[0] == "progress"], [("progress", 1, 2), ("progress", 2, 2)])
        self.assertEqual([notice for notice in notices(events) if notice.startswith("[")], ["[2/2] second.wav"])
        self.assertEqual(preview.count("[Voice message transcript: part 1]"), 2)
        self.assertEqual([voice["status"] for voice in voices(model_in(self.page))], ["ok", "ok"])
        self.assertEqual(gui.summary_text(summary), f"Transcribed 2 of 2 voice messages, 1 reused. Saved in {self.folder.resolve()}")

    def test_second_job_reuses_the_folder_s_transcripts_unless_it_starts_over(self):
        self.run_job()
        constructor, events = self.run_job()
        # Nothing is left to transcribe, so no model is loaded at all.
        constructor.assert_not_called()
        kind, summary, preview = events[-1]
        self.assertEqual((kind, summary["reused"], summary["failures"], summary["stopped"]), ("done", 2, 0, False))
        self.assertEqual([result["text"] for result in summary["report"]["results"]], ["part 1", "part 2"])
        self.assertIn("[Voice message transcript: part 2]", preview)
        self.assertEqual([event for event in events if event[0] == "progress"], [("progress", 2, 2)])
        reusing = [notice for notice in notices(events) if notice.startswith("Reusing")]
        self.assertEqual(len(reusing), 1)
        self.assertTrue(reusing[0].startswith("Reusing 2 transcripts from transcripts.json"), reusing)
        # The notice names the window's box, not the command's option.
        self.assertTrue(reusing[0].endswith("0 left to transcribe. Tick “Start over” to transcribe everything again."), reusing)
        self.assertWindowWords(events)
        constructor, events = self.run_job(fresh=True)
        constructor.assert_called_once_with("tiny")
        self.assertEqual((events[-1][1]["reused"], len(constructor.return_value.calls)), (0, 2))
        self.assertFalse(any(notice.startswith("Reusing") for notice in notices(events)))

    def test_earlier_report_is_imported_instead_of_transcribing_again(self):
        # Something that is no report stops the job before a model is loaded or anything is written.
        constructor, events = self.run_job(reuse=self.export / "chat.txt")
        constructor.assert_not_called()
        self.assertEqual(events[-1], ("failed", "chat.txt is not a VoxPad transcript report."))
        self.assertFalse(self.folder.exists())
        # The browser app's download names its model in an object and the recordings by their path in the ZIP.
        earlier = save_report(self.root / "downloads" / "voxpad.json", [result("WhatsApp Chat/first.wav", "desde antes", duration_seconds=1.0)],
                              model={"name": "onnx-community/whisper-tiny"})
        constructor, events = self.run_job(reuse=earlier)
        kind, summary, preview = events[-1]
        self.assertEqual((kind, summary["reused"]), ("done", 1))
        self.assertEqual([result["text"] for result in summary["report"]["results"]], ["desde antes", "part 1"])
        self.assertEqual(len(constructor.return_value.calls), 1)
        self.assertIn("first.wav (archivo adjunto)\r\n[Voice message transcript: desde antes]\r\n", preview)
        self.assertTrue(any(notice.startswith("Reusing 1 transcript from voxpad.json") for notice in notices(events)), notices(events))
        self.assertIn("1 earlier transcript was matched by file name only.", notices(events))
        self.assertEqual([voice["text"] for voice in voices(model_in(self.page))], ["desde antes", "part 1"])
        self.assertWindowWords(events)

    def test_folder_with_transcripts_of_another_export_is_refused_in_the_window_s_words(self):
        save_report(self.folder / gui.REPORT_NAME, [result("PTT-20250101-WA0001.opus", "de otro chat")])
        before = (self.folder / gui.REPORT_NAME).read_bytes()
        constructor, events = self.run_job()
        constructor.assert_not_called()
        self.assertEqual(events[-1], ("failed", "transcripts.json holds 1 transcript of a recording that is not in this export. "
                                                "Choose another folder to save in, or tick “Start over” to replace it."))
        self.assertEqual((self.folder / gui.REPORT_NAME).read_bytes(), before)
        # Starting over is the way out that the message names.
        constructor, events = self.run_job(fresh=True)
        self.assertEqual((events[-1][0], len(constructor.return_value.calls)), ("done", 2))
        # An earlier report must not be a file that the run is about to replace.
        misnamed = save_report(self.folder / "transcripts.txt", [])
        constructor, events = self.run_job(reuse=misnamed)
        constructor.assert_not_called()
        self.assertEqual(events[-1], ("failed", "The earlier transcripts must not be one of the files this run writes."))

    def test_file_that_is_no_report_is_replaced_by_a_run_and_left_out_of_a_view(self):
        self.folder.mkdir()
        (self.folder / gui.REPORT_NAME).write_text("{}", encoding="utf-8")
        events = self.view()
        self.assertEqual(events[-1][0], "viewed")
        self.assertIn("transcripts.json is not a VoxPad transcript report. It is not used.", events[-1][2].splitlines())
        self.assertEqual((self.folder / gui.REPORT_NAME).read_text(encoding="utf-8"), "{}")
        _, events = self.run_job()
        self.assertIn("transcripts.json is not a VoxPad transcript report. It will be replaced.", notices(events))
        self.assertEqual(gui.saved_transcripts(self.folder), 2)

    def test_viewer_that_cannot_be_written_does_not_lose_the_run(self):
        with mock.patch.object(stats, "write_conversation", side_effect=OSError("No space left on device")) as writer:
            _, events = self.run_job(events_file=self.root / "events.txt")
        kind, summary, preview = events[-1]
        self.assertEqual(kind, "done")
        self.assertEqual((summary["viewer"], summary["viewer_problem"]), (None, "No space left on device"))
        self.assertIn("[Voice message transcript: part 2]", preview)
        self.assertEqual(gui.saved_transcripts(self.folder), 2)
        # The page is built from the report this run saved, and the run's own files are not read as chats.
        output = self.folder.resolve() / gui.REPORT_NAME
        writer.assert_called_once_with(
            self.export, self.page, transcripts=[output], events=self.root / "events.txt", audio="auto",
            excluded={output, output.with_suffix(".txt"), self.folder.resolve() / gui.CHAT_NAME}, optional=True, notify=mock.ANY)
        # A page that needs the command's own words to explain itself gets the window's.
        with mock.patch.object(stats, "write_conversation", side_effect=ValueError("Nothing copied; --audio none skips this.")):
            _, events = self.run_job()
        self.assertEqual(events[-1][1]["viewer_problem"], "Nothing copied; untick “Include voice messages for playback” to skip this.")
        # Even a fault nobody foresaw there leaves the run done, with its transcripts shown.
        with mock.patch.object(stats, "write_conversation", side_effect=KeyError("boom")):
            _, events = self.run_job()
        self.assertEqual((events[-1][0], events[-1][1]["viewer_problem"]), ("done", "Unexpected error: 'boom'"))

    def test_viewing_writes_the_viewer_and_the_summary_without_transcribing(self):
        events = self.view()
        kind, conversation, text = events[-1]
        self.assertEqual(kind, "viewed")
        self.assertEqual(conversation["viewer"], self.page)
        # No report, no annotated chat: only the page and the recordings it plays.
        self.assertEqual(names(self.folder), ["conversation.html", "conversation_audio"])
        self.assertEqual([(voice["status"], voice["seconds"], voice["src"]) for voice in voices(model_in(self.page))],
                         [("pending", 1.0, "./conversation_audio/first.wav"), ("pending", 1.0, "./conversation_audio/second.wav")])
        lines = text.splitlines()
        # What voxpad-stats prints: its notices, then its summary and where the page is.
        self.assertEqual((lines[0], lines[2]), ("Measured the length of 2 recordings from their files.", ""))
        self.assertRegex(lines[1], COPIED.format(2))
        self.assertEqual(lines[3:], stats.summary_lines(conversation, command=False) + stats.viewer_lines(conversation, command=False))
        for line in ("Parsed 2 messages from 2026-10-01 to 2026-10-01", "Voice messages: 2 detected, 2 with a recording, 2 with a duration.",
                     "Spoken words cover 0 of 2 voice messages (2 not transcribed, 0 failed, 0 without a recording).",
                     "2 voice messages have no transcript: transcribe them to count their words", f"Viewer: {self.page}",
                     "Keep conversation_audio/ beside it: the voice messages play from there.",
                     "conversation.html contains the whole conversation and its transcripts."):
            self.assertIn(line, lines)
        self.assertIn(TABLE_HEAD, [line.split() for line in lines])
        self.assertEqual(notices(events), lines[:2])
        self.assertWindowWords(events)

    def test_viewing_uses_the_transcripts_already_there_and_the_events(self):
        self.run_job()
        before = (self.folder / gui.REPORT_NAME).read_bytes()
        marked = self.root / "events.txt"
        marked.write_text("2026-10-01, Ana llega\nsin fecha\n", encoding="utf-8")
        events = self.view(events_file=marked, playback=False)
        kind, conversation, text = events[-1]
        self.assertEqual(kind, "viewed")
        lines = text.splitlines()
        self.assertEqual(lines[:3], ["Events line 2 has no valid date and was skipped.", "Using transcripts.json: 2 of 2 voice messages matched", ""])
        self.assertIn("Events: 1 shown", lines)
        self.assertFalse(any(line.startswith("Spoken words cover") for line in lines))
        model = model_in(self.page)
        self.assertEqual(model["events"], [{"date": "2026-10-01", "label": "Ana llega"}])
        self.assertEqual([(voice["text"], voice["words"], voice["src"]) for voice in voices(model)], [("part 1", 2, None), ("part 2", 2, None)])
        self.assertEqual((conversation["audio"], conversation["transcripts"]), ("none", ["transcripts.json"]))
        self.assertEqual((self.folder / gui.REPORT_NAME).read_bytes(), before)

    def test_viewing_reads_the_earlier_report_and_one_beside_the_chat(self):
        earlier = save_report(self.root / "downloads" / "voxpad.json", [result("first.wav", "desde antes", duration_seconds=1.0)])
        kind, conversation, text = self.view(reuse=earlier)[-1]
        self.assertEqual((kind, conversation["transcripts"], conversation["matched"]), ("viewed", ["voxpad.json"], 1))
        self.assertEqual([voice["status"] for voice in voices(conversation["model"])], ["ok", "pending"])
        # The folder's own report is the later word on a recording; the same file given twice is read once.
        save_report(self.folder / gui.REPORT_NAME, [result("first.wav", "más reciente"), result("second.wav", "dos")])
        kind, conversation, text = self.view(reuse=earlier)[-1]
        self.assertEqual(conversation["transcripts"], ["voxpad.json", "transcripts.json"])
        self.assertEqual([voice["text"] for voice in voices(conversation["model"])], ["más reciente", "dos"])
        kind, conversation, text = self.view(reuse=self.folder / gui.REPORT_NAME)[-1]
        self.assertEqual(conversation["transcripts"], ["transcripts.json"])
        # With nothing chosen and nothing in the folder, a report left beside the chat is found.
        (self.folder / gui.REPORT_NAME).unlink()
        save_report(self.export / "transcripts.json", [result("second.wav", "junto al chat")])
        kind, conversation, text = self.view()[-1]
        self.assertIn("Using transcripts.json: 1 of 2 voice messages matched", text.splitlines())
        self.assertEqual([voice["text"] for voice in voices(conversation["model"])], ["", "junto al chat"])

    def test_view_that_cannot_be_written_says_why_in_the_window_s_words(self):
        recordings = self.root / "recordings"
        recordings.mkdir()
        write_wav(recordings / "alone.wav")
        archive = self.root / "export.zip"
        with zipfile.ZipFile(archive, "w") as packed:
            packed.writestr("a.txt", self.chat)
            packed.writestr("b.txt", OTHER_CHAT)
        (self.export / "other.txt").write_text(OTHER_CHAT, encoding="utf-8")
        for source, expected in (
            (recordings, "No WhatsApp chat was found. Choose an export ZIP, its folder or the chat .txt."),
            (self.export, "Found 2 chats: chat.txt, other.txt. Choose the one to view as the export."),
            (archive, "Found 2 chats: a.txt, b.txt. Extract export.zip and choose the one to view as the export."),
            (self.root / "gone.zip", None),
        ):
            with self.subTest(source=source.name):
                events = self.view(source)
                self.assertEqual(events[-1][0], "failed")
                if expected:
                    self.assertEqual(events[-1][1], expected)
                self.assertWindowWords(events)
                self.assertFalse(self.folder.exists())
        # The chat to view can then be chosen as the export, as the message says.
        self.assertEqual(self.view(self.export / "other.txt")[-1][1]["chat"], "other.txt")

    def test_failures_are_reported_instead_of_raised(self):
        empty = self.root / "empty"
        empty.mkdir()
        _, events = self.run_job(empty)
        self.assertEqual(events[-1][0], "failed")
        self.assertIn("No audio files found", events[-1][1])
        with mock.patch.object(transcribe, "transcribe_export", side_effect=KeyError("boom")):
            gui.Job(self.export, self.folder, model="tiny", language=None, events=self.events).run()
        self.assertEqual(drain(self.events)[-1], ("failed", "Unexpected error: 'boom'"))
        with mock.patch.object(stats, "write_conversation", side_effect=KeyError("boom")):
            gui.Job(self.export, self.folder, events=self.events, view_only=True).run()
        self.assertEqual(drain(self.events)[-1], ("failed", "Unexpected error: 'boom'"))

    def test_remote_job_uploads_with_the_given_token_and_passes_on_a_refusal(self):
        service = FakeService(token=None, outcomes=["Hola", http_error(401, "Invalid token")])
        local = mock.Mock(side_effect=AssertionError("Whisper must not run on this computer"))
        job = gui.Job(self.export, self.folder, model="tiny", language="es", events=self.events,
                      remote="deepinfra", token="hf_typed")
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}), mock.patch.object(transcribe, "Whisper", local):
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
        # The token is used to connect and is shown or saved nowhere, the viewer included.
        saved = b"".join(path.read_bytes() for path in self.folder.rglob("*") if path.is_file())
        self.assertIn(b"Hola", (self.folder / gui.VIEWER_NAME).read_bytes())
        self.assertNotIn("hf_typed", repr(events))
        self.assertNotIn(b"hf_typed", saved)

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

    def test_viewing_needs_no_token_and_no_service(self):
        # The window passes on what its boxes hold; a view uses none of it.
        service = FakeService(token=None)
        job = gui.Job(self.export, self.folder, events=self.events, view_only=True, remote="deepinfra", model="/not/a/model", language="xx")
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}):
            job.run()
        self.assertEqual(drain(self.events)[-1][0], "viewed")
        self.assertEqual((service.lookups, service.clients, service.calls), ([], [], []))


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
        self.folder = self.root / "export transcripts"
        stay_in(self, self.root)
        # A dialog that a test does not expect would wait for an answer forever: note it and fail the test instead.
        self.unexpected = []
        self.addCleanup(lambda: self.assertEqual(self.unexpected, [], "a dialog was shown that the test did not expect"))
        for kind in ("critical", "warning", "question"):
            patcher = mock.patch.object(QtWidgets.QMessageBox, kind, side_effect=lambda parent, title, text, *more: self.unexpected.append(text))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.window = gui.create_window()
        self.addCleanup(self.close_window)

    def close_window(self):
        # A test that failed while a job worked must not leave it writing into a folder that is being removed.
        job = self.window.job
        if job is not None and job.is_alive():
            job.stop()
            job.join(30)
        self.window.timer.stop()
        self.window.deleteLater()

    def finish(self):
        deadline = time.monotonic() + 30
        while self.window.job is not None and time.monotonic() < deadline:
            self.application.processEvents()
            self.window.poll()
            time.sleep(0.01)
        self.assertIsNone(self.window.job, "the transcription did not finish")

    def run_transcription(self):
        constructor = mock.Mock(return_value=FakeWhisper())
        with mock.patch.object(transcribe, "Whisper", constructor):
            self.window.start_button.click()
            self.finish()
        return constructor

    def run_view(self):
        """Press "View without transcribing"; the test fails if that transcribes anything or asks for a model."""
        with mock.patch.object(transcribe, "transcribe_export", side_effect=AssertionError("Nothing may be transcribed")) as run, \
                mock.patch.object(transcribe, "Whisper", side_effect=AssertionError("No model may be loaded")) as constructor:
            self.window.view_button.click()
            self.finish()
        run.assert_not_called()
        constructor.assert_not_called()

    def choose(self, button, path):
        with mock.patch.object(QtWidgets.QFileDialog, "getOpenFileName", return_value=(str(path), "")) as dialog:
            button.click()
        dialog.assert_called_once()

    def test_window_starts_idle_and_enables_transcribing_once_an_export_is_chosen(self):
        window = self.window
        self.assertEqual(window.windowTitle(), "VoxPad")
        self.assertFalse(window.start_button.isEnabled())
        self.assertFalse(window.stop_button.isEnabled())
        self.assertEqual(window.language.currentText(), gui.AUTOMATIC)
        self.assertTrue(window.model.currentText().startswith(engines.DEFAULT_MODEL))
        # Nothing to view or to continue yet, and both optional files are empty.
        self.assertFalse(window.view_button.isEnabled())
        self.assertFalse(window.viewer_button.isEnabled())
        self.assertTrue(window.folder_note.isHidden())
        self.assertTrue(window.fresh.isHidden())
        self.assertEqual((window.fresh.text(), window.fresh.isChecked()), ("Start over", False))
        self.assertEqual((window.playback.text(), window.playback.isChecked()), ("Include voice messages for playback", True))
        self.assertEqual((window.reuse_box.text(), window.events_box.text()), ("", ""))
        self.assertTrue(window.reuse_box.isReadOnly() and window.events_box.isReadOnly())
        self.assertEqual([button.text() for button in (window.reuse_button, window.reuse_clear, window.events_button, window.events_clear)],
                         ["Choose…", "Clear", "Choose…", "Clear"])
        self.assertFalse(window.reuse_clear.isEnabled() or window.events_clear.isEnabled())
        self.assertEqual((window.view_button.text(), window.viewer_button.text()), ("View without transcribing", "Open viewer"))
        window.set_source(self.export)
        self.assertTrue(window.start_button.isEnabled())
        self.assertTrue(window.view_button.isEnabled())
        self.assertFalse(window.viewer_button.isEnabled())
        self.assertEqual(window.source_box.text(), str(self.export))
        self.assertEqual(window.folder_box.text(), str(self.root / "export transcripts"))

    def test_transcribing_shows_the_conversation_and_saves_the_reports(self):
        window = self.window
        window.set_source(self.export)
        window.language.setCurrentText("Spanish")
        window.model.setCurrentText("tiny")
        constructor = mock.Mock(return_value=FakeWhisper())
        with mock.patch.object(transcribe, "Whisper", constructor):
            window.start_button.click()
            self.assertTrue(window.stop_button.isEnabled())
            self.assertFalse(window.start_button.isEnabled())
            self.assertFalse(window.model.isEnabled())
            for widget in (window.view_button, window.reuse_button, window.events_button, window.fresh, window.playback):
                self.assertFalse(widget.isEnabled())
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
        # Every run leaves the viewer beside the reports, with the recordings it plays.
        self.assertEqual(names(folder), [gui.CHAT_NAME, "conversation.html", "conversation_audio", gui.REPORT_NAME, "transcripts.txt"])
        self.assertEqual(names(folder / "conversation_audio"), ["voice.wav"])
        self.assertEqual([(voice["text"], voice["src"]) for voice in voices(model_in(folder / gui.VIEWER_NAME))],
                         [("part 1", "./conversation_audio/voice.wav")])
        self.assertTrue(window.viewer_button.isEnabled())
        self.assertTrue(window.view_button.isEnabled() and window.playback.isEnabled() and window.reuse_button.isEnabled())
        # The saved conversation keeps the export's own line endings.
        self.assertIn(b"(archivo adjunto)\r\n[Voice message transcript: part 1]\r\n", (folder / gui.CHAT_NAME).read_bytes())

    def test_folder_that_holds_transcripts_says_so_and_a_new_run_continues_from_them(self):
        window = self.window
        window.set_source(self.export)
        self.assertEqual(window.folder_note.text(), "")
        self.run_transcription().assert_called_once()
        # Re-evaluated after the run: what was just saved will not be transcribed again.
        self.assertEqual(window.folder_note.text(), "This folder already holds 1 transcript; it will be reused.")
        self.assertFalse(window.folder_note.isHidden() or window.fresh.isHidden())
        self.assertTrue(window.fresh.isEnabled())
        self.run_transcription().assert_not_called()
        self.assertEqual(window.status.text(), f"Transcribed 1 of 1 voice messages, 1 reused. Saved in {self.folder}")
        self.assertIn("[Voice message transcript: part 1]", window.text.toPlainText())
        # Ticked, the box sets the folder's transcripts aside for one run.
        window.fresh.click()
        self.assertEqual(window.folder_note.text(), "This folder already holds 1 transcript; “Start over” replaces it.")
        self.run_transcription().assert_called_once()
        self.assertEqual(window.status.text(), f"Transcribed 1 of 1 voice messages. Saved in {self.folder}")
        self.assertFalse(window.fresh.isChecked())
        self.assertEqual(window.folder_note.text(), "This folder already holds 1 transcript; it will be reused.")

    def test_folder_note_follows_the_chosen_folder(self):
        window = self.window
        window.set_source(self.export)
        held = save_report(self.root / "held" / gui.REPORT_NAME, [result("voice.wav", "uno"), result("other.opus", "dos")]).parent
        with mock.patch.object(QtWidgets.QFileDialog, "getExistingDirectory", return_value=str(held)):
            window.output_button.click()
        self.assertEqual(window.folder_box.text(), str(held))
        self.assertEqual(window.folder_note.text(), "This folder already holds 2 transcripts; they will be reused.")
        self.assertFalse(window.fresh.isHidden())
        window.fresh.click()
        self.assertIn("“Start over” replaces them", window.folder_note.text())
        # Another folder is another question: the box does not stay ticked for it.
        empty = self.root / "empty"
        empty.mkdir()
        window.set_folder(empty)
        self.assertEqual((window.folder_note.text(), window.folder_note.isHidden(), window.fresh.isHidden(), window.fresh.isChecked()),
                         ("", True, True, False))
        window.set_folder(held)
        self.assertEqual(window.folder_note.text(), "This folder already holds 2 transcripts; they will be reused.")
        # A file of that name that VoxPad did not write is replaced by a run, which leaves nothing to start over from.
        (empty / gui.REPORT_NAME).write_text("[]", encoding="utf-8")
        window.set_folder(empty)
        self.assertEqual(window.folder_note.text(), "transcripts.json in this folder is not a VoxPad transcript report; it will be replaced.")
        self.assertEqual((window.folder_note.isHidden(), window.fresh.isHidden()), (False, True))
        # A new export brings its own folder.
        window.set_source(self.export)
        self.assertTrue(window.folder_note.isHidden())

    def test_folder_with_transcripts_of_another_export_is_explained_and_start_over_replaces_them(self):
        window = self.window
        window.set_source(self.export)
        save_report(self.folder / gui.REPORT_NAME, [result("PTT-20250101-WA0001.opus", "de otro chat")])
        window.set_folder(self.folder)
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog:
            self.run_transcription().assert_not_called()
        self.assertEqual(dialog.call_args.args[2], "transcripts.json holds 1 transcript of a recording that is not in this export. "
                                                   "Choose another folder to save in, or tick “Start over” to replace it.")
        self.assertEqual(window.status.text(), "Could not transcribe this export.")
        self.assertFalse(window.fresh.isHidden())
        window.fresh.click()
        self.run_transcription().assert_called_once()
        report = json.loads((self.folder / gui.REPORT_NAME).read_text(encoding="utf-8"))
        self.assertEqual([entry["file"] for entry in report["results"]], ["voice.wav"])

    def test_open_viewer_needs_a_page_in_the_output_folder_and_opens_it_in_the_browser(self):
        window = self.window
        window.set_source(self.export)
        self.assertFalse(window.viewer_button.isEnabled())
        # A page left by an earlier session can be opened as soon as its folder is chosen.
        self.folder.mkdir()
        (self.folder / gui.VIEWER_NAME).write_text("<!doctype html>", encoding="utf-8")
        window.set_folder(self.folder)
        self.assertTrue(window.viewer_button.isEnabled())
        with mock.patch.object(QtGui.QDesktopServices, "openUrl") as opened:
            window.viewer_button.click()
        opened.assert_called_once_with(QtCore.QUrl.fromLocalFile(str(self.folder / gui.VIEWER_NAME)))
        # It stays readable while a run works, and is the new page once the run has replaced it.
        with mock.patch.object(transcribe, "Whisper", mock.Mock(return_value=FakeWhisper())):
            window.start_button.click()
            self.assertTrue(window.viewer_button.isEnabled())
            self.assertFalse(window.open_button.isEnabled())
            self.finish()
        self.assertTrue(window.viewer_button.isEnabled())
        self.assertEqual([voice["text"] for voice in voices(model_in(self.folder / gui.VIEWER_NAME))], ["part 1"])
        window.set_folder(self.root / "elsewhere")
        self.assertFalse(window.viewer_button.isEnabled())
        with mock.patch.object(QtGui.QDesktopServices, "openUrl") as opened:
            window.viewer_button.click()
        opened.assert_not_called()

    def test_viewing_without_transcribing_shows_the_summary_and_enables_the_viewer(self):
        window = self.window
        window.set_source(self.export)
        # Whatever the transcription boxes hold is beside the point.
        window.language.setCurrentText("Klingon")
        with mock.patch.object(transcribe, "transcribe_export", side_effect=AssertionError("Nothing may be transcribed")) as run:
            window.view_button.click()
            self.assertEqual(window.status.text(), "Reading the conversation…")
            for widget in (window.start_button, window.stop_button, window.view_button, window.output_button):
                self.assertFalse(widget.isEnabled())
            # There are no steps to count, so the bar only shows that the window is busy.
            self.assertEqual((window.progress.minimum(), window.progress.maximum()), (0, 0))
            self.finish()
        run.assert_not_called()
        lines = window.text.toPlainText().splitlines()
        for line in ("Parsed 2 messages from 2026-10-01 to 2026-10-01", "Voice messages: 1 detected, 1 with a recording, 1 with a duration.",
                     "1 voice message has no transcript: transcribe it to count its words", f"Viewer: {self.folder / gui.VIEWER_NAME}"):
            self.assertIn(line, lines)
        self.assertNotRegex(window.text.toPlainText(), COMMAND_WORDS)
        self.assertEqual(window.status.text(), f"Viewer saved in {self.folder}")
        self.assertEqual((window.progress.value(), window.progress.maximum()), (1, 1))
        self.assertEqual(names(self.folder), ["conversation.html", "conversation_audio"])
        self.assertEqual([voice["status"] for voice in voices(model_in(self.folder / gui.VIEWER_NAME))], ["pending"])
        for widget in (window.viewer_button, window.open_button, window.start_button, window.view_button):
            self.assertTrue(widget.isEnabled())
        self.assertFalse(window.stop_button.isEnabled())
        # Nothing was transcribed, so there is nothing for a run to continue from.
        self.assertTrue(window.fresh.isHidden())
        # After a run the same button shows the transcripts that are there now.
        window.language.setCurrentText(gui.AUTOMATIC)
        self.run_transcription()
        self.run_view()
        lines = window.text.toPlainText().splitlines()
        self.assertIn("Using transcripts.json: 1 of 1 voice messages matched", lines)
        self.assertIn("Voice messages: 1 detected, 1 with a recording, 1 with a duration.", lines)
        self.assertFalse(any("have no transcript" in line for line in lines))
        self.assertEqual(window.folder_note.text(), "This folder already holds 1 transcript; it will be reused.")

    def test_view_that_fails_is_explained_and_leaves_the_window_usable(self):
        window = self.window
        empty = self.root / "empty"
        empty.mkdir()
        window.set_source(empty)
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog:
            self.run_view()
        self.assertEqual(dialog.call_args.args[2], "No WhatsApp chat was found. Choose an export ZIP, its folder or the chat .txt.")
        self.assertEqual(window.status.text(), "Could not write the viewer.")
        self.assertEqual((window.progress.value(), window.progress.maximum()), (0, 1))
        self.assertTrue(window.view_button.isEnabled() and window.start_button.isEnabled())
        self.assertFalse(window.viewer_button.isEnabled() or window.open_button.isEnabled())

    def test_viewer_problem_after_a_run_is_shown_and_the_transcripts_are_kept(self):
        window = self.window
        window.set_source(self.export)
        with mock.patch.object(stats, "write_conversation", side_effect=OSError("No space left on device")), \
                mock.patch.object(QtWidgets.QMessageBox, "warning") as dialog:
            self.run_transcription()
        self.assertEqual(dialog.call_args.args[2], "The transcripts are saved, but the viewer could not be written: No space left on device")
        self.assertIn("Transcribed 1 of 1 voice messages", window.status.text())
        self.assertIn("[Voice message transcript: part 1]", window.text.toPlainText())
        self.assertFalse(window.viewer_button.isEnabled())
        self.assertTrue(window.open_button.isEnabled())
        self.assertEqual(window.folder_note.text(), "This folder already holds 1 transcript; it will be reused.")

    def test_earlier_transcripts_row_takes_a_report_and_the_run_reuses_it(self):
        window = self.window
        window.set_source(self.export)
        report = save_report(self.root / "downloads" / "voxpad.json", [result("WhatsApp Chat/voice.wav", "desde antes")])
        # Anything that is not a report is turned down when it is chosen, not when the run starts.
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog:
            self.choose(window.reuse_button, self.export / "chat.txt")
        self.assertEqual(dialog.call_args.args[2], "chat.txt is not a VoxPad transcript report.")
        self.assertEqual((window.reuse, window.reuse_box.text(), window.reuse_clear.isEnabled()), (None, "", False))
        self.choose(window.reuse_button, report)
        self.assertEqual((window.reuse, window.reuse_box.text(), window.reuse_clear.isEnabled()), (report, str(report), True))
        self.assertEqual(window.status.text(), "voxpad.json holds 1 transcript.")
        # Closing the dialog without choosing keeps what was chosen.
        self.choose(window.reuse_button, "")
        self.assertEqual(window.reuse, report)
        constructor = mock.Mock(return_value=FakeWhisper())
        with mock.patch.object(transcribe, "Whisper", constructor):
            window.start_button.click()
            self.assertFalse(window.reuse_clear.isEnabled())
            self.finish()
        constructor.assert_not_called()
        self.assertIn("voice.wav (archivo adjunto)\n[Voice message transcript: desde antes]\n", window.text.toPlainText())
        self.assertEqual(window.status.text(), f"Transcribed 1 of 1 voice messages, 1 reused. Saved in {self.folder}")
        self.assertEqual(window.reuse_box.text(), str(report))
        window.reuse_clear.click()
        self.assertEqual((window.reuse, window.reuse_box.text(), window.reuse_clear.isEnabled()), (None, "", False))
        # A report that went away after it was chosen stops the run in the window.
        self.choose(window.reuse_button, report)
        report.unlink()
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog:
            window.start_button.click()
            self.assertIsNone(window.job)
            window.view_button.click()
            self.assertIsNone(window.job)
        self.assertEqual([call.args[2] for call in dialog.call_args_list], [f"The earlier transcripts no longer exist: {report}"] * 2)

    def test_events_row_takes_a_file_of_dated_events_and_the_viewer_shows_them(self):
        window = self.window
        window.set_source(self.export)
        marked = self.root / "events.txt"
        marked.write_text("# what happened\n2026-10-01, Ana llega\nsin fecha\n", encoding="utf-8")
        undated = self.root / "notes.txt"
        undated.write_text("nothing here has a date\n", encoding="utf-8")
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog:
            self.choose(window.events_button, undated)
            self.choose(window.events_button, self.root / "missing.txt")
        self.assertEqual(dialog.call_args_list[0].args[2], "notes.txt holds no dated event. Write one per line, for example: 2024-01-15, label")
        self.assertEqual(dialog.call_count, 2)
        self.assertEqual((window.events_file, window.events_box.text(), window.events_clear.isEnabled()), (None, "", False))
        self.choose(window.events_button, marked)
        self.assertEqual((window.events_file, window.events_box.text(), window.events_clear.isEnabled()), (marked, str(marked), True))
        self.assertEqual(window.status.text(), "Read 1 event from events.txt; 1 line without a valid date will be skipped.")
        self.run_transcription()
        self.assertEqual(model_in(self.folder / gui.VIEWER_NAME)["events"], [{"date": "2026-10-01", "label": "Ana llega"}])
        self.run_view()
        self.assertIn("Events: 1 shown", window.text.toPlainText().splitlines())
        self.assertIn("Events line 3 has no valid date and was skipped.", window.text.toPlainText().splitlines())
        window.events_clear.click()
        self.assertEqual((window.events_file, window.events_box.text(), window.events_clear.isEnabled()), (None, "", False))
        self.run_view()
        self.assertEqual(model_in(self.folder / gui.VIEWER_NAME)["events"], [])
        self.choose(window.events_button, marked)
        marked.unlink()
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog:
            window.view_button.click()
        self.assertIsNone(window.job)
        self.assertEqual(dialog.call_args.args[2], f"The events file no longer exists: {marked}")

    def test_playback_box_decides_whether_the_recordings_go_with_the_viewer(self):
        window = self.window
        window.set_source(self.export)
        window.playback.click()
        self.assertFalse(window.playback.isChecked())
        self.run_transcription()
        self.assertEqual(names(self.folder), [gui.CHAT_NAME, "conversation.html", gui.REPORT_NAME, "transcripts.txt"])
        self.assertEqual([voice["src"] for voice in voices(model_in(self.folder / gui.VIEWER_NAME))], [None])
        window.playback.click()
        self.run_view()
        self.assertEqual(names(self.folder / "conversation_audio"), ["voice.wav"])
        self.assertEqual([voice["src"] for voice in voices(model_in(self.folder / gui.VIEWER_NAME))], ["./conversation_audio/voice.wav"])
        self.assertIn("Keep conversation_audio/ beside it: the voice messages play from there.", window.text.toPlainText().splitlines())

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
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog, mock.patch.object(transcribe, "Whisper", mock.Mock()):
            window.start_button.click()
            self.finish()
        self.assertIn("No audio files found", dialog.call_args.args[2])
        self.assertTrue(window.start_button.isEnabled())
        self.assertFalse(window.open_button.isEnabled())
        self.assertFalse(window.viewer_button.isEnabled())
        # An export that went away since it was chosen is noticed before anything starts.
        empty.rmdir()
        with mock.patch.object(QtWidgets.QMessageBox, "critical") as dialog:
            window.start_button.click()
            window.view_button.click()
        self.assertIsNone(window.job)
        self.assertEqual([call.args[2] for call in dialog.call_args_list], [f"The export no longer exists: {empty}"] * 2)

    def test_closing_while_working_asks_first(self):
        window = self.window
        window.set_source(self.export)
        buttons = QtWidgets.QMessageBox.StandardButton
        window.job = gui.Job(self.export, self.folder, events=window.events, view_only=True)
        with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=buttons.No) as question:
            self.assertFalse(window.close())
        self.assertEqual(question.call_args.args[2], "The viewer is being written. Close anyway?")
        window.job = gui.Job(self.export, self.folder, events=window.events)
        with mock.patch.object(QtWidgets.QMessageBox, "question", return_value=buttons.Yes) as question:
            self.assertTrue(window.close())
        self.assertEqual(question.call_args.args[2], "A transcription is running. Close anyway?")
        window.job = None

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
                mock.patch.object(transcribe, "Whisper", local), \
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
            # Viewing uploads nothing, so it needs no token even with a service chosen.
            window.view_button.click()
            self.finish()
            self.assertEqual((service.lookups, service.clients), ([], []))
            self.assertEqual(window.status.text(), f"Viewer saved in {self.folder}")
            window.token.setText(" hf_typed ")
            window.start_button.click()
            self.assertFalse(window.place.isEnabled())
            self.assertFalse(window.token.isEnabled())
            self.finish()
        local.assert_not_called()
        self.assertEqual(service.clients[0]["token"], "hf_typed")
        self.assertEqual((service.calls[0]["model"], service.calls[0]["extra_body"]),
                         ("openai/whisper-large-v3-turbo", {"language": "es"}))
        refusal.assert_called_once()
        self.assertIn("Credits used up", refusal.call_args.args[2])
        self.assertIn("stopped early", window.status.text())
        self.assertTrue(window.place.isEnabled())
        self.assertTrue(window.token.isEnabled())
        self.assertNotIn("hf_typed", window.text.toPlainText() + window.status.text())
        self.assertNotIn(b"hf_typed", b"".join(path.read_bytes() for path in self.folder.rglob("*") if path.is_file()))


if __name__ == "__main__":
    unittest.main()
