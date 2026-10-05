"""Tests for the HTML file that carries the viewer: what is written into it, and what it allows itself."""

import base64
from contextlib import nullcontext
import hashlib
from html.parser import HTMLParser
from importlib import resources
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from voxpad import viewer
from tests.support import ExportTestCase


# Each would end the model's script element, start a script of its own, open a comment or pass for an entity.
HOSTILE = "</script><script>window.stolen = 1</script><!-- &amp; </SCRIPT > & "
# How JSON writes the three characters that markup could act on; put together here so that no tool rewrites them.
LESS, GREATER, AMPERSAND = ("\\" + "u" + code for code in ("003c", "003e", "0026"))
MODULE = "const greeting = 'hello';\nfunction mountViewer() {}\n" + viewer.EXPORT + "\n"
SHEET = ".voxpad-viewer { color: black; }\n"


def hostile_model(src=None):
    return {
        "schema": 1, "title": HOSTILE + "title", "date_order": "DMY", "date_order_ambiguous": False,
        "participants": [HOSTILE + "Ana", "José"],
        "messages": [
            {"time": "2026-01-10T10:00:00", "sender": 0, "kind": "text", "text": HOSTILE + "text", "words": 9},
            {"time": "2026-01-10T10:01:00", "sender": 1, "kind": "voice", "text": "", "words": 0,
             "voice": {"file": HOSTILE + "voice.opus", "src": src, "seconds": 2.5, "status": "ok", "text": HOSTILE + "transcript",
                       "words": 9, "languages": ["es"]}},
            {"time": None, "sender": 1, "kind": "media", "text": "", "words": 0, "media": {"type": "document", "file": HOSTILE + "file.pdf"}},
        ],
        # An emoji, a line separator and half a surrogate pair: none of them may be lost or stop the page from being written.
        "events": [{"date": "2026-01-10", "label": HOSTILE + "event " + chr(0x1F600) + chr(0x2028) + chr(0xD83D)}],
        "warnings": [],
    }


class Page(HTMLParser):
    """The parts of a written page: its elements in order, and the text inside each script, style and title."""

    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.texts = []
        self.open = None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attributes):
        self.tags.append((tag, dict(attributes)))
        if tag in {"script", "style", "title"}:
            self.open = [tag, dict(attributes), ""]
            self.texts.append(self.open)

    def handle_endtag(self, tag):
        self.open = None

    def handle_data(self, data):
        if self.open:
            self.open[2] += data

    def text(self, tag, place=0):
        return [text for name, _, text in self.texts if name == tag][place]

    def policy(self):
        content = next(attributes["content"] for tag, attributes in self.tags if tag == "meta" and "http-equiv" in attributes)
        return dict(part.split(" ", 1) for part in content.split("; "))


