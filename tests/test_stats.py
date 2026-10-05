"""Tests for voxpad-stats and write_conversation; no recording is decoded unless a test says so."""

import builtins
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock
import warnings
import zipfile

from voxpad import stats
from voxpad.reports import write_reports
from tests.support import ExportTestCase, codecpod, np, write_wav


OPUS, WAV = "PTT-20260113-WA0001.opus", "AUD-20260114-WA0002.wav"
LINES = (
    "13/01/26, 10:00 - Ana: Hola José\n",
    f"13/01/26, 10:01 - José: {OPUS} (file attached)\n",
    f"14/01/26, 09:00 - Ana: {WAV} (file attached)\n",
    "14/01/26, 09:05 - José: Todo bien\n",
)
CHAT = "".join(LINES)
ANNOTATED = "".join((*LINES[:2], "[Voice message transcript: uno dos tres]\n", LINES[2], "[Voice message transcript: cuatro cinco]\n", LINES[3]))
OTHER_CHAT = "20/03/26, 18:00 - Li: Another conversation\n20/03/26, 18:01 - Ana: Yes\n"
TABLE_HEAD = ["Messages", "Typed", "words", "Spoken", "words", "Total", "words", "Voice", "notes", "Voice", "time"]
# The two recordings of export(): 727 and 16044 bytes.
COPIED = "Copied 2 voice messages (16 KiB) to {}/ so the viewer can play them; --audio none skips this."
MEASURED = "Measured the length of {} recordings from their files."
MEASURED_ONE = "Measured the length of 1 recording from its file."


