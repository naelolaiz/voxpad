"""VoxPad desktop application: choose an export, transcribe it, read the result and open its viewer.

Whisper runs on this computer unless a hosted service is chosen in the window.
Run with `voxpad-app`, `python -m voxpad.gui` or `python voxpad/gui.py`. The
window is built with Qt (PySide6), installed with the other dependencies.
"""

import argparse
from pathlib import Path
import queue
import re
import sys
import threading
import zipfile

try:
    from . import engines, stats, transcribe
    from .analysis import parse_events
    from .export import AUDIO_EXTENSIONS
    from .reports import load_previous_results
except ImportError:  # Started as a file: python voxpad/gui.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from voxpad import engines, stats, transcribe
    from voxpad.analysis import parse_events
    from voxpad.export import AUDIO_EXTENSIONS
    from voxpad.reports import load_previous_results


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
VIEWER_NAME = "conversation.html"
LOCAL = "This computer"
# Where Whisper runs. Every choice that sends the audio elsewhere says so.
PLACES = ((LOCAL, None), *((f"{name} (uploads the audio)", service) for service, name in engines.REMOTE_SERVICES.items()))
EXPORT_HINT = "Export the chat with media included, then choose it or drop it here."
START_OVER = "Start over"
PLAYBACK = "Include voice messages for playback"
FAILURES = (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile)
# The commands' notices and errors name their options; the window names its own controls instead.
WORDING = tuple((re.compile(pattern, re.DOTALL), words) for pattern, words in (
    (r" --fresh transcribes everything again\.", f" Tick “{START_OVER}” to transcribe everything again."),
    (r"Choose another --output, or pass --fresh to replace it\.", f"Choose another folder to save in, or tick “{START_OVER}” to replace it."),
    (r"--reuse must differ from the files this run writes", "The earlier transcripts must not be one of the files this run writes."),
    (r"; --audio none skips this\.", f"; untick “{PLAYBACK}” to skip this."),
    (r"Pass an export ZIP, its folder or the chat \.txt\.", "Choose an export ZIP, its folder or the chat .txt."),
    (r"\. Pass the one to analyse, for example: voxpad-stats \".*\"\Z", ". Choose the one to view as the export."),
    (r" and pass the one to analyse, for example: voxpad-stats \".*\"\Z", " and choose the one to view as the export."),
))


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
    if text.lower() in engines.LANGUAGES:
        return text.lower()
    raise ValueError(f"Unknown language: {text}. Choose one from the list or type a Whisper language code such as es.")


def model_name(choice: str) -> str:
    """Turn the model box's text into a model name; other text is used as typed."""
    for name, description in MODELS:
        if choice in (name, f"{name} — {description}"):
            return name
    return choice.strip() or engines.DEFAULT_MODEL


def remote_service(choice: str) -> str | None:
    """Turn the "Run on" box's text into a hosted service, or None for this computer."""
    return dict(PLACES).get(choice)


def privacy_note(remote: str | None) -> str:
    """What the window says about where the voice messages go."""
    if not remote:
        return f"Everything runs on this computer. {EXPORT_HINT}"
    return (f"Voice messages are uploaded to {engines.REMOTE_SERVICES[remote]} to be transcribed; "
            f"the chat text stays on this computer. {EXPORT_HINT}")


def remote_problem(remote: str | None, *, language: str | None, model: str, token: str | None) -> str | None:
    """Say, in the window's words, why the chosen hosted service cannot start.

    Only what the window holds is checked here. Whether a token is saved, and whether
    the service offers the model, is found out by the job, away from the window's thread.
    """
    if not remote:
        return None
    if language and remote not in engines.REMOTE_LANGUAGE_SERVICES:
        return (f"{engines.REMOTE_SERVICES[remote]} cannot be told the language. "
                f"Choose “{AUTOMATIC}”, or run on another service.")
    if engines.remote_model_problem(model):
        return ("A hosted service needs a Whisper size or a Hugging Face repository name such as "
                "openai/whisper-large-v3-turbo as the model, not a folder or a web address.")
    return engines.token_problem(token) if token else None


def window_words(text: str) -> str:
    """Reword a notice or an error of the commands for the window, which has controls where they have options."""
    for pattern, words in WORDING:
        text = pattern.sub(lambda match: words, text)
    return text


def counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def transcripts_in(report: Path) -> int:
    """Count the transcripts a report holds; ValueError when the file is not a VoxPad report."""
    return sum(result["status"] == "ok" for result in load_previous_results([report])[0].results)


