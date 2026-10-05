"""Tests for transcribing recordings and exports: real decoding, a fake model, nothing downloaded."""

import builtins
import codecs
from contextlib import contextmanager, redirect_stdout
import io
import json
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from unittest import mock
import wave

from voxpad import transcribe
from voxpad.engines import SAMPLE_RATE, RemoteTokenNeeded
from voxpad.reports import PreviousReport
from voxpad.transcribe import chunk_end, previous_candidates, reusable, transcribe_audio, transcribe_export
from tests.support import ExportTestCase, FakeService, FakeWhisper, codecpod, np, write_wav


def earlier(file, text, **fields):
    """A transcript as an earlier run reported it, for a chat exported under other names."""
    return {
        "file": file, "messages": [{"chat_file": "WhatsApp Chat with Ana.txt", "timestamp": "10/1/26, 10:00 AM", "sender": "Ana María"}],
        "status": "ok", "text": text, "languages": ["es"], "duration_seconds": 1.5,
        "segments": [{"start": 0.0, "end": 1.5, "text": text, "language": "es"}], **fields,
    }


def save_report(path, results, model="Whisper large-v3-turbo"):
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {"source": "export", "model": model, "sample_rate": 16000, "chunk_seconds": 30, "results": results}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class Watching(FakeWhisper):
    """Note which recordings the saved report holds whenever one more is transcribed."""

    def __init__(self, output):
        super().__init__()
        self.output = output
        self.seen = []

    def transcribe(self, samples, **options):
        self.seen.append([result["file"] for result in json.loads(self.output.read_text(encoding="utf-8"))["results"]])
        return super().transcribe(samples, **options)


class Interrupted(FakeWhisper):
    """Stand in for Ctrl-C arriving while a later recording is transcribed."""

    def __init__(self, after):
        super().__init__()
        self.after = after

    def transcribe(self, samples, **options):
        if len(self.calls) >= self.after:
            raise KeyboardInterrupt
        return super().transcribe(samples, **options)


@unittest.skipUnless(codecpod is not None and np is not None, "Install codecpod and NumPy for audio conversion tests")
class AudioTests(ExportTestCase):
    def transcribe(self, path, model, **overrides):
        options = dict(chunk_seconds=30, language=None, keywords=[], word_timestamps=False)
        options.update(overrides)
        return transcribe_audio(path, model, **options)

    def test_long_recording_covers_every_frame_and_offsets_word_times(self):
        audio = self.root / "long.wav"
        frame_count = 61 * SAMPLE_RATE + 4007
        original = write_wav(audio, frames=frame_count)
        model = FakeWhisper()
        with mock.patch.object(tempfile, "TemporaryDirectory", side_effect=AssertionError("Audio chunks must stay in memory")), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("Decoding must not invoke subprocesses")):
            result = self.transcribe(audio, model, language="es", keywords=["José"], word_timestamps=True)

        # Chunks end within the last five of their 30 seconds, and cover every frame once.
        lengths = [len(call["samples"]) for call in model.calls]
        self.assertEqual(len(lengths), 3)
        self.assertEqual(sum(lengths), frame_count)
        for length in lengths[:-1]:
            self.assertGreater(length, 25 * SAMPLE_RATE)
            self.assertLessEqual(length, 30 * SAMPLE_RATE)
        normalized_original = np.frombuffer(original, dtype="<i2").astype(np.float32) / np.float32(32768)
        np.testing.assert_array_equal(np.concatenate([call["samples"] for call in model.calls]), normalized_original)
        self.assertEqual(result["text"], "part 1 part 2 part 3")
        self.assertEqual(result["languages"], ["en", "es"])
        self.assertAlmostEqual(result["duration_seconds"], frame_count / SAMPLE_RATE)
        starts = [sum(lengths[:number]) / SAMPLE_RATE for number in range(3)]
        ends = [sum(lengths[:number + 1]) / SAMPLE_RATE for number in range(3)]
        self.assertEqual([(segment["start"], segment["end"]) for segment in result["segments"]], list(zip(starts, ends)))
        self.assertEqual([word["start"] for word in result["words"]], [0.1 + start for start in starts])
        self.assertEqual([word["end"] for word in result["words"]], [0.25 + start for start in starts])
        self.assertEqual([word["probability"] for word in result["words"]], [0.9, 0.9, 0.9])
        for call in model.calls:
            self.assertEqual(call["samples"].ndim, 1)
            self.assertEqual(call["samples"].dtype, np.dtype("float32"))
            self.assertEqual(call["options"], {"language": "es", "keywords": ["José"], "word_timestamps": True})

    def test_long_recording_is_cut_inside_pauses_near_the_chunk_limit(self):
        rate = SAMPLE_RATE
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
        samples = np.zeros(3 * SAMPLE_RATE, dtype=np.float32)
        self.assertEqual(chunk_end(samples, 0, 3 * SAMPLE_RATE), len(samples))
        self.assertEqual(chunk_end(samples, SAMPLE_RATE, 30 * SAMPLE_RATE), len(samples))
        # A chunk too short to search for a pause is cut at its limit.
        self.assertEqual(chunk_end(samples, 0, 1600), 1600)
        self.assertEqual(chunk_end(samples, 0, 1), 1)

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

    def test_real_opus_decodes_in_memory_without_external_converter(self):
        self.check_compressed_recording("voice.opus", codecpod.Opus(application="voip"))

    def test_real_m4a_decodes_in_memory_without_external_converter(self):
        self.check_compressed_recording("voice.m4a", codecpod.Aac(bit_rate=64000))

    def check_compressed_recording(self, filename, codec):
        source = np.sin(np.arange(2 * SAMPLE_RATE, dtype=np.float32) * np.float32(2 * np.pi * 440 / SAMPLE_RATE)) * np.float32(0.2)
        audio = self.root / filename
        codecpod.save(str(audio), source, SAMPLE_RATE, codec)
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


