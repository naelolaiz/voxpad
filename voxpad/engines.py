"""Whisper on this computer, or on a hosted service through Hugging Face."""

import io
from pathlib import Path
import re
import wave


LANGUAGES = tuple((
    "af am ar as az ba be bg bn bo br bs ca cs cy da de el en es et eu fa fi fo fr gl gu haw ha he hi hr ht hu hy id is "
    "it ja jw ka kk km kn ko la lb ln lo lt lv mg mi mk ml mn mr ms mt my ne nl nn no oc pa pl ps pt ro ru sa sd si sk "
    "sl sn so sq sr su sv sw ta te tg th tk tl tr tt uk ur uz vi yi yo zh"
).split())
DEFAULT_MODEL = "large-v3-turbo"
# Hosted services that can run Whisper instead of this computer, and who receives the audio.
REMOTE_SERVICES = {"deepinfra": "DeepInfra through Hugging Face", "hf-inference": "Hugging Face"}
# Of those, the services that can be told the spoken language; the others always detect it.
REMOTE_LANGUAGE_SERVICES = {"deepinfra"}
REMOTE_TIMEOUT_SECONDS = 120
# A Whisper size or an owner/name repository. A URL or a path would be used as the address to upload to.
REMOTE_MODEL = re.compile(r"[\w.-]+(/[\w.-]+)?", re.ASCII)
# Only a token of this shape is routed through Hugging Face, and it is safe to send as a header.
REMOTE_TOKEN = re.compile(r"hf_[\x21-\x7e]+")
SAMPLE_RATE = 16000


class Whisper:
    """Run a Whisper model through faster-whisper, one chunk of audio at a time."""

    def __init__(self, model: str = DEFAULT_MODEL):
        try:
            from faster_whisper import WhisperModel
        except ImportError as error:
            raise RuntimeError("Install the Python dependencies: python -m pip install -r requirements.txt") from error
        # Uses a GPU when one is available, and the fastest number format the device supports.
        self._model = WhisperModel(model, device="auto", compute_type="auto")

    def transcribe(self, samples, *, language: str | None = None, keywords: list[str] | None = None,
                   word_timestamps: bool = False) -> dict:
        segments, info = self._model.transcribe(
            samples, language=language, hotwords=" ".join(keywords) if keywords else None,
            word_timestamps=word_timestamps,
            # Chunks are independent; earlier text must not steer or repeat into later ones.
            condition_on_previous_text=False,
        )
        segments = list(segments)
        result = {"text": "".join(segment.text for segment in segments).strip(), "language": info.language}
        if word_timestamps:
            result["words"] = [
                {"word": word.word.strip(), "start": word.start, "end": word.end, "probability": word.probability}
                for segment in segments for word in segment.words or []
            ]
        return result


class RemoteRefused(RuntimeError):
    """A hosted service turned a request down for a reason the next recording would meet too."""


class RemoteTokenNeeded(RuntimeError):
    """No Hugging Face access token was given, set or saved."""


def remote_model(model: str) -> str:
    """Name a Whisper size as its Hugging Face repository; a repository is used as given."""
    # The local runtime's short names for these sizes are not repositories.
    model = {"turbo": "large-v3-turbo", "large": "large-v3"}.get(model, model)
    return model if "/" in model else f"openai/whisper-{model}"


def remote_model_problem(model: str) -> bool:
    """Whether a model is something other than a Whisper size or a repository name."""
    return Path(model).is_dir() or not REMOTE_MODEL.fullmatch(model)


def token_problem(token: str) -> str | None:
    """Say what is wrong with an access token's shape, without repeating it."""
    if REMOTE_TOKEN.fullmatch(token):
        return None
    return "That is not a Hugging Face access token: one starts with hf_ and has no spaces, quotes around it or line breaks."


def remote_conflict(remote: str | None, *, model: str, language: str | None, keywords: list[str] | None,
                    word_timestamps: bool) -> str | None:
    """Say which option the chosen hosted service cannot honor, if any."""
    if not remote:
        return None
    if remote not in REMOTE_SERVICES:
        return f"--remote must be one of: {', '.join(REMOTE_SERVICES)}"
    if keywords:
        return "--keyword is not available with --remote"
    if word_timestamps:
        return "--word-timestamps is not available with --remote"
    if language and remote not in REMOTE_LANGUAGE_SERVICES:
        return f"--language is not available with --remote {remote}; use --remote deepinfra, or let Whisper detect the language"
    if remote_model_problem(model):
        return "--remote needs a Whisper size or a Hugging Face repository as --model, not a folder, path or URL"
    return None