def saved_transcripts(folder: Path) -> int:
    """Count the transcripts already in an output folder, which the next run saving there reuses.

    0 when the folder holds no report; ValueError when the file standing there is not one of VoxPad's.
    """
    report = folder / REPORT_NAME
    try:
        return transcripts_in(report) if report.is_file() else 0
    except OSError:
        # A report that cannot be read is the run's to explain.
        return 0


def folder_note(saved: int | None, fresh: bool = False) -> str:
    """What the window says about the output folder: `saved` transcripts are there, or None for a file that is no report."""
    if saved is None:
        return f"{REPORT_NAME} in this folder is not a VoxPad transcript report; it will be replaced."
    if not saved:
        return ""
    it, they = ("it", "it") if saved == 1 else ("them", "they")
    return (f"This folder already holds {counted(saved, 'transcript')}; "
            + (f"“{START_OVER}” replaces {it}." if fresh else f"{they} will be reused."))


class Job(threading.Thread):
    """Transcribe one export in the background, or only write its viewer, and report through a queue.

    Events are tuples: ("status", text), ("progress", done, total),
    ("done", summary, preview), ("viewed", conversation, text) and ("failed", message).
    `summary` is transcribe_export's with two keys added: `viewer`, the page written or
    None, and `viewer_problem`, why it could not be written or None. `conversation` is
    write_conversation's result and `text` what voxpad-stats prints about it.
    """

    def __init__(self, source: Path, folder: Path, *, model: str = engines.DEFAULT_MODEL, language: str | None = None,
                 events: queue.Queue, remote: str | None = None, token: str | None = None,
                 reuse: Path | None = None, fresh: bool = False, events_file: Path | None = None,
                 playback: bool = True, view_only: bool = False):
        super().__init__(daemon=True)
        self.source = source
        self.folder = folder
        self.model = model
        self.language = language
        self.remote = remote
        self.token = token
        self.reuse = reuse
        self.fresh = fresh
        self.events_file = events_file
        self.playback = playback
        self.view_only = view_only
        self.events = events
        self._stop_requested = threading.Event()

    def stop(self) -> None:
        self._stop_requested.set()

    def notify(self, text: str) -> None:
        self.events.put(("status", window_words(text)))

    def write_viewer(self, transcripts: list[Path], **options) -> dict | None:
        return stats.write_conversation(
            self.source, self.folder.resolve() / VIEWER_NAME, transcripts=transcripts, events=self.events_file,
            audio="auto" if self.playback else "none", **options)

    def run_transcription(self) -> None:
        output, chat = self.folder.resolve() / REPORT_NAME, self.folder.resolve() / CHAT_NAME
        summary = transcribe.transcribe_export(
            self.source, output, chat,
            model=self.model, language=self.language, remote=self.remote, remote_token=self.token,
            chat_output_optional=True, reuse=[self.reuse] if self.reuse else [], fresh=self.fresh,
            notify=self.notify,
            progress=lambda done, total: self.events.put(("progress", done, total)),
            should_stop=self._stop_requested.is_set,
        )
        summary.update(viewer=None, viewer_problem=None)
        # Like the annotated chat, the viewer needs a conversation: recordings alone have none.
        if summary["chats"]:
            self.events.put(("status", "Writing the viewer…"))
            # The transcripts are saved: whatever happens here, the run is done and says what became of its viewer.
            try:
                conversation = self.write_viewer([output], excluded={output, output.with_suffix(".txt"), chat},
                                                 optional=True, notify=self.notify)
                summary["viewer"] = conversation and conversation["viewer"]
            except FAILURES as error:
                summary["viewer_problem"] = window_words(str(error))
            except Exception as error:
                summary["viewer_problem"] = f"Unexpected error: {error}"
        # Show the conversation when there is one, otherwise the plain report.
        shown = summary["chat_output"] or summary["output"].with_suffix(".txt")
        self.events.put(("done", summary, shown.read_bytes().decode("utf-8")))

    def run_view(self) -> None:
        notices = []

        def notify(text: str) -> None:
            notices.append(window_words(text))
            self.events.put(("status", notices[-1]))

        report = self.folder.resolve() / REPORT_NAME
        reports = [self.reuse] if self.reuse else []
        # The report in the output folder is the latest word on its recordings, so it is read last.
        if report.is_file() and report not in [path.resolve() for path in reports]:
            try:
                load_previous_results([report])
            except ValueError as error:
                notify(f"{error} It is not used.")
            else:
                reports.append(report)
        # With no report at hand, one is looked for where voxpad-stats looks: beside the chat, then in the current folder.
        conversation = self.write_viewer(reports, discover=True, notify=notify)
        lines = [*notices, *([""] if notices else []),
                 *stats.summary_lines(conversation, command=False), *stats.viewer_lines(conversation, command=False)]
        self.events.put(("viewed", conversation, "\n".join(lines)))

    def run(self) -> None:
        try:
            if self.view_only:
                self.run_view()
            else:
                self.run_transcription()
        except engines.RemoteTokenNeeded as error:
            self.events.put(("failed", f"{error} You can also paste one in the Token box."))
        except FAILURES as error:
            self.events.put(("failed", window_words(str(error))))
        except Exception as error:  # Keep the window usable and say what happened.
            self.events.put(("failed", f"Unexpected error: {error}"))