class MatchingTests(unittest.TestCase):
    """Which result of an earlier report stands for which recording of an export."""

    def texts(self, files, results):
        found = previous_candidates(files, PreviousReport("earlier.json", None, results))
        return [candidate and candidate["text"] for candidate in found]

    def test_a_result_stands_for_the_recording_at_its_path_or_else_for_the_one_with_its_name(self):
        results = [{"file": "voice.opus", "status": "ok", "text": "top"}, {"file": "media/other.opus", "status": "error", "text": "other"}]
        self.assertEqual(self.texts(["media/other.opus", "voice.opus"], results), ["other", "top"])
        # The export was read from another folder, or from its ZIP: names are enough while each is used once.
        self.assertEqual(self.texts(["Export/Media/voice.opus", "Export/new.opus", "Export/other.opus"], results), ["top", None, "other"])
        self.assertEqual(self.texts([], results), [])
        self.assertEqual(self.texts(["voice.opus"], []), [None])

    def test_a_name_used_twice_stands_for_no_recording(self):
        first, second = ({"file": f"{folder}/voice.opus", "status": "ok", "text": folder} for folder in ("first", "second"))
        # Paths tell the two apart.
        self.assertEqual(self.texts(["first/voice.opus", "second/voice.opus"], [second, first]), ["first", "second"])
        # Twice in the report, whatever became of them: neither is known to be this recording.
        self.assertEqual(self.texts(["voice.opus"], [first, second]), [None])
        self.assertEqual(self.texts(["voice.opus"], [first, {**second, "status": "error"}]), [None])
        # Twice in the export: the result is not known to be either recording, unless its path says so.
        self.assertEqual(self.texts(["a/voice.opus", "b/voice.opus"], [{"file": "voice.opus", "status": "ok", "text": "top"}]), [None, None])
        self.assertEqual(self.texts(["b/voice.opus", "voice.opus"], [{"file": "voice.opus", "status": "ok", "text": "top"}]), [None, "top"])

    def test_paths_and_names_are_compared_after_unicode_normalization(self):
        composed, decomposed = (unicodedata.normalize(form, "média/café.opus") for form in ("NFC", "NFD"))
        self.assertNotEqual(composed, decomposed)
        for file, recorded in ((composed, decomposed), (decomposed, composed)):
            with self.subTest(file=file):
                results = [{"file": recorded, "status": "ok", "text": "found"}]
                self.assertEqual(self.texts([file], results), ["found"])
                self.assertEqual(self.texts([file.rsplit("/", 1)[-1]], results), ["found"])

    def test_of_results_naming_one_file_a_transcript_is_preferred_and_then_the_later_one(self):
        def result(status, text):
            return {"file": "voice.opus", "status": status, "text": text}

        for results, expected in (
            ([result("ok", "first"), result("error", "second")], "first"),
            ([result("error", "first"), result("ok", "second")], "second"),
            ([result("ok", "first"), result("ok", "second")], "second"),
            ([result("error", "first"), result("error", "second")], "second"),
        ):
            with self.subTest(results=results):
                self.assertEqual(self.texts(["voice.opus"], results), [expected])
                # By name alone, two results are one too many.
                self.assertEqual(self.texts(["media/voice.opus"], results), [None])

    def test_a_transcript_is_reused_unless_it_failed_the_recording_changed_or_word_times_are_wanted_and_missing(self):
        transcript = {"file": "voice.opus", "status": "ok", "text": "hola"}
        self.assertTrue(reusable(transcript, 5))
        self.assertTrue(reusable(transcript, None))
        # Silence is a result too.
        self.assertTrue(reusable({**transcript, "text": ""}, 5))
        self.assertTrue(reusable({**transcript, "bytes": 5}, 5))
        self.assertFalse(reusable({**transcript, "bytes": 6}, 5))
        self.assertFalse(reusable({**transcript, "bytes": 6}, None))
        self.assertFalse(reusable({**transcript, "status": "error", "error": "Could not decode audio"}, 5))
        self.assertFalse(reusable(None, 5))
        self.assertFalse(reusable(transcript, 5, word_timestamps=True))
        self.assertTrue(reusable({**transcript, "words": []}, 5, word_timestamps=True))


