import unittest
from datetime import datetime

from whatsapp_archive.parser import parse_export


class ParseExportTests(unittest.TestCase):
    def test_parses_day_first_android_export_and_continuation(self):
        result = parse_export("""31.12.23, 22:15 - Ada: First line
continued
01.01.24, 00:05 - Bob: Happy New Year""")

        self.assertEqual(len(result.messages), 2)
        self.assertEqual(result.messages[0].timestamp, datetime(2023, 12, 31, 22, 15))
        self.assertEqual(result.messages[0].sender, "Ada")
        self.assertEqual(result.messages[0].text, "First line\ncontinued")
        self.assertEqual(result.messages[1].sender, "Bob")
        self.assertEqual(result.warnings, [])

    def test_parses_bracketed_month_first_ios_export(self):
        result = parse_export("[12/31/23, 10:25:30 PM] Ada: Hello", date_order="month-first")

        self.assertEqual(result.messages[0].timestamp, datetime(2023, 12, 31, 22, 25, 30))
        self.assertEqual(result.messages[0].text, "Hello")

    def test_keeps_system_messages_without_sender(self):
        result = parse_export("01/02/2024, 09:15 - Messages and calls are end-to-end encrypted.")

        self.assertIsNone(result.messages[0].sender)
        self.assertEqual(result.messages[0].text, "Messages and calls are end-to-end encrypted.")

    def test_reports_non_message_prefix_without_copying_its_content(self):
        result = parse_export("Export header\n31.12.23, 22:15 - Ada: Hello")

        self.assertEqual(result.warnings, ["Ignored 1 non-message line(s) before the first timestamp"])
        self.assertEqual(len(result.messages), 1)

    def test_empty_export_is_reported(self):
        result = parse_export("nothing timestamped")

        self.assertEqual(result.messages, [])
        self.assertEqual(result.warnings, ["Ignored 1 non-message line(s) before the first timestamp", "No timestamped messages were found"])


if __name__ == "__main__":
    unittest.main()