def summary_text(summary: dict) -> str:
    # A stopped run keeps earlier transcripts for recordings it did not reach again; those are not counted as done.
    done = summary.get("done", len(summary["report"]["results"]))
    text = f"Transcribed {done - summary['failures']} of {summary['total']} voice messages"
    if summary["reused"]:
        text += f", {summary['reused']} reused"
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
            self.reuse: Path | None = None
            self.events_file: Path | None = None
            # Transcripts already in the output folder, or None when the report there is not VoxPad's.
            self.saved: int | None = 0
            self.setWindowTitle("VoxPad")
            self.resize(840, 680)
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
            # What the output folder already holds, and the way to set that aside: both hidden while there is nothing to say.
            self.folder_note = QtWidgets.QLabel()
            self.folder_note.setWordWrap(True)
            self.fresh = QtWidgets.QCheckBox(START_OVER, toggled=self.show_folder_note)
            self.language = QtWidgets.QComboBox(editable=True)
            self.language.addItems([AUTOMATIC, *(name for _, name in LANGUAGES)])
            self.model = QtWidgets.QComboBox(editable=True)
            self.model.addItems([f"{name} — {description}" for name, description in MODELS])
            # A pasted token is used for this session only; it is never saved.
            self.token = QtWidgets.QLineEdit(
                echoMode=QtWidgets.QLineEdit.EchoMode.Password, enabled=False,
                placeholderText="Hugging Face access token; leave empty to use HF_TOKEN or a saved login")
            # An export dropped here must reach the window, not be taken for a token.
            self.token.setAcceptDrops(False)
            self.place = QtWidgets.QComboBox()
            self.place.addItems([label for label, _ in PLACES])
            self.place.currentTextChanged.connect(self.place_changed)
            self.reuse_box = QtWidgets.QLineEdit(
                readOnly=True, placeholderText="Optional: a transcripts.json or the browser app's JSON download")
            self.reuse_button = QtWidgets.QPushButton("Choose…", clicked=self.choose_reuse)
            self.reuse_clear = QtWidgets.QPushButton("Clear", clicked=self.clear_reuse, enabled=False)
            self.events_box = QtWidgets.QLineEdit(
                readOnly=True, placeholderText="Optional: dated events for the viewer, one per line: 2024-01-15, label")
            self.events_button = QtWidgets.QPushButton("Choose…", clicked=self.choose_events)
            self.events_clear = QtWidgets.QPushButton("Clear", clicked=self.clear_events, enabled=False)
            self.playback = QtWidgets.QCheckBox(PLAYBACK, checked=True)

            # The note has the width of its row to itself, so that it stays on one line.
            note = QtWidgets.QHBoxLayout()
            note.addWidget(self.folder_note, 1)
            note.addWidget(self.fresh)

            form = QtWidgets.QGridLayout()
            form.setColumnStretch(1, 1)
            for row, (label, field, buttons) in enumerate((
                ("Export", self.source_box, (self.file_button, self.folder_button)),
                ("Save in", self.folder_box, (self.output_button,)),
                ("", note, ()),
                ("Language", self.language, ()),
                ("Model", self.model, ()),
                ("Run on", self.place, ()),
                ("Token", self.token, ()),
                ("Earlier transcripts", self.reuse_box, (self.reuse_button, self.reuse_clear)),
                ("Events", self.events_box, (self.events_button, self.events_clear)),
                ("Viewer", self.playback, ()),
            )):
                if label:
                    form.addWidget(QtWidgets.QLabel(label), row, 0)
                if field is note:
                    form.addLayout(note, row, 1, 1, 3)
                else:
                    form.addWidget(field, row, 1)
                for column, button in enumerate(buttons, 2):
                    form.addWidget(button, row, column)

            self.start_button = QtWidgets.QPushButton("Transcribe", clicked=self.start, enabled=False, default=True)
            self.stop_button = QtWidgets.QPushButton("Stop", clicked=self.stop, enabled=False)
            self.view_button = QtWidgets.QPushButton("View without transcribing", clicked=self.view_without_transcribing, enabled=False)
            self.viewer_button = QtWidgets.QPushButton("Open viewer", clicked=self.open_viewer, enabled=False)
            self.open_button = QtWidgets.QPushButton("Open folder", clicked=self.open_folder, enabled=False)
            actions = QtWidgets.QHBoxLayout()
            for button in (self.start_button, self.stop_button, self.view_button, self.viewer_button, self.open_button):
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

            self.refresh_folder()
            self.timer = QtCore.QTimer(self, interval=100, timeout=self.poll)
            self.timer.start()
            if source:
                self.set_source(source)

        def set_source(self, source: Path) -> None:
            self.source = source.expanduser().resolve()
            self.source_box.setText(str(self.source))
            self.set_folder(default_output_folder(self.source))
            self.start_button.setEnabled(self.job is None)
            self.view_button.setEnabled(self.job is None)
            self.status.setText("Ready.")

        def set_folder(self, folder: Path) -> None:
            self.folder = folder
            self.folder_box.setText(str(folder))
            self.refresh_folder()

        def refresh_folder(self) -> None:
            """Say what the output folder holds; called when the folder changes and after every run."""
            try:
                self.saved = saved_transcripts(self.folder) if self.folder else 0
            except ValueError:
                self.saved = None
            # Starting over is asked for one run at a time: left ticked, it would discard what a stopped run saved.
            self.fresh.setChecked(False)
            self.fresh.setVisible(bool(self.saved))
            self.show_folder_note()
            # A page from an earlier run can be read while the next one works: it is replaced in one step at the end.
            self.viewer_button.setEnabled(self.folder is not None and (self.folder / VIEWER_NAME).is_file())

        def show_folder_note(self) -> None:
            note = folder_note(self.saved, self.fresh.isChecked())
            self.folder_note.setText(note)
            self.folder_note.setVisible(bool(note))

        def set_reuse(self, path: Path | None) -> None:
            if path:
                try:
                    count = transcripts_in(path)
                except (OSError, ValueError) as error:
                    QtWidgets.QMessageBox.critical(self, "VoxPad", str(error))
                    return
                self.status.setText(f"{path.name} holds {counted(count, 'transcript')}.")
            self.reuse = path
            self.reuse_box.setText(str(path or ""))
            self.reuse_clear.setEnabled(path is not None)

        def set_events(self, path: Path | None) -> None:
            if path:
                try:
                    marked, skipped = parse_events(path.read_bytes().decode("utf-8", errors="replace"))
                except OSError as error:
                    QtWidgets.QMessageBox.critical(self, "VoxPad", str(error))
                    return
                if not marked:
                    QtWidgets.QMessageBox.critical(
                        self, "VoxPad", f"{path.name} holds no dated event. Write one per line, for example: 2024-01-15, label")
                    return
                self.status.setText(f"Read {counted(len(marked), 'event')} from {path.name}"
                                    + (f"; {counted(len(skipped), 'line')} without a valid date will be skipped." if skipped else "."))
            self.events_file = path
            self.events_box.setText(str(path or ""))
            self.events_clear.setEnabled(path is not None)

        def choose_file(self) -> None:
            patterns = " ".join(f"*{extension}" for extension in sorted(AUDIO_EXTENSIONS | {".zip", ".txt"}))
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

        def choose_reuse(self) -> None:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, "Choose earlier transcripts", "", "Transcript report (*.json);;All files (*)")
            if path:
                self.set_reuse(Path(path))

        def clear_reuse(self) -> None:
            self.set_reuse(None)

        def choose_events(self) -> None:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose an events file", "", "Text file (*.txt);;All files (*)")
            if path:
                self.set_events(Path(path))

        def clear_events(self) -> None:
            self.set_events(None)

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
            for widget in (self.file_button, self.folder_button, self.output_button, self.fresh, self.language, self.model,
                           self.place, self.reuse_button, self.events_button, self.playback):
                widget.setEnabled(not running)
            self.token.setEnabled(not running and remote_service(self.place.currentText()) is not None)
            self.reuse_clear.setEnabled(not running and self.reuse is not None)
            self.events_clear.setEnabled(not running and self.events_file is not None)
            self.start_button.setEnabled(not running and self.source is not None)
            self.view_button.setEnabled(not running and self.source is not None)
            # Writing the viewer is one step: there is nothing to stop between.
            self.stop_button.setEnabled(running and not self.job.view_only)

        def missing(self) -> str | None:
            """Name the chosen file that is no longer where it was."""
            for path, problem in ((self.source, "The export no longer exists"),
                                  (self.reuse, "The earlier transcripts no longer exist"),
                                  (self.events_file, "The events file no longer exists")):
                if path and not path.exists():
                    return f"{problem}: {path}"
            return None

        def begin(self, status: str, **options) -> None:
            self.text.clear()
            # Viewing has no steps to count, so the bar only shows that it is busy.
            self.progress.setRange(0, 0 if options.get("view_only") else 1)
            self.progress.setValue(0)
            self.open_button.setEnabled(False)
            self.status.setText(status)
            self.job = Job(self.source, self.folder, events=self.events, reuse=self.reuse, events_file=self.events_file,
                           playback=self.playback.isChecked(), **options)
            self.set_running(True)
            self.job.start()

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
            problem = remote_problem(remote, language=language, model=model, token=token) or self.missing()
            if problem:
                QtWidgets.QMessageBox.critical(self, "VoxPad", problem)
                return
            self.begin("Starting…", model=model, language=language, remote=remote, token=token, fresh=self.fresh.isChecked())

        def view_without_transcribing(self) -> None:
            if self.job or not self.source or not self.folder:
                return
            problem = self.missing()
            if problem:
                QtWidgets.QMessageBox.critical(self, "VoxPad", problem)
                return
            self.begin("Reading the conversation…", view_only=True)

        def stop(self) -> None:
            if self.job:
                self.job.stop()
                self.stop_button.setEnabled(False)
                self.status.setText("Stopping after the current voice message…")

        def open_folder(self) -> None:
            if self.folder:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.folder)))

        def open_viewer(self) -> None:
            if self.folder:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.folder / VIEWER_NAME)))

        def handle(self, event: tuple) -> None:
            kind = event[0]
            viewing = self.job is not None and self.job.view_only
            if kind == "status":
                self.status.setText(event[1].strip())
                self.text.appendPlainText(event[1])
            elif kind == "progress":
                self.progress.setRange(0, max(event[2], 1))
                self.progress.setValue(event[1])
            elif kind == "done":
                self.text.setPlainText(event[2])
                self.status.setText(summary_text(event[1]))
            elif kind == "viewed":
                self.text.setPlainText(event[2])
                self.status.setText(f"Viewer saved in {event[1]['viewer'].parent}")
            elif kind == "failed":
                self.status.setText("Could not write the viewer." if viewing else "Could not transcribe this export.")
            if kind in ("done", "viewed", "failed"):
                self.job = None
                if viewing:
                    self.progress.setRange(0, 1)
                    self.progress.setValue(int(kind == "viewed"))
                self.set_running(False)
                self.open_button.setEnabled(kind != "failed")
                self.refresh_folder()
            if kind == "failed":
                QtWidgets.QMessageBox.critical(self, "VoxPad", event[1])
            elif kind == "done" and event[1].get("refusal"):
                # A hosted service ended the run: say why instead of leaving it in the transcript.
                QtWidgets.QMessageBox.warning(self, "VoxPad", event[1]["refusal"])
            if kind == "done" and event[1].get("viewer_problem"):
                QtWidgets.QMessageBox.warning(
                    self, "VoxPad", f"The transcripts are saved, but the viewer could not be written: {event[1]['viewer_problem']}")

        def poll(self) -> None:
            try:
                while True:
                    self.handle(self.events.get_nowait())
            except queue.Empty:
                pass

        def closeEvent(self, event) -> None:
            buttons = QtWidgets.QMessageBox.StandardButton
            running = "The viewer is being written." if self.job and self.job.view_only else "A transcription is running."
            if self.job and QtWidgets.QMessageBox.question(self, "VoxPad", f"{running} Close anyway?") != buttons.Yes:
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
