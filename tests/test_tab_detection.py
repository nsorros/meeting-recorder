"""Naming a recording after the meeting tab, not after whatever tab is first.

The 2026-08-19 Standup recorded as
`..._https-arxiv.org-pdf-2608.06370https-www.ft.com-content-418e0159-...`:
the AppleScript returned `rows as text`, which joins with AppleScript's text
item delimiters (default ""), so every tab arrived glued into one string and
the parser handed back the first tab's URL with every later tab's title stuck
to it. These tests pin the row separator and the tab that gets to name a call.
"""
import unittest
from unittest import mock

import meeting_recorder as m


def row(url: str, title: str) -> str:
    return f"{url}{m.TAB_FIELD_SEP}{title}{m.TAB_ROW_SEP}"


class ParseTabRowsTests(unittest.TestCase):
    def test_each_tab_is_its_own_row(self):
        out = row("https://arxiv.org/pdf/2608.06370", "") + row(
            "https://meet.google.com/hfb-zgpr-oqc", "Meet – Standup"
        )
        self.assertEqual(
            m.parse_tab_rows("Safari", out),
            [
                ("Safari", "https://arxiv.org/pdf/2608.06370", ""),
                ("Safari", "https://meet.google.com/hfb-zgpr-oqc", "Meet – Standup"),
            ],
        )

    def test_commas_in_titles_do_not_split_a_tab(self):
        """The old parser split on commas, so any comma cut a tab in half."""
        out = row("https://example.com/a", "Roadmap, Q3, and budget")
        self.assertEqual(
            m.parse_tab_rows("Safari", out),
            [("Safari", "https://example.com/a", "Roadmap, Q3, and budget")],
        )

    def test_no_tabs_is_no_rows(self):
        """A browser that is not running returns empty output, not a bad row."""
        self.assertEqual(m.parse_tab_rows("Safari", ""), [])


class DetectMeetingTabTests(unittest.TestCase):
    def setUp(self):
        self.procs = mock.patch.object(m, "active_processes", return_value=["Finder"])
        self.mic = mock.patch.object(m, "mic_input_holders", return_value=[])
        self.procs.start()
        self.mic.start()
        self.addCleanup(self.procs.stop)
        self.addCleanup(self.mic.stop)

    def test_meeting_tab_names_the_recording_not_the_first_tab(self):
        tabs = [
            ("Safari", "https://arxiv.org/pdf/2608.06370", ""),
            ("Safari", "https://www.ft.com/content/418e0159", "UK examines economic hit"),
            ("Safari", "https://meet.google.com/hfb-zgpr-oqc", "Meet – Standup"),
        ]
        with mock.patch.object(m, "browser_tabs", return_value=tabs):
            self.assertEqual(m.detect_meeting(), "Safari: Meet – Standup")

    def test_live_call_beats_an_earlier_weak_title_match(self):
        """A tab merely mentioning "Google Meet" must not outrank the call itself."""
        tabs = [
            ("Safari", "https://support.google.com/a/answer/9282720", "Set up Google Meet"),
            ("Safari", "https://meet.google.com/hfb-zgpr-oqc", "Meet – Standup"),
        ]
        with mock.patch.object(m, "browser_tabs", return_value=tabs):
            self.assertEqual(m.detect_meeting(), "Safari: Meet – Standup")

    def test_reading_tabs_is_not_a_meeting(self):
        tabs = [
            ("Safari", "https://arxiv.org/pdf/2608.06370", ""),
            ("Safari", "https://www.ft.com/content/418e0159", "UK examines economic hit"),
        ]
        with mock.patch.object(m, "browser_tabs", return_value=tabs):
            self.assertIsNone(m.detect_meeting())


if __name__ == "__main__":
    unittest.main()
