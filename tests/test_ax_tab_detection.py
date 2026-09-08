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
        out = "1" + m.TAB_ROW_SEP + m.TAB_ROW_SEP.join(["Home", "Meet – finant/Springcoast demo"])
        self.assertEqual(
            m.parse_ax_tab_rows("DuckDuckGo", out),
            [("DuckDuckGo", "", "Home"), ("DuckDuckGo", "", "Meet – finant/Springcoast demo")],
        )

    def test_empty_output_is_no_tabs(self):
        self.assertEqual(m.parse_ax_tab_rows("DuckDuckGo", ""), [])

    def test_window_count_is_separated_from_titles(self):
        """"0 windows" and "windows but no meeting tab" are different findings."""
        self.assertEqual(m.parse_ax_output(""), (None, []))
        self.assertEqual(m.parse_ax_output("0"), (0, []))
        self.assertEqual(m.parse_ax_output("2" + m.TAB_ROW_SEP + "Home"), (2, ["Home"]))

    def test_a_tab_titled_like_a_number_is_still_a_tab(self):
        self.assertEqual(m.parse_ax_output("1" + m.TAB_ROW_SEP + "42"), (1, ["42"]))


class AxBrowserTabsTests(unittest.TestCase):
    def setUp(self):
        m._AX_DENIED_LOGGED.clear()
        m._AX_ERROR_LOGGED_AT.clear()
        m._AX_LAST.clear()
        self.addCleanup(m._AX_DENIED_LOGGED.clear)
        self.addCleanup(m._AX_ERROR_LOGGED_AT.clear)
        self.addCleanup(m._AX_LAST.clear)

    def test_accessibility_denial_is_logged_once_not_every_poll(self):
        """The watcher polls every few seconds; a missing grant must not fill the log."""
        error = RuntimeError('System Events got an error: osascript is not allowed assistive access. (-25211)')
        with mock.patch.object(m, "osascript", side_effect=error), \
             mock.patch.object(m, "log") as logged:
            for _ in range(3):
                self.assertEqual(m.ax_browser_tabs("DuckDuckGo"), [])
        self.assertEqual(logged.call_count, 1)
        self.assertIn("Accessibility", logged.call_args[0][0])

    def test_invalid_index_is_a_read_failure_not_a_denial(self):
        """-1719 is what a window changing under the walk raises. Calling it a
        denial logged it once and then hid every later failure for the life of
        the daemon — the 2026-09-08 standup was cut mid-call with nothing in the
        log to say why."""
        error = RuntimeError("System Events got an error: Can’t get item 2 of every window. Invalid index. (-1719)")
        with mock.patch.object(m, "osascript", side_effect=error), \
             mock.patch.object(m, "log") as logged:
            self.assertEqual(m.ax_browser_tabs("DuckDuckGo"), [])
        self.assertEqual(logged.call_count, 1)
        self.assertIn("could not inspect DuckDuckGo", logged.call_args[0][0])
        self.assertIn("-1719", logged.call_args[0][0])
        self.assertIn("-1719", m.ax_last_summary("DuckDuckGo"))

    def test_read_failures_are_throttled_but_not_silenced(self):
        error = RuntimeError("Invalid index. (-1719)")
        clock = [1000.0]
        with mock.patch.object(m, "osascript", side_effect=error), \
             mock.patch.object(m.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(m, "log") as logged:
            for _ in range(5):
                m.ax_browser_tabs("DuckDuckGo")
            self.assertEqual(logged.call_count, 1)
            clock[0] += m._AX_ERROR_LOG_SECONDS
            m.ax_browser_tabs("DuckDuckGo")
            self.assertEqual(logged.call_count, 2)

    def test_last_read_is_summarised_for_the_log(self):
        with mock.patch.object(m, "osascript", return_value="2" + m.TAB_ROW_SEP + "Home"):
            m.ax_browser_tabs("DuckDuckGo")
        self.assertEqual(m.ax_last_summary("DuckDuckGo"), "2 windows, 1 tab")
        with mock.patch.object(m, "osascript", return_value=""):
            m.ax_browser_tabs("DuckDuckGo")
        self.assertEqual(m.ax_last_summary("DuckDuckGo"), "not running")

    def test_every_window_is_walked_inside_its_own_try(self):
        """One window raising mid-walk must not hide the tabs of the others."""
        script = m.ax_tab_script("DuckDuckGo")
        self.assertIn("repeat with w in ws", script)
        body = script.split("repeat with w in ws", 1)[1]
        self.assertRegex(body, r"set windowCount to windowCount \+ 1\s+try\s+repeat with a in")
        self.assertIn("set ws to windows", script)
        self.assertNotIn("set rows to", script)  # `row` is a System Events class


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
