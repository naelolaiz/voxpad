"""Read the messages of an exported WhatsApp chat."""

from dataclasses import dataclass
import re


DATE = r"\d{1,4}[./-]\d{1,2}[./-]\d{1,4}"
PERIOD = r"(?:[aApP]\.?\s*[mM]\.?|[صم]|上午|下午|午前|午後)"
CLOCK = r"\d{1,2}[:：]\d{2}(?:[:：]\d{2})?"
TIME = rf"(?:{PERIOD}\s*)?{CLOCK}(?:\s*{PERIOD})?"
HEADER = re.compile(
    rf"^(?:\[(?P<ios>{DATE}[,،]?\s*{TIME})\]\s*|(?P<android>{DATE}[,،]?\s+{TIME})\s+-\s+)(?P<body>.*)$"
)
SENDER = re.compile(r"[:：]\s")
NEWLINE = re.compile(r"\r\n|\r|\n")
INVISIBLE = str.maketrans("", "", "\ufeff\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


@dataclass
class Message:
    timestamp: str
    sender: str | None
    text: str
    end_line: int


def parse_chat(text: str) -> list[Message]:
    """Read common iOS/Android headers without guessing the date locale."""
    messages: list[Message] = []
    # Only CR LF, CR and LF end a line. str.splitlines() also breaks at form feeds
    # and at the Unicode line separators, which people paste into their messages.
    lines = NEWLINE.split(text)
    if not lines[-1]:
        lines.pop()
    for line_number, line in enumerate(lines):
        clean = line.translate(INVISIBLE)
        match = HEADER.match(clean)
        if match:
            body = match["body"]
            separator = SENDER.search(body)
            messages.append(Message(
                match["ios"] or match["android"],
                body[:separator.start()] if separator else None,
                body[separator.end():] if separator else body,
                line_number,
            ))
        elif messages:
            messages[-1].text += "\n" + clean
            messages[-1].end_line = line_number
    return messages