class ReuseTests(ExportTestCase):
    """Runs that transcribe nothing: no recording is decoded, no model loaded and no service asked."""

    def setUp(self):
        super().setUp()
        self.export = self.root / "export"
        self.first = self.touch("export/PTT-20261001-WA0001.opus", b"first recording")
        self.second = self.touch("export/PTT-20261001-WA0002.opus", b"second")
        self.chat = "01/10/26, 10:00 - Ana: PTT-20261001-WA0001.opus (attached)\n01/10/26, 10:01 - José: PTT-20261001-WA0002.opus (attached)\n"
        self.touch("export/chat.txt", self.chat.encode("utf-8"))
        self.output = self.root / "results/transcripts.json"
        self.complete = [earlier("PTT-20261001-WA0001.opus", "uno"), earlier("PTT-20261001-WA0002.opus", "dos")]
        self.notes = []
        self.steps = []

    @contextmanager
    def nothing_loaded(self):
        """Fail the test when a model, the decoder or the client of the hosted service is asked for."""
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.split(".")[0] in {"faster_whisper", "ctranslate2", "codecpod", "numpy", "huggingface_hub"}:
                raise AssertionError(f"Nothing may import {name}")
            return original_import(name, *args, **kwargs)

        engine = mock.Mock(side_effect=AssertionError("No model may be loaded and no service asked"))
        with mock.patch.object(transcribe, "Whisper", engine), mock.patch.object(transcribe, "RemoteWhisper", engine), \
                mock.patch.object(builtins, "__import__", side_effect=guarded_import), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("No subprocess may be started")):
            yield
        engine.assert_not_called()

    def run_export(self, source=None, output=None, chat_output=None, **options):
        self.notes, self.steps = [], []
        with self.nothing_loaded():
            return transcribe_export(source or self.export, output or self.output, chat_output, notify=self.notes.append,
                                     progress=lambda done, total: self.steps.append((done, total)), **options)

    def report(self, output=None):
        return json.loads((output or self.output).read_text(encoding="utf-8"))

    def test_a_complete_earlier_report_is_imported_without_a_model_a_token_or_a_connection(self):
        imported = save_report(self.root / "another folder/transcripts.json", self.complete, model="Whisper small")
        before = imported.read_bytes()
        for number, (options, model) in enumerate((
            ({}, "Whisper large-v3-turbo"),
            ({"remote": "deepinfra", "language": "es"}, "openai/whisper-large-v3-turbo on DeepInfra through Hugging Face"),
            ({"remote": "hf-inference", "model": "large-v3"}, "openai/whisper-large-v3 on Hugging Face"),
            ({"model": "small"}, "Whisper small"),
        )):
            with self.subTest(options=options):
                output = self.root / f"results {number}/transcripts.json"
                annotated = output.with_name("chat_with_transcripts.txt")
                # Nothing is left to stop, so nobody is asked.
                summary = self.run_export(output=output, chat_output=annotated, reuse=[imported], should_stop=lambda: True, **options)
                self.assertEqual({key: summary[key] for key in ("total", "reused", "failures", "stopped", "refusal", "chats")},
                                 {"total": 2, "reused": 2, "failures": 0, "stopped": False, "refusal": None, "chats": 1})
                self.assertEqual((summary["output"], summary["chat_output"]), (output, annotated))
                self.assertEqual(self.steps, [(2, 2)])
                self.assertEqual(self.notes, [
                    "Reusing 2 transcripts from transcripts.json (made with Whisper small); 0 left to transcribe.",
                    "2 earlier transcripts were matched by file name only.",
                ])
                report = self.report(output)
                self.assertEqual(summary["report"], report)
                # The report is named after the model this run would have used; each transcript says what made it.
                self.assertEqual({key: value for key, value in report.items() if key != "results"},
                                 {"source": "export", "model": model, "sample_rate": 16000, "chunk_seconds": 30})
                made_with = {} if model == "Whisper small" else {"model": "Whisper small"}
                self.assertEqual(report["results"], [
                    {"file": "PTT-20261001-WA0001.opus", "bytes": 15, "status": "ok", "text": "uno", "languages": ["es"],
                     # Messages are the ones of this export, not those the earlier report named.
                     "messages": [{"chat_file": "chat.txt", "timestamp": "01/10/26, 10:00", "sender": "Ana"}],
                     "duration_seconds": 1.5, "segments": [{"start": 0.0, "end": 1.5, "text": "uno", "language": "es"}], **made_with},
                    {"file": "PTT-20261001-WA0002.opus", "bytes": 6, "status": "ok", "text": "dos", "languages": ["es"],
                     "messages": [{"chat_file": "chat.txt", "timestamp": "01/10/26, 10:01", "sender": "José"}],
                     "duration_seconds": 1.5, "segments": [{"start": 0.0, "end": 1.5, "text": "dos", "language": "es"}], **made_with},
                ])
                self.assertEqual(output.with_suffix(".txt").read_text(encoding="utf-8"), (
                    "PTT-20261001-WA0001.opus\n  01/10/26, 10:00 | Ana\nuno\n\n"
                    "PTT-20261001-WA0002.opus\n  01/10/26, 10:01 | José\ndos\n"
                ))
                self.assertEqual(annotated.read_text(encoding="utf-8"), (
                    "01/10/26, 10:00 - Ana: PTT-20261001-WA0001.opus (attached)\n[Voice message transcript: uno]\n"
                    "01/10/26, 10:01 - José: PTT-20261001-WA0002.opus (attached)\n[Voice message transcript: dos]\n"
                ))
                self.assertEqual(sorted(path.name for path in output.parent.iterdir()),
                                 ["chat_with_transcripts.txt", "transcripts.json", "transcripts.txt"])
        self.assertEqual(imported.read_bytes(), before)

    def test_a_report_of_another_export_of_the_chat_is_matched_by_file_names_also_for_a_zip(self):
        # The other export sat in a folder; this one is its ZIP, with another chat file and contact name.
        archive = self.make_zip([
            ("WhatsApp Chat with Ana/_chat.txt", "[01/10/26, 10:00:00] Anita: <attached: PTT-20261001-WA0001.opus>\n"
                                                 "[01/10/26, 10:01:00] José: <attached: PTT-20261001-WA0002.opus>\n"),
            ("WhatsApp Chat with Ana/PTT-20261001-WA0001.opus", b"first recording"),
            ("WhatsApp Chat with Ana/PTT-20261001-WA0002.opus", b"second"),
        ])
        imported = save_report(self.root / "earlier.json", self.complete)
        summary = self.run_export(archive, reuse=[imported])
        self.assertEqual((summary["total"], summary["reused"]), (2, 2))
        report = self.report()
        self.assertEqual(report["source"], "export.zip")
        self.assertEqual([(result["file"], result["text"], result["messages"]) for result in report["results"]], [
            ("WhatsApp Chat with Ana/PTT-20261001-WA0001.opus", "uno",
             [{"chat_file": "WhatsApp Chat with Ana/_chat.txt", "timestamp": "01/10/26, 10:00:00", "sender": "Anita"}]),
            ("WhatsApp Chat with Ana/PTT-20261001-WA0002.opus", "dos",
             [{"chat_file": "WhatsApp Chat with Ana/_chat.txt", "timestamp": "01/10/26, 10:01:00", "sender": "José"}]),
        ])
        # Run again, the report at the output names the same paths and records the sizes.
        self.run_export(archive)
        self.assertEqual(self.notes, [
            "Reusing 2 transcripts from transcripts.json (made with Whisper large-v3-turbo); 0 left to transcribe. --fresh transcribes everything again.",
        ])
        self.assertEqual(self.report(), report)

    def test_of_two_reports_the_last_given_wins_unless_it_has_nothing_usable(self):
        first_name, second_name = "PTT-20261001-WA0001.opus", "PTT-20261001-WA0002.opus"
        one = save_report(self.root / "one.json", [earlier(first_name, "one: first"), earlier(second_name, "one: second")], model="Whisper small")
        failed = {"file": second_name, "messages": [], "status": "error", "text": "", "error": "Out of memory"}
        two = save_report(self.root / "two.json", [earlier(first_name, "two: first"), failed], model="Whisper medium")

        def transcripts(output=None):
            return [(result["text"], result.get("model")) for result in self.report(output)["results"]]

        self.run_export(reuse=[one, two])
        self.assertEqual(transcripts(), [("two: first", "Whisper medium"), ("one: second", "Whisper small")])
        self.assertEqual(self.notes[0], "Reusing 2 transcripts: 1 from one.json (made with Whisper small), "
                                        "1 from two.json (made with Whisper medium); 0 left to transcribe.")
        other = self.root / "other/transcripts.json"
        self.run_export(output=other, reuse=[two, one])
        self.assertEqual(transcripts(other), [("one: first", "Whisper small"), ("one: second", "Whisper small")])
        self.assertEqual(self.notes[0], "Reusing 2 transcripts from one.json (made with Whisper small); 0 left to transcribe.")
        # The report already at the output counts as given last, and keeps saying what made each transcript.
        summary = self.run_export(reuse=[two, one, one])
        self.assertEqual(transcripts(), [("two: first", "Whisper medium"), ("one: second", "Whisper small")])
        self.assertEqual(self.notes, [
            "Reusing 2 transcripts from transcripts.json (made with Whisper medium and Whisper small); 0 left to transcribe. "
            "--fresh transcribes everything again.",
        ])
        self.assertEqual(summary["reused"], 2)
        # Starting over sets that report aside, not the ones given.
        self.run_export(reuse=[two, one], fresh=True)
        self.assertEqual(transcripts(), [("one: first", "Whisper small"), ("one: second", "Whisper small")])
        self.assertEqual(self.notes[0], "Reusing 2 transcripts from one.json (made with Whisper small); 0 left to transcribe.")
        # A recording whose size the last report got from another file falls back on the earlier report too.
        two = save_report(self.root / "two.json", [earlier(first_name, "two: first", bytes=1), earlier(second_name, "two: second", bytes=6)])
        self.run_export(reuse=[one, two], fresh=True)
        self.assertEqual(transcripts(), [("one: first", "Whisper small"), ("two: second", None)])

    def test_a_file_that_the_run_writes_is_not_read_as_an_earlier_report(self):
        annotated = (self.root / "results/chat_with_transcripts.txt").resolve()
        for path in (self.output.with_suffix(".txt"), annotated):
            with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, "--reuse must differ from the files this run writes"):
                self.run_export(chat_output=annotated, reuse=[save_report(self.root / "earlier.json", self.complete), path])
        self.assertFalse(self.output.parent.exists())

    def test_the_annotated_chat_an_earlier_run_left_in_the_export_is_written_again(self):
        export = self.export.resolve()
        chat = export / "chat.txt"
        # A byte order mark before the first message and no line ending after the last, as some exports have.
        chat.write_bytes(codecs.BOM_UTF8 + self.chat.replace("\n", "\r\n").rstrip("\r\n").encode("utf-8"))
        original = chat.read_bytes()
        output, annotated = export / "transcripts.json", export / "chat_with_transcripts.txt"
        self.run_export(export, output=output, chat_output=annotated, reuse=[save_report(self.root / "earlier.json", self.complete)])
        first = annotated.read_bytes()
        self.assertTrue(first.startswith(codecs.BOM_UTF8 + b"01/10/26, 10:00 - Ana: PTT-20261001-WA0001.opus (attached)\r\n[Voice message transcript: uno]\r\n"))
        self.assertTrue(first.endswith(b"(attached)\r\n[Voice message transcript: dos]\r\n"))
        # The copy is neither a second chat nor an original, and gets no transcript twice.
        summary = self.run_export(export, output=output, chat_output=annotated)
        self.assertEqual((summary["chats"], summary["reused"], summary["chat_output"]), (1, 2, annotated))
        self.assertEqual((annotated.read_bytes(), chat.read_bytes()), (first, original))
        self.assertEqual(self.report(output)["results"][0]["messages"][0]["chat_file"], "chat.txt")
        with self.assertRaisesRegex(ValueError, "must not overwrite an original chat or audio file"):
            self.run_export(export, output=output, chat_output=chat)
        self.assertEqual(chat.read_bytes(), original)

    def test_reports_that_share_a_name_are_told_apart_by_their_folders(self):
        stopped = save_report(self.root / "1/transcripts.json", [earlier("PTT-20261001-WA0001.opus", "uno")])
        complete = save_report(self.root / "2/transcripts.json", self.complete, model="Whisper small")
        self.run_export(reuse=[complete, stopped])
        self.assertEqual(self.notes[0], "Reusing 2 transcripts: 1 from 2/transcripts.json (made with Whisper small), "
                                        "1 from 1/transcripts.json (made with Whisper large-v3-turbo); 0 left to transcribe.")
        self.run_export(reuse=[complete])
        self.assertEqual(self.notes, ["Reusing 2 transcripts from results/transcripts.json (made with Whisper large-v3-turbo and Whisper small); "
                                      "0 left to transcribe. --fresh transcribes everything again."])
        # Whatever the folders are called, nothing else of a path is shown.
        self.assertNotIn(str(self.root), " ".join(self.notes))

    def test_recordings_that_share_a_name_are_told_apart_by_their_folders(self):
        shared = self.root / "shared"
        for name in ("first/voice.opus", "second/voice.opus", "other.opus"):
            self.touch(f"shared/{name}")

        def listing(results):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                self.assertIsNone(self.run_export(shared, reuse=[save_report(self.root / "earlier.json", results)], dry_run=True))
            return stdout.getvalue().splitlines()

        self.assertEqual(listing([earlier("second/voice.opus", "dos"), earlier("media/other.opus", "tres")]),
                         ["first/voice.opus", "other.opus", "  (already transcribed)", "second/voice.opus", "  (already transcribed)"])
        self.assertEqual(self.notes[0], "Reusing 2 transcripts from earlier.json (made with Whisper large-v3-turbo); 1 left to transcribe.")
        # A report that names no folder cannot say which of the two it transcribed.
        self.assertEqual(listing([earlier("voice.opus", "uno"), earlier("other.opus", "tres")]),
                         ["first/voice.opus", "other.opus", "  (already transcribed)", "second/voice.opus"])
        # A dry run writes nothing and reports no progress.
        self.assertFalse(self.output.parent.exists())
        self.assertEqual(self.steps, [])

    def test_a_dry_run_lists_messages_and_marks_what_is_already_transcribed(self):
        save_report(self.output, [earlier("PTT-20261001-WA0002.opus", "dos")])
        before = self.output.read_bytes()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertIsNone(self.run_export(dry_run=True))
        self.assertEqual(stdout.getvalue(), (
            "PTT-20261001-WA0001.opus\n  01/10/26, 10:00 | Ana\n"
            "PTT-20261001-WA0002.opus\n  01/10/26, 10:01 | José\n  (already transcribed)\n"
        ))
        self.assertEqual(self.notes, [
            "Reusing 1 transcript from transcripts.json (made with Whisper large-v3-turbo); 1 left to transcribe. --fresh transcribes everything again.",
            "1 earlier transcript was matched by file name only.",
        ])
        with redirect_stdout(io.StringIO()) as stdout:
            self.run_export(dry_run=True, fresh=True)
        self.assertNotIn("already transcribed", stdout.getvalue())
        self.assertEqual(self.notes, [])
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual([path.name for path in self.output.parent.iterdir()], ["transcripts.json"])

    def test_an_output_holding_transcripts_of_other_recordings_is_not_replaced(self):
        failed = {"file": "PTT-20250101-WA0009.opus", "messages": [], "status": "error", "text": "", "error": "Could not decode audio"}
        save_report(self.output, [earlier("PTT-20261001-WA0001.opus", "uno"), earlier("PTT-20250101-WA0007.opus", "another chat"),
                                  earlier("Media/PTT-20250101-WA0008.opus", "another chat"), failed])
        before = self.output.read_bytes()
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), self.assertRaises(ValueError) as error:
                self.run_export(dry_run=dry_run)
            self.assertEqual(str(error.exception), "transcripts.json holds 2 transcripts of recordings that are not in this export. "
                                                   "Choose another --output, or pass --fresh to replace it.")
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual([path.name for path in self.output.parent.iterdir()], ["transcripts.json"])
        # Given as a report to take from, it is only read: what else it holds is left where it is.
        other = self.root / "other/transcripts.json"
        with redirect_stdout(io.StringIO()) as stdout:
            self.run_export(output=other, reuse=[self.output], dry_run=True)
        self.assertEqual(stdout.getvalue().count("(already transcribed)"), 1)
        # Starting over is the way to replace it.
        with redirect_stdout(io.StringIO()) as stdout:
            self.run_export(dry_run=True, fresh=True)
        self.assertEqual(stdout.getvalue().count("(already transcribed)"), 0)

    def test_a_file_that_is_not_a_report_ends_the_run_unless_it_stands_at_the_output(self):
        notes = self.touch("private folder/notes.json", b'{"results": "none"}')
        with self.assertRaises(ValueError) as error:
            self.run_export(reuse=[save_report(self.root / "earlier.json", self.complete), notes])
        self.assertEqual(str(error.exception), "notes.json is not a VoxPad transcript report.")
        self.assertFalse(self.output.parent.exists())
        # At the output it is what the report replaces, as it always was; the run says so first.
        self.output.parent.mkdir()
        self.output.write_text("an earlier report", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.run_export(dry_run=True)
        self.assertEqual(self.notes, [])
        self.assertEqual(self.output.read_text(encoding="utf-8"), "an earlier report")
        self.run_export(reuse=[save_report(self.root / "earlier.json", self.complete)])
        self.assertEqual(self.notes[0], "transcripts.json is not a VoxPad transcript report. It will be replaced.")
        self.assertEqual([result["text"] for result in self.report()["results"]], ["uno", "dos"])


@unittest.skipUnless(codecpod is not None and np is not None, "Install codecpod and NumPy for audio conversion tests")
class ResumeTests(ExportTestCase):
    """Runs that take some transcripts from earlier reports and transcribe the rest with a fake model."""

    def setUp(self):
        super().setUp()
        # Outputs are recognized inside an export by their resolved paths, as the command passes them.
        self.root = self.root.resolve()
        self.export = self.root / "export"
        self.export.mkdir()
        for name in ("a.wav", "b.wav", "c.wav"):
            write_wav(self.export / name, frames=1600)
        self.size = (self.export / "a.wav").stat().st_size
        self.chat = "01/10/26, 10:00 - Ana: a.wav (attached)\n01/10/26, 10:01 - José: b.wav (attached)\n01/10/26, 10:02 - Ana: c.wav (attached)\n"
        (self.export / "chat.txt").write_bytes(self.chat.encode("utf-8"))
        self.output = self.root / "results/transcripts.json"
        self.annotated = self.root / "results/chat_with_transcripts.txt"

    def run_export(self, *, model=None, **options):
        self.model = model or FakeWhisper()
        self.notes, self.steps = [], []
        self.constructor = mock.Mock(return_value=self.model)
        with mock.patch.object(transcribe, "Whisper", self.constructor):
            return transcribe_export(self.export, self.output, self.annotated, notify=self.notes.append,
                                     progress=lambda done, total: self.steps.append((done, total)), **options)

    def report(self):
        return json.loads(self.output.read_text(encoding="utf-8"))

    def transcripts(self):
        return [(result["file"], result["text"]) for result in self.report()["results"]]

    def annotated_with(self, **transcripts):
        """The chat with a transcript after the message of each recording named."""
        text = self.chat
        for name, transcript in transcripts.items():
            text = text.replace(f"{name}.wav (attached)\n", f"{name}.wav (attached)\n[Voice message transcript: {transcript}]\n")
        return text

    def test_a_stopped_run_continues_where_it_ended(self):
        answers = iter([False, True])
        stopped = self.run_export(should_stop=lambda: next(answers))
        self.assertEqual((stopped["stopped"], stopped["reused"], self.transcripts()), (True, 0, [("a.wav", "part 1")]))
        self.assertEqual(self.steps, [(0, 3), (1, 3)])
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), self.annotated_with(a="part 1"))
        watching = Watching(self.output)
        summary = self.run_export(model=watching)
        self.constructor.assert_called_once_with("large-v3-turbo")
        self.assertEqual(self.notes, [
            "Reusing 1 transcript from transcripts.json (made with Whisper large-v3-turbo); 2 left to transcribe. --fresh transcribes everything again.",
            "Loading Whisper large-v3-turbo (the first run downloads the model)...",
            # Each recording keeps its place in the export.
            "[2/3] b.wav",
            "[3/3] c.wav",
        ])
        # What was reused counts as done from the start.
        self.assertEqual(self.steps, [(1, 3), (2, 3), (3, 3)])
        self.assertEqual({key: summary[key] for key in ("total", "reused", "failures", "stopped", "refusal", "chats")},
                         {"total": 3, "reused": 1, "failures": 0, "stopped": False, "refusal": None, "chats": 1})
        self.assertEqual(len(watching.calls), 2)
        self.assertEqual(self.transcripts(), [("a.wav", "part 1"), ("b.wav", "part 1"), ("c.wav", "part 2")])
        report = self.report()
        self.assertEqual(summary["report"], report)
        self.assertEqual([result["bytes"] for result in report["results"]], [self.size] * 3)
        self.assertEqual([result["messages"][0]["sender"] for result in report["results"]], ["Ana", "José", "Ana"])
        # One model made them all, so no transcript needs to say which.
        self.assertEqual([key for result in report["results"] for key in result if key == "model"], [])
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), self.annotated_with(a="part 1", b="part 1", c="part 2"))
        self.assertEqual(self.output.with_suffix(".txt").read_text(encoding="utf-8").count("part"), 3)
        # Once complete, the same call finds nothing to do and loads nothing.
        summary = self.run_export()
        self.constructor.assert_not_called()
        self.assertEqual((summary["reused"], self.steps, self.report()), (3, [(3, 3)], report))

    def test_reused_transcripts_are_saved_before_anything_is_transcribed_and_in_the_order_of_the_export(self):
        imported = save_report(self.root / "earlier.json", [earlier("c.wav", "tres")], model="Whisper small")
        watching = Watching(self.output)
        summary = self.run_export(model=watching, reuse=[imported])
        self.assertEqual(watching.seen, [["c.wav"], ["a.wav", "c.wav"]])
        self.assertEqual(self.transcripts(), [("a.wav", "part 1"), ("b.wav", "part 2"), ("c.wav", "tres")])
        self.assertEqual(self.notes, [
            "Reusing 1 transcript from earlier.json (made with Whisper small); 2 left to transcribe.",
            "1 earlier transcript was matched by file name only.",
            "Loading Whisper large-v3-turbo (the first run downloads the model)...",
            "[1/3] a.wav",
            "[2/3] b.wav",
        ])
        self.assertEqual(self.steps, [(1, 3), (2, 3), (3, 3)])
        report = self.report()
        self.assertEqual(report["model"], "Whisper large-v3-turbo")
        self.assertEqual([result.get("model") for result in report["results"]], [None, None, "Whisper small"])
        self.assertEqual(summary["reused"], 1)
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), self.annotated_with(a="part 1", b="part 2", c="tres"))

    def test_stopping_at_once_keeps_the_reused_transcripts(self):
        imported = save_report(self.root / "earlier.json", [earlier("b.wav", "dos")])
        summary = self.run_export(reuse=[imported], should_stop=lambda: True)
        self.assertEqual((summary["stopped"], summary["reused"], summary["total"], summary["failures"]), (True, 1, 3, 0))
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.transcripts(), [("b.wav", "dos")])
        self.assertEqual(self.steps, [(1, 3)])
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), self.annotated_with(b="dos"))
        # Stopped again later, the run keeps what it took over and what it added.
        answers = iter([False, True])
        summary = self.run_export(should_stop=lambda: next(answers))
        self.assertEqual((summary["stopped"], summary["reused"]), (True, 1))
        self.assertEqual(self.transcripts(), [("a.wav", "part 1"), ("b.wav", "dos")])
        self.assertEqual(self.steps, [(1, 3), (2, 3)])

    def test_a_recording_that_failed_earlier_is_transcribed_again(self):
        failed = {"file": "b.wav", "bytes": self.size, "messages": [], "status": "error", "text": "", "error": "Could not decode audio"}
        save_report(self.output, [earlier("a.wav", "uno", bytes=self.size), failed, earlier("c.wav", "tres", bytes=self.size)])
        summary = self.run_export()
        self.assertEqual(self.transcripts(), [("a.wav", "uno"), ("b.wav", "part 1"), ("c.wav", "tres")])
        self.assertEqual((summary["reused"], summary["failures"]), (2, 0))
        self.assertEqual(self.notes, [
            "Reusing 2 transcripts from transcripts.json (made with Whisper large-v3-turbo); 1 left to transcribe. --fresh transcribes everything again.",
            "Loading Whisper large-v3-turbo (the first run downloads the model)...",
            "[2/3] b.wav",
        ])
        self.assertEqual(self.steps, [(2, 3), (3, 3)])
        self.assertNotIn("error", self.report()["results"][1])

    def test_a_recording_that_changed_since_it_was_transcribed_is_transcribed_again(self):
        save_report(self.output, [earlier("a.wav", "uno", bytes=self.size), earlier("b.wav", "of another file", bytes=self.size + 1),
                                  earlier("c.wav", "tres")])
        summary = self.run_export()
        self.assertEqual(self.transcripts(), [("a.wav", "uno"), ("b.wav", "part 1"), ("c.wav", "tres")])
        self.assertEqual(summary["reused"], 2)
        self.assertEqual(self.notes[:3], [
            "Reusing 2 transcripts from transcripts.json (made with Whisper large-v3-turbo); 1 left to transcribe. --fresh transcribes everything again.",
            # The third had no size to compare.
            "1 earlier transcript was matched by file name only.",
            "1 recording changed since it was transcribed; it is transcribed again.",
        ])
        self.assertEqual([result["bytes"] for result in self.report()["results"]], [self.size] * 3)

    def test_a_transcript_to_be_replaced_stays_in_the_report_until_its_recording_is_reached(self):
        words = [{"word": "tres", "start": 0.0, "end": 0.1, "probability": 0.8}]
        save_report(self.output, [earlier("a.wav", "uno"), earlier("b.wav", "of another file", bytes=self.size + 1),
                                  earlier("c.wav", "tres", words=words)])
        # Stopped before the first recording: neither the transcript without word times nor the one of a changed file is lost.
        summary = self.run_export(word_timestamps=True, should_stop=lambda: True)
        self.assertEqual(self.transcripts(), [("a.wav", "uno"), ("b.wav", "of another file"), ("c.wav", "tres")])
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), self.annotated_with(a="uno", b="of another file", c="tres"))
        self.assertEqual((summary["reused"], summary["done"], summary["stopped"]), (1, 1, True))
        # The kept entry still says which file it was made from, so the next run sees the change again.
        self.assertEqual([result.get("bytes") for result in self.report()["results"]], [None, self.size + 1, self.size])
        self.run_export()
        self.assertEqual(self.transcripts(), [("a.wav", "uno"), ("b.wav", "part 1"), ("c.wav", "tres")])
        self.assertEqual([result["bytes"] for result in self.report()["results"]], [self.size] * 3)

    def test_word_times_are_not_taken_from_a_transcript_without_them(self):
        words = [{"word": "tres", "start": 0.0, "end": 0.1, "probability": 0.8}]
        save_report(self.output, [earlier("a.wav", "uno"), earlier("c.wav", "tres", words=words)])
        summary = self.run_export(word_timestamps=True)
        self.assertEqual(self.transcripts(), [("a.wav", "part 1"), ("b.wav", "part 2"), ("c.wav", "tres")])
        self.assertEqual(summary["reused"], 1)
        self.assertIn("1 earlier transcript has no word timestamps; that recording is transcribed again.", self.notes)
        results = self.report()["results"]
        self.assertEqual([word["word"] for result in results for word in result["words"]], ["part1", "part2", "tres"])
        # Without the option every transcript will do, with or without word times.
        save_report(self.output, [earlier("a.wav", "uno"), earlier("b.wav", "dos"), earlier("c.wav", "tres", words=words)])
        self.run_export()
        self.constructor.assert_not_called()
        self.assertEqual(["words" in result for result in self.report()["results"]], [False, False, True])

    def test_a_download_of_the_browser_app_continues_with_its_pending_and_failed_recordings(self):
        message = {"chat_file": "export/chat.txt", "timestamp": "01/10/26, 10:00", "sender": "Ana"}
        download = self.root / "voxpad-transcripts.json"
        download.write_text(json.dumps({
            "schema_version": 1, "source": "export.zip", "sample_rate": 16000,
            "chats": [{"file": "export/chat.txt", "original_text": self.chat, "annotated_text": self.chat, "messages": []}],
            "results": [
                {"file": "export/a.wav", "messages": [message], "status": "ok", "text": "uno", "languages": ["es"],
                 "segments": [{"start": 0, "end": 0.1, "text": "uno"}], "duration_seconds": 0.1},
                {"file": "export/b.wav", "messages": [], "status": "pending", "text": "", "languages": [], "segments": []},
                {"file": "export/c.wav", "messages": [], "status": "error", "text": "", "languages": [], "segments": [], "error": "Out of memory"},
            ],
            "warnings": [],
            "model": {"name": "Whisper small", "repository": "onnx-community/whisper-small", "runtime": "WebGPU"},
        }), encoding="utf-8")
        summary = self.run_export(reuse=[download])
        self.assertEqual(self.notes[0], "Reusing 1 transcript from voxpad-transcripts.json (made with Whisper small); 2 left to transcribe.")
        self.assertEqual(self.transcripts(), [("a.wav", "uno"), ("b.wav", "part 1"), ("c.wav", "part 2")])
        self.assertEqual((summary["reused"], summary["failures"]), (1, 0))
        first = self.report()["results"][0]
        self.assertEqual((first["model"], first["duration_seconds"], first["bytes"], first["messages"][0]["chat_file"]),
                         ("Whisper small", 0.1, self.size, "chat.txt"))

    def test_a_run_that_cannot_start_writes_nothing_and_keeps_the_report_it_would_continue(self):
        imported = save_report(self.root / "earlier.json", [earlier("a.wav", "uno")])
        missing = mock.Mock(side_effect=RuntimeError("Install the Python dependencies: python -m pip install -r requirements.txt"))
        notes = []
        with mock.patch.object(transcribe, "Whisper", missing), self.assertRaisesRegex(RuntimeError, "Install the Python dependencies"):
            transcribe_export(self.export, self.output, self.annotated, reuse=[imported], notify=notes.append)
        missing.assert_called_once()
        self.assertFalse(self.output.parent.exists())
        # Neither is the report replaced that the run was going to continue.
        save_report(self.output, [earlier("a.wav", "uno")])
        before = self.output.read_bytes()
        service = FakeService(token=None)
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}), self.assertRaises(RemoteTokenNeeded):
            transcribe_export(self.export, self.output, self.annotated, remote="deepinfra", notify=notes.append)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual([path.name for path in self.output.parent.iterdir()], ["transcripts.json"])
        self.assertEqual((service.clients, service.calls), ([], []))

    def test_an_interruption_keeps_what_is_done_and_is_passed_on(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_export(model=Interrupted(after=1))
        self.assertEqual(self.transcripts(), [("a.wav", "part 1")])
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), self.annotated_with(a="part 1"))
        self.assertEqual(self.output.with_suffix(".txt").read_text(encoding="utf-8"), "a.wav\n  01/10/26, 10:00 | Ana\npart 1\n")
        # The last call says how far the run got.
        self.assertEqual(self.steps, [(0, 3), (1, 3), (1, 3)])
        self.assertEqual(sorted(path.name for path in self.output.parent.iterdir()),
                         ["chat_with_transcripts.txt", "transcripts.json", "transcripts.txt"])
        # Interrupted again after taking that transcript over, nothing of it is lost.
        with self.assertRaises(KeyboardInterrupt):
            self.run_export(model=Interrupted(after=1))
        self.assertEqual(self.transcripts(), [("a.wav", "part 1"), ("b.wav", "part 1")])
        self.assertEqual(self.steps, [(1, 3), (2, 3), (2, 3)])
        summary = self.run_export()
        self.assertEqual((summary["reused"], summary["stopped"]), (2, False))
        self.assertEqual(self.transcripts(), [("a.wav", "part 1"), ("b.wav", "part 1"), ("c.wav", "part 1")])
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), self.annotated_with(a="part 1", b="part 1", c="part 1"))

    def test_starting_over_transcribes_everything_and_replaces_the_report(self):
        save_report(self.output, [earlier("a.wav", "uno"), earlier("PTT-20250101-WA0007.opus", "another chat")])
        summary = self.run_export(fresh=True)
        self.assertEqual((summary["reused"], self.transcripts()), (0, [("a.wav", "part 1"), ("b.wav", "part 2"), ("c.wav", "part 3")]))
        self.assertEqual(self.notes, ["Loading Whisper large-v3-turbo (the first run downloads the model)...", "[1/3] a.wav", "[2/3] b.wav", "[3/3] c.wav"])
        self.assertEqual(self.steps, [(0, 3), (1, 3), (2, 3), (3, 3)])

    def test_the_same_call_may_write_its_annotated_chat_inside_the_export_again(self):
        self.output = self.export / "transcripts.json"
        self.annotated = self.export / "chat_with_transcripts.txt"
        answers = iter([False, True])
        self.run_export(should_stop=lambda: next(answers))
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), self.annotated_with(a="part 1"))
        # The copy the first run left is neither taken for a second chat nor for an original.
        summary = self.run_export()
        self.assertEqual((summary["total"], summary["reused"], summary["chats"], summary["chat_output"]), (3, 1, 1, self.annotated))
        complete = self.annotated_with(a="part 1", b="part 1", c="part 2")
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), complete)
        self.assertEqual([result["messages"] for result in self.report()["results"]], [
            [{"chat_file": "chat.txt", "timestamp": f"01/10/26, 10:0{minute}", "sender": sender}] for minute, sender in enumerate(("Ana", "José", "Ana"))
        ])
        self.run_export()
        self.constructor.assert_not_called()
        self.assertEqual(self.annotated.read_text(encoding="utf-8"), complete)
        self.assertEqual((self.export / "chat.txt").read_text(encoding="utf-8"), self.chat)
        # A chat that is no such copy stays protected, whatever transcripts it ends with.
        other = self.export / "other chat.txt"
        other.write_text(self.chat.replace("José", "Eva") + "[Voice message transcript: typed by hand]\n", encoding="utf-8")
        for protected in (self.export / "chat.txt", other):
            with self.subTest(protected=protected.name):
                before = protected.read_bytes()
                self.annotated = protected
                with self.assertRaisesRegex(ValueError, "must not overwrite an original chat or audio file"):
                    self.run_export()
                self.assertEqual(protected.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
