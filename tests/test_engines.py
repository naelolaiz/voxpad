"""Tests for the Whisper adapters; no model is downloaded and nothing is uploaded."""

import io
import struct
import sys
import types
import unittest
from unittest import mock
import wave

from voxpad import engines
from tests.support import FakeService, codecpod, http_error, np


class WhisperTests(unittest.TestCase):
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
            model = engines.Whisper("small")
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


@unittest.skipUnless(codecpod is not None and np is not None, "Install codecpod and NumPy for audio conversion tests")
class RemoteTests(unittest.TestCase):
    """The hosted service is replaced by a fake: these tests never connect anywhere."""

    def engine(self, service, *arguments):
        with mock.patch.dict(sys.modules, {"huggingface_hub": service.module()}):
            return engines.RemoteWhisper(*arguments)

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
                (recording.getnchannels(), recording.getsampwidth(), recording.getframerate()), (1, 2, engines.SAMPLE_RATE))
            frames = recording.readframes(recording.getnframes())
        # Samples beyond full scale are clipped instead of wrapping around.
        self.assertEqual(struct.unpack("<4h", frames), (0, 16384, 32767, -32767))

    def test_adapter_does_not_start_without_a_usable_token(self):
        service = FakeService(token=None)
        with self.assertRaisesRegex(engines.RemoteTokenNeeded, "HF_TOKEN"):
            self.engine(service)
        # Anything not shaped like Hugging Face's tokens would be sent to DeepInfra directly, and a
        # line break would get the token quoted in an error: such a token is neither used nor repeated.
        for token in ('"hf_quoted"', "HF_TOKEN=hf_pasted", "Bearer hf_pasted", "hf_two\nlines", "hf_with space", "sk-other"):
            with self.subTest(token=token):
                with self.assertRaises(RuntimeError) as caught:
                    self.engine(service, "large-v3-turbo", "deepinfra", token)
                self.assertIn("starts with hf_", str(caught.exception))
                self.assertNotIn(token, str(caught.exception))
        self.assertIsNotNone(engines.token_problem("hf_"))
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
                self.assertEqual(isinstance(caught.exception, engines.RemoteRefused), refused)
                message = str(caught.exception)
                self.assertTrue(message.startswith("DeepInfra through Hugging Face did not transcribe the audio: "))
                self.assertIn(reason, message)
                self.assertNotIn("\n", message)
                self.assertNotIn("hf_secret", message)
        # A failed request releases the client as well.
        self.assertEqual(service.closed, len(cases))


if __name__ == "__main__":
    unittest.main()
