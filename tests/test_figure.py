"""Tests for the activity figure. They draw real images: matplotlib is a development dependency, so none of them is skipped."""

import builtins
from datetime import date
import struct
import unittest
from unittest import mock
import warnings

from voxpad.analysis import build_model, parse_events
from voxpad.figure import (COLORS, MAX_PEOPLE, NEED_MATPLOTLIB, STYLE, _plan_list, chat_span, choose_people, require_matplotlib,
                           split_events, write_figure)
from tests.support import ExportTestCase


CHAT = (
    "13/01/26, 10:00 - Ana: Hola José, ¿cómo estás?\n"
    "13/01/26, 10:01 - José: PTT-20260113-WA0001.opus (file attached)\n"
    "14/01/26, 09:00 - Ana: PTT-20260114-WA0002.opus (file attached)\n"
    "17/01/26, 09:30 - José: Todo bien por aquí\n"
    "18/01/26, 11:00 - Ana: Nos vemos mañana\n"
    "02/02/26, 08:00 - Li: Hello both\n"
)
RESULTS = [{"file": "PTT-20260113-WA0001.opus", "status": "ok", "text": "uno dos tres cuatro cinco", "duration_seconds": 90.0}]
EVENTS = (
    "2026-01-14, First call\n"
    "2026-01-14 | The same day, told at length: " + "what happened then and why it mattered to everyone involved; " * 6 + "\n"
    "2025-06-01 Long before the chat\n"
    "2026-02-20, Shortly after the chat\n"
    "2026-03-01, Too long after it\n"
)


def model_of(chat=CHAT, results=RESULTS, events=None):
    return build_model(chat, results=results, events=parse_events(events)[0] if events else None)


def png_size(path):
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", data[16:24])


