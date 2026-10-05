"""Tests for the voxpad command; they run without downloading Whisper models or invoking inference."""

from contextlib import redirect_stderr, redirect_stdout
import builtins
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import unittest
from unittest import mock

from voxpad import cli, transcribe
from tests.support import ExportTestCase, FakeService, FakeWhisper, codecpod, http_error, np, write_wav


class CommandLineTests(ExportTestCase):
    def test_dry_run_never_imports_backend_or_decoder_and_writes_no_reports(self):
        self.touch("voice.opus")
        self.touch("chat.txt", b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")
        output = self.root / "results.json"
        annotated = self.root / "annotated.txt"
        constructor = mock.Mock(side_effect=AssertionError("Whisper must not load"))
        stdout = io.StringIO()
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"faster_whisper", "ctranslate2", "codecpod", "numpy", "huggingface_hub"}:
                raise AssertionError(f"Dry run must not import {name}")
            return original_import(name, *args, **kwargs)

        with mock.patch.object(transcribe, "Whisper", constructor), \
                mock.patch.object(builtins, "__import__", side_effect=guarded_import), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("Dry run must not invoke subprocesses")), \
                redirect_stdout(stdout):
            code = cli.main([str(self.root), "--dry-run", "-o", str(output), "--chat-output", str(annotated)])
        self.assertEqual(code, 0)
        constructor.assert_not_called()
        self.assertIn("voice.opus", stdout.getvalue())
        self.assertIn("Ana", stdout.getvalue())
        self.assertFalse(output.exists())
        self.assertFalse(output.with_suffix(".txt").exists())
        self.assertFalse(annotated.exists())

    def test_output_collisions_never_overwrite_original_chat_or_audio(self):
        self.touch("voice.opus")
        self.touch("other.txt", b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")
        protected = self.touch("protected.txt", b"01/10/26, 10:01 - Bob: Original message\n")
        empty_chat = self.touch("_chat.txt", b"")
        cases = (
            ["-o", str(self.root / "protected.json")],
            ["--chat-output", str(protected)],
            ["--chat-output", str(self.root / "voice.opus")],
            ["--chat-output", str(empty_chat)],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    code = cli.main([str(self.root), "--dry-run", *arguments])
                self.assertEqual(code, 1)
                self.assertIn("must not overwrite an original", stderr.getvalue())
                self.assertEqual(protected.read_bytes(), b"01/10/26, 10:01 - Bob: Original message\n")
                self.assertEqual((self.root / "voice.opus").read_bytes(), b"audio")
                self.assertEqual(empty_chat.read_bytes(), b"")

    def test_report_companion_symlink_cannot_overwrite_original_chat(self):
        self.touch("voice.opus")
        original = self.touch("chat.txt", b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")
        companion = self.root / "transcripts.txt"
        self.symlink(companion, original)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = cli.main([str(self.root), "--dry-run", "-o", str(self.root / "transcripts.json")])
        self.assertEqual(code, 1)
        self.assertIn("must not overwrite an original", stderr.getvalue())
        self.assertEqual(original.read_bytes(), b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")

    def test_existing_report_and_unrelated_text_do_not_count_as_extra_chats(self):
        self.touch("voice.opus")
        self.touch("chat.txt", b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")
        self.touch("transcripts.txt", b"voice.opus\n  01/10/26, 10:00 | Ana\nPrevious transcript\n")
        self.touch("notes.txt", b"Notes about this export")
        with redirect_stdout(io.StringIO()):
            code = cli.main([
                str(self.root), "--dry-run", "-o", str(self.root / "transcripts.json"),
                "--chat-output", str(self.root / "annotated.txt"),
            ])
        self.assertEqual(code, 0)

    def test_invalid_chunk_sizes_are_rejected_before_loading_backend(self):
        audio = self.touch("voice.opus")
        for size in ("0", "-1", "30.01", "nan", "inf"):
            with self.subTest(size=size), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.main([str(audio), "--dry-run", f"--chunk-seconds={size}"])
                self.assertEqual(error.exception.code, 2)

    def test_remote_options_the_service_cannot_honor_are_rejected_before_any_work(self):
        audio = self.touch("voice.opus")
        folder = self.root / "converted-model"
        folder.mkdir()
        cases = (
            ["--remote", "deepinfra", "--keyword", "José"],
            ["--remote", "deepinfra", "--word-timestamps"],
            ["--remote", "hf-inference", "--language", "es"],
            ["--remote", "deepinfra", "--model", str(folder)],
            # A URL or a path would be used as the address to upload to.
            ["--remote", "hf-inference", "--model", "http://127.0.0.1:8000/elsewhere"],
            ["--remote", "deepinfra", "--model", "https://example.org/whisper"],
            ["--remote", "deepinfra", "--model", "some/deep/path"],
            ["--remote", "elsewhere"],
        )
        constructor = mock.Mock(side_effect=AssertionError("Nothing may be uploaded"))
        with mock.patch.object(transcribe, "RemoteWhisper", constructor):
            for arguments in cases:
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        cli.main([str(audio), *arguments])
                    self.assertEqual(error.exception.code, 2)
            # Callers of the function get the same answer as the command line.
            with self.assertRaisesRegex(ValueError, "--keyword is not available"):
                transcribe.transcribe_export(audio, self.root / "transcripts.json", remote="deepinfra", keywords=["José"])
            # A dry run lists the recordings without a token or a connection.
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([str(audio), "--remote", "deepinfra", "--language", "es", "--dry-run"]), 0)
        constructor.assert_not_called()

    def save_report(self, relative, results, model="Whisper small"):
        return self.touch(relative, json.dumps({"source": "export", "model": model, "results": results}).encode("utf-8"))

    def test_reuse_must_name_an_existing_file_that_this_run_does_not_write(self):
        audio = self.touch("voice.opus")
        report = self.save_report("earlier.json", [])
        companion = self.touch("results.txt", b"{}")
        cases = (
            ["--reuse", str(self.root / "missing.json")],
            ["--reuse", str(self.root)],
            # Read first and replaced afterwards, the file would be lost.
            ["--reuse", str(report), "--chat-output", str(report)],
            ["--reuse", str(companion), "-o", str(self.root / "results.json")],
            ["--reuse"],
        )
        with mock.patch.object(transcribe, "transcribe_export", side_effect=AssertionError("Nothing may be read or written")):
            for arguments in cases:
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        cli.main([str(audio), "--dry-run", *arguments])
                    self.assertEqual(error.exception.code, 2)
        # The report at the output may be named as well: it is continued either way.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([str(audio), "--dry-run", "-o", str(report), "--reuse", str(report), "--reuse", str(report)]), 0)
            self.assertEqual(cli.main([str(audio), "--dry-run", "-o", str(report), "--reuse", str(report), "--fresh"]), 0)

    def test_dry_run_marks_the_recordings_an_earlier_report_holds(self):
        for name in ("a.opus", "b.opus"):
            self.touch(f"export/{name}")
        self.touch("export/chat.txt", b"01/10/26, 10:00 - Ana: a.opus (attached)\n01/10/26, 10:01 - Bob: b.opus (attached)\n")
        report = self.save_report("earlier/transcripts.json", [{"file": "b.opus", "messages": [], "status": "ok", "text": "dos"}])
        output = self.root / "results/transcripts.json"
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(transcribe, "Whisper", side_effect=AssertionError("Whisper must not load")), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main([str(self.root / "export"), "--dry-run", "-o", str(output), "--reuse", str(report)])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "a.opus\n  01/10/26, 10:00 | Ana\nb.opus\n  01/10/26, 10:01 | Bob\n  (already transcribed)\n")
        self.assertEqual(stderr.getvalue(), "Reusing 1 transcript from transcripts.json (made with Whisper small); 1 left to transcribe.\n"
                                            "1 earlier transcript was matched by file name only.\n")
        self.assertFalse(output.parent.exists())
        # A file that is not a report is named, without the folder it is in.
        notes = self.touch("private folder/notes.json", b"[]")
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = cli.main([str(self.root / "export"), "--dry-run", "-o", str(output), "--reuse", str(notes)])
        self.assertEqual(code, 1)
        self.assertTrue(stderr.getvalue().endswith("Error: notes.json is not a VoxPad transcript report.\n"))

    def test_a_complete_report_is_imported_without_whisper_a_token_or_a_connection(self):
        archive = self.make_zip([
            ("Chat/_chat.txt", "[01/10/26, 10:00:00] Ana: <attached: a.opus>\n[01/10/26, 10:01:00] José: <attached: b.opus>\n"),
            ("Chat/a.opus", b"one"),
            ("Chat/b.opus", b"three"),
        ])
        report = self.save_report("earlier/transcripts.json", [
            {"file": name, "messages": [], "status": "ok", "text": text, "languages": ["es"], "duration_seconds": 2.5, "segments": []}
            for name, text in (("a.opus", "uno"), ("b.opus", "dos"))
        ])
        output = self.root / "results/transcripts.json"
        annotated = self.root / "results/annotated.txt"
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"faster_whisper", "ctranslate2", "codecpod", "numpy", "huggingface_hub"}:
                raise AssertionError(f"Nothing may import {name}")
            return original_import(name, *args, **kwargs)

        for number, remote in enumerate(([], ["--remote", "deepinfra"])):
            with self.subTest(remote=remote):
                engine = mock.Mock(side_effect=AssertionError("No model may be loaded and no service asked"))
                stdout, stderr = io.StringIO(), io.StringIO()
                arguments = [str(archive), "-o", str(output), "--chat-output", str(annotated), "--reuse", str(report), *remote]
                with mock.patch.object(transcribe, "Whisper", engine), mock.patch.object(transcribe, "RemoteWhisper", engine), \
                        mock.patch.object(builtins, "__import__", side_effect=guarded_import), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(arguments)
                self.assertEqual(code, 0, stderr.getvalue())
                engine.assert_not_called()
                self.assertEqual(stdout.getvalue().splitlines(), [
                    f"Saved 2 transcripts (2 reused, 0 failed) to {output.resolve()} and {output.resolve().with_suffix('.txt')}",
                    f"Annotated chat: {annotated.resolve()}",
                    f"View it: voxpad-stats {cli.shell_word(archive)} --transcripts {cli.shell_word(output)}",
                ])
                # The first run takes the transcripts from the report given. The second finds them at the output,
                # and names the folder because both reports have the same name.
                self.assertEqual(stderr.getvalue().splitlines()[0], (
                    "Reusing 2 transcripts from results/transcripts.json (made with Whisper small); 0 left to transcribe. "
                    "--fresh transcribes everything again."
                ) if number else "Reusing 2 transcripts from transcripts.json (made with Whisper small); 0 left to transcribe.")
                self.assertEqual([result["text"] for result in json.loads(output.read_text(encoding="utf-8"))["results"]], ["uno", "dos"])
                self.assertEqual(annotated.read_text(encoding="utf-8").count("[Voice message transcript: "), 2)

    def test_an_output_holding_another_export_is_kept_unless_fresh_is_given(self):
        audio = self.touch("voice.opus")
        output = self.save_report("results/transcripts.json", [{"file": "other.opus", "messages": [], "status": "ok", "text": "another chat"}])
        before = output.read_bytes()
        stderr = io.StringIO()
        with mock.patch.object(transcribe, "Whisper", side_effect=AssertionError("Whisper must not load")), redirect_stderr(stderr):
            code = cli.main([str(audio), "-o", str(output)])
        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "Error: transcripts.json holds 1 transcript of a recording that is not in this export. "
                                            "Choose another --output, or pass --fresh to replace it.\n")
        self.assertEqual(output.read_bytes(), before)
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main([str(audio), "-o", str(output), "--fresh", "--dry-run"]), 0)
        self.assertEqual(stdout.getvalue(), "voice.opus\n")

    def test_viewer_options_are_checked_before_any_work(self):
        audio = self.touch("voice.opus")
        events = self.touch("events.html", b"2026-01-10, First call\n")
        page = self.root / "out" / "conversation.html"
        cases = (
            ["--events", str(events)],
            ["--audio", "copy"],
            ["--viewer", str(self.root / "conversation.htm")],
            ["--viewer", str(self.root / "notes.txt")],
            ["--viewer", str(page), "--events", str(self.root / "missing.txt")],
            ["--viewer", str(page), "--audio", "embed"],
            # Read first and replaced afterwards, the file would be lost.
            ["--viewer", str(self.root / "earlier.html"), "--reuse", str(self.root / "earlier.html")],
            ["--viewer", str(events), "--events", str(events)],
            # The page would replace the annotated chat written just before it.
            ["--viewer", str(page), "--chat-output", str(page)],
            ["--viewer"],
        )
        with mock.patch.object(transcribe, "transcribe_export", side_effect=AssertionError("Nothing may be read or written")):
            for arguments in cases:
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        cli.main([str(audio), *arguments])
                    self.assertEqual(error.exception.code, 2)
        self.assertEqual(events.read_bytes(), b"2026-01-10, First call\n")
        self.assertFalse(page.parent.exists())

    def test_viewer_needs_one_chat_and_says_so_before_anything_is_transcribed(self):
        self.touch("export/a.opus")
        self.touch("export/chat.txt", b"01/10/26, 10:00 - Ana: a.opus (attached)\n")
        other = self.touch("export/other.txt", b"02/10/26, 10:00 - Li: Another chat\n")
        page = self.root / "out" / "conversation.html"
        needs_one = "Error: --viewer requires exactly one readable UTF-8 chat .txt; use a chat .txt as the input to select it.\n"
        engine = mock.Mock(side_effect=AssertionError("No model may be loaded"))
        with mock.patch.object(transcribe, "Whisper", engine):
            # Two chats, and a recording that belongs to no chat at all; the same without --dry-run.
            for arguments in ([str(self.root / "export"), "--dry-run"], [str(self.root / "export" / "a.opus"), "--dry-run"], [str(self.root / "export")]):
                with self.subTest(arguments=arguments):
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = cli.main([*arguments, "-o", str(self.root / "out" / "transcripts.json"), "--viewer", str(page)])
                    self.assertEqual((code, stdout.getvalue(), stderr.getvalue()), (1, "", needs_one))
            # With the chat chosen, a dry run lists the recordings and writes no page.
            other.unlink()
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = cli.main([str(self.root / "export"), "--dry-run", "-o", str(self.root / "out" / "transcripts.json"), "--viewer", str(page)])
            self.assertEqual((code, stdout.getvalue()), (0, "a.opus\n  01/10/26, 10:00 | Ana\n"))
        engine.assert_not_called()
        self.assertFalse(page.parent.exists())

    def test_viewer_is_written_from_the_report_of_the_run_with_the_recordings_beside_it(self):
        archive = self.make_zip([
            ("Chat/_chat.txt", "[01/10/26, 10:00:00] Ana: <attached: a.opus>\n[01/10/26, 10:01:00] José: <attached: b.opus>\n"),
            ("Chat/a.opus", b"one"),
            ("Chat/b.opus", b"three"),
        ])
        report = self.save_report("earlier/transcripts.json", [
            {"file": name, "messages": [], "status": "ok", "text": text, "languages": ["es"], "duration_seconds": 2.5, "segments": []}
            for name, text in (("a.opus", "uno"), ("b.opus", "dos tres"))
        ])
        events = self.touch("events.txt", b"2026-10-01, First call\n")
        output = self.root / "results/transcripts.json"
        page = self.root / "results/conversation.html"
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"faster_whisper", "ctranslate2", "codecpod", "numpy", "huggingface_hub", "matplotlib"}:
                raise AssertionError(f"Nothing may import {name}")
            return original_import(name, *args, **kwargs)

        def run(*arguments):
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch.object(transcribe, "Whisper", side_effect=AssertionError("No model may be loaded")), \
                    mock.patch.object(builtins, "__import__", side_effect=guarded_import), redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main([str(archive), "-o", str(output), "--viewer", str(page), *arguments])
            return code, stdout.getvalue().splitlines(), stderr.getvalue().splitlines()

        def model():
            return json.loads(re.search(r'<script type="application/json" id="voxpad-model">(.*?)</script>', page.read_text(encoding="utf-8"))[1])

        saved = f"Saved 2 transcripts (2 reused, 0 failed) to {output.resolve()} and {output.resolve().with_suffix('.txt')}"
        code, stdout, stderr = run("--reuse", str(report), "--events", str(events))
        self.assertEqual(code, 0, stderr)
        # The page takes the place of the hint that says how to make one.
        self.assertEqual(stdout, [
            saved,
            f"Viewer: {page.resolve()}",
            "Open it in a browser; it needs no connection.",
            "Keep conversation_audio/ beside it: the voice messages play from there.",
            "conversation.html contains the whole conversation and its transcripts.",
        ])
        self.assertEqual(stderr, [
            "Reusing 2 transcripts from transcripts.json (made with Whisper small); 0 left to transcribe.",
            "2 earlier transcripts were matched by file name only.",
            "Using transcripts.json: 2 of 2 voice messages matched",
            "Copied 2 voice messages (1 KiB) to conversation_audio/ so the viewer can play them; --audio none skips this.",
        ])
        self.assertEqual(sorted(path.relative_to(self.root).as_posix() for path in (self.root / "results").rglob("*") if path.is_file()), [
            "results/conversation.html", "results/conversation_audio/a.opus", "results/conversation_audio/b.opus",
            "results/transcripts.json", "results/transcripts.txt",
        ])
        self.assertEqual((page.parent / "conversation_audio" / "b.opus").read_bytes(), b"three")
        self.assertEqual([(message["voice"]["src"], message["voice"]["text"], message["voice"]["seconds"]) for message in model()["messages"]],
                         [("./conversation_audio/a.opus", "uno", 2.5), ("./conversation_audio/b.opus", "dos tres", 2.5)])
        self.assertEqual(model()["events"], [{"date": "2026-10-01", "label": "First call"}])
        self.assertNotIn(str(self.root.resolve()), page.read_text(encoding="utf-8"))
        # The second run finds everything at the output, and --audio none leaves the recordings out of the page.
        code, stdout, stderr = run("--audio", "none")
        self.assertEqual((code, stdout[1:]), (0, [f"Viewer: {page.resolve()}", "Open it in a browser; it needs no connection.",
                                                  "conversation.html contains the whole conversation and its transcripts."]))
        self.assertEqual([message["voice"]["src"] for message in model()["messages"]], [None, None])
        self.assertEqual(model()["events"], [])
        # A page that cannot be written fails the command; the transcripts are saved all the same.
        before = page.read_bytes()
        code, stdout, stderr = run("--audio", "link")
        self.assertEqual((code, stdout), (1, [saved]))
        self.assertEqual(stderr[-1], "Error: --audio link cannot be used with a ZIP: its recordings exist only while it is read. Use --audio copy.")
        self.assertEqual(page.read_bytes(), before)
        self.assertEqual([result["text"] for result in json.loads(output.read_text(encoding="utf-8"))["results"]], ["uno", "dos tres"])

    def test_paths_in_the_suggested_command_can_be_pasted(self):
        self.assertEqual(cli.shell_word(Path("transcripts.json")), "transcripts.json")
        quoted = cli.shell_word(Path("WhatsApp Chat with Ana.zip"))
        self.assertNotEqual(quoted, "WhatsApp Chat with Ana.zip")
        if os.name != "nt":
            self.assertEqual(shlex.split(f"voxpad-stats {quoted} --transcripts {cli.shell_word(Path('it is.json'))}"),
                             ["voxpad-stats", "WhatsApp Chat with Ana.zip", "--transcripts", "it is.json"])


