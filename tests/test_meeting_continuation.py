"""Keeping a recording alive while the meeting's tab is out of sight.

The 2026-09-08 standup ran in DuckDuckGo. Eight minutes in, with the user in
another window, the tab list came back empty for five polls; the watcher took
that as the meeting ending, cut the recording, transcribed it, and offered to
record "a new meeting" the moment the tabs were readable again. Tab detection
only says what the browser's window tree shows at that instant; a browser in a
call holds the microphone for the whole call. These pin the rule that the
second signal keeps a recording going, and only ever a recording.
"""
import unittest
from unittest import mock

import meeting_recorder as m

DDG = "DuckDuckGo: Meet – Standup 🙋🏻‍♂️"
DDG_ON_MIC = [(2508, "com.apple.WebKit.GPU", "DuckDuckGo")]


class MeetingSourceAppTests(unittest.TestCase):
    def test_tab_reasons_name_their_browser(self):
        self.assertEqual(m.meeting_source_app(DDG), "DuckDuckGo")
        self.assertEqual(m.meeting_source_app("Safari: https://meet.google.com/abc"), "Safari")

    def test_process_and_mic_reasons_have_no_browser(self):
        for reason in ("Zoom", "Microsoft Teams", "Slack Huddle", "manual-meeting"):
            with self.subTest(reason=reason):
                self.assertIsNone(m.meeting_source_app(reason))

    def test_a_title_with_a_colon_does_not_invent_a_browser(self):
        self.assertIsNone(m.meeting_source_app("Notes: Meet – Standup"))


class MeetingAppHoldsMicTests(unittest.TestCase):
    def test_browser_helper_on_the_mic_counts_for_its_browser(self):
        """WebKit holds the mic from a GPU helper whose parent is launchd; the
        owning-app column is the only thing tying it to DuckDuckGo."""
        with mock.patch.object(m, "mic_input_holders", return_value=DDG_ON_MIC):
            self.assertTrue(m.meeting_app_holds_mic(DDG))

    def test_another_browser_on_the_mic_does_not_count(self):
        with mock.patch.object(m, "mic_input_holders", return_value=DDG_ON_MIC):
            self.assertFalse(m.meeting_app_holds_mic("Safari: https://meet.google.com/abc"))

    def test_nothing_on_the_mic_means_the_call_is_over(self):
        with mock.patch.object(m, "mic_input_holders", return_value=[]):
            self.assertFalse(m.meeting_app_holds_mic(DDG))

    def test_non_browser_reasons_never_use_the_mic_rule(self):
        with mock.patch.object(m, "mic_input_holders", return_value=[(1, "zoom.us", "zoom.us")]):
            self.assertFalse(m.meeting_app_holds_mic("Zoom"))

    def test_respects_the_mic_detect_switch(self):
        with mock.patch.object(m, "MIC_DETECT", False), \
             mock.patch.object(m, "mic_input_holders", return_value=DDG_ON_MIC) as holders:
            self.assertFalse(m.meeting_app_holds_mic(DDG))
            holders.assert_not_called()


class MeetingStillOnTests(unittest.TestCase):
    def test_a_visible_tab_is_detected(self):
        with mock.patch.object(m, "detect_meeting", return_value=DDG), \
             mock.patch.object(m, "mic_input_holders", return_value=[]):
            self.assertEqual(m.meeting_still_on(DDG), "detected")

    def test_tab_out_of_sight_but_browser_on_the_call_keeps_going(self):
        with mock.patch.object(m, "detect_meeting", return_value=None), \
             mock.patch.object(m, "mic_input_holders", return_value=DDG_ON_MIC):
            self.assertEqual(m.meeting_still_on(DDG), "DuckDuckGo holds the mic")

    def test_tab_gone_and_mic_released_is_over(self):
        with mock.patch.object(m, "detect_meeting", return_value=None), \
             mock.patch.object(m, "mic_input_holders", return_value=[]):
            self.assertIsNone(m.meeting_still_on(DDG))

    def test_the_mic_rule_never_starts_a_recording(self):
        """Only the recording loop consults it; detect_meeting() stays browser-blind."""
        with mock.patch.object(m, "browser_tabs", return_value=[]), \
             mock.patch.object(m, "active_processes", return_value=[]), \
             mock.patch.object(m, "mic_input_holders", return_value=DDG_ON_MIC):
            self.assertIsNone(m.detect_meeting())


if __name__ == "__main__":
    unittest.main()