def ogg_page(stream, sequence, position, packets, flags=0):
    lacing = b"".join(b"\xff" * (len(packet) // 255) + bytes([len(packet) % 255]) for packet in packets)
    return b"OggS" + struct.pack("<BBqIIIB", 0, flags, position, stream, sequence, 0, len(lacing)) + lacing + b"".join(packets)


def opus_bytes(seconds, *, skipped=312, stream=7, sound=b"\x01" * 600):
    """An Ogg Opus file as far as its headers go: the first page says what is skipped, the last how far the stream runs."""
    head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, skipped, 48000, 0, 0)
    tags = b"OpusTags" + struct.pack("<I", 6) + b"voxpad" + struct.pack("<I", 0)
    return (ogg_page(stream, 0, 0, [head], flags=2) + ogg_page(stream, 1, 0, [tags])
            + ogg_page(stream, 2, skipped + round(seconds * 48000), [sound], flags=4))


def result(file, text, seconds=None, status="ok", **more):
    entry = {"file": file, "messages": [], "status": status, "text": text, **more}
    if seconds is not None:
        entry["duration_seconds"] = seconds
    return entry


def model_in(page):
    return json.loads(re.search(r'<script type="application/json" id="voxpad-model">(.*?)</script>', page.read_text(encoding="utf-8"))[1])


def policy_in(page):
    return re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', page.read_text(encoding="utf-8"))[1]


def voices(model):
    return [message["voice"] for message in model["messages"] if message["kind"] == "voice"]


def without_imports(*names):
    """Make the named packages impossible to import, as on a computer that does not have them."""
    original = builtins.__import__

    def guarded(name, *arguments, **options):
        if name.split(".")[0] in names:
            raise ImportError(f"No module named {name!r}")
        return original(name, *arguments, **options)

    return mock.patch.object(builtins, "__import__", side_effect=guarded)


class StatsTestCase(ExportTestCase):
    def setUp(self):
        super().setUp()
        self.root = self.root.resolve()
        # The page goes to the current folder unless a test says otherwise.
        self.work = self.root / "work"
        self.work.mkdir()
        previous = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, previous)

    def run_stats(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = stats.main([str(argument) for argument in arguments])
            except SystemExit as exit_:
                code = exit_.code
        return code, stdout.getvalue(), stderr.getvalue()

    def save_report(self, relative, results, model="Whisper small"):
        return self.touch(relative, json.dumps({"source": "export", "model": model, "results": results}).encode("utf-8"))

    def export(self, folder="export", chat=CHAT):
        """A folder with the chat and both of its recordings: 2.5 seconds of Opus and half a second of WAV."""
        self.touch(f"{folder}/chat.txt", chat.encode("utf-8"))
        self.touch(f"{folder}/{OPUS}", opus_bytes(2.5))
        write_wav(self.root / folder / WAV, frames=8000)
        return self.root / folder

    def files(self, folder=None):
        folder = folder or self.root
        return sorted(path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file())

    def contents(self):
        return {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}


class CommandTests(StatsTestCase):
    def test_what_an_earlier_run_left_is_enough_to_count_and_view_the_chat(self):
        folder = self.root / "earlier"
        folder.mkdir()
        (folder / "chat_with_transcripts.txt").write_bytes(ANNOTATED.encode("utf-8"))
        write_reports(folder / "transcripts.json", {"source": "export.zip", "model": "Whisper small", "sample_rate": 16000, "chunk_seconds": 30,
                                                    "results": [result(OPUS, "uno dos tres", 75.4, languages=["es"]), result(WAV, "cuatro cinco", 30)]})
        self.assertEqual(self.files(folder), ["chat_with_transcripts.txt", "transcripts.json", "transcripts.txt"])
        before = self.contents()
        code, stdout, stderr = self.run_stats(folder)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "Using transcripts.json: 2 of 2 voice messages matched\n")
        page = self.work / "conversation.html"
        lines = stdout.splitlines()
        self.assertEqual(lines[:3], ["Parsed 4 messages from 2026-01-13 to 2026-01-14", "Dates are read as day/month/year (DMY).", ""])
        table = lines[3:7]
        self.assertEqual([line.split() for line in table], [
            TABLE_HEAD,
            ["Ana", "2", "2", "2", "4", "1", "30", "s"],
            ["José", "2", "2", "3", "5", "1", "1", "min", "15", "s"],
            ["Total", "4", "4", "5", "9", "2", "1", "min", "45", "s"],
        ])
        # Names stand at the left, and every number ends under the end of its heading.
        self.assertEqual({len(line) for line in table}, {len(table[0])})
        self.assertTrue(table[0].startswith("       Messages  Typed words  "))
        self.assertTrue(table[1].startswith("Ana  "))
        self.assertEqual(lines[7:], [
            "",
            "Voice messages: 2 detected, 0 with a recording, 2 with a duration.",
            f"Viewer: {page}",
            "Open it in a browser; it needs no connection.",
            "conversation.html contains the whole conversation and its transcripts; share the PNG (--png) when you only want the charts.",
        ])
        model = model_in(page)
        self.assertEqual((model["title"], model["participants"], model["date_order"]), ("Ana · José", ["Ana", "José"], "DMY"))
        self.assertEqual([(voice["file"], voice["src"], voice["seconds"], voice["status"], voice["text"], voice["words"]) for voice in voices(model)],
                         [(OPUS, None, 75.4, "ok", "uno dos tres", 3), (WAV, None, 30, "ok", "cuatro cinco", 2)])
        # Without a recording to play, the page may load no media at all.
        self.assertNotIn("media-src", policy_in(page))
        # The run read the folder and wrote one file, in the current folder.
        self.assertEqual(self.contents(), {**before, page: page.read_bytes()})
        self.assertNotIn(str(self.root), page.read_text(encoding="utf-8"))

    def test_the_chat_alone_gives_its_transcripts_and_says_what_is_not_covered(self):
        chat = self.touch("earlier/chat_with_transcripts.txt", ANNOTATED.replace("cuatro cinco", "Error: The recording could not be decoded").encode("utf-8"))
        code, stdout, stderr = self.run_stats(chat, "--no-viewer")
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(stdout.endswith(
            "Voice messages: 2 detected, 0 with a recording, 0 with a duration.\n"
            "Spoken words cover 1 of 2 voice messages (0 not transcribed, 1 failed, 0 without a recording).\n"
            "Voice time covers 0 of 2 voice messages.\n"
            "1 voice message has no transcript: pass --transcripts FILE, or transcribe it with voxpad\n"), stdout)
        self.assertEqual(self.files(self.work), [])
        # A recording in which nobody speaks was transcribed all the same: only its length is missing.
        chat.write_bytes(ANNOTATED.replace("cuatro cinco", "No speech detected").encode("utf-8"))
        code, stdout, stderr = self.run_stats(chat, "--no-viewer")
        self.assertTrue(stdout.endswith("Voice messages: 2 detected, 0 with a recording, 0 with a duration.\nVoice time covers 0 of 2 voice messages.\n"), stdout)
        # A report of a run that was stopped: one transcribed, one not reached.
        self.save_report("stopped/transcripts.json", [result(OPUS, "uno dos tres", 75.4)])
        self.touch("stopped/chat.txt", CHAT.encode("utf-8"))
        before = self.contents()
        code, stdout, stderr = self.run_stats(self.root / "stopped", "--no-viewer")
        self.assertEqual((code, stderr), (0, "Using transcripts.json: 1 of 2 voice messages matched\n"))
        self.assertTrue(stdout.endswith(
            "Voice messages: 2 detected, 0 with a recording, 1 with a duration.\n"
            "Spoken words cover 1 of 2 voice messages (1 not transcribed, 0 failed, 0 without a recording).\n"
            "Voice time covers 1 of 2 voice messages.\n"
            "1 voice message has no transcript: pass --transcripts FILE, or transcribe it with voxpad\n"), stdout)
        self.assertEqual(self.contents(), before)
        # An export made without its media names no voice message, and the summary says why nothing is counted.
        bare = self.touch("bare/chat.txt", "13/01/26, 10:00 - Ana: <Media omitted>\n13/01/26, 10:01 - José: Hola\n".encode("utf-8"))
        code, stdout, stderr = self.run_stats(bare, "--no-viewer")
        self.assertEqual((code, stderr), (0, ""))
        self.assertTrue(stdout.endswith(
            "Voice messages: 0 detected, 0 with a recording, 0 with a duration.\n"
            "Warning: the chat names no voice message. An export made without media shows every attachment as <Media omitted>; "
            "export the chat again with media to count voice messages per person.\n"), stdout)

    def test_more_than_one_chat_stops_with_their_names_and_copies_of_one_chat_count_as_one(self):
        self.touch("export/A.txt", CHAT.encode("utf-8"))
        self.touch("export/sub/B.txt", OTHER_CHAT.encode("utf-8"))
        self.touch("export/notes.txt", b"Notes about this export")
        code, stdout, stderr = self.run_stats(self.root / "export")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, 'Error: Found 2 chats: A.txt, sub/B.txt. Pass the one to analyse, for example: voxpad-stats "export/A.txt"\n')
        self.assertEqual(self.files(self.work), [])
        # The chat .txt itself is always the chat, whatever lies beside it.
        code, stdout, stderr = self.run_stats(self.root / "export" / "sub" / "B.txt")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(model_in(self.work / "conversation.html")["participants"], ["Ana", "Li"])
        archive = self.make_zip([("Chat/A.txt", CHAT), ("Chat/B.txt", OTHER_CHAT)])
        code, stdout, stderr = self.run_stats(archive, "--no-viewer")
        self.assertEqual((code, stderr), (1, "Error: Found 2 chats: Chat/A.txt, Chat/B.txt. Extract export.zip and pass the one to analyse, "
                                             'for example: voxpad-stats "FOLDER/Chat/A.txt"\n'))
        # write_conversation gives up silently where the caller says the viewer is an extra.
        self.assertIsNone(stats.write_conversation(self.root / "export", self.work / "extra.html", optional=True, notify=self.fail))
        self.assertFalse((self.work / "extra.html").exists())
        with self.assertRaisesRegex(ValueError, "Found 2 chats"):
            stats.write_conversation(self.root / "export", None, notify=self.fail)
        # Files a run wrote into the source are named by the caller and not read as chats.
        done = stats.write_conversation(self.root / "export", None, excluded=[self.root / "export" / "sub" / "B.txt"], notify=self.fail)
        self.assertEqual(done["chat"], "A.txt")

        # The chat and its copy with transcripts are one conversation, even when an editor changed a space and the last line.
        self.touch("both/chat.txt", CHAT.replace("Hola José", "Hola" + chr(0xA0) + "José").rstrip("\n").encode("utf-8"))
        self.touch("both/chat_with_transcripts.txt", ANNOTATED.encode("utf-8"))
        code, stdout, stderr = self.run_stats(self.root / "both", "--no-viewer", "--no-transcripts")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "chat.txt, chat_with_transcripts.txt hold the same conversation; reading chat_with_transcripts.txt.\n")
        self.assertEqual([line.split() for line in stdout.splitlines()[4:6]],
                         [["Ana", "2", "2", "2", "4", "1", "0", "s"], ["José", "2", "2", "3", "5", "1", "0", "s"]])
        # One word more in a message makes them two chats again.
        self.touch("both/chat.txt", CHAT.replace("Todo bien", "Todo muy bien").encode("utf-8"))
        code, stdout, stderr = self.run_stats(self.root / "both", "--no-viewer")
        self.assertEqual((code, stderr), (1, "Error: Found 2 chats: chat.txt, chat_with_transcripts.txt. Pass the one to analyse, "
                                             'for example: voxpad-stats "both/chat.txt"\n'))
        for name, source in (("no chat", self.touch("none/notes.txt", b"Nothing here")), ("a recording", self.touch("none/voice.opus"))):
            with self.subTest(name):
                code, stdout, stderr = self.run_stats(source, "--no-viewer")
                self.assertEqual((code, stdout, stderr), (1, "", "Error: No WhatsApp chat was found. Pass an export ZIP, its folder or the chat .txt.\n"))

    def test_a_report_is_looked_for_beside_the_chat_then_in_the_current_folder(self):
        export = self.root / "export"
        chat = self.touch("export/chat.txt", CHAT.encode("utf-8"))
        beside = self.save_report("export/transcripts.json", [result(OPUS, "beside the chat", 10)])
        named = self.save_report("export/chat.json", [result(OPUS, "named like the chat it belongs to", 20)])
        here = self.save_report("work/transcripts.json", [result(OPUS, "in the current folder with more words", 30)])
        matched = "Using {}: 1 of 2 voice messages matched"

        def found(*arguments, source=export):
            code, stdout, stderr = self.run_stats(source, *arguments)
            self.assertEqual(code, 0, stderr)
            return stderr.splitlines(), [voice["text"] for voice in voices(model_in(self.work / "conversation.html")) if voice["text"]]

        self.assertEqual(found(), ([matched.format("transcripts.json")], ["beside the chat"]))
        self.assertEqual(found(source=chat), ([matched.format("transcripts.json")], ["beside the chat"]))
        beside.write_text("[]", encoding="utf-8")
        self.assertEqual(found(), (["transcripts.json is not a VoxPad transcript report. It is not used.", matched.format("chat.json")],
                                   ["named like the chat it belongs to"]))
        # A report of another chat matches nothing here and is passed over as well.
        beside.write_text(json.dumps({"results": [result("PTT-20200101-WA0009.opus", "another chat", 5)]}), encoding="utf-8")
        named.unlink()
        self.assertEqual(found(), (["transcripts.json matches no voice message of this chat; it is not used.", matched.format("transcripts.json")],
                                   ["in the current folder with more words"]))
        here.unlink()
        self.assertEqual(found(), (["transcripts.json matches no voice message of this chat; it is not used."], []))
        beside.write_text(json.dumps({"results": [result(OPUS, "beside the chat", 10)]}), encoding="utf-8")
        self.assertEqual(found("--no-transcripts"), ([], []))
        # Reports that are named are the ones used: a later one wins, and a transcript wins over a failure.
        first = self.save_report("reports/first.json", [result(OPUS, "first", 1), result(WAV, "kept", 2)])
        second = self.save_report("reports/second.json", [result(OPUS, "second", 3), result(WAV, "", status="error", error="Could not decode")])
        self.assertEqual(found("--transcripts", first, "--transcripts", second),
                         (["Using first.json and second.json: 2 of 2 voice messages matched"], ["second", "kept"]))
        code, stdout, stderr = self.run_stats(export, "--transcripts", self.touch("private folder/notes.json", b"{}"))
        self.assertEqual((code, stdout, stderr), (1, "", "Error: notes.json is not a VoxPad transcript report.\n"))
        # Beside a ZIP, where the voxpad command writes its report by default.
        archive = self.make_zip([("chat.txt", CHAT)])
        self.save_report("transcripts.json", [result(OPUS, "beside the archive", 10)])
        self.assertEqual(found(source=archive), ([matched.format("transcripts.json")], ["beside the archive"]))

    def test_a_zip_gets_its_recordings_copied_beside_the_page(self):
        sound = opus_bytes(2.5)
        write_wav(self.root / "tone.wav", frames=8000)
        tone = (self.root / "tone.wav").read_bytes()
        archive = self.make_zip([("Chat/_chat.txt", CHAT), (f"Chat/{OPUS}", sound), (f"Chat/{WAV}", tone), ("Chat/unused.opus", b"not in the chat")])
        self.save_report("transcripts.json", [result(f"Chat/{OPUS}", "uno dos tres", 75.4)])
        code, stdout, stderr = self.run_stats(archive)
        self.assertEqual(code, 0, stderr)
        page = self.work / "conversation.html"
        # The report times one recording; the other is measured from its header.
        self.assertEqual(stderr.splitlines(), ["Using transcripts.json: 1 of 2 voice messages matched", MEASURED_ONE, COPIED.format("conversation_audio")])
        self.assertEqual(stdout.splitlines()[-4:], [
            f"Viewer: {page}",
            "Open it in a browser; it needs no connection.",
            "Keep conversation_audio/ beside it: the voice messages play from there.",
            "conversation.html contains the whole conversation and its transcripts; share the PNG (--png) when you only want the charts.",
        ])
        self.assertIn("Voice messages: 2 detected, 2 with a recording, 2 with a duration.\n", stdout)
        self.assertEqual(self.files(self.work), ["conversation.html", f"conversation_audio/{WAV}", f"conversation_audio/{OPUS}"])
        self.assertEqual((self.work / "conversation_audio" / OPUS).read_bytes(), sound)
        self.assertEqual((self.work / "conversation_audio" / WAV).read_bytes(), tone)
        self.assertEqual([(voice["src"], voice["seconds"]) for voice in voices(model_in(page))],
                         [(f"./conversation_audio/{OPUS}", 75.4), (f"./conversation_audio/{WAV}", 0.5)])
        self.assertIn("media-src 'self'", policy_in(page))
        self.assertNotIn(str(self.root), page.read_text(encoding="utf-8"))
        # The folder is named after the page, and --audio none writes the page alone.
        named = self.root / "out put" / "Ana y José.html"
        code, stdout, stderr = self.run_stats(archive, "--viewer", named, "--audio", "copy", "--no-transcripts")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr.splitlines(), [MEASURED.format(2), COPIED.format("Ana y José_audio")])
        self.assertEqual(self.files(named.parent), ["Ana y José.html", f"Ana y José_audio/{WAV}", f"Ana y José_audio/{OPUS}"])
        self.assertEqual(voices(model_in(named))[0]["src"], f"./Ana%20y%20Jos%C3%A9_audio/{OPUS}")
        silent = self.root / "silent" / "page.html"
        code, stdout, stderr = self.run_stats(archive, "--viewer", silent, "--audio", "none", "--no-transcripts")
        self.assertEqual((code, stderr), (0, MEASURED.format(2) + "\n"))
        self.assertEqual(self.files(silent.parent), ["page.html"])
        self.assertEqual([(voice["src"], voice["seconds"]) for voice in voices(model_in(silent))], [(None, 2.5), (None, 0.5)])
        self.assertNotIn("media-src", policy_in(silent))
        self.assertNotIn("beside it", stdout)
        # A ZIP's recordings exist only while it is read, so there is nothing a page could link to.
        code, stdout, stderr = self.run_stats(archive, "--viewer", self.root / "linked" / "page.html", "--audio", "link", "--no-transcripts")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr, "Error: --audio link cannot be used with a ZIP: its recordings exist only while it is read. Use --audio copy.\n")
        self.assertFalse((self.root / "linked").exists())
        # Not even when it happens to be unpacked below the page's folder, as for a page written to the temporary folder.
        unpack = tempfile.TemporaryDirectory
        with mock.patch.object(tempfile, "TemporaryDirectory", side_effect=lambda **options: unpack(dir=self.root / "silent", **options)):
            code, stdout, stderr = self.run_stats(archive, "--viewer", silent, "--no-transcripts")
        self.assertEqual((code, stderr.splitlines()), (0, [MEASURED.format(2), COPIED.format("page_audio")]))
        self.assertEqual([voice["src"] for voice in voices(model_in(silent))], [f"./page_audio/{OPUS}", f"./page_audio/{WAV}"])
        self.assertEqual(self.files(silent.parent), ["page.html", f"page_audio/{WAV}", f"page_audio/{OPUS}"])

    def test_copies_whose_names_differ_only_by_case_get_a_number(self):
        self.touch("Probe.txt")
        if (self.root / "probe.txt").exists():
            self.skipTest("This file system does not tell names apart by case")
        chat = "13/01/26, 10:00 - Ana: Voice.opus (file attached)\n13/01/26, 10:01 - José: voice.opus (file attached)\n"
        archive = self.make_zip([("chat.txt", chat), ("Voice.opus", opus_bytes(1.0)), ("voice.opus", opus_bytes(2.0))])
        code, stdout, stderr = self.run_stats(archive)
        self.assertEqual(code, 0, stderr)
        # The page may be opened where the two names are one, so the copies must not depend on telling them apart.
        self.assertEqual([(voice["src"], voice["seconds"]) for voice in voices(model_in(self.work / "conversation.html"))],
                         [("./conversation_audio/Voice.opus", 1.0), ("./conversation_audio/2-voice.opus", 2.0)])
        self.assertEqual((self.work / "conversation_audio" / "2-voice.opus").read_bytes(), opus_bytes(2.0))

    def test_recordings_are_linked_where_the_page_can_reach_them_without_leaving_its_folder(self):
        export = self.export("work/my export #1")
        page = self.work / "conversation.html"
        code, stdout, stderr = self.run_stats(export, "--no-transcripts")
        self.assertEqual(code, 0, stderr)
        # Nothing is copied: the page names the recordings where they are, below its own folder.
        self.assertEqual(stderr, MEASURED.format(2) + "\n")
        self.assertEqual(self.files(self.work), ["conversation.html", f"my export #1/{WAV}", f"my export #1/{OPUS}", "my export #1/chat.txt"])
        self.assertEqual([(voice["src"], voice["seconds"], voice["status"]) for voice in voices(model_in(page))],
                         [(f"./my%20export%20%231/{OPUS}", 2.5, "pending"), (f"./my%20export%20%231/{WAV}", 0.5, "pending")])
        self.assertIn("media-src 'self'", policy_in(page))
        self.assertIn("Leave it where it is: the voice messages play from the recordings beside it.\n", stdout)
        self.assertNotIn(str(self.root), page.read_text(encoding="utf-8"))
        # In the export's own folder the names alone are enough.
        code, stdout, stderr = self.run_stats(export / "chat.txt", "--viewer", export / "view.html", "--audio", "link", "--no-transcripts")
        self.assertEqual(code, 0, stderr)
        self.assertEqual([voice["src"] for voice in voices(model_in(export / "view.html"))], [f"./{OPUS}", f"./{WAV}"])
        (export / "view.html").unlink()

        # From anywhere else the path would lead out of the page's folder and name folders of this computer.
        elsewhere = self.root / "elsewhere" / "page.html"
        code, stdout, stderr = self.run_stats(export, "--viewer", elsewhere, "--audio", "link", "--no-transcripts")
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("Error: --audio link needs every recording in the viewer's folder or below it", stderr)
        self.assertIn("use --audio copy", stderr)
        self.assertFalse(elsewhere.parent.exists())
        # Left to choose, the command copies them instead.
        code, stdout, stderr = self.run_stats(export, "--viewer", elsewhere, "--no-transcripts")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr.splitlines(), [MEASURED.format(2), COPIED.format("page_audio")])
        self.assertEqual([voice["src"] for voice in voices(model_in(elsewhere))], [f"./page_audio/{OPUS}", f"./page_audio/{WAV}"])
        self.assertNotIn("..", elsewhere.read_text(encoding="utf-8").split("<script>")[0].split('id="voxpad-model">')[1])

        # Copies inside the export would be recordings of the chat to the next run: refused when asked for, left out otherwise.
        before = self.files(export)
        code, stdout, stderr = self.run_stats(export, "--viewer", export / "view.html", "--audio", "copy", "--no-transcripts")
        self.assertEqual((code, stdout), (1, ""))
        self.assertIn("Error: --audio copy would put view_audio/ inside the export", stderr)
        inner = export / "pages" / "view.html"
        code, stdout, stderr = self.run_stats(export, "--viewer", inner, "--no-transcripts")
        self.assertEqual(code, 0, stderr)
        self.assertIn("view.html is written without the recordings", stderr)
        self.assertEqual([voice["src"] for voice in voices(model_in(inner))], [None, None])
        self.assertEqual(self.files(export), sorted(before + ["pages/view.html"]))

    def test_outputs_never_replace_what_the_command_reads(self):
        export = self.export()
        events = self.touch("inputs/events.html", b"2026-01-13, First call\n")
        report = self.save_report("inputs/report.png", [result(OPUS, "uno", 1)])
        named = self.touch("work/conversation.html", b"2026-01-13, An events file with the name the page gets\n")
        before = self.contents()
        cases = (
            ["--viewer", events, "--events", events],
            ["--png", report, "--transcripts", report],
            ["--events", named],
            ["--viewer", export / "chat.txt"],
            ["--viewer", export / OPUS],
            ["--viewer", self.root / "page.htm"],
            ["--png", export / "chat.txt"],
            ["--png", self.root / "figure.jpg"],
        )
        for arguments in cases:
            with self.subTest(arguments=[Path(argument).name for argument in arguments]):
                code, stdout, stderr = self.run_stats(export, *arguments)
                self.assertEqual((code, stdout), (2, ""), stderr)
                self.assertEqual(self.contents(), before)
        # Other callers get the same answer from the function, whatever the file is called.
        for viewer in (export / "chat.txt", export / OPUS, events, report):
            with self.subTest(viewer=viewer.name), self.assertRaisesRegex(ValueError, f"The viewer must not replace {re.escape(viewer.name)}"):
                stats.write_conversation(export, viewer, transcripts=[report], events=events, notify=lambda text: None)
        self.assertEqual(self.contents(), before)

    def test_events_dates_and_buckets_reach_the_page_and_the_summary(self):
        chat = self.touch("export/chat.txt", "01/02/26, 10:00 - Ana: Hola\n03/02/26, 10:00 - José: Qué tal\n".encode("utf-8"))
        # Saved by an old editor: a byte that is not UTF-8 costs one letter, not the file.
        events = self.touch("events.txt", (chr(0xFEFF) + "# What happened\n2026-02-01, First call\nno date here\n\n20/01/2026 | Lunch & <b>talk</b>\n"
                                           "2024-06-01 Long before\n").encode("utf-8") + b"2026-02-02, Caf\xe9\n")
        ambiguous = "Warning: the dates of this chat can also be read in another order."
        code, stdout, stderr = self.run_stats(chat, "--events", events, "--bucket", "week")
        self.assertEqual(code, 0, stderr)
        # A line is named by its number only: what it says may be private.
        self.assertEqual(stderr, "Events line 3 has no valid date and was skipped.\n")
        page = self.work / "conversation.html"
        model = model_in(page)
        # Read day first the chat covers three days of February; month first it would run from January to March.
        self.assertEqual((model["date_order"], model["date_order_ambiguous"]), ("DMY", True))
        self.assertEqual(stdout.splitlines()[:3], ["Parsed 2 messages from 2026-02-01 to 2026-02-03", "Dates are read as day/month/year (DMY).",
                                                   ambiguous + " If the days look wrong, pass --date-order DMY, MDY or YMD."])
        self.assertIn("\nEvents: 3 shown; 1 far outside 2026-02-01–2026-02-03 not plotted\n", stdout)
        # The page lists every event, also the one the charts have no room for.
        self.assertEqual(model["events"], [{"date": "2024-06-01", "label": "Long before"}, {"date": "2026-01-20", "label": "Lunch & <b>talk</b>"},
                                           {"date": "2026-02-01", "label": "First call"}, {"date": "2026-02-02", "label": "Caf" + chr(0xFFFD)}])
        self.assertIn('data-vp-theme="auto" data-bucket="week"></div>', page.read_text(encoding="utf-8"))
        code, stdout, stderr = self.run_stats(chat, "--events", events, "--date-order", "MDY")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.splitlines()[:3], ["Parsed 2 messages from 2026-01-02 to 2026-03-02", "Dates are read as month/day/year (MDY).", ""])
        self.assertIn("\nEvents: 3 shown; 1 far outside 2026-01-02–2026-03-02 not plotted\n", stdout)
        self.assertIn('data-vp-theme="auto"></div>', page.read_text(encoding="utf-8"))
        # Elsewhere than at a command line the same summary names no options.
        done = stats.write_conversation(chat, None, notify=self.fail)
        self.assertIsNone(done["viewer"])
        self.assertEqual(stats.viewer_lines(done), [])
        self.assertIn(ambiguous, stats.summary_lines(done, command=False))
        for lines in (stats.summary_lines(done, command=False), stats.viewer_lines({**done, "viewer": Path("conversation.html")}, command=False)):
            self.assertNotIn("--", "\n".join(lines))
        self.assertEqual(stats.viewer_lines({**done, "viewer": Path("conversation.html")}, command=False)[-1],
                         "conversation.html contains the whole conversation and its transcripts.")
        # A message whose date cannot be read is counted with the one before it, and the summary says so.
        odd = self.touch("odd/chat.txt", b"13/01/26, 10:00 - Ana: Hola\n99/99/26, 10:01 - Ana: When was this\n")
        code, stdout, stderr = self.run_stats(odd, "--no-viewer")
        self.assertEqual(stdout.splitlines()[:3], ["Parsed 2 messages from 2026-01-13 to 2026-01-13", "Dates are read as day/month/year (DMY).",
                                                   "1 messages have no date that can be read; they are counted on the day of the message before them."])

    def test_the_figure_is_drawn_for_the_people_named_or_nothing_is_written(self):
        export = self.export("export", CHAT + "15/01/26, 10:00 - Li: Hello\n")
        events = self.touch("events.txt", b"2026-01-14, First call\n")
        figure = self.root / "out" / "activity.png"
        code, stdout, stderr = self.run_stats(export, "--no-transcripts", "--no-viewer", "--png", figure, "--events", events,
                                              "--person", "Li", "--person", "José", "--event-style", "key", "--bucket", "week")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(figure.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(stdout.splitlines()[-2:], ["Events: 1 shown", f"Figure: {figure}"])
        self.assertEqual(self.files(self.root / "out"), ["activity.png"])
        self.assertEqual(self.files(self.work), [])
        # Each of those options reaches the image: without it another one is drawn.
        asked = ["--no-transcripts", "--no-viewer", "--events", events, "--person", "Li", "--person", "José", "--event-style", "key", "--bucket", "week"]
        for left_out in ("--bucket", "--event-style", "--events", "--person"):
            with self.subTest(left_out=left_out):
                place = asked.index(left_out)
                other = self.root / "other" / "activity.png"
                code, stdout, stderr = self.run_stats(export, "--png", other, *asked[:place], *asked[place + 2:])
                self.assertEqual(code, 0, stderr)
                self.assertNotEqual(other.read_bytes(), figure.read_bytes())
        code, stdout, stderr = self.run_stats(export, "--png", other, *asked)
        self.assertEqual(other.read_bytes(), figure.read_bytes())
        # Without names the two most active are drawn, and a note says who was left out.
        code, stdout, stderr = self.run_stats(export, "--no-transcripts", "--png", figure)
        self.assertEqual(code, 0, stderr)
        lines = stdout.splitlines()
        place = lines.index(f"Figure: {figure}")
        self.assertEqual(lines[place - 1], "3 people wrote in this chat; the figure draws the two most active, Ana and José. The others sent 1 messages. "
                                           "Choose who is drawn with --person.")
        self.assertEqual(lines[place + 1], f"Viewer: {self.work / 'conversation.html'}")

        # A name nobody has stops the command before the page, the copies or the image exist.
        figure.unlink()
        elsewhere = self.root / "elsewhere" / "page.html"
        code, stdout, stderr = self.run_stats(export, "--no-transcripts", "--viewer", elsewhere, "--png", figure, "--person", "Ana", "--person", "Bob")
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(stderr.splitlines()[-1], "Error: Nobody in this chat is called Bob. Its participants are: Ana, José, Li")
        self.assertFalse(figure.exists() or elsewhere.parent.exists())
        # So does a computer without matplotlib, with the way to get it.
        with without_imports("matplotlib"):
            code, stdout, stderr = self.run_stats(export, "--no-transcripts", "--viewer", elsewhere, "--png", figure)
        self.assertEqual((code, stdout, stderr), (1, "", 'Error: The figure needs matplotlib: python -m pip install "voxpad[plot]"\n'))
        self.assertFalse(figure.exists() or elsewhere.parent.exists())
        # The page alone needs neither matplotlib nor a decoder.
        with without_imports("matplotlib", "codecpod", "numpy"):
            code, stdout, stderr = self.run_stats(export, "--no-transcripts", "--viewer", elsewhere)
        self.assertEqual(code, 0, stderr)
        self.assertEqual([voice["seconds"] for voice in voices(model_in(elsewhere))], [2.5, 0.5])

    def test_options_that_contradict_each_other_are_refused_before_any_work(self):
        export = self.export()
        cases = (
            ["--viewer", self.work / "page.html", "--no-viewer"],
            ["--audio", "copy", "--no-viewer"],
            ["--transcripts", self.save_report("report.json", []), "--no-transcripts"],
            ["--person", "Ana"],
            ["--event-style", "key"],
            ["--png", self.work / "figure.png", *(argument for place in range(7) for argument in ("--person", f"Person {place}"))],
            ["--audio", "embed"],
            ["--bucket", "year"],
            ["--date-order", "DYM"],
            ["--event-style", "legend", "--png", self.work / "figure.png"],
            ["--transcripts", self.root / "missing.json"],
            ["--transcripts", export],
            ["--events", self.root / "missing.txt"],
            ["--transcripts"],
        )
        with mock.patch.object(stats, "write_conversation", side_effect=AssertionError("Nothing may be read or written")):
            for arguments in cases:
                with self.subTest(arguments=[Path(argument).name for argument in arguments]):
                    code, stdout, stderr = self.run_stats(export, *arguments)
                    self.assertEqual((code, stdout), (2, ""), stderr)
            code, stdout, stderr = self.run_stats(self.root / "missing")
            self.assertEqual((code, stdout), (2, ""))
            self.assertIn("Input does not exist", stderr)
        self.assertEqual(self.files(self.work), [])

    def test_exit_status_is_1_for_an_error_and_130_for_an_interruption(self):
        export = self.export()
        outcomes = (
            (None, 0, MEASURED.format(2) + "\n"),
            (OSError("The disk is full"), 1, "Error: The disk is full\n"),
            (zipfile.BadZipFile("File is not a zip file"), 1, "Error: File is not a zip file\n"),
            (KeyboardInterrupt(), 130, "\nInterrupted.\n"),
        )
        for outcome, status, said in outcomes:
            with self.subTest(status=status, said=said):
                stderr = io.StringIO()
                with mock.patch.object(stats, "write_conversation", side_effect=outcome) if outcome else nullcontext(), \
                        mock.patch.object(sys, "argv", ["voxpad-stats", str(export), "--no-transcripts", "--audio", "none"]), \
                        redirect_stdout(io.StringIO()), redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_:
                    stats.cli()
                self.assertEqual((exit_.exception.code, stderr.getvalue()), (status, said))
        # The module runs as the command does.
        done = subprocess.run([sys.executable, "-m", "voxpad.stats", "--help"], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("usage: voxpad-stats "))
        for option in ("--transcripts", "--no-transcripts", "--events", "--date-order", "--viewer", "--no-viewer", "--audio", "--bucket", "--png",
                       "--person", "--event-style"):
            self.assertIn(option, done.stdout)
        self.assertIn("A two-digit year written first is read as a day unless YMD is given", " ".join(done.stdout.split()))


class ConversationTests(StatsTestCase):
    def test_reports_of_one_chat_made_from_a_zip_and_from_its_folder_agree(self):
        export = self.export()
        heard = [{"chat_file": "chat.txt", "timestamp": "13/01/26, 10:01", "sender": "José"}]
        folder = self.save_report("folder/transcripts.json", [result(OPUS, "from the folder", 2.5, messages=heard)])
        # The ZIP keeps its files in a folder, so its report names the same recording by another path.
        packed = [{**heard[0], "chat_file": "WhatsApp Chat/chat.txt"}]
        zipped = self.save_report("zip/transcripts.json", [result(f"WhatsApp Chat/{OPUS}", "from the zip", 2.5, messages=packed)])
        refused = self.save_report("refused/transcripts.json", [result(f"media/{OPUS}", "", status="error", error="refused", messages=heard)])
        for reports, text in (([folder, zipped], "from the zip"), ([zipped, folder], "from the folder"), ([folder, refused], "from the folder")):
            with self.subTest(reports=[report.parent.name for report in reports]):
                done = stats.write_conversation(export, None, transcripts=reports, notify=lambda text: None)
                self.assertEqual([(voice["status"], voice["text"]) for voice in voices(done["model"])], [("ok", text), ("pending", "")])
                self.assertEqual(done["model"]["warnings"], [])

    def test_write_conversation_reports_what_it_found_and_did(self):
        export = self.export()
        report = self.save_report("reports/transcripts.json", [result(OPUS, "uno dos tres", 75.4)])
        events = self.touch("events.txt", b"2026-01-13, First call\nnot an event\n")
        page = self.root / "out" / "conversation.html"
        notes = []
        done = stats.write_conversation(export, page, transcripts=[report], events=events, bucket="month", notify=notes.append)
        self.assertEqual(sorted(done), ["audio", "audio_folder", "chat", "copied", "copied_bytes", "event_warnings", "matched", "measured", "model",
                                        "recordings", "transcripts", "viewer"])
        self.assertEqual({key: value for key, value in done.items() if key != "model"}, {
            "viewer": page, "chat": "chat.txt", "transcripts": ["transcripts.json"], "matched": 1, "recordings": 2, "measured": 1,
            "audio": "copy", "audio_folder": self.root / "out" / "conversation_audio", "copied": 2, "copied_bytes": 727 + 16044,
            "event_warnings": ["Events line 2 has no valid date and was skipped."],
        })
        self.assertEqual(done["model"], model_in(page))
        self.assertEqual(done["model"]["events"], [{"date": "2026-01-13", "label": "First call"}])
        self.assertEqual(notes, ["Events line 2 has no valid date and was skipped.", "Using transcripts.json: 1 of 2 voice messages matched",
                                 MEASURED_ONE, COPIED.format("conversation_audio")])
        self.assertIn('data-bucket="month"', page.read_text(encoding="utf-8"))
        # Without a page there is a model and nothing on disk; the lengths are still measured for it.
        before = self.contents()
        done = stats.write_conversation(export, None, audio="copy", notify=lambda text: None)
        self.assertEqual((done["viewer"], done["audio"], done["audio_folder"], done["copied"], done["transcripts"]), (None, "none", None, 0, []))
        self.assertEqual([(voice["src"], voice["seconds"]) for voice in voices(done["model"])], [(None, 2.5), (None, 0.5)])
        self.assertEqual(self.contents(), before)
        # check() sees the model before anything is written and can stop it.
        seen = []

        def refuse(model):
            seen.append([voice["src"] for voice in voices(model)])
            raise ValueError("Not this one")

        other = self.root / "other" / "page.html"
        with self.assertRaisesRegex(ValueError, "Not this one"):
            stats.write_conversation(export, other, check=refuse, notify=lambda text: None)
        self.assertEqual(seen, [[f"./page_audio/{OPUS}", f"./page_audio/{WAV}"]])
        with self.assertRaisesRegex(ValueError, "Unknown audio mode: embed"):
            stats.write_conversation(export, other, audio="embed")
        with self.assertRaisesRegex(ValueError, "Unknown date order"):
            stats.write_conversation(export, other, date_order="DYM", notify=lambda text: None)
        self.assertFalse(other.parent.exists())

    def test_a_message_that_names_a_recording_of_the_export_is_a_voice_message(self):
        # No attachment line, as some exports and forwarded chats have it: the recording being there is what tells.
        chat = "13/01/26, 10:00 - Ana: escucha nota-de-voz.opus cuando puedas\n13/01/26, 10:01 - José: no tengo nota.opus\n"
        self.touch("export/chat.txt", chat.encode("utf-8"))
        self.touch("export/nota-de-voz.opus", opus_bytes(3.0))
        done = stats.write_conversation(self.root / "export", None, notify=lambda text: None)
        self.assertEqual([(message["kind"], message["text"]) for message in done["model"]["messages"]],
                         [("voice", "escucha  cuando puedas"), ("text", "no tengo nota.opus")])
        self.assertEqual((done["recordings"], voices(done["model"])[0]["file"], voices(done["model"])[0]["seconds"]), (1, "nota-de-voz.opus", 3.0))

    def test_a_recording_named_twice_in_the_export_is_the_one_beside_the_chat(self):
        export = self.export()
        self.touch(f"export/older/{OPUS}", opus_bytes(9.0))
        self.touch(f"export/older/{WAV}", b"not a recording")
        done = stats.write_conversation(export, self.work / "page.html", notify=lambda text: None)
        self.assertEqual([(voice["src"], voice["seconds"]) for voice in voices(done["model"])], [(f"./page_audio/{OPUS}", 2.5), (f"./page_audio/{WAV}", 0.5)])
        self.assertEqual((self.work / "page_audio" / OPUS).read_bytes(), opus_bytes(2.5))
        # With no copy beside the chat nobody can say which one a message means.
        (export / "newer").mkdir()
        for name in (OPUS, WAV):
            (export / name).rename(export / "newer" / name)
        done = stats.write_conversation(export, None, notify=lambda text: None)
        self.assertEqual((done["recordings"], [voice["seconds"] for voice in voices(done["model"])]), (0, [None, None]))


class DurationTests(StatsTestCase):
    def test_lengths_come_from_the_headers_of_ogg_opus_and_wav_files(self):
        recording = self.touch("voice.opus", opus_bytes(73.9))
        self.assertAlmostEqual(stats.opus_seconds(recording), 73.9, places=9)
        self.assertEqual(stats.measure_seconds(recording), stats.opus_seconds(recording))
        head = b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 312, 48000, 0, 0)
        cases = {
            # What is skipped at the start is not played.
            "nothing skipped": (opus_bytes(1.0, skipped=0), 1.0),
            "long skip": (opus_bytes(1.0, skipped=3840), 1.0),
            "no sound at all": (opus_bytes(0.0), 0.0),
            "headers only": (opus_bytes(1.0)[:97], 0.0),
            "less than is skipped": (ogg_page(7, 0, 0, [head]) + ogg_page(7, 1, 100, [b"x"]), 0.0),
            # Sound may hold the four letters every page begins with.
            "capture pattern in the sound": (opus_bytes(2.0, sound=b"OggS" * 150), 2.0),
            # Only the end of a long file is read.
            "long recording": (opus_bytes(600.0, sound=b"\x02" * 60000) + b"".join(
                ogg_page(7, 2 + place, 312 + 48000 * (600 + place), [b"\x03" * 60000]) for place in range(1, 6)), 605.0),
            # A page on which no packet ends has no position of its own.
            "last page without a position": (opus_bytes(4.0) + ogg_page(7, 3, -1, [b"\x04" * 255]), 4.0),
            "pages of another stream after it": (opus_bytes(4.0) + ogg_page(8, 0, 480000, [b"\x05" * 100]), 4.0),
        }
        for name, (data, seconds) in cases.items():
            with self.subTest(name):
                self.assertAlmostEqual(stats.opus_seconds(self.touch("case.opus", data)), seconds, places=9)
        vorbis = ogg_page(7, 0, 0, [b"\x01vorbis" + bytes(23)]) + ogg_page(7, 1, 48000, [b"x" * 50])
        for name, data in {"vorbis": vorbis, "cut short": opus_bytes(2.0)[:60], "not ogg": b"RIFF" + bytes(200), "empty": b""}.items():
            with self.subTest(name):
                self.assertIsNone(stats.opus_seconds(self.touch("case.opus", data)))
        self.assertIsNone(stats.opus_seconds(self.root / "missing.opus"))

        tone = self.root / "tone.wav"
        write_wav(tone, frames=24000, sample_rate=16000, channels=2)
        self.assertEqual(stats.wav_seconds(tone), 1.5)
        self.assertEqual(stats.measure_seconds(tone), 1.5)
        for name, data in {"not wav": b"RIFF" + bytes(200), "empty": b"", "cut short": tone.read_bytes()[:20]}.items():
            with self.subTest(name):
                self.assertIsNone(stats.wav_seconds(self.touch("case.wav", data)))
        self.assertIsNone(stats.wav_seconds(self.root / "missing.wav"))
        # Nothing is decoded for any of this.
        with without_imports("codecpod", "numpy"):
            self.assertAlmostEqual(stats.measure_seconds(recording), 73.9, places=9)

    def test_other_formats_are_decoded_when_a_decoder_is_there(self):
        class Refused(Exception):
            pass

        asked = []

        def load(path, **options):
            asked.append((Path(path).name, options))
            if "broken" in path:
                raise Refused("Cannot decode")
            return types.SimpleNamespace(shape=(88200,)), 44100

        decoder = types.SimpleNamespace(load=load, CodecpodError=Refused)
        song = self.touch("song.m4a")
        self.assertIsNone(stats.measure_seconds(song))
        self.assertEqual(stats.measure_seconds(song, decoder), 2.0)
        self.assertIsNone(stats.measure_seconds(self.touch("broken.m4a"), decoder))
        # A header that tells the length keeps the decoder out of it; one that does not falls back to it.
        self.assertEqual(stats.measure_seconds(self.touch("voice.opus", opus_bytes(1.0)), decoder), 1.0)
        self.assertEqual(stats.measure_seconds(self.touch("old.ogg", b"OggS but not Opus"), decoder), 2.0)
        self.assertEqual([name for name, _ in asked], ["song.m4a", "broken.m4a", "old.ogg"])
        self.assertEqual(asked[0][1], {"mono": True})

        chat = "13/01/26, 10:00 - Ana: song.m4a (file attached)\n13/01/26, 10:01 - José: broken.m4a (file attached)\n"
        self.touch("export/chat.txt", chat.encode("utf-8"))
        self.touch("export/song.m4a")
        self.touch("export/broken.m4a")
        notes = []
        with mock.patch.dict(sys.modules, {"codecpod": decoder}):
            done = stats.write_conversation(self.root / "export", None, notify=notes.append)
        self.assertEqual(([voice["seconds"] for voice in voices(done["model"])], done["measured"]), ([2.0, None], 1))
        self.assertEqual(notes, ["The length of 1 recording could not be read.", MEASURED_ONE])
        # Without the decoder the command still runs, and says what would help.
        notes = []
        with without_imports("codecpod"):
            done = stats.write_conversation(self.root / "export", None, notify=notes.append)
        self.assertEqual(([voice["seconds"] for voice in voices(done["model"])], done["measured"]), ([None, None], 0))
        self.assertEqual(notes, ["The length of 2 recordings could not be read; with codecpod installed they are decoded to measure them."])
        # A length the report gives is never measured again.
        report = self.save_report("report.json", [result("song.m4a", "la la", 123.0), result("broken.m4a", "", status="error", error="Cannot decode")])
        asked.clear()
        with mock.patch.dict(sys.modules, {"codecpod": decoder}):
            done = stats.write_conversation(self.root / "export", None, transcripts=[report], notify=lambda text: None)
        self.assertEqual(([voice["seconds"] for voice in voices(done["model"])], [name for name, _ in asked]), ([123.0, None], ["broken.m4a"]))

    @unittest.skipUnless(codecpod is not None and np is not None, "Install codecpod and NumPy for audio conversion tests")
    def test_the_length_read_from_a_real_opus_file_is_the_length_it_decodes_to(self):
        recording = self.root / "voice.opus"
        samples = (np.sin(np.arange(int(48000 * 1.37)) / 30) * 0.3).astype("float32")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            codecpod.save(str(recording), samples, 48000, codecpod.Opus())
        decoded, rate = codecpod.load(str(recording), mono=True)
        self.assertAlmostEqual(stats.opus_seconds(recording), decoded.shape[-1] / rate, places=6)
        self.assertLess(abs(stats.opus_seconds(recording) - 1.37), 0.03)
        # What the transcription itself would report for it.
        resampled, rate = codecpod.load(str(recording), sample_rate=16000, mono=True)
        self.assertLess(abs(stats.opus_seconds(recording) - len(resampled) / rate), 0.001)


if __name__ == "__main__":
    unittest.main()
