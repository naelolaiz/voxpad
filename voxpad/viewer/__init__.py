"""The conversation viewer, shared with the browser app and written by VoxPad as one HTML file."""

import base64
import hashlib
import html
from importlib import resources
import json
from pathlib import Path
from urllib.parse import quote

from ..reports import atomic_write


BUCKETS = ("day", "week", "month")
EXPORT = "export { mountViewer, aggregate, totals, parseEvents, countWords };"
# What the page must not carry, compared without regard to case. The first four would end or hide the
# script element the source is copied into; the rest is what the page's promises rule out: markup built
# from chat text, storage, the network, code made at run time.
FORBIDDEN_SCRIPT = (
    "</script", "<script", "<!--", "-->", "import.meta", "import(",
    "innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setAttribute('style'", 'setAttribute("style"',
    "localStorage", "sessionStorage", "indexedDB", "caches.", "document.cookie", "fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon",
    "new Worker",
)
FORBIDDEN_STYLE = ("</style", "@import", "url(")
# The page is the viewer and nothing else, so the viewer fills the window. dvh follows a phone's address bar where it exists.
PAGE_STYLE = """html, body { height: 100%; margin: 0; }
body { background: #fffefa; }
#voxpad-root { --vp-height: 100vh; border-width: 0; }
@supports (height: 100dvh) { #voxpad-root { --vp-height: 100dvh; } }
@media (prefers-color-scheme: dark) { body { background: #18221d; } }
"""
MOUNT = ('mountViewer(document.getElementById("voxpad-root"), JSON.parse(document.getElementById("voxpad-model").textContent), '
         '{ theme: "auto", bucket: document.getElementById("voxpad-root").getAttribute("data-bucket") || undefined });')
# Inside a script element nothing is decoded, so the model is kept from ending it by JSON's own escapes, never by entities.
MODEL_ESCAPES = {ord(character): "\\" + "u%04x" % ord(character) for character in "<>&"}


def _asset(name: str) -> str:
    """Read a file of the viewer as browsers will hash it: without a byte order mark, every line ending a line feed."""
    text = resources.files(__name__).joinpath(name).read_bytes().decode("utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _refuse(name: str, text: str, forbidden: tuple[str, ...]) -> None:
    lower = text.lower()
    for needle in forbidden:
        if needle.lower() in lower:
            raise RuntimeError(f"{name} contains {needle!r}, which the viewer's page cannot carry.")


def _script() -> str:
    """The viewer as one script that runs where it stands: the module without its export line, then the call that mounts it."""
    source = _asset("viewer.js")
    lines = source.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[-1] != EXPORT:
        raise RuntimeError(f"viewer.js must end with the line {EXPORT!r}.")
    body = lines[:-1]
    if any(line.startswith(("import ", "export ")) for line in body):
        raise RuntimeError("viewer.js must be one module without imports and with a single export line.")
    _refuse("viewer.js", source, FORBIDDEN_SCRIPT)
    return '(function () {\n"use strict";\n' + "\n".join(body) + "\n" + MOUNT + "\n})();\n"


def _style() -> str:
    style = _asset("viewer.css")
    _refuse("viewer.css", style, FORBIDDEN_STYLE)
    return style + ("" if style.endswith("\n") else "\n") + PAGE_STYLE


def _hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


def _plays(model: dict) -> bool:
    return any(isinstance(message.get("voice"), dict) and message["voice"].get("src") for message in model.get("messages", ()))


def audio_link(recording: Path, page: Path) -> str | None:
    """Return the address of a recording as the page at `page` reaches it, or None when it lies outside the page's folder.

    A page may only name what is in its own folder or below it: a path leading
    out would write the names of the user's folders into a file meant to be shared.
    """
    try:
        relative = recording.resolve().relative_to(page.parent.resolve())
    except ValueError:
        return None
    return "./" + "/".join(quote(part, safe="") for part in relative.parts)


def render(model: dict, *, bucket: str | None = None) -> str:
    """Return the viewer of a conversation model as one HTML document that needs nothing else.

    `bucket` is the day, week or month the Activity charts open with; None leaves
    the choice to the viewer. The page allows itself only its own script and
    stylesheet, by their hashes, and recordings beside it when the model names any.
    """
    if bucket is not None and bucket not in BUCKETS:
        raise ValueError(f"Unknown bucket: {bucket}")
    script, style = _script(), _style()
    data = json.dumps(model, ensure_ascii=True, separators=(",", ":"), allow_nan=False).translate(MODEL_ESCAPES)
    policy = ["default-src 'none'", f"script-src {_hash(script)}", f"style-src {_hash(style)}"]
    if _plays(model):
        policy.append("media-src 'self'")
    policy += ["base-uri 'none'", "form-action 'none'"]
    title = model.get("title")
    title = title if isinstance(title, str) and title else "Conversation"
    opening = f' data-bucket="{html.escape(bucket)}"' if bucket else ""
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{"; ".join(policy)}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{html.escape(title)}</title>\n"
        # Nothing may stand between a tag and the text its hash was taken of.
        f"<style>{style}</style>\n</head>\n<body>\n"
        f'<main><div id="voxpad-root" class="voxpad-viewer" data-vp-theme="auto"{opening}></div></main>\n'
        f'<script type="application/json" id="voxpad-model">{data}</script>\n'
        f"<script>{script}</script>\n</body>\n</html>\n"
    )


def write_viewer(path: Path, model: dict, *, bucket: str | None = None) -> None:
    """Write the viewer of a conversation model to `path`, replacing the file only once the page is complete."""
    atomic_write(Path(path), render(model, bucket=bucket))