def wav_bytes(samples) -> bytes:
    """Encode 16 kHz mono samples as a 16-bit WAV file held in memory."""
    import numpy
    pcm = numpy.rint(numpy.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(SAMPLE_RATE)
        recording.writeframes(pcm.tobytes())
    return buffer.getvalue()


class RemoteWhisper:
    """Run Whisper on a hosted service through Hugging Face, uploading one chunk of audio at a time."""

    def __init__(self, model: str = DEFAULT_MODEL, service: str = "deepinfra", token: str | None = None):
        try:
            from huggingface_hub import HfApi, InferenceClient, get_token
        except ImportError as error:
            raise RuntimeError("Install the Python dependencies: python -m pip install -r requirements.txt") from error
        if remote_model_problem(model):
            raise RuntimeError("Remote transcription needs a Whisper size or a Hugging Face repository as the model.")
        # A token given here wins over HF_TOKEN and a saved login.
        token = token or get_token()
        if not token:
            raise RemoteTokenNeeded("Remote transcription needs a Hugging Face access token that may call Inference "
                                    "Providers: set HF_TOKEN or run `hf auth login`.")
        problem = token_problem(token)
        if problem:
            raise RuntimeError(problem)
        self._token = token
        self.model = remote_model(model)
        self.service = REMOTE_SERVICES[service]
        # Ask before anything is uploaded or any earlier report replaced: few Whisper sizes are hosted.
        try:
            offers = HfApi().model_info(self.model, expand=["inferenceProviderMapping"], token=token,
                                        timeout=REMOTE_TIMEOUT_SECONDS).inference_provider_mapping
        except Exception as error:  # A rejected token, an unknown repository or no connection.
            raise RuntimeError(f"Could not ask Hugging Face who offers {self.model}: {self._reason(error)}") from error
        if not any(offer.provider == service and offer.task == "automatic-speech-recognition" for offer in offers or []):
            raise RuntimeError(f"{self.service} does not offer {self.model} for transcription. "
                               f"Choose another model, such as {DEFAULT_MODEL}, or another service.")
        # Hugging Face's own service tells raw audio apart by this header; DeepInfra receives a form upload.
        headers = {"Content-Type": "audio/wav"} if service == "hf-inference" else None
        self._client = InferenceClient(provider=service, token=token, timeout=REMOTE_TIMEOUT_SECONDS, headers=headers)

    def _reason(self, error: Exception) -> str:
        """The explanation carried by an error, on one line and never with the access token."""
        if type(error).__name__ == "RepositoryNotFoundError":
            # The Hub answers "Invalid username or password" for a repository that does not exist as well.
            return "there is no such repository, or the access token was not accepted"
        reason = getattr(error, "server_message", None) or str(error) or type(error).__name__
        return " ".join(reason.replace(self._token, "<token>").split())

    def transcribe(self, samples, *, language: str | None = None, keywords: list[str] | None = None,
                   word_timestamps: bool = False) -> dict:
        audio = wav_bytes(samples)
        try:
            output = self._client.automatic_speech_recognition(
                audio, model=self.model, extra_body={"language": language} if language else None)
        except Exception as error:  # Network, service and response failures arrive as unrelated types.
            status = getattr(getattr(error, "response", None), "status_code", None)
            message = f"{self.service} did not transcribe the audio: {self._reason(error)}"
            # A rejected token, used-up credits or an answer that cannot be read fail every recording alike.
            if isinstance(error, ValueError) or (status is not None and 400 <= status < 500 and status not in (408, 429)):
                raise RemoteRefused(message) from error
            raise RuntimeError(message) from error
        finally:
            # The client otherwise keeps every uploaded chunk in memory until the run ends.
            self._client.close()
        # The service does not say which language it heard.
        return {"text": (output.text or "").strip(), "language": language or ""}
