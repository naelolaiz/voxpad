"""Tests for finding an export's recordings and chats and linking them."""

import codecs
from contextlib import redirect_stderr
import io
import stat
import unittest
import zipfile

from voxpad.chat import parse_chat
from voxpad.export import index_messages, open_export, read_chats, scan_export
from tests.support import ExportTestCase


class ExportTests(ExportTestCase):
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
        export = scan_export(self.root, set())
        contexts, texts, occurrences = index_messages(export)

        self.assertEqual(contexts[spanish], [{"chat_file": "_chat.txt", "timestamp": "01/10/26, 09:10:11", "sender": "Ana"}])
        self.assertEqual(contexts[german][0]["sender"], "Jörg")
        self.assertEqual(contexts[french][0]["sender"], "Chloé")
        self.assertEqual(occurrences[(chat, 1)], [spanish])
        self.assertEqual(texts[chat], text)
        self.assertEqual(parse_chat(text)[0].text, f"<adjunto: {spanish.name}>\nDescripción de la nota")

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
        contexts, _, occurrences = index_messages(scan_export(self.root, set()))
        messages = parse_chat(text)

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
        contexts, _, _ = index_messages(scan_export(self.root, set()))
        self.assertEqual([message["sender"] for message in contexts[audio]], ["C"])

    def test_duplicate_basenames_prefer_same_folder_and_skip_ambiguous_links(self):
        first = self.touch("first/voice.opus")
        second = self.touch("second/voice.opus")
        self.touch("first/chat.txt", b"01/10/26, 10:00 - Ana: voice.opus (attached)\n")
        self.touch("chat.txt", b"01/10/26, 10:01 - Bob: voice.opus (attached)\n")
        error_output = io.StringIO()
        with redirect_stderr(error_output):
            contexts, _, _ = index_messages(scan_export(self.root, set()))
        self.assertEqual([message["sender"] for message in contexts[first]], ["Ana"])
        self.assertEqual(contexts[second], [])
        self.assertIn("ambiguous", error_output.getvalue())

    def test_chats_are_read_whether_or_not_the_export_has_recordings(self):
        text = codecs.BOM_UTF8.decode("utf-8") + "01/10/26, 10:00 - Ana: Hola\r\n01/10/26, 10:01 - José: <Media omitted>\r\n"
        chat = self.touch("WhatsApp Chat.txt", text.encode("utf-8"))
        unrecognized = self.touch("ios/_chat.txt", b"An export in a format that is not recognized")
        self.touch("notes.txt", b"Notes about this export")
        self.touch("latin.txt", "01/10/26, 10:00 - José: Adiós\n".encode("latin-1"))
        error_output = io.StringIO()
        with redirect_stderr(error_output):
            export = scan_export(self.root, set())
            chats = read_chats(export)
            contexts, texts, occurrences = index_messages(export)
        # The exact text, mark and line endings included, in the order the chats were found.
        self.assertEqual(list(chats.items()), [(chat, text), (unrecognized, "An export in a format that is not recognized")])
        self.assertEqual((contexts, texts, occurrences), ({}, chats, {}))
        self.assertIn("skipping non-UTF-8 chat text: latin.txt", error_output.getvalue())
        # With recordings the same chats are read.
        audio = self.touch("voice.opus")
        with redirect_stderr(io.StringIO()):
            export = scan_export(self.root, set())
            self.assertEqual(read_chats(export), chats)
            self.assertEqual(index_messages(export), ({audio: []}, chats, {}))

    def test_folder_discovery_excludes_outputs_and_hidden_folders(self):
        audio = self.touch("media/VOICE.OPUS")
        chat = self.touch("chat.txt", b"chat")
        output = self.touch("transcripts.txt", b"previous report")
        self.touch(".cache/ignore.wav")
        self.touch("photo.jpg")
        with open_export(self.root, {output.resolve()}) as export:
            self.assertEqual(export.audio, [audio])
            self.assertEqual(export.chats, [chat])

    def test_folder_discovery_does_not_follow_symlinks(self):
        audio = self.touch("media/VOICE.OPUS")
        self.symlink(self.root / "linked.opus", audio)
        self.symlink(self.root / "linked-folder", audio.parent, directory=True)
        with open_export(self.root) as export:
            self.assertEqual(export.audio, [audio])

    def test_chat_input_selects_chat_and_audio_input_selects_only_recording(self):
        audio = self.touch("voice.opus")
        self.touch("another.wav")
        chat = self.touch("selected.txt", b"chat")
        self.touch("other.txt", b"other chat")
        with open_export(chat) as export:
            self.assertEqual(export.chats, [chat])
            self.assertEqual(len(export.audio), 2)
        with open_export(audio) as export:
            self.assertEqual(export.audio, [audio])
            self.assertEqual(export.chats, [])

    def test_zip_extracts_supported_files_and_cleans_up(self):
        archive = self.make_zip([
            ("Export/_chat.txt", "chat"),
            ("Export/Media/voice.OPUS", b"audio"),
            ("Export/photo.jpg", b"image"),
            ("Export/ignored.exe", b"program"),
        ])
        with open_export(archive) as export:
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
                    with open_export(archive):
                        self.fail("Unsafe archive was accepted")

    def test_zip_rejects_duplicate_normalized_paths(self):
        for names in (("voice.opus", "voice.opus"), ("media//voice.opus", "media/voice.opus")):
            with self.subTest(names=names):
                archive = self.make_zip([(name, b"audio") for name in names])
                with self.assertRaisesRegex(ValueError, "Duplicate ZIP"):
                    with open_export(archive):
                        self.fail("Duplicate archive was accepted")

    def test_zip_rejects_symlinks(self):
        link = zipfile.ZipInfo("linked.opus")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.make_zip([(link, "../../outside.opus")])
        with self.assertRaisesRegex(ValueError, "Unsafe ZIP"):
            with open_export(archive):
                self.fail("Symlink archive was accepted")


if __name__ == "__main__":
    unittest.main()
