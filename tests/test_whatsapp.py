"""Tests run without downloading Whistle models or invoking inference."""

from contextlib import redirect_stderr, redirect_stdout
import builtins
import errno
import io
import json
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import warnings
import wave
import zipfile

from voxpad import whatsapp as app


try:
    import codecpod
    import numpy as np
except ImportError:
    codecpod = None
    np = None


def write_wav(path, *, frames=16000, sample_rate=16000, channels=1):
    """Write recognizable PCM samples so lost or duplicated chunks are detectable."""
    pattern = struct.pack("<7h", 0, 1, -1, 32767, -32768, 12345, -12345)
    size = frames * channels * 2
    audio = (pattern * ((size + len(pattern) - 1) // len(pattern)))[:size]
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(channels)
        recording.setsampwidth(2)
        recording.setframerate(sample_rate)
        recording.writeframes(audio)
    return audio


class FakeWhistle:
    """Capture actual audio sent to the backend and return predictable text/times."""

    def __init__(self):
        self.calls = []

    def transcribe(self, samples, **options):
        call = {"samples": np.asarray(samples).copy(), "options": options}
        self.calls.append(call)
        number = len(self.calls)
        return {
            "text": f"  part {number}  ",
            "language": "en" if number == 1 else "es",
            "words": [{"word": f"part{number}", "start": 0.1, "end": 0.25, "probability": 0.9}],
        }


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def touch(self, relative, data=b"audio"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def symlink(self, path, target, *, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except NotImplementedError:
            self.skipTest("Symbolic links are unsupported on this platform")
        except OSError as error:
            if error.errno in {errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP} or getattr(error, "winerror", None) in {50, 1314}:
                self.skipTest(f"Cannot create symbolic links on this platform: {error}")
            raise

    def make_zip(self, entries):
        path = self.root / "export.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as archive:
                for name, content in entries:
                    archive.writestr(name, content)
        return path

    def test_ios_multilingual_attachments_and_multiline_context(self):
        spanish = self.touch("00000001-AUDIO-2026-10-01-09-10-11.opus")
        german = self.touch("audio deutsch.m4a")
        french = self.touch("français.ogg")
        chat = self.root / "_chat.txt"
        text = (
            "\ufeff[01/10/26, 09:10:11] \u200eAna: \u200e<adjunto: " + spanish.name + ">\r\n"
            "Descripción de la nota\r\n"
            "[01/10/26, 09:11:00] Jörg: <Anhang: " + german.name + ">\r\n"
            "[01/10/26, 09:12:00] Chloé: <pièce jointe : " + french.name + ">\r\n"
        )
        chat.write_bytes(text.encode("utf-8"))
        export = app.scan_export(self.root, set())
        contexts, texts, occurrences = app.index_messages(export)

        self.assertEqual(contexts[spanish], [{"chat_file": "_chat.txt", "timestamp": "01/10/26, 09:10:11", "sender": "Ana"}])
        self.assertEqual(contexts[german][0]["sender"], "Jörg")
        self.assertEqual(contexts[french][0]["sender"], "Chloé")
        self.assertEqual(occurrences[(chat, 1)], [spanish])
        self.assertIn("Descripción de la nota", texts[chat])
        self.assertEqual(app.parse_chat(text)[0].text, f"<adjunto: {spanish.name}>\nDescripción de la nota")

    def test_android_12_hour_headers_and_attachment_on_continuation_line(self):
        audio = self.touch("PTT-20261001-WA0001.opus")
        chat = self.root / "WhatsApp Chat.txt"
        text = (
            "10/1/26, 9:10\u202fPM - Messages are end-to-end encrypted.\n"
            "10/1/26, 9:11\u202fPM - José: Here is the recording\n"
            f"{audio.name} (archivo adjunto)\n"
            "One more line of context\n"
            "10/1/26, 9:12\u202fPM - Emma: Thanks!\n"
        )
        chat.write_text(text, encoding="utf-8")
        contexts, _, occurrences = app.index_messages(app.scan_export(self.root, set()))
        messages = app.parse_chat(text)

        self.assertIsNone(messages[0].sender)
        self.assertEqual(messages[1].end_line, 3)
        self.assertEqual(contexts[audio][0]["timestamp"], "10/1/26, 9:11\u202fPM")
        self.assertEqual(contexts[audio][0]["sender"], "José")
        self.assertEqual(occurrences, {(chat, 3): [audio]})

    def test_filename_matching_rejects_substrings_and_normalizes_unicode(self):
        audio = self.touch("café.opus")
        chat = self.root / "chat.txt"
        chat.write_text(
            "01/10/26, 10:00 - A: other-café.opus\n"
            "01/10/26, 10:01 - B: café.opus.backup\n"
            "01/10/26, 10:02 - C: <attached: cafe\u0301.opus>\n",
            encoding="utf-8",
        )
        contexts, _, _ = app.index_messages(app.scan_export(self.root, set()))
        self.assertEqual([message["sender"] for message in contexts[audio]], ["C"])

    def test_duplicate_basenames_prefer_same_folder_and_skip_ambiguous_links(self):
        first = self.touch("first/voice.opus")
        second = self.touch("second/voice.opus")
        self.touch("first/chat.txt", b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")
        self.touch("chat.txt", b"01/10/26, 10:01 - Bob: voice.opus (attached)\n")
        error_output = io.StringIO()
        with redirect_stderr(error_output):
            contexts, _, _ = app.index_messages(app.scan_export(self.root, set()))
        self.assertEqual([message["sender"] for message in contexts[first]], ["Ana"])
        self.assertEqual(contexts[second], [])
        self.assertIn("ambiguous", error_output.getvalue())

    def test_folder_discovery_excludes_outputs_and_hidden_folders(self):
        audio = self.touch("media/VOICE.OPUS")
        chat = self.touch("chat.txt", b"chat")
        output = self.touch("transcripts.txt", b"previous report")
        self.touch(".cache/ignore.wav")
        self.touch("photo.jpg")
        with app.open_export(self.root, {output.resolve()}) as export:
            self.assertEqual(export.audio, [audio])
            self.assertEqual(export.chats, [chat])

    def test_folder_discovery_does_not_follow_symlinks(self):
        audio = self.touch("media/VOICE.OPUS")
        self.symlink(self.root / "linked.opus", audio)
        self.symlink(self.root / "linked-folder", audio.parent, directory=True)
        with app.open_export(self.root) as export:
            self.assertEqual(export.audio, [audio])

    def test_chat_input_selects_chat_and_audio_input_selects_only_recording(self):
        audio = self.touch("voice.opus")
        self.touch("another.wav")
        chat = self.touch("selected.txt", b"chat")
        self.touch("other.txt", b"other chat")
        with app.open_export(chat) as export:
            self.assertEqual(export.chats, [chat])
            self.assertEqual(len(export.audio), 2)
        with app.open_export(audio) as export:
            self.assertEqual(export.audio, [audio])
            self.assertEqual(export.chats, [])

    def test_zip_extracts_supported_files_and_cleans_up(self):
        archive = self.make_zip([
            ("Export/_chat.txt", "chat"),
            ("Export/Media/voice.OPUS", b"audio"),
            ("Export/photo.jpg", b"image"),
            ("Export/ignored.exe", b"program"),
        ])
        with app.open_export(archive) as export:
            extracted = export.root
            self.assertEqual([path.relative_to(extracted).as_posix() for path in export.audio], ["Export/Media/voice.OPUS"])
            self.assertEqual([path.relative_to(extracted).as_posix() for path in export.chats], ["Export/_chat.txt"])
            self.assertEqual(export.audio[0].read_bytes(), b"audio")
            self.assertFalse((extracted / "Export/photo.jpg").exists())
        self.assertFalse(extracted.exists())

    def test_zip_rejects_traversal_absolute_and_windows_paths(self):
        for name in ("../outside.opus", "folder/../../outside.opus", "/tmp/outside.opus", "..\\outside.opus", "C:\\outside.opus", "\\\\server\\outside.opus"):
            with self.subTest(name=name):
                archive = self.make_zip([(name, b"audio")])
                with self.assertRaisesRegex(ValueError, "Unsafe ZIP"):
                    with app.open_export(archive):
                        self.fail("Unsafe archive was accepted")

    def test_zip_rejects_duplicate_normalized_paths(self):
        for names in (("voice.opus", "voice.opus"), ("media//voice.opus", "media/voice.opus")):
            with self.subTest(names=names):
                archive = self.make_zip([(name, b"audio") for name in names])
                with self.assertRaisesRegex(ValueError, "Duplicate ZIP"):
                    with app.open_export(archive):
                        self.fail("Duplicate archive was accepted")

    def test_zip_rejects_symlinks(self):
        link = zipfile.ZipInfo("linked.opus")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.make_zip([(link, "../../outside.opus")])
        with self.assertRaisesRegex(ValueError, "Unsafe ZIP"):
            with app.open_export(archive):
                self.fail("Symlink archive was accepted")

    def test_annotated_chat_inserts_after_complete_message_and_keeps_original_text(self):
        audio = self.touch("voice.opus")
        chat = self.root / "chat.txt"
        text = "01/10/26, 10:00 - Ana: voice.opus (attached)\r\nA continuation\r\n01/10/26, 10:01 - Bob: Bye"
        destination = self.root / "annotated.txt"
        app.write_annotated_chat(
            destination, chat, text, {(chat, 1): [audio]},
            {audio: {"status": "ok", "text": "Hola, ¿qué tal?"}},
        )
        result = destination.read_bytes().decode("utf-8")
        self.assertEqual(result, text.replace("A continuation\r\n", "A continuation\r\n[Voice message transcript: Hola, ¿qué tal?]\n"))

    def test_atomic_write_preserves_mixed_line_endings_with_windows_text_defaults(self):
        destination = self.root / "report.txt"
        text = "José\r\nWindows line\nUnix line\rOld Mac line"
        named_temporary_file = tempfile.NamedTemporaryFile

        def windows_temporary_file(*args, **kwargs):
            if kwargs.get("newline") is None:
                kwargs["newline"] = "\r\n"
            return named_temporary_file(*args, **kwargs)

        with mock.patch.object(app.tempfile, "NamedTemporaryFile", side_effect=windows_temporary_file):
            app.atomic_write(destination, text)
        self.assertEqual(destination.read_bytes(), text.encode("utf-8"))
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_dry_run_never_imports_backend_or_decoder_and_writes_no_reports(self):
        self.touch("voice.opus")
        self.touch("chat.txt", b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")
        output = self.root / "results.json"
        annotated = self.root / "annotated.txt"
        constructor = mock.Mock(side_effect=AssertionError("Whistle must not load"))
        stdout = io.StringIO()
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"needle", "codecpod", "numpy"}:
                raise AssertionError(f"Dry run must not import {name}")
            return original_import(name, *args, **kwargs)

        with mock.patch.dict(sys.modules, {"needle": types.SimpleNamespace(Whistle=constructor)}), \
                mock.patch.object(builtins, "__import__", side_effect=guarded_import), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("Dry run must not invoke subprocesses")), \
                redirect_stdout(stdout):
            code = app.main([str(self.root), "--dry-run", "-o", str(output), "--chat-output", str(annotated)])
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
                    code = app.main([str(self.root), "--dry-run", *arguments])
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
            code = app.main([str(self.root), "--dry-run", "-o", str(self.root / "transcripts.json")])
        self.assertEqual(code, 1)
        self.assertIn("must not overwrite an original", stderr.getvalue())
        self.assertEqual(original.read_bytes(), b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")

    def test_existing_report_and_unrelated_text_do_not_count_as_extra_chats(self):
        self.touch("voice.opus")
        self.touch("chat.txt", b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")
        self.touch("transcripts.txt", b"voice.opus\n  01/10/26, 10:00 | Ana\nPrevious transcript\n")
        self.touch("notes.txt", b"Notes about this export")
        with redirect_stdout(io.StringIO()):
            code = app.main([
                str(self.root), "--dry-run", "-o", str(self.root / "transcripts.json"),
                "--chat-output", str(self.root / "annotated.txt"),
            ])
        self.assertEqual(code, 0)

    def test_invalid_chunk_sizes_are_rejected_before_loading_backend(self):
        audio = self.touch("voice.opus")
        for size in ("0", "-1", "30.01", "nan", "inf"):
            with self.subTest(size=size), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    app.main([str(audio), "--dry-run", f"--chunk-seconds={size}"])
                self.assertEqual(error.exception.code, 2)


@unittest.skipUnless(codecpod is not None and np is not None, "Install codecpod and NumPy for audio conversion tests")
class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def transcribe(self, path, model, **overrides):
        options = dict(chunk_seconds=30, language=None, keywords=[], word_timestamps=False)
        options.update(overrides)
        return app.transcribe_audio(path, model, **options)

    def test_long_recording_covers_every_frame_and_offsets_word_times(self):
        audio = self.root / "long.wav"
        frame_count = 61 * app.SAMPLE_RATE + 4007
        original = write_wav(audio, frames=frame_count)
        model = FakeWhistle()
        with mock.patch.object(app.tempfile, "TemporaryDirectory", side_effect=AssertionError("Audio chunks must stay in memory")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("Decoding must not invoke subprocesses")):
            result = self.transcribe(audio, model, language="es", keywords=["José"], word_timestamps=True)

        self.assertEqual([len(call["samples"]) for call in model.calls], [480000, 480000, frame_count - 960000])
        normalized_original = np.frombuffer(original, dtype="<i2").astype(np.float32) / np.float32(32768)
        np.testing.assert_array_equal(np.concatenate([call["samples"] for call in model.calls]), normalized_original)
        self.assertEqual(result["text"], "part 1 part 2 part 3")
        self.assertEqual(result["languages"], ["en", "es"])
        self.assertAlmostEqual(result["duration_seconds"], frame_count / app.SAMPLE_RATE)
        self.assertEqual([(segment["start"], segment["end"]) for segment in result["segments"]], [(0, 30), (30, 60), (60, frame_count / app.SAMPLE_RATE)])
        self.assertEqual([word["start"] for word in result["words"]], [0.1, 30.1, 60.1])
        self.assertEqual([word["end"] for word in result["words"]], [0.25, 30.25, 60.25])
        self.assertEqual([word["probability"] for word in result["words"]], [0.9, 0.9, 0.9])
        for call in model.calls:
            self.assertEqual(call["samples"].ndim, 1)
            self.assertEqual(call["samples"].dtype, np.dtype("float32"))
            self.assertEqual(call["options"], {"language": "es", "keywords": ["José"], "word_timestamps": True})

    def test_decoder_converts_stereo_to_16khz_mono_float32(self):
        audio = self.root / "stereo.wav"
        write_wav(audio, frames=44100, sample_rate=44100, channels=2)
        model = FakeWhistle()
        result = self.transcribe(audio, model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0]["samples"].shape, (16000,))
        self.assertEqual(model.calls[0]["samples"].dtype, np.dtype("float32"))
        self.assertAlmostEqual(result["duration_seconds"], 1.0)
        self.assertIsNone(model.calls[0]["options"]["keywords"])
        self.assertNotIn("words", result)

    def test_decode_failure_does_not_invoke_model(self):
        audio = self.root / "broken.opus"
        audio.write_bytes(b"not a recording")
        model = FakeWhistle()
        with self.assertRaisesRegex(RuntimeError, "decode"):
            self.transcribe(audio, model)
        self.assertEqual(model.calls, [])

    def test_empty_wav_reports_no_samples(self):
        audio = self.root / "empty.wav"
        write_wav(audio, frames=0)
        with self.assertRaisesRegex(ValueError, "no samples"):
            self.transcribe(audio, FakeWhistle())

    def test_main_writes_unicode_reports_and_annotated_chat_while_preserving_failures(self):
        audio = self.root / "voice.wav"
        write_wav(audio)
        (self.root / "broken.opus").write_bytes(b"invalid")
        chat = self.root / "chat.txt"
        chat.write_text(
            "01/10/26, 10:00 - José: voice.wav (archivo adjunto)\n"
            "01/10/26, 10:01 - Ana: broken.opus (attached)\n", encoding="utf-8",
        )
        output = self.root / "reports/results.json"
        annotated = self.root / "reports/annotated.txt"
        model = FakeWhistle()
        constructor = mock.Mock(return_value=model)
        with mock.patch.dict(sys.modules, {"needle": types.SimpleNamespace(Whistle=constructor)}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = app.main([str(chat), "-o", str(output), "--chat-output", str(annotated), "--language", "es"])
        self.assertEqual(code, 1)
        constructor.assert_called_once()
        report = json.loads(output.read_text(encoding="utf-8"))
        results = {result["file"]: result for result in report["results"]}
        self.assertEqual(results["voice.wav"]["text"], "part 1")
        self.assertEqual(results["voice.wav"]["messages"][0]["sender"], "José")
        self.assertEqual(results["broken.opus"]["status"], "error")
        self.assertIn("decode", results["broken.opus"]["error"].lower())
        self.assertIn("José", output.with_suffix(".txt").read_text(encoding="utf-8"))
        annotated_text = annotated.read_text(encoding="utf-8")
        self.assertIn("[Voice message transcript: part 1]", annotated_text)
        self.assertIn("[Voice message transcript: Error:", annotated_text)

    def test_real_opus_decodes_in_memory_without_external_converter(self):
        self.check_compressed_recording("voice.opus", codecpod.Opus(application="voip"))

    def test_real_m4a_decodes_in_memory_without_external_converter(self):
        self.check_compressed_recording("voice.m4a", codecpod.Aac(bit_rate=64000))

    def check_compressed_recording(self, filename, codec):
        source = np.sin(np.arange(2 * app.SAMPLE_RATE, dtype=np.float32) * np.float32(2 * np.pi * 440 / app.SAMPLE_RATE)) * np.float32(0.2)
        audio = self.root / filename
        codecpod.save(str(audio), source, app.SAMPLE_RATE, codec)
        model = FakeWhistle()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("Decoding must not invoke subprocesses")):
            result = self.transcribe(audio, model)
        self.assertEqual(len(model.calls), 1)
        samples = model.calls[0]["samples"]
        self.assertEqual(samples.shape, source.shape)
        self.assertEqual(samples.dtype, np.dtype("float32"))
        self.assertTrue(np.isfinite(samples).all())
        self.assertGreater(np.sqrt(np.mean(samples ** 2)), 0.05)
        self.assertLess(np.sqrt(np.mean((samples - source) ** 2)), 0.03)
        self.assertAlmostEqual(result["duration_seconds"], 2.0)
        self.assertEqual(result["text"], "part 1")


if __name__ == "__main__":
    unittest.main()
