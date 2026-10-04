"""VoxPad desktop application: choose an export, transcribe it locally, read the result.

Run with `voxpad-app` or `python -m voxpad.gui`. Needs Tkinter, which ships
with most Python installations.
"""

import argparse
from pathlib import Path
import queue
import sys
import threading
import zipfile

from . import whatsapp


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


class Job(threading.Thread):
    """Transcribe one export in the background and report through a queue.

    Events are tuples: ("status", text), ("progress", done, total),
    ("done", summary, preview) and ("failed", message).
    """

    def __init__(self, source: Path, folder: Path, *, model: str, language: str | None, events: queue.Queue):
        super().__init__(daemon=True)
        self.source = source
        self.folder = folder
        self.model = model
        self.language = language
        self.events = events
        self._stop_requested = threading.Event()

    def stop(self) -> None:
        self._stop_requested.set()

    def run(self) -> None:
        try:
            summary = whatsapp.transcribe_export(
                self.source, self.folder.resolve() / REPORT_NAME, self.folder.resolve() / CHAT_NAME,
                model=self.model, language=self.language, chat_output_optional=True,
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


class App:
    """The window. Tkinter is imported here so the rest of the module works without it."""

    def __init__(self, root, source: Path | None = None):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        self.tk, self.filedialog, self.messagebox = tk, filedialog, messagebox
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.job: Job | None = None
        self.source: Path | None = None
        self.folder: Path | None = None

        root.title("VoxPad")
        root.minsize(720, 560)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(8, weight=1)

        ttk.Label(frame, text="Turn WhatsApp voice messages into text", font=("TkDefaultFont", 14, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(frame, text="Everything runs on this computer. Export the chat with media included.").grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(2, 14))

        ttk.Label(frame, text="Export").grid(row=2, column=0, sticky="w", padx=(0, 10))
        self.source_text = tk.StringVar(value="No export chosen")
        ttk.Label(frame, textvariable=self.source_text, relief="sunken", padding=4).grid(row=2, column=1, sticky="ew")
        self.file_button = ttk.Button(frame, text="Choose ZIP or file…", command=self.choose_file)
        self.file_button.grid(row=2, column=2, padx=(8, 0))
        self.folder_button = ttk.Button(frame, text="Choose folder…", command=self.choose_folder)
        self.folder_button.grid(row=2, column=3, padx=(8, 0))

        ttk.Label(frame, text="Save in").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        self.folder_text = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.folder_text, relief="sunken", padding=4).grid(row=3, column=1, sticky="ew", pady=(8, 0))
        self.output_button = ttk.Button(frame, text="Change…", command=self.choose_output)
        self.output_button.grid(row=3, column=2, padx=(8, 0), pady=(8, 0), sticky="ew")

        ttk.Label(frame, text="Language").grid(row=4, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        self.language = ttk.Combobox(frame, values=[AUTOMATIC, *(name for _, name in LANGUAGES)])
        self.language.set(AUTOMATIC)
        self.language.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(8, 0))

        ttk.Label(frame, text="Model").grid(row=5, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        self.model = ttk.Combobox(frame, values=[f"{name} — {description}" for name, description in MODELS])
        self.model.current(0)
        self.model.grid(row=5, column=1, columnspan=2, sticky="ew", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, columnspan=4, sticky="w", pady=(14, 6))
        self.start_button = ttk.Button(buttons, text="Transcribe", command=self.start, state="disabled")
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.open_button = ttk.Button(buttons, text="Open folder", command=self.open_folder, state="disabled")
        self.open_button.pack(side="left", padx=(8, 0))

        self.progress = ttk.Progressbar(frame, mode="determinate")
        self.progress.grid(row=7, column=0, columnspan=4, sticky="ew")
        text_frame = ttk.Frame(frame)
        text_frame.grid(row=8, column=0, columnspan=4, sticky="nsew", pady=(8, 0))
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)
        self.text = tk.Text(text_frame, wrap="word", state="disabled", height=14)
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(text_frame, command=self.text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scrollbar.set)
        self.status = tk.StringVar(value="Choose an export to begin.")
        ttk.Label(frame, textvariable=self.status).grid(row=9, column=0, columnspan=4, sticky="w", pady=(8, 0))

        root.protocol("WM_DELETE_WINDOW", self.close)
        if source:
            self.set_source(source)
        self.poll()

    def set_source(self, source: Path) -> None:
        self.source = source.expanduser().resolve()
        self.source_text.set(str(self.source))
        self.set_folder(default_output_folder(self.source))
        self.start_button.configure(state="normal")
        self.status.set("Ready.")

    def set_folder(self, folder: Path) -> None:
        self.folder = folder
        self.folder_text.set(str(folder))

    def choose_file(self) -> None:
        patterns = " ".join(f"*{extension}" for extension in sorted(whatsapp.AUDIO_EXTENSIONS | {".zip", ".txt"}))
        path = self.filedialog.askopenfilename(
            title="Choose a WhatsApp export", filetypes=[("WhatsApp export, chat or audio", patterns), ("All files", "*")])
        if path:
            self.set_source(Path(path))

    def choose_folder(self) -> None:
        path = self.filedialog.askdirectory(title="Choose an extracted export folder")
        if path:
            self.set_source(Path(path))

    def choose_output(self) -> None:
        path = self.filedialog.askdirectory(title="Choose where to save the transcripts")
        if path:
            self.set_folder(Path(path))

    def show(self, content: str, *, append: bool = False) -> None:
        self.text.configure(state="normal")
        if not append:
            self.text.delete("1.0", "end")
        # The text box draws carriage returns as boxes; the saved files keep them.
        self.text.insert("end", content.replace("\r\n", "\n").replace("\r", "\n"))
        self.text.see("end" if append else "1.0")
        self.text.configure(state="disabled")

    def set_running(self, running: bool) -> None:
        idle = "disabled" if running else "normal"
        for widget in (self.file_button, self.folder_button, self.output_button, self.language, self.model, self.start_button):
            widget.configure(state=idle)
        self.stop_button.configure(state="normal" if running else "disabled")

    def start(self) -> None:
        if self.job or not self.source or not self.folder:
            return
        try:
            language = language_code(self.language.get())
        except ValueError as error:
            self.messagebox.showerror("VoxPad", str(error))
            return
        if not self.source.exists():
            self.messagebox.showerror("VoxPad", f"The export no longer exists: {self.source}")
            return
        self.show("")
        self.progress.configure(value=0, maximum=1)
        self.open_button.configure(state="disabled")
        self.set_running(True)
        self.status.set("Starting…")
        self.job = Job(self.source, self.folder, model=model_name(self.model.get()), language=language, events=self.events)
        self.job.start()

    def stop(self) -> None:
        if self.job:
            self.job.stop()
            self.stop_button.configure(state="disabled")
            self.status.set("Stopping after the current voice message…")

    def open_folder(self) -> None:
        import os
        import subprocess
        if not self.folder:
            return
        if sys.platform == "win32":
            os.startfile(self.folder)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(self.folder)])

    def handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "status":
            self.status.set(event[1].strip())
            self.show(event[1] + "\n", append=True)
        elif kind == "progress":
            self.progress.configure(value=event[1], maximum=max(event[2], 1))
        elif kind == "done":
            self.show(event[2])
            self.status.set(summary_text(event[1]))
            self.open_button.configure(state="normal")
        elif kind == "failed":
            self.status.set("Could not transcribe this export.")
            self.messagebox.showerror("VoxPad", event[1])
        if kind in ("done", "failed"):
            self.job = None
            self.set_running(False)

    def poll(self) -> None:
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self) -> None:
        if self.job and not self.messagebox.askokcancel("VoxPad", "A transcription is running. Close anyway?"):
            return
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voxpad-app", description="VoxPad desktop application: transcribe WhatsApp voice messages locally.")
    parser.add_argument("source", nargs="?", type=Path, help="Export ZIP, folder, chat .txt or audio file to open")
    args = parser.parse_args(argv)
    try:
        import tkinter as tk
    except ImportError:
        print("VoxPad's window needs Tkinter. Install it (for example the python3-tk package on Linux) "
              "or use the voxpad command instead.", file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(f"Could not open a window: {error}", file=sys.stderr)
        return 1
    App(root, args.source)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
