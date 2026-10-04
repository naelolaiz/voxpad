"""VoxPad desktop application: choose an export, transcribe it, read the result.

Whisper runs on this computer unless a hosted service is chosen in the window.
Run with `voxpad-app`, `python -m voxpad.gui` or `python voxpad/gui.py`. The
window is built with Qt (PySide6), installed with the other dependencies.
"""

import argparse
from pathlib import Path
import queue
import sys
import threading
import zipfile

try:
    from . import whatsapp
except ImportError:  # Started as a file: python voxpad/gui.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import whatsapp


MODELS = (
    ("large-v3-turbo", "Most accurate (about 1.6 GB download)"),
    ("medium", "Accurate, slower (about 1.5 GB download)"),
    ("small", "Balanced (about 480 MB download)"),
    ("base", "Fast (about 150 MB download)"),
    ("tiny", "Fastest, least accurate (about 80 MB download)"),
)
AUTOMATIC = "Detect automatically"
# Common languages first; every Whisper language code can also be typed.
LANGUAGES = (
    ("es", "Spanish"), ("en", "English"), ("pt", "Portuguese"), ("fr", "French"), ("de", "German"),
    ("it", "Italian"), ("nl", "Dutch"), ("pl", "Polish"), ("ca", "Catalan"), ("ru", "Russian"),
    ("uk", "Ukrainian"), ("tr", "Turkish"), ("ar", "Arabic"), ("hi", "Hindi"), ("zh", "Chinese"),
    ("ja", "Japanese"), ("ko", "Korean"),
)
REPORT_NAME = "transcripts.json"
CHAT_NAME = "chat_with_transcripts.txt"
LOCAL = "This computer"
# Where Whisper runs. Every choice that sends the audio elsewhere says so.
PLACES = ((LOCAL, None), *((f"{name} (uploads the audio)", service) for service, name in whatsapp.REMOTE_SERVICES.items()))
EXPORT_HINT = "Export the chat with media included, then choose it or drop it here."


def default_output_folder(source: Path) -> Path:
    """A new folder beside the input, never inside an export folder."""
    name = source.stem if source.is_file() else source.name
    return source.parent / f"{name} transcripts"


def language_code(choice: str) -> str | None:
    """Turn the language box's text into a Whisper code, or None to detect it."""
    text = choice.strip()
    if not text or text == AUTOMATIC:
        return None
    for code, name in LANGUAGES:
        if text.lower() in (name.lower(), code):
            return code
    if text.lower() in whatsapp.LANGUAGES:
        return text.lower()
    raise ValueError(f"Unknown language: {text}. Choose one from the list or type a Whisper language code such as es.")


def model_name(choice: str) -> str:
    """Turn the model box's text into a model name; other text is used as typed."""
    for name, description in MODELS:
        if choice in (name, f"{name} — {description}"):
            return name
    return choice.strip() or whatsapp.DEFAULT_MODEL


def remote_service(choice: str) -> str | None:
    """Turn the "Run on" box's text into a hosted service, or None for this computer."""
    return dict(PLACES).get(choice)


def privacy_note(remote: str | None) -> str:
    """What the window says about where the voice messages go."""
    if not remote:
        return f"Everything runs on this computer. {EXPORT_HINT}"
    return (f"Voice messages are uploaded to {whatsapp.REMOTE_SERVICES[remote]} to be transcribed; "
            f"the chat text stays on this computer. {EXPORT_HINT}")


def saved_token() -> bool:
    """Whether HF_TOKEN or a saved Hugging Face login provides an access token."""
    try:
        from huggingface_hub import get_token
    except ImportError:
        return False
    return bool(get_token())


def remote_problem(remote: str | None, *, language: str | None, model: str, token: str | None) -> str | None:
    """Say, in the window's words, why the chosen hosted service cannot start."""
    if not remote:
        return None
    if language and remote not in whatsapp.REMOTE_LANGUAGE_SERVICES:
        return (f"{whatsapp.REMOTE_SERVICES[remote]} cannot be told the language. "
                f"Choose “{AUTOMATIC}”, or run on another service.")
    if Path(model).is_dir():
        return "A model folder cannot be used on a hosted service. Choose a Whisper size or type a Hugging Face repository name."
    if not token and not saved_token():
        return "Paste a Hugging Face access token that may call Inference Providers in the Token box."
    return None


class Job(threading.Thread):
    """Transcribe one export in the background and report through a queue.

    Events are tuples: ("status", text), ("progress", done, total),
    ("done", summary, preview) and ("failed", message).
    """

    def __init__(self, source: Path, folder: Path, *, model: str, language: str | None, events: queue.Queue,
                 remote: str | None = None, token: str | None = None):
        super().__init__(daemon=True)
        self.source = source
        self.folder = folder
        self.model = model
        self.language = language
        self.remote = remote
        self.token = token
        self.events = events
        self._stop_requested = threading.Event()

    def stop(self) -> None:
        self._stop_requested.set()

    def run(self) -> None:
        try:
            summary = whatsapp.transcribe_export(
                self.source, self.folder.resolve() / REPORT_NAME, self.folder.resolve() / CHAT_NAME,
                model=self.model, language=self.language, remote=self.remote, remote_token=self.token,
                chat_output_optional=True,
                notify=lambda text: self.events.put(("status", text)),
                progress=lambda done, total: self.events.put(("progress", done, total)),
                should_stop=self._stop_requested.is_set,
            )
            # Show the conversation when there is one, otherwise the plain report.
            shown = summary["chat_output"] or summary["output"].with_suffix(".txt")
            self.events.put(("done", summary, shown.read_bytes().decode("utf-8")))
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
            self.events.put(("failed", str(error)))
        except Exception as error:  # Keep the window usable and say what happened.
            self.events.put(("failed", f"Unexpected error: {error}"))


