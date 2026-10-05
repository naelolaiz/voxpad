"""Tests for reading the messages of a chat."""

import unittest

from voxpad.chat import parse_chat


class ChatTests(unittest.TestCase):
    def test_headers_accept_localized_dates_and_time_markers(self):
        cases = (
            ("10/4/26, 9:10 PM - José: Hola", "10/4/26, 9:10 PM"),
            ("2026-10-4, 9:10 p. m. - José: Hola", "2026-10-4, 9:10 p. m."),
            ("[٤/١٠/٢٠٢٦، ٩:١٠ م] José: Hola", "٤/١٠/٢٠٢٦، ٩:١٠ م"),
            ("[2026/10/4, 上午9:10] José: Hola", "2026/10/4, 上午9:10"),
            ("[2026/10/4, 9:10 午後] José: Hola", "2026/10/4, 9:10 午後"),
            ("[04.10.2026, 09：10] José： Hola", "04.10.2026, 09：10"),
        )
        for text, timestamp in cases:
            with self.subTest(text=text):
                message, = parse_chat(text)
                self.assertEqual((message.timestamp, message.sender, message.text), (timestamp, "José", "Hola"))

    def test_lines_end_only_at_cr_lf_cr_and_lf(self):
        # A line separator or a form feed typed into a message is part of its text, not the start of a line.
        first, second = parse_chat("01/10/26, 10:00 - Ana: one\u2028two\n01/10/26, 10:01 - Bob: x\x0cy")
        self.assertEqual((first.sender, first.text, first.end_line), ("Ana", "one\u2028two", 0))
        self.assertEqual((second.sender, second.text, second.end_line), ("Bob", "x\x0cy", 1))
        # So is every other character that str.splitlines() breaks at.
        for character in "\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029":
            with self.subTest(character=ascii(character)):
                message, = parse_chat(f"01/10/26, 10:00 - Ana: one{character}01/10/26, 10:01 - Bob: two")
                self.assertEqual((message.text, message.end_line), (f"one{character}01/10/26, 10:01 - Bob: two", 0))
        # Each of the three endings ends one line, and an ending that closes the text starts no further line.
        messages = parse_chat("01/10/26, 10:00 - Ana: one\r\ntwo\rthree\n\n01/10/26, 10:01 - Bob: four\r\n")
        self.assertEqual([(message.text, message.end_line) for message in messages], [("one\ntwo\nthree\n", 3), ("four", 4)])
        self.assertEqual(parse_chat(""), [])


if __name__ == "__main__":
    unittest.main()