class FigureTests(ExportTestCase):
    def draw(self, model, name="figure.png", **options):
        notes = []
        outcome = write_figure(self.root / name, model, notify=notes.append, **options)
        return outcome, notes

    def test_draws_a_panel_per_metric_with_totals_and_what_they_cover(self):
        import matplotlib
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            outcome, notes = self.draw(model_of())
        self.assertEqual(caught, [])
        wide, tall = png_size(self.root / "figure.png")
        self.assertGreater(wide, 1500)
        self.assertGreater(tall, 1200)
        self.assertEqual(outcome["path"], self.root / "figure.png")
        self.assertEqual(outcome["people"], ["Ana", "José"])
        self.assertEqual(outcome["panels"], ["messages", "words", "voice notes", "minutes of audio"])
        # Spoken words stand beside the typed ones, and a note says how much of the voice messages they and the minutes cover.
        self.assertEqual(outcome["titles"], [
            "Messages    Ana: 3   ·   José: 2",
            "Words    Ana: 7 typed + 0 spoken   ·   José: 4 typed + 5 spoken    (1 of 2 voice notes transcribed)",
            "Voice notes    Ana: 1   ·   José: 1",
            "Minutes of audio    Ana: 0.0 min   ·   José: 1.5 min    (1 of 2 timed)",
        ])
        self.assertEqual(notes, ["3 people wrote in this chat; the figure draws the two most active, Ana and José. "
                                 "The others sent 1 messages. Choose who is drawn with --person."])
        self.assertEqual((outcome["events"], outcome["far_events"]), (0, 0))
        # Drawing changes no setting of a program that uses matplotlib itself, and leaves nothing beside the image.
        self.assertEqual({key: matplotlib.rcParams[key] for key in STYLE}, {key: matplotlib.rcParamsOrig[key] for key in STYLE})
        self.assertNotEqual(matplotlib.rcParams["axes.edgecolor"], STYLE["axes.edgecolor"])
        self.assertEqual([path.name for path in self.root.iterdir()], ["figure.png"])

    def test_a_metric_nobody_has_anything_of_gets_no_panel(self):
        chat = "13/01/26, 10:00 - Ana: Hola\n14/01/26, 10:00 - José: <Media omitted>\n15/01/26, 10:00 - José: Qué tal\n"
        outcome, notes = self.draw(model_of(chat, None))
        self.assertEqual(outcome["panels"], ["messages", "words"])
        self.assertEqual(notes, ["[skip] no data for voice notes.", "[skip] no data for minutes of audio."])
        # Without voice messages there are no spoken words to tell apart.
        self.assertEqual(outcome["titles"][1], "Words    José: 2   ·   Ana: 1")
        full = png_size(self.root / "figure.png")
        # Voice messages that nobody transcribed or timed: counted, and said to be uncovered rather than drawn as silence.
        outcome, notes = self.draw(model_of(results=None))
        self.assertEqual(outcome["panels"], ["messages", "words", "voice notes"])
        self.assertEqual(outcome["titles"][1], "Words    Ana: 7 typed + 0 spoken   ·   José: 4 typed + 0 spoken    (0 of 2 voice notes transcribed)")
        self.assertEqual(notes[1:], ["[skip] no data for minutes of audio."])
        self.assertGreater(png_size(self.root / "figure.png")[1], full[1])
        # Everything transcribed and timed needs no note.
        results = RESULTS + [{"file": "PTT-20260114-WA0002.opus", "status": "ok", "text": "seis siete", "duration_seconds": 30}]
        outcome, _ = self.draw(model_of(results=results))
        self.assertEqual(outcome["titles"][1], "Words    Ana: 7 typed + 2 spoken   ·   José: 4 typed + 5 spoken")
        self.assertEqual(outcome["titles"][3], "Minutes of audio    Ana: 0.5 min   ·   José: 1.5 min")

    def test_people_are_the_two_most_active_or_the_ones_named(self):
        model = model_of()
        self.assertEqual(choose_people(model), ([0, 1], "3 people wrote in this chat; the figure draws the two most active, Ana and José. "
                                                        "The others sent 1 messages. Choose who is drawn with --person."))
        self.assertEqual(choose_people(model, ["Li", "Ana", "Li"]), ([2, 0], None))
        # A name typed with a combining accent is the same name.
        self.assertEqual(choose_people(model, ["Jose" + chr(0x301)]), ([1], None))
        with self.assertRaisesRegex(ValueError, "Nobody in this chat is called Bob or ana. Its participants are: Ana, José, Li"):
            choose_people(model, ["Bob", "Ana", "ana"])
        two = model_of(CHAT.rsplit("\n", 2)[0] + "\n")
        self.assertEqual(choose_people(two), ([0, 1], None))
        alone = model_of("13/01/26, 10:00 - Ana: Hola\n", None)
        self.assertEqual(choose_people(alone), ([0], None))
        with self.assertRaisesRegex(ValueError, "Nothing to plot"):
            choose_people(model_of("13/01/26, 10:00 - Messages are end-to-end encrypted.\n", None))
        outcome, notes = self.draw(model, people=["Li", "José"])
        self.assertEqual(outcome["people"], ["Li", "José"])
        self.assertEqual(outcome["titles"][0], "Messages    Li: 1   ·   José: 2")
        self.assertEqual(notes, [])
        outcome, _ = self.draw(alone)
        self.assertEqual(outcome["titles"], ["Messages    Ana: 1", "Words    Ana: 1"])
        # Seven people cannot be told apart by colour.
        self.assertEqual((MAX_PEOPLE, len(COLORS)), (6, 6))
        crowd = model_of("".join(f"13/01/26, 10:0{place} - Person {place}: hola\n" for place in range(7)), None)
        with self.assertRaisesRegex(ValueError, "at most 6 people"):
            write_figure(self.root / "crowd.png", crowd, people=[f"Person {place}" for place in range(7)])
        self.assertFalse((self.root / "crowd.png").exists())

    def test_six_people_with_long_names_get_titles_that_fit(self):
        names = [f"Participant number {place} with a rather long family name" for place in range(6)]
        chat = "".join(f"1{place}/01/26, 10:00 - {name}: {'hola ' * (place + 1)}\n" for place, name in enumerate(names))
        outcome, notes = self.draw(model_of(chat, None), people=names)
        self.assertEqual(outcome["people"], names)
        for title in outcome["titles"]:
            # Every total is there, on as many lines as the width needs.
            self.assertGreaterEqual(len(title.split("\n")), 2)
            self.assertEqual(title.replace("\n", "   ·   ").count("   ·   "), 5)
        # The panels move apart to make room for those lines.
        tall = outcome["size"][1]
        short, _ = self.draw(model_of(chat, None), "short.png")
        self.assertGreater(tall, short["size"][1])

    def test_events_far_outside_the_chat_are_left_out(self):
        span = (date(2026, 1, 13), date(2026, 2, 2))
        self.assertEqual(chat_span(model_of()), span)
        self.assertIsNone(chat_span(model_of("no chat here", None)))

        def split(days, span=span):
            events = [{"date": day, "label": day} for day in days]
            near, far = split_events(events, span)
            return [event["date"] for event in near], [event["date"] for event in far]

        # Three weeks of room on either side of a short chat.
        self.assertEqual(split(["2025-12-22", "2025-12-23", "2026-01-20", "2026-02-23", "2026-02-24"]),
                         (["2025-12-23", "2026-01-20", "2026-02-23"], ["2025-12-22", "2026-02-24"]))
        # A long chat gives 15 % of its length: 60 days of 400.
        long = (date(2025, 1, 1), date(2026, 2, 5))
        self.assertEqual(split(["2024-11-01", "2024-11-02", "2026-04-06", "2026-04-07"], long), (["2024-11-02", "2026-04-06"], ["2024-11-01", "2026-04-07"]))
        self.assertEqual(split(["2026-01-20"], None), ([], ["2026-01-20"]))
        model = model_of(events=EVENTS)
        plain, _ = self.draw(model_of(), "plain.png")
        for style in ("auto", "list", "key"):
            with self.subTest(style):
                outcome, _ = self.draw(model, f"{style}.png", event_style=style)
                self.assertEqual((outcome["events"], outcome["far_events"]), (3, 2))
                # The events take their room below the panels.
                self.assertGreater(outcome["size"][1], plain["size"][1] + 0.5)
                self.assertGreater(png_size(self.root / f"{style}.png")[1], png_size(self.root / "plain.png")[1])
        self.assertEqual((self.root / "auto.png").read_bytes(), (self.root / "list.png").read_bytes())
        self.assertNotEqual((self.root / "key.png").read_bytes(), (self.root / "list.png").read_bytes())
        with self.assertRaisesRegex(ValueError, "Unknown event style"):
            write_figure(self.root / "other.png", model, event_style="legend")

    def test_event_texts_are_placed_by_their_measured_size_and_never_overlap(self):
        def measure(text, size, **options):
            lines = text.split("\n")
            return max(map(len, lines)) * 0.06, len(lines) * 0.2

        events = [(place + 1, 20000.0 + place // 3, date(2026, 1, 1 + place // 3), "word " * (5 + 9 * place)) for place in range(12)]
        plan = _plan_list(events, 20.0, (19990.0, 20030.0), measure, "%a %d %b")
        boxes = [item["box"] for item in plan["items"]]
        for place, (left, right, top, bottom) in enumerate(boxes):
            for other_left, other_right, other_top, other_bottom in boxes[:place]:
                self.assertTrue(right <= other_left or other_right <= left or bottom <= other_top or other_bottom <= top)
        self.assertGreaterEqual(plan["height"], max(bottom for _, _, _, bottom in boxes))
        # The height is the one measured: with taller lines the same texts need a taller strip.
        taller = _plan_list(events, 20.0, (19990.0, 20030.0), lambda text, size, **options: (measure(text, size)[0], measure(text, size)[1] * 2), "%a %d %b")
        self.assertGreater(taller["height"], plan["height"] * 1.8)
        self.assertEqual([item["text"].split(" · ")[0] for item in plan["items"][:4]], ["Thu 01 Jan"] * 3 + ["Fri 02 Jan"])

    def test_weeks_and_months_are_drawn_and_other_buckets_refused(self):
        model = model_of(events="2026-01-14, First call\n")
        for bucket in ("day", "week", "month"):
            with self.subTest(bucket):
                outcome, _ = self.draw(model, f"{bucket}.png", bucket=bucket)
                self.assertEqual(outcome["panels"], ["messages", "words", "voice notes", "minutes of audio"])
                self.assertGreater(png_size(self.root / f"{bucket}.png")[0], 1500)
        self.assertNotEqual((self.root / "day.png").read_bytes(), (self.root / "week.png").read_bytes())
        with self.assertRaisesRegex(ValueError, "Unknown bucket: year"):
            write_figure(self.root / "year.png", model, bucket="year")
        # A chat without a single readable date has no time to draw over.
        undated = model_of("99/99/26, 10:00 - Ana: Hola\n99/99/26, 10:01 - José: Hola\n", None)
        with self.assertRaisesRegex(ValueError, "Nothing to plot: no message of this chat has a date"):
            write_figure(self.root / "undated.png", undated)
        self.assertFalse((self.root / "undated.png").exists())

    def test_names_and_labels_are_drawn_as_they_are_written(self):
        # Between dollar signs matplotlib would read a formula, and fail on one that is none.
        li = chr(0x674E) + " Li"
        chat = ("13/01/26, 10:00 - Ana $5: Hola\n13/01/26, 10:01 - José_$: \\frac{1}{2\n14/01/26, 10:00 - Ana $5: Hola " + chr(0x1F600) + "\n"
                "14/01/26, 10:01 - " + li + ": hi\n")
        events = "2026-01-13, Paid $5 and $10: 100% of \\frac{1}{ {x_1}^2 & more\n2026-01-14, _hidden_ $\\alpha$ " + chr(0x674E) + "\n"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for style in ("list", "key"):
                outcome, _ = self.draw(model_of(chat, None, events), f"{style}.png", event_style=style, people=["Ana $5", "José_$", li])
                self.assertEqual(outcome["events"], 2)
                self.assertEqual(outcome["titles"][0], "Messages    Ana \\$5: 2   ·   José_\\$: 1   ·   " + li + ": 1")
        # A name in a script the font lacks is drawn with boxes, without a warning for each letter.
        self.assertEqual([str(warning.message) for warning in caught], [])

    def test_missing_matplotlib_is_explained_before_anything_is_written(self):
        original = builtins.__import__

        def without(name, *arguments, **options):
            if name.split(".")[0] == "matplotlib":
                raise ImportError("No module named 'matplotlib'")
            return original(name, *arguments, **options)

        with mock.patch.object(builtins, "__import__", side_effect=without):
            for call in (require_matplotlib, lambda: write_figure(self.root / "figure.png", model_of())):
                with self.assertRaises(RuntimeError) as error:
                    call()
                self.assertEqual(str(error.exception), 'The figure needs matplotlib: python -m pip install "voxpad[plot]"')
        self.assertEqual(NEED_MATPLOTLIB, 'The figure needs matplotlib: python -m pip install "voxpad[plot]"')
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