def digest(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


class ViewerTests(ExportTestCase):
    def assets(self, script=MODULE, style=SHEET):
        """A folder that stands in for the package's own, holding the two files as given."""
        folder = Path(tempfile.mkdtemp(dir=self.root))
        (folder / "viewer.js").write_bytes(script if isinstance(script, bytes) else script.encode("utf-8"))
        (folder / "viewer.css").write_bytes(style if isinstance(style, bytes) else style.encode("utf-8"))
        return mock.patch.object(resources, "files", return_value=folder)

    def test_chat_text_cannot_leave_the_model_block_and_comes_back_unchanged(self):
        model = hostile_model()
        text = viewer.render(model)
        page = Page(text)
        scripts = [(attributes, content) for tag, attributes, content in page.texts if tag == "script"]
        # The model and the viewer, and nothing a message could have added.
        self.assertEqual([attributes for attributes, _ in scripts], [{"type": "application/json", "id": "voxpad-model"}, {}])
        self.assertEqual(json.loads(scripts[0][1]), model)
        self.assertEqual(text.lower().count("<script"), 2)
        self.assertEqual(text.lower().count("</script"), 2)
        self.assertNotIn("<!--", text)
        # JSON's own escapes, six characters each: an entity would be shown as it is written.
        block = scripts[0][1]
        self.assertFalse(set("<>&") & set(block))
        self.assertTrue(block.isascii())
        self.assertIn(f"{LESS}/script{GREATER}{LESS}script{GREATER}window.stolen = 1{LESS}/script{GREATER}{LESS}!-- {AMPERSAND}amp; ", block)
        self.assertEqual(len(LESS), 6)
        self.assertNotIn("&lt;", block)
        self.assertEqual(block, json.dumps(model, ensure_ascii=True, separators=(",", ":")).replace("<", LESS).replace(">", GREATER)
                         .replace("&", AMPERSAND))
        # The title is markup, and is escaped as markup.
        self.assertEqual(page.text("title"), model["title"])
        self.assertIn("<title>&lt;/script&gt;&lt;script&gt;window.stolen = 1&lt;/script&gt;&lt;!-- &amp;amp; ", text)

    def test_document_has_the_parts_in_order_and_the_script_wraps_the_module(self):
        with self.assets():
            text = viewer.render(hostile_model(), bucket="week")
        page = Page(text)
        self.assertTrue(text.startswith('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'))
        self.assertEqual([tag for tag, _ in page.tags],
                         ["html", "head", "meta", "meta", "meta", "title", "style", "body", "main", "div", "script", "script"])
        metas = [attributes for tag, attributes in page.tags if tag == "meta"]
        self.assertEqual(metas[0], {"charset": "utf-8"})
        self.assertEqual(metas[1]["http-equiv"], "Content-Security-Policy")
        self.assertEqual(metas[2], {"name": "viewport", "content": "width=device-width, initial-scale=1"})
        self.assertEqual(dict(page.tags)["div"], {"id": "voxpad-root", "class": "voxpad-viewer", "data-vp-theme": "auto", "data-bucket": "week"})
        self.assertEqual(page.text("script", 1), '(function () {\n"use strict";\n' + "const greeting = 'hello';\nfunction mountViewer() {}\n"
                         + viewer.MOUNT + "\n})();\n")
        self.assertIn('mountViewer(document.getElementById("voxpad-root"), JSON.parse(document.getElementById("voxpad-model").textContent), '
                      '{ theme: "auto", bucket:', viewer.MOUNT)
        self.assertIn('getAttribute("data-bucket")', viewer.MOUNT)
        self.assertTrue(page.text("style").startswith(SHEET))
        # The page is the viewer and nothing else, so it gets the whole window.
        self.assertIn("--vp-height: 100dvh", page.text("style"))
        # Without a bucket the viewer chooses, and an unknown one is refused before it reaches the markup.
        with self.assets():
            self.assertIn('<div id="voxpad-root" class="voxpad-viewer" data-vp-theme="auto"></div>', viewer.render(hostile_model()))
            with self.assertRaisesRegex(ValueError, "Unknown bucket"):
                viewer.render(hostile_model(), bucket='"><script>')

    def test_policy_allows_only_the_two_hashed_texts_and_recordings_beside_the_page(self):
        names = ["default-src", "script-src", "style-src", "media-src", "base-uri", "form-action"]
        for real in (True, False):
            for src in (None, "./conversation_audio/voice.opus"):
                with self.subTest(real=real, src=src), nullcontext() if real else self.assets():
                    page = Page(viewer.render(hostile_model(src)))
                    policy = page.policy()
                    self.assertEqual(list(policy), names if src else [name for name in names if name != "media-src"])
                    self.assertEqual(policy["default-src"], "'none'")
                    self.assertEqual(policy["base-uri"], "'none'")
                    self.assertEqual(policy["form-action"], "'none'")
                    # Recordings may come from beside the page and from nowhere else; without any, from nowhere at all.
                    self.assertEqual(policy.get("media-src"), "'self'" if src else None)
                    # Each hash is of exactly the text between the tags.
                    self.assertEqual(policy["script-src"], digest(page.text("script", 1)))
                    self.assertEqual(policy["style-src"], digest(page.text("style")))

    def test_line_endings_and_a_byte_order_mark_in_the_assets_do_not_change_the_page(self):
        with self.assets():
            expected = viewer.render(hostile_model())
        changes = {
            # A checkout that turns line endings into CR LF would otherwise give hashes no browser arrives at.
            "crlf": lambda text: text.replace("\n", "\r\n").encode("utf-8"),
            "cr": lambda text: text.replace("\n", "\r").encode("utf-8"),
            "mark": lambda text: b"\xef\xbb\xbf" + text.encode("utf-8"),
        }
        for name, change in changes.items():
            with self.subTest(name), self.assets(change(MODULE), change(SHEET)):
                text = viewer.render(hostile_model())
                self.assertEqual(text, expected)
                self.assertNotIn("\r", text)
        with self.assets(MODULE + "\n  \n"):
            self.assertEqual(viewer.render(hostile_model()), expected)

    def test_assets_that_the_page_cannot_carry_are_refused(self):
        body, export = MODULE.split(viewer.EXPORT)[0], viewer.EXPORT + "\n"
        scripts = {
            "no export line": body,
            "another export line": body + "export { greeting };\n",
            "export line is not last": export + body,
            "export line changed": body + "export { mountViewer };\n",
            "import": 'import { helper } from "./helper.js";\n' + body + export,
            "second export": "export const other = 1;\n" + body + export,
            "script end": body + "const a = '</script>';\n" + export,
            "script end in capitals": body + "const a = '</SCRIPT>';\n" + export,
            "script start": body + "const a = '<script>';\n" + export,
            "comment": body + "const a = '<!-- note';\n" + export,
            "comment end": body + "let a = 2;\nwhile (a --> 0) {}\n" + export,
            "import.meta": body + "const here = import.meta.url;\n" + export,
            "import()": body + "const later = () => import('./more.js');\n" + export,
            "markup from text": body + "function unsafe(node, text) { node.innerHTML = text; }\n" + export,
            "network": body + "function send(url) { return fetch(url); }\n" + export,
            "storage": body + "const kept = () => window.localStorage;\n" + export,
            "run-time code": body + "const made = new Function('return 1');\n" + export,
        }
        for name, script in scripts.items():
            with self.subTest(name), self.assets(script=script), self.assertRaises(RuntimeError):
                viewer.render(hostile_model())
        styles = {
            "style end": SHEET + "a::after { content: '</style>'; }\n",
            "import": '@import "other.css";\n' + SHEET,
            "import in capitals": '@IMPORT "other.css";\n' + SHEET,
            "url": SHEET + ".voxpad-viewer .vp-x { background: url(https://example.org/x.png); }\n",
        }
        for name, style in styles.items():
            with self.subTest(name), self.assets(style=style), self.assertRaises(RuntimeError):
                viewer.render(hostile_model())
        # The files that ship pass every one of these checks, and the module's last line is the one cut off.
        text = viewer.render(hostile_model())
        self.assertNotIn(viewer.EXPORT, text)
        self.assertIn("function mountViewer(", text)

    def test_recordings_are_named_by_quoted_paths_below_the_page_and_never_above_it(self):
        page = self.root / "out put" / "conversation.html"
        inside = self.touch("out put/media #1/100% José.opus")
        beside = self.touch("out put/voice.opus")
        outside = self.touch("elsewhere/voice.opus")
        above = self.touch("voice.opus")
        self.assertEqual(viewer.audio_link(inside, page), "./media%20%231/100%25%20Jos%C3%A9.opus")
        self.assertEqual(viewer.audio_link(beside, page), "./voice.opus")
        # Reaching these would take "..", and with it the names of folders that are nobody else's business.
        self.assertIsNone(viewer.audio_link(outside, page))
        self.assertIsNone(viewer.audio_link(above, page))
        # A path that leaves the folder and comes back is judged by where it ends.
        self.assertEqual(viewer.audio_link(self.root / "elsewhere" / ".." / "out put" / "voice.opus", page), "./voice.opus")
        link = self.root / "out put" / "linked.opus"
        self.symlink(link, outside)
        self.assertIsNone(viewer.audio_link(link, page))

    def test_write_viewer_replaces_the_page_in_one_step_and_writes_line_feeds_only(self):
        page = self.root / "pages" / "conversation.html"
        model = hostile_model("./conversation_audio/voice.opus")
        model["title"] = "Ana · José"
        viewer.write_viewer(page, model, bucket="month")
        written = page.read_bytes()
        self.assertEqual(written, viewer.render(model, bucket="month").encode("utf-8"))
        self.assertNotIn(b"\r", written)
        # Nothing says where on this computer the page or the recordings are.
        self.assertNotIn(str(self.root).encode("utf-8"), written)
        self.assertNotIn(b"file://", written)
        with self.assets(script="broken"), self.assertRaises(RuntimeError):
            viewer.write_viewer(page, hostile_model())
        # A page that cannot be made leaves the earlier one, and nothing else, in its folder.
        self.assertEqual(page.read_bytes(), written)
        self.assertEqual([path.name for path in page.parent.iterdir()], ["conversation.html"])
        with self.assertRaises(ValueError):
            viewer.render({**model, "warnings": [float("nan")]})

    def test_viewer_stats_and_figure_import_without_any_third_party_package(self):
        # The browser test writes the page with a bare python3, and the command must start without matplotlib.
        check = (
            "import sys\n"
            "import voxpad.viewer, voxpad.stats, voxpad.figure, voxpad.analysis\n"
            "extra = sorted({name.split('.')[0] for name in sys.modules} & "
            "{'matplotlib', 'numpy', 'codecpod', 'faster_whisper', 'ctranslate2', 'huggingface_hub', 'PySide6', 'PIL'})\n"
            "print(extra)\n"
        )
        done = subprocess.run([sys.executable, "-c", check], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "[]"), done.stderr)


if __name__ == "__main__":
    unittest.main()
