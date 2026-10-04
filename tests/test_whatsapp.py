"""Tests run without downloading Whisper models or invoking inference."""

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


class FakeWhisper:
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


class FakeService:
    """Stand in for the hosted service: capture what would be uploaded, then answer or fail as told."""

    def __init__(self, *, token="hf_secret", outcomes=(), offers=("deepinfra", "hf-inference"), lookup_error=None):
        self.token = token
        self.outcomes = list(outcomes)
        # The services that offer every model asked about, and what asking fails with.
        self.offers = offers
        self.lookup_error = lookup_error
        self.lookups = []
        self.clients = []
        self.calls = []
        self.closed = 0

    def module(self):
        """A replacement for the huggingface_hub module."""
        service = self

        class Api:
            def model_info(self, repository, **options):
                service.lookups.append({"repository": repository, **options})
                if service.lookup_error:
                    raise service.lookup_error
                return types.SimpleNamespace(inference_provider_mapping=[
                    types.SimpleNamespace(provider=name, task="automatic-speech-recognition") for name in service.offers
                ] or None)

        class Client:
            def __init__(self, **options):
                service.clients.append(options)

            def automatic_speech_recognition(self, audio, **options):
                service.calls.append({"audio": audio, **options})
                outcome = service.outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return types.SimpleNamespace(text=outcome)

            def close(self):
                service.closed += 1

        return types.SimpleNamespace(HfApi=Api, InferenceClient=Client, get_token=lambda: service.token)


