"""Detecting a call in a browser that has no AppleScript dictionary.

The 2026-08-19 Springcoast demo ran in DuckDuckGo and was never detected: DDG
ships no scripting dictionary, so `browser_tabs()` — which only knows how to ask
Safari and the Chrome family for their tabs — saw nothing, and the recording had
to be started by hand. These tests pin the accessibility fallback that reads
DDG's tab strip instead, and the dash normalisation the title-only path needs.
"""
import unittest
from unittest import mock

import meeting_recorder as m


class ParseAxTabRowsTests(unittest.TestCase):
    def test_each_tab_is_its_own_row_with_no_url(self):
        out = m.TAB_ROW_SEP.join(["Home", "Meet – finant/Springcoast demo"]) + m.TAB_ROW_SEP
        self.assertEqual(
            m.parse_ax_tab_rows("DuckDuckGo", out),
            [("DuckDuckGo", "", "Home"), ("DuckDuckGo", "", "Meet – finant/Springcoast demo")],
        )

    def test_empty_output_is_no_tabs(self):
        self.assertEqual(m.parse_ax_tab_rows("DuckDuckGo", ""), [])


class AxBrowserTabsTests(unittest.TestCase):
    def setUp(self):
        m._AX_DENIED_LOGGED.clear()
        self.addCleanup(m._AX_DENIED_LOGGED.clear)

    def test_accessibility_denial_is_logged_once_not_every_poll(self):
        """The watcher polls every few seconds; a missing grant must not fill the log."""
        error = RuntimeError('System Events got an error: ... (-1719)')
        with mock.patch.object(m, "osascript", side_effect=error), \
             mock.patch.object(m, "log") as logged:
            for _ in range(3):
                self.assertEqual(m.ax_browser_tabs("DuckDuckGo"), [])
        self.assertEqual(logged.call_count, 1)
        self.assertIn("Accessibility", logged.call_args[0][0])


class DetectMeetingTests(unittest.TestCase):
    def setUp(self):
        self.procs = mock.patch.object(m, "active_processes", return_value=[])
        self.mic = mock.patch.object(m, "mic_input_holders", return_value=[])
        self.procs.start()
        self.mic.start()
        self.addCleanup(self.procs.stop)
        self.addCleanup(self.mic.stop)

    def test_call_in_a_background_tab_is_detected(self):
        """DDG's window title names only the focused tab; the tab strip has them all."""
        tabs = [
            ("DuckDuckGo", "", "Home"),
            ("DuckDuckGo", "", "Meet – finant/Springcoast demo"),
            ("DuckDuckGo", "", "Nick Sorros CV - Google Docs"),
        ]
        with mock.patch.object(m, "browser_tabs", return_value=tabs):
            self.assertEqual(m.detect_meeting(), "DuckDuckGo: Meet – finant/Springcoast demo")

    def test_en_dash_title_matches_a_hyphen_hint(self):
        """Every browser writes "Meet – X" with an en dash; the hint list uses "-"."""
        for title in ("Meet – Standup", "Meet — Standup", "Meet - Standup"):
            with self.subTest(title=title), \
                 mock.patch.object(m, "browser_tabs", return_value=[("DuckDuckGo", "", title)]):
                self.assertEqual(m.detect_meeting(), f"DuckDuckGo: {title}")

    def test_reading_tabs_in_duckduckgo_is_not_a_meeting(self):
        tabs = [
            ("DuckDuckGo", "", "AI in search — embeddings is not a strategy — Nick Sorros"),
            ("DuckDuckGo", "", "Here is how R1 is trained — Nick Sorros"),
        ]
        with mock.patch.object(m, "browser_tabs", return_value=tabs):
            self.assertIsNone(m.detect_meeting())


if __name__ == "__main__":
    unittest.main()
