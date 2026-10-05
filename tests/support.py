"""What the test modules share: recognizable recordings, fake engines and a folder to build an export in."""

import errno
from pathlib import Path
import struct
import tempfile
import types
import unittest
import warnings
import wave
import zipfile


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


class ExportTestCase(unittest.TestCase):
    """A temporary folder, `self.root`, and the means to fill it like an export."""

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