def http_error(status, message=None):
    """An error shaped like the ones the model hub raises for an HTTP status."""
    error = OSError(f"{status} Error for url: https://router.example/v1")
    error.response = types.SimpleNamespace(status_code=status)
    error.server_message = message
    return error


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
        self.assertEqual(texts[chat], text)
        self.assertEqual(app.parse_chat(text)[0].text, f"<adjunto: {spanish.name}>\nDescripción de la nota")

    def test_headers_accept_localized_dates_and_time_markers(self):
        cases = (
            ("10/4/26, 9:10 PM - José: Hola", "10/4/26, 9:10 PM"),
            ("2026-10-4, 9:10 p. m. - José: Hola", "2026-10-4, 9:10 p. m."),
            ("[٤/١٠/٢٠٢٦، ٩:١٠ م] José: Hola", "٤/١٠/٢٠٢٦، ٩:١٠ م"),
            ("[2026/10/4, 上午9:10] José: Hola", "2026/10/4, 上午9:10"),
            ("[2026/10/4, 9:10 午後] José: Hola", "2026/10/4, 9:10 午後"),
            ("[04.10.2026, 09：10] José： Hola", "04.10.2026, 09：10"),
        )
        for text, timestamp in cases:
            with self.subTest(text=text):
                message, = app.parse_chat(text)
                self.assertEqual((message.timestamp, message.sender, message.text), (timestamp, "José", "Hola"))

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
        for name in ("../outside.opus", "folder/../../outside.opus", "/tmp/outside.opus", "..\\outside.opus", "C:\\outside.opus", "\\\\server\\outside.opus", "media/C:/outside.opus"):
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
        self.assertEqual(result, text.replace("A continuation\r\n", "A continuation\r\n[Voice message transcript: Hola, ¿qué tal?]\r\n"))

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
        constructor = mock.Mock(side_effect=AssertionError("Whisper must not load"))
        stdout = io.StringIO()
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"faster_whisper", "ctranslate2", "codecpod", "numpy", "huggingface_hub"}:
                raise AssertionError(f"Dry run must not import {name}")
            return original_import(name, *args, **kwargs)

        with mock.patch.object(app, "Whisper", constructor), \
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

    def test_whisper_adapter_passes_options_and_collects_text_and_words(self):
        calls = []
        word = types.SimpleNamespace(word=" hola", start=0.1, end=0.4, probability=0.9)

        class FakeModel:
            def __init__(self, name, **options):
                calls.append((name, options))

            def transcribe(self, samples, **options):
                calls.append(options)
                segments = [types.SimpleNamespace(text=" Hola", words=[word]), types.SimpleNamespace(text=" mundo.", words=None)]
                return iter(segments), types.SimpleNamespace(language="es")

        with mock.patch.dict(sys.modules, {"faster_whisper": types.SimpleNamespace(WhisperModel=FakeModel)}):
            model = app.Whisper("small")
        result = model.transcribe([0.0], language="es", keywords=["José", "Ana"], word_timestamps=True)
        self.assertEqual(calls[0], ("small", {"device": "auto", "compute_type": "auto"}))
        self.assertEqual(calls[1], {
            "language": "es", "hotwords": "José Ana", "word_timestamps": True, "condition_on_previous_text": False,
        })
        self.assertEqual(result, {
            "text": "Hola mundo.", "language": "es",
            "words": [{"word": "hola", "start": 0.1, "end": 0.4, "probability": 0.9}],
        })
        plain = model.transcribe([0.0])
        self.assertNotIn("words", plain)
        self.assertIsNone(calls[2]["hotwords"])

    def test_invalid_chunk_sizes_are_rejected_before_loading_backend(self):
        audio = self.touch("voice.opus")
        for size in ("0", "-1", "30.01", "nan", "inf"):
            with self.subTest(size=size), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    app.main([str(audio), "--dry-run", f"--chunk-seconds={size}"])
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
        with mock.patch.object(app, "RemoteWhisper", constructor):
            for arguments in cases:
                with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        app.main([str(audio), *arguments])
                    self.assertEqual(error.exception.code, 2)
            # Callers of the function get the same answer as the command line.
            with self.assertRaisesRegex(ValueError, "--keyword is not available"):
                app.transcribe_export(audio, self.root / "transcripts.json", remote="deepinfra", keywords=["José"])
            # A dry run lists the recordings without a token or a connection.
            with redirect_stdout(io.StringIO()):
                self.assertEqual(app.main([str(audio), "--remote", "deepinfra", "--language", "es", "--dry-run"]), 0)
        constructor.assert_not_called()


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
        model = FakeWhisper()
        with mock.patch.object(app.tempfile, "TemporaryDirectory", side_effect=AssertionError("Audio chunks must stay in memory")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("Decoding must not invoke subprocesses")):
            result = self.transcribe(audio, model, language="es", keywords=["José"], word_timestamps=True)

        # Chunks end within the last five of their 30 seconds, and cover every frame once.
        lengths = [len(call["samples"]) for call in model.calls]
        self.assertEqual(len(lengths), 3)
        self.assertEqual(sum(lengths), frame_count)
        for length in lengths[:-1]:
            self.assertGreater(length, 25 * app.SAMPLE_RATE)
            self.assertLessEqual(length, 30 * app.SAMPLE_RATE)
        normalized_original = np.frombuffer(original, dtype="<i2").astype(np.float32) / np.float32(32768)
        np.testing.assert_array_equal(np.concatenate([call["samples"] for call in model.calls]), normalized_original)
        self.assertEqual(result["text"], "part 1 part 2 part 3")
        self.assertEqual(result["languages"], ["en", "es"])
        self.assertAlmostEqual(result["duration_seconds"], frame_count / app.SAMPLE_RATE)
        starts = [sum(lengths[:number]) / app.SAMPLE_RATE for number in range(3)]
        ends = [sum(lengths[:number + 1]) / app.SAMPLE_RATE for number in range(3)]
        self.assertEqual([(segment["start"], segment["end"]) for segment in result["segments"]], list(zip(starts, ends)))
        self.assertEqual([word["start"] for word in result["words"]], [0.1 + start for start in starts])
        self.assertEqual([word["end"] for word in result["words"]], [0.25 + start for start in starts])
        self.assertEqual([word["probability"] for word in result["words"]], [0.9, 0.9, 0.9])
        for call in model.calls:
            self.assertEqual(call["samples"].ndim, 1)
            self.assertEqual(call["samples"].dtype, np.dtype("float32"))
            self.assertEqual(call["options"], {"language": "es", "keywords": ["José"], "word_timestamps": True})

    def test_long_recording_is_cut_inside_pauses_near_the_chunk_limit(self):
        rate = app.SAMPLE_RATE
        # The phase keeps the tone away from zero where the pauses below end.
        tone = np.sin(np.arange(61 * rate) * (2 * np.pi * 440 / rate) + 1) * 0.2
        for start, end in ((27 * rate, 27 * rate + rate // 2), (883200, 889600), (10 * rate, 12 * rate)):
            tone[start:end] = 0
        audio = self.root / "pauses.wav"
        with wave.open(str(audio), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(rate)
            recording.writeframes((tone * 32767).astype("<i2").tobytes())
        model = FakeWhisper()
        result = self.transcribe(audio, model)
        # Each cut is the middle of the last silent tenth of a second: 27.45 s and
        # 55.55 s. The pause at 10 s is too far from the limit to be used.
        self.assertEqual([len(call["samples"]) for call in model.calls], [439200, 449600, 87200])
        self.assertEqual([(segment["start"], segment["end"]) for segment in result["segments"]], [(0, 27.45), (27.45, 55.55), (55.55, 61)])

    def test_short_chunks_and_exact_fits_keep_their_limits(self):
        samples = np.zeros(3 * app.SAMPLE_RATE, dtype=np.float32)
        self.assertEqual(app.chunk_end(samples, 0, 3 * app.SAMPLE_RATE), len(samples))
        self.assertEqual(app.chunk_end(samples, app.SAMPLE_RATE, 30 * app.SAMPLE_RATE), len(samples))
        # A chunk too short to search for a pause is cut at its limit.
        self.assertEqual(app.chunk_end(samples, 0, 1600), 1600)
        self.assertEqual(app.chunk_end(samples, 0, 1), 1)

    def test_decoder_converts_stereo_to_16khz_mono_float32(self):
        audio = self.root / "stereo.wav"
        write_wav(audio, frames=44100, sample_rate=44100, channels=2)
        model = FakeWhisper()
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
        model = FakeWhisper()
        with self.assertRaisesRegex(RuntimeError, "decode"):
            self.transcribe(audio, model)
        self.assertEqual(model.calls, [])

    def test_empty_wav_reports_no_samples(self):
        audio = self.root / "empty.wav"
        write_wav(audio, frames=0)
        with self.assertRaisesRegex(ValueError, "no samples"):
            self.transcribe(audio, FakeWhisper())

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
        with mock.patch.object(app, "Whisper", constructor), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = app.main([str(chat), "-o", str(output), "--chat-output", str(annotated), "--language", "es"])
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

    def test_real_opus_decodes_in_memory_without_external_converter(self):
        self.check_compressed_recording("voice.opus", codecpod.Opus(application="voip"))

    def test_real_m4a_decodes_in_memory_without_external_converter(self):
        self.check_compressed_recording("voice.m4a", codecpod.Aac(bit_rate=64000))

    def check_compressed_recording(self, filename, codec):
        source = np.sin(np.arange(2 * app.SAMPLE_RATE, dtype=np.float32) * np.float32(2 * np.pi * 440 / app.SAMPLE_RATE)) * np.float32(0.2)
        audio = self.root / filename
        codecpod.save(str(audio), source, app.SAMPLE_RATE, codec)
        model = FakeWhisper()
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


@unittest.skipUnless(codecpod is not None and np is not None, "Install codecpod and NumPy for audio conversion tests")
class RemoteTests(unittest.TestCase):
    """The hosted service is replaced by a fake: these tests never connect anywhere."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def engine(self, service, *arguments):
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}):
            return app.RemoteWhisper(*arguments)

    def test_adapter_uploads_wav_names_the_repository_and_forces_the_language(self):
        service = FakeService(outcomes=["  Hola, ¿qué tal?  ", None])
        routed = self.engine(service, "large-v3-turbo", "deepinfra")
        own = self.engine(service, "openai/whisper-large-v3", "hf-inference")
        samples = np.array([0.0, 0.5, 1.5, -1.5], dtype=np.float32)
        self.assertEqual(routed.transcribe(samples, language="es"), {"text": "Hola, ¿qué tal?", "language": "es"})
        # Without a forced language the service does not report one.
        self.assertEqual(own.transcribe(samples), {"text": "", "language": ""})
        self.assertEqual(service.clients, [
            {"provider": "deepinfra", "token": "hf_secret", "timeout": 120, "headers": None},
            {"provider": "hf-inference", "token": "hf_secret", "timeout": 120, "headers": {"Content-Type": "audio/wav"}},
        ])
        # Each engine first asks, with the same token, whether its service offers the model.
        self.assertEqual(service.lookups, [
            {"repository": repository, "expand": ["inferenceProviderMapping"], "token": "hf_secret", "timeout": 120}
            for repository in ("openai/whisper-large-v3-turbo", "openai/whisper-large-v3")
        ])
        # The client is released after every request, so uploaded chunks do not pile up in memory.
        self.assertEqual(service.closed, 2)
        first, second = service.calls
        self.assertEqual((first["model"], first["extra_body"]), ("openai/whisper-large-v3-turbo", {"language": "es"}))
        self.assertEqual((second["model"], second["extra_body"]), ("openai/whisper-large-v3", None))
        with wave.open(io.BytesIO(first["audio"])) as recording:
            self.assertEqual(
                (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()), (1, 2, app.SAMPLE_RATE))
            frames = recording.readframes(recording.getnframes())
        # Samples beyond full scale are clipped instead of wrapping around.
        self.assertEqual(struct.unpack("<4h", frames), (0, 16384, 32767, -32767))

    def test_adapter_does_not_start_without_a_usable_token(self):
        service = FakeService(token=None)
        with self.assertRaisesRegex(app.RemoteTokenNeeded, "HF_TOKEN"):
            self.engine(service)
        # Anything not shaped like Hugging Face's tokens would be sent to DeepInfra directly, and a
        # line break would get the token quoted in an error: such a token is neither used nor repeated.
        for token in ('"hf_quoted"', "HF_TOKEN=hf_pasted", "Bearer hf_pasted", "hf_two\nlines", "hf_with space", "sk-other"):
            with self.subTest(token=token):
                with self.assertRaises(RuntimeError) as caught:
                    self.engine(service, "large-v3-turbo", "deepinfra", token)
                self.assertIn("starts with hf_", str(caught.exception))
                self.assertNotIn(token, str(caught.exception))
        self.assertIsNotNone(app.token_problem("hf_"))
        # A token from HF_TOKEN or a saved login is held to the same shape.
        saved = FakeService(token='"hf_quoted"')
        with self.assertRaisesRegex(RuntimeError, "starts with hf_"):
            self.engine(saved)
        for attempted in (service, saved):
            self.assertEqual((attempted.lookups, attempted.clients), ([], []))
        # Tokens of a browser login carry dots and dashes.
        self.assertEqual(self.engine(FakeService(token=None), "large-v3-turbo", "deepinfra", "hf_oauth_a.b-c").model,
                         "openai/whisper-large-v3-turbo")

    def test_adapter_asks_who_offers_the_model_before_anything_is_uploaded(self):
        service = FakeService(offers=("hf-inference",))
        with self.assertRaisesRegex(RuntimeError, "DeepInfra through Hugging Face does not offer openai/whisper-small"):
            self.engine(service, "small", "deepinfra")
        self.assertEqual(self.engine(service, "small", "hf-inference").model, "openai/whisper-small")
        with self.assertRaisesRegex(RuntimeError, "Hugging Face does not offer openai/whisper-small"):
            self.engine(FakeService(offers=()), "small", "hf-inference")

        # The Hub answers alike for a missing repository and a token it does not accept.
        class RepositoryNotFoundError(Exception):
            pass

        with self.assertRaisesRegex(RuntimeError, "openai/whisper-trubo: there is no such repository, or the access token"):
            self.engine(FakeService(lookup_error=RepositoryNotFoundError("401 Invalid username or password.")), "trubo")
        # The local runtime's short names select the repositories of the sizes they stand for.
        self.assertEqual([self.engine(FakeService(), name).model for name in ("turbo", "large")],
                         ["openai/whisper-large-v3-turbo", "openai/whisper-large-v3"])
        offline = FakeService(lookup_error=OSError("Connection refused\nwhile sending Bearer hf_secret"))
        with self.assertRaises(RuntimeError) as caught:
            self.engine(offline)
        self.assertEqual(str(caught.exception), "Could not ask Hugging Face who offers openai/whisper-large-v3-turbo: "
                                                "Connection refused while sending Bearer <token>")
        self.assertEqual(offline.clients, [])
        # A URL or a path is never asked about, let alone contacted.
        unasked = FakeService()
        for model in ("http://127.0.0.1:8000/elsewhere", "https://example.org/whisper", "some/deep/path", "C:\\models\\whisper"):
            with self.subTest(model=model):
                with self.assertRaisesRegex(RuntimeError, "Whisper size or a Hugging Face repository"):
                    self.engine(unasked, model, "hf-inference")
        self.assertEqual((unasked.lookups, unasked.clients), ([], []))

    def test_a_model_the_service_lacks_ends_the_run_before_an_earlier_report_is_replaced(self):
        audio = self.root / "voice.wav"
        write_wav(audio)
        output = self.root / "reports/transcripts.json"
        output.parent.mkdir()
        output.write_text("an earlier report", encoding="utf-8")
        service = FakeService(offers=())
        stderr = io.StringIO()
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}), redirect_stderr(stderr):
            code = app.main([str(audio), "-o", str(output), "--model", "small", "--remote", "deepinfra"])
        self.assertEqual(code, 1)
        self.assertIn("does not offer openai/whisper-small", stderr.getvalue())
        self.assertEqual(output.read_text(encoding="utf-8"), "an earlier report")
        self.assertEqual([path.name for path in output.parent.iterdir()], ["transcripts.json"])
        self.assertEqual(service.calls, [])

    def test_failures_that_would_repeat_are_told_apart_from_passing_ones(self):
        cases = (
            (http_error(401, "Invalid token"), True, "Invalid token"),
            (http_error(402, "Credits used up"), True, "Credits used up"),
            (http_error(403, "No permission to call Inference Providers"), True, "No permission"),
            (http_error(404), True, "404 Error for url"),
            (ValueError("Unexpected output format"), True, "Unexpected output format"),
            (http_error(408, "Request timeout"), False, "Request timeout"),
            (http_error(429, "Too many requests"), False, "Too many requests"),
            (http_error(503), False, "503 Error for url"),
            (TimeoutError("The request\ntimed out"), False, "The request timed out"),
            # An error that quotes the request must not carry the token into the reports.
            (OSError("Illegal header value b'Bearer hf_secret'"), False, "Illegal header value b'Bearer <token>'"),
            (Exception(), False, "Exception"),
        )
        service = FakeService(outcomes=[error for error, _, _ in cases])
        engine = self.engine(service)
        for error, refused, reason in cases:
            with self.subTest(error=error):
                with self.assertRaises(RuntimeError) as caught:
                    engine.transcribe(np.zeros(16, dtype=np.float32))
                self.assertEqual(isinstance(caught.exception, app.RemoteRefused), refused)
                message = str(caught.exception)
                self.assertTrue(message.startswith("DeepInfra through Hugging Face did not transcribe the audio: "))
                self.assertIn(reason, message)
                self.assertNotIn("\n", message)
                self.assertNotIn("hf_secret", message)
        # A failed request releases the client as well.
        self.assertEqual(service.closed, len(cases))

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
                mock.patch.object(app, "Whisper", local), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = app.main([
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
        self.assertIn("Stopped early", stderr.getvalue())
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


if __name__ == "__main__":
    unittest.main()