@unittest.skipUnless(codecpod is not None and np is not None, "Install codecpod and NumPy for audio conversion tests")
class RunTests(ExportTestCase):
    """Whole runs with real decoding; Whisper and the hosted service are replaced by fakes."""

    def test_main_writes_unicode_reports_and_annotated_chat_while_preserving_failures(self):
        audio = self.root / "voice.wav"
        write_wav(audio)
        (self.root / "broken.opus").write_bytes(b"invalid")
        chat = self.root / "chat.txt"
        original = (
            "﻿01/10/26, 10:00 - José: voice.wav (archivo adjunto)\r\n"
            "01/10/26, 10:01 - Ana: broken.opus (attached)\r\n"
        )
        chat.write_bytes(original.encode("utf-8"))
        output = self.root / "reports/results.json"
        annotated = self.root / "reports/annotated.txt"
        model = FakeWhisper()
        constructor = mock.Mock(return_value=model)
        with mock.patch.object(transcribe, "Whisper", constructor), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.main([str(chat), "-o", str(output), "--chat-output", str(annotated), "--language", "es"])
        self.assertEqual(code, 1)
        constructor.assert_called_once()
        report = json.loads(output.read_text(encoding="utf-8"))
        results = {result["file"]: result for result in report["results"]}
        # Reports name the input but never say where it is stored.
        self.assertEqual(report["source"], "chat.txt")
        constructor.assert_called_once_with("large-v3-turbo")
        self.assertEqual(report["model"], "Whisper large-v3-turbo")
        self.assertNotIn(str(self.root), output.read_text(encoding="utf-8"))
        self.assertNotIn(str(self.root), output.with_suffix(".txt").read_text(encoding="utf-8"))
        self.assertEqual(results["voice.wav"]["text"], "part 1")
        self.assertEqual(results["voice.wav"]["messages"][0]["sender"], "José")
        self.assertEqual(results["broken.opus"]["status"], "error")
        self.assertIn("decode", results["broken.opus"]["error"].lower())
        self.assertIn("José", output.with_suffix(".txt").read_text(encoding="utf-8"))
        annotated_text = annotated.read_bytes().decode("utf-8")
        # The original text, BOM and CRLF included, survives around the inserted lines.
        first, second = original.splitlines(keepends=True)
        self.assertTrue(annotated_text.startswith(
            f"{first}[Voice message transcript: part 1]\r\n{second}[Voice message transcript: Error:"
        ))
        self.assertTrue(annotated_text.endswith("]\r\n"))

    def test_a_model_the_service_lacks_ends_the_run_before_an_earlier_report_is_replaced(self):
        audio = self.root / "voice.wav"
        write_wav(audio)
        output = self.root / "reports/transcripts.json"
        output.parent.mkdir()
        output.write_text("an earlier report", encoding="utf-8")
        service = FakeService(offers=())
        stderr = io.StringIO()
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}), redirect_stderr(stderr):
            code = cli.main([str(audio), "-o", str(output), "--model", "small", "--remote", "deepinfra"])
        self.assertEqual(code, 1)
        self.assertIn("does not offer openai/whisper-small", stderr.getvalue())
        self.assertEqual(output.read_text(encoding="utf-8"), "an earlier report")
        self.assertEqual([path.name for path in output.parent.iterdir()], ["transcripts.json"])
        self.assertEqual(service.calls, [])

    def test_main_uploads_only_with_remote_and_stops_when_the_service_refuses(self):
        for name in ("a.wav", "b.wav", "c.wav"):
            write_wav(self.root / name)
        chat = self.root / "chat.txt"
        chat.write_text(
            "01/10/26, 10:00 - Ana: a.wav (attached)\n"
            "01/10/26, 10:01 - Bob: b.wav (attached)\n"
            "01/10/26, 10:02 - Eva: c.wav (attached)\n",
            encoding="utf-8",
        )
        output = self.root / "reports/results.json"
        annotated = self.root / "reports/annotated.txt"
        service = FakeService(outcomes=["Hola", http_error(402, "Credits used up")])
        local = mock.Mock(side_effect=AssertionError("Whisper must not run on this computer"))
        stderr = io.StringIO()
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}), \
                mock.patch.object(transcribe, "Whisper", local), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = cli.main([
                str(chat), "-o", str(output), "--chat-output", str(annotated), "--language", "es", "--remote", "deepinfra",
            ])
        self.assertEqual(code, 1)
        local.assert_not_called()
        # The third recording is never uploaded once the second is refused.
        self.assertEqual([(call["model"], call["extra_body"]) for call in service.calls],
                         [("openai/whisper-large-v3-turbo", {"language": "es"})] * 2)
        written = output.read_text(encoding="utf-8")
        report = json.loads(written)
        self.assertEqual(report["model"], "openai/whisper-large-v3-turbo on DeepInfra through Hugging Face")
        self.assertEqual([(result["file"], result["status"]) for result in report["results"]],
                         [("a.wav", "ok"), ("b.wav", "error")])
        self.assertEqual((report["results"][0]["text"], report["results"][0]["languages"]), ("Hola", ["es"]))
        self.assertIn("Credits used up", report["results"][1]["error"])
        self.assertIn("Uploading the audio to DeepInfra through Hugging Face", stderr.getvalue())
        self.assertTrue(stderr.getvalue().endswith("Stopped after 2 of 3. Run the same command again to continue.\n"))
        # The token is used to connect and appears nowhere else.
        for text in (written, output.with_suffix(".txt").read_text(encoding="utf-8"), stderr.getvalue()):
            self.assertNotIn("hf_secret", text)
        self.assertEqual(annotated.read_text(encoding="utf-8").splitlines(), [
            "01/10/26, 10:00 - Ana: a.wav (attached)",
            "[Voice message transcript: Hola]",
            "01/10/26, 10:01 - Bob: b.wav (attached)",
            "[Voice message transcript: Error: DeepInfra through Hugging Face did not transcribe the audio: Credits used up]",
            "01/10/26, 10:02 - Eva: c.wav (attached)",
        ])

        # The same command continues: the transcript is kept, the refused recording is tried again.
        service = FakeService(outcomes=["Qué tal", "Adiós"])
        stdout, stderr = io.StringIO(), io.StringIO()
        arguments = [str(chat), "-o", str(output), "--chat-output", str(annotated), "--language", "es", "--remote", "deepinfra"]
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}), mock.patch.object(transcribe, "Whisper", local), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(arguments)
        self.assertEqual(code, 0)
        self.assertEqual(len(service.calls), 2)
        self.assertEqual(stderr.getvalue().splitlines(), [
            "Reusing 1 transcript from results.json (made with openai/whisper-large-v3-turbo on DeepInfra through Hugging Face); "
            "2 left to transcribe. --fresh transcribes everything again.",
            "Uploading the audio to DeepInfra through Hugging Face to transcribe it with openai/whisper-large-v3-turbo...",
            "[2/3] b.wav",
            "[3/3] c.wav",
        ])
        self.assertEqual(stdout.getvalue().splitlines(), [
            f"Saved 3 transcripts (1 reused, 0 failed) to {output.resolve()} and {output.resolve().with_suffix('.txt')}",
            f"Annotated chat: {annotated.resolve()}",
            f"View it: voxpad-stats {cli.shell_word(chat)} --transcripts {cli.shell_word(output)}",
        ])
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual([(result["file"], result["status"], result["text"]) for result in report["results"]],
                         [("a.wav", "ok", "Hola"), ("b.wav", "ok", "Qué tal"), ("c.wav", "ok", "Adiós")])
        self.assertEqual(annotated.read_text(encoding="utf-8").splitlines(), [
            "01/10/26, 10:00 - Ana: a.wav (attached)",
            "[Voice message transcript: Hola]",
            "01/10/26, 10:01 - Bob: b.wav (attached)",
            "[Voice message transcript: Qué tal]",
            "01/10/26, 10:02 - Eva: c.wav (attached)",
            "[Voice message transcript: Adiós]",
        ])
        # --fresh uploads every recording again.
        service = FakeService(outcomes=["uno", "dos", "tres"])
        stdout = io.StringIO()
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = cli.main([*arguments, "--fresh"])
        self.assertEqual((code, len(service.calls)), (0, 3))
        self.assertIn("Saved 3 transcripts (0 reused, 0 failed) to", stdout.getvalue())

    def test_ctrl_c_keeps_what_is_done_says_how_to_continue_and_exits_with_130(self):
        export = self.root / "export"
        export.mkdir()
        for name in ("a.wav", "b.wav", "c.wav"):
            write_wav(export / name, frames=1600)
        output = self.root / "results/transcripts.json"
        arguments = [str(export), "-o", str(output)]

        def interrupted_model():
            """A model that is interrupted, as by Ctrl-C, while it transcribes its second recording."""
            model = FakeWhisper()
            transcribe_chunk = model.transcribe

            def answer(samples, **options):
                if model.calls:
                    raise KeyboardInterrupt
                return transcribe_chunk(samples, **options)

            model.transcribe = answer
            return model

        def texts():
            return [(result["file"], result["text"]) for result in json.loads(output.read_text(encoding="utf-8"))["results"]]

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(transcribe, "Whisper", return_value=interrupted_model()), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(arguments)
            except KeyboardInterrupt:
                # Let through, it would end the whole test run instead of failing this test.
                self.fail("The interruption was passed on without saying how to continue")
        self.assertEqual(code, 130)
        self.assertTrue(stderr.getvalue().endswith("[2/3] b.wav\n\nStopped after 1 of 3. Run the same command again to continue.\n"))
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(texts(), [("a.wav", "part 1")])
        # The command itself exits with that status, and going on is not what --fresh does.
        stderr = io.StringIO()
        with mock.patch.object(transcribe, "Whisper", return_value=interrupted_model()), redirect_stderr(stderr), \
                mock.patch.object(sys, "argv", ["voxpad", *arguments, "--fresh"]), self.assertRaises(SystemExit) as exit_:
            cli.cli()
        self.assertEqual(exit_.exception.code, 130)
        self.assertTrue(stderr.getvalue().endswith("\nStopped after 1 of 3. Run the command again without --fresh to continue.\n"))
        self.assertEqual(texts(), [("a.wav", "part 1")])
        # An interruption before the first recording has nothing to count.
        stderr = io.StringIO()
        with mock.patch.object(transcribe, "Whisper", side_effect=KeyboardInterrupt), redirect_stderr(stderr), \
                mock.patch.object(sys, "argv", ["voxpad", *arguments]), self.assertRaises(SystemExit) as exit_:
            cli.cli()
        self.assertEqual(exit_.exception.code, 130)
        self.assertIn("Interrupted.", stderr.getvalue())
        self.assertNotIn("Stopped after", stderr.getvalue())
        self.assertEqual(texts(), [("a.wav", "part 1")])
        # Continued, the run transcribes what is left.
        stdout = io.StringIO()
        with mock.patch.object(transcribe, "Whisper", return_value=FakeWhisper()), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = cli.main(arguments)
        self.assertEqual(code, 0)
        self.assertEqual(texts(), [("a.wav", "part 1"), ("b.wav", "part 1"), ("c.wav", "part 2")])
        # Recordings without a chat have no conversation to view.
        self.assertEqual(stdout.getvalue().splitlines(), [
            f"Saved 3 transcripts (1 reused, 0 failed) to {output.resolve()} and {output.resolve().with_suffix('.txt')}",
        ])

    def test_a_stopped_run_leaves_a_viewer_of_what_is_done_and_the_next_run_completes_it(self):
        export = self.root / "export"
        export.mkdir()
        for name in ("a.wav", "b.wav", "c.wav"):
            write_wav(export / name, frames=1600)
        (export / "chat.txt").write_text("01/10/26, 10:00 - Ana: a.wav (attached)\n01/10/26, 10:01 - Bob: b.wav (attached)\n"
                                         "01/10/26, 10:02 - Eva: c.wav (attached)\n", encoding="utf-8")
        output = self.root / "results/transcripts.json"
        page = self.root / "results/conversation.html"
        # The annotated chat goes into the export itself: the page must not take it for a second chat.
        annotated = export / "chat_with_transcripts.txt"
        arguments = [str(export), "-o", str(output), "--viewer", str(page), "--chat-output", str(annotated)]
        model = FakeWhisper()
        transcribe_chunk = model.transcribe

        def answer(samples, **options):
            # Interrupted, as by Ctrl-C, during the second recording.
            if model.calls:
                raise KeyboardInterrupt
            return transcribe_chunk(samples, **options)

        model.transcribe = answer

        def voices():
            found = json.loads(re.search(r'<script type="application/json" id="voxpad-model">(.*?)</script>', page.read_text(encoding="utf-8"))[1])
            return [(message["voice"]["src"], message["voice"]["status"], message["voice"]["text"], message["voice"]["seconds"])
                    for message in found["messages"]]

        closing = [f"Viewer: {page.resolve()}", "Open it in a browser; it needs no connection.",
                   "Keep conversation_audio/ beside it: the voice messages play from there.",
                   "conversation.html contains the whole conversation and its transcripts."]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(transcribe, "Whisper", return_value=model), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(arguments)
            except KeyboardInterrupt:
                self.fail("The interruption was passed on without saying how to continue")
        self.assertEqual(code, 130)
        self.assertEqual(stdout.getvalue().splitlines(), closing)
        self.assertIn("[2/3] b.wav\n\nUsing transcripts.json: 1 of 3 voice messages matched\n", stderr.getvalue())
        self.assertTrue(stderr.getvalue().endswith("\nStopped after 1 of 3. Run the same command again to continue.\n"))
        # The recordings that were not reached are there to be heard, with their lengths, and marked as not transcribed.
        self.assertEqual(voices(), [("./conversation_audio/a.wav", "ok", "part 1", 0.1), ("./conversation_audio/b.wav", "pending", "", 0.1),
                                    ("./conversation_audio/c.wav", "pending", "", 0.1)])
        self.assertEqual(sorted(path.name for path in (page.parent / "conversation_audio").iterdir()), ["a.wav", "b.wav", "c.wav"])
        stdout = io.StringIO()
        with mock.patch.object(transcribe, "Whisper", return_value=FakeWhisper()), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = cli.main(arguments)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().splitlines(), [
            f"Saved 3 transcripts (1 reused, 0 failed) to {output.resolve()} and {output.resolve().with_suffix('.txt')}",
            f"Annotated chat: {annotated.resolve()}", *closing])
        self.assertEqual([voice[1:3] for voice in voices()], [("ok", "part 1"), ("ok", "part 1"), ("ok", "part 2")])
        self.assertEqual(annotated.read_text(encoding="utf-8").count("[Voice message transcript: part"), 3)


if __name__ == "__main__":
    unittest.main()