def summary_text(summary: dict) -> str:
    done = len(summary["report"]["results"])
    text = f"Transcribed {done - summary['failures']} of {summary['total']} voice messages"
    if summary["failures"]:
        text += f", {summary['failures']} failed"
    if summary["stopped"]:
        text += " (stopped early)"
    return f"{text}. Saved in {summary['output'].parent}"


def create_window(source: Path | None = None):
    """Build the window. Qt is imported here so the rest of the module works without it."""
    from PySide6 import QtCore, QtGui, QtWidgets

    class Window(QtWidgets.QWidget):
        def __init__(self):
            super().__init__()
            self.events: queue.Queue = queue.Queue()
            self.job: Job | None = None
            self.source: Path | None = None
            self.folder: Path | None = None
            self.setWindowTitle("VoxPad")
            self.resize(820, 620)
            self.setAcceptDrops(True)

            title = QtWidgets.QLabel("Turn WhatsApp voice messages into text")
            font = title.font()
            font.setPointSize(font.pointSize() + 5)
            font.setBold(True)
            title.setFont(font)
            self.subtitle = QtWidgets.QLabel(privacy_note(None))
            self.subtitle.setWordWrap(True)

            self.source_box = QtWidgets.QLineEdit(readOnly=True, placeholderText="No export chosen")
            self.file_button = QtWidgets.QPushButton("Choose ZIP or file…", clicked=self.choose_file)
            self.folder_button = QtWidgets.QPushButton("Choose folder…", clicked=self.choose_folder)
            self.folder_box = QtWidgets.QLineEdit(readOnly=True)
            self.output_button = QtWidgets.QPushButton("Change…", clicked=self.choose_output)
            self.language = QtWidgets.QComboBox(editable=True)
            self.language.addItems([AUTOMATIC, *(name for _, name in LANGUAGES)])
            self.model = QtWidgets.QComboBox(editable=True)
            self.model.addItems([f"{name} — {description}" for name, description in MODELS])
            # A pasted token is used for this session only; it is never saved.
            self.token = QtWidgets.QLineEdit(
                echoMode=QtWidgets.QLineEdit.EchoMode.Password, enabled=False,
                placeholderText="Hugging Face access token; leave empty to use HF_TOKEN or a saved login")
            self.place = QtWidgets.QComboBox()
            self.place.addItems([label for label, _ in PLACES])
            self.place.currentTextChanged.connect(self.place_changed)

            form = QtWidgets.QGridLayout()
            form.setColumnStretch(1, 1)
            for row, (label, widget, buttons) in enumerate((
                ("Export", self.source_box, (self.file_button, self.folder_button)),
                ("Save in", self.folder_box, (self.output_button,)),
                ("Language", self.language, ()),
                ("Model", self.model, ()),
                ("Run on", self.place, ()),
                ("Token", self.token, ()),
            )):
                form.addWidget(QtWidgets.QLabel(label), row, 0)
                form.addWidget(widget, row, 1)
                for column, button in enumerate(buttons, 2):
                    form.addWidget(button, row, column)

            self.start_button = QtWidgets.QPushButton("Transcribe", clicked=self.start, enabled=False, default=True)
            self.stop_button = QtWidgets.QPushButton("Stop", clicked=self.stop, enabled=False)
            self.open_button = QtWidgets.QPushButton("Open folder", clicked=self.open_folder, enabled=False)
            actions = QtWidgets.QHBoxLayout()
            for button in (self.start_button, self.stop_button, self.open_button):
                actions.addWidget(button)
            actions.addStretch(1)

            self.progress = QtWidgets.QProgressBar(minimum=0, maximum=1, value=0, textVisible=False)
            self.text = QtWidgets.QPlainTextEdit(readOnly=True)
            self.text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
            self.status = QtWidgets.QLabel("Choose an export to begin.")
            self.status.setWordWrap(True)

            layout = QtWidgets.QVBoxLayout(self)
            layout.setContentsMargins(18, 16, 18, 14)
            layout.addWidget(title)
            layout.addWidget(self.subtitle)
            layout.addSpacing(8)
            layout.addLayout(form)
            layout.addSpacing(6)
            layout.addLayout(actions)
            layout.addWidget(self.progress)
            layout.addWidget(self.text, 1)
            layout.addWidget(self.status)

            self.timer = QtCore.QTimer(self, interval=100, timeout=self.poll)
            self.timer.start()
            if source:
                self.set_source(source)

        def set_source(self, source: Path) -> None:
            self.source = source.expanduser().resolve()
            self.source_box.setText(str(self.source))
            self.set_folder(default_output_folder(self.source))
            self.start_button.setEnabled(self.job is None)
            self.status.setText("Ready.")

        def set_folder(self, folder: Path) -> None:
            self.folder = folder
            self.folder_box.setText(str(folder))

        def choose_file(self) -> None:
            patterns = " ".join(f"*{extension}" for extension in sorted(whatsapp.AUDIO_EXTENSIONS | {".zip", ".txt"}))
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Choose a WhatsApp export", "", f"WhatsApp export, chat or audio ({patterns});;All files (*)")
            if path:
                self.set_source(Path(path))

        def choose_folder(self) -> None:
            path = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose an extracted export folder")
            if path:
                self.set_source(Path(path))

        def choose_output(self) -> None:
            path = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose where to save the transcripts")
            if path:
                self.set_folder(Path(path))

        def dragEnterEvent(self, event) -> None:
            if self.job is None and any(url.isLocalFile() for url in event.mimeData().urls()):
                event.acceptProposedAction()

        def dropEvent(self, event) -> None:
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    self.set_source(Path(url.toLocalFile()))
                    event.acceptProposedAction()
                    return

        def place_changed(self, choice: str) -> None:
            remote = remote_service(choice)
            self.subtitle.setText(privacy_note(remote))
            self.token.setEnabled(remote is not None)

        def set_running(self, running: bool) -> None:
            for widget in (self.file_button, self.folder_button, self.output_button, self.language, self.model, self.place):
                widget.setEnabled(not running)
            self.token.setEnabled(not running and remote_service(self.place.currentText()) is not None)
            self.start_button.setEnabled(not running and self.source is not None)
            self.stop_button.setEnabled(running)

        def start(self) -> None:
            if self.job or not self.source or not self.folder:
                return
            try:
                language = language_code(self.language.currentText())
            except ValueError as error:
                QtWidgets.QMessageBox.critical(self, "VoxPad", str(error))
                return
            model = model_name(self.model.currentText())
            remote = remote_service(self.place.currentText())
            token = self.token.text().strip() or None
            problem = remote_problem(remote, language=language, model=model, token=token)
            if problem:
                QtWidgets.QMessageBox.critical(self, "VoxPad", problem)
                return
            if not self.source.exists():
                QtWidgets.QMessageBox.critical(self, "VoxPad", f"The export no longer exists: {self.source}")
                return
            self.text.clear()
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
            self.open_button.setEnabled(False)
            self.status.setText("Starting…")
            self.job = Job(self.source, self.folder, model=model, language=language, events=self.events,
                           remote=remote, token=token)
            self.set_running(True)
            self.job.start()

        def stop(self) -> None:
            if self.job:
                self.job.stop()
                self.stop_button.setEnabled(False)
                self.status.setText("Stopping after the current voice message…")

        def open_folder(self) -> None:
            if self.folder:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.folder)))

        def handle(self, event: tuple) -> None:
            kind = event[0]
            if kind == "status":
                self.status.setText(event[1].strip())
                self.text.appendPlainText(event[1])
            elif kind == "progress":
                self.progress.setRange(0, max(event[2], 1))
                self.progress.setValue(event[1])
            elif kind == "done":
                self.text.setPlainText(event[2])
                self.status.setText(summary_text(event[1]))
                self.open_button.setEnabled(True)
            elif kind == "failed":
                self.status.setText("Could not transcribe this export.")
            if kind in ("done", "failed"):
                self.job = None
                self.set_running(False)
            if kind == "failed":
                QtWidgets.QMessageBox.critical(self, "VoxPad", event[1])
            elif kind == "done" and event[1].get("refusal"):
                # A hosted service ended the run: say why instead of leaving it in the transcript.
                QtWidgets.QMessageBox.warning(self, "VoxPad", event[1]["refusal"])

        def poll(self) -> None:
            try:
                while True:
                    self.handle(self.events.get_nowait())
            except queue.Empty:
                pass

        def closeEvent(self, event) -> None:
            buttons = QtWidgets.QMessageBox.StandardButton
            if self.job and QtWidgets.QMessageBox.question(
                    self, "VoxPad", "A transcription is running. Close anyway?") != buttons.Yes:
                event.ignore()
                return
            self.timer.stop()
            event.accept()

    return Window()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voxpad-app", description="VoxPad desktop application: transcribe WhatsApp voice messages.")
    parser.add_argument("source", nargs="?", type=Path, help="Export ZIP, folder, chat .txt or audio file to open")
    args = parser.parse_args(argv)
    try:
        from PySide6 import QtWidgets
    except ImportError as error:
        print(f"VoxPad's window needs PySide6, which could not be loaded: {error}\n"
              "Install the dependencies with: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    application.setApplicationName("VoxPad")
    window = create_window(args.source)
    window.show()
    return application.exec()


if __name__ == "__main__":
    sys.exit(main())
