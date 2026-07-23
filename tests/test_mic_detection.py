"""Detection wiring for mic-based (Slack huddle) meeting detection.

The CoreAudio probe itself is verified by running it (see `mrec mic-probe`);
these tests cover the decisions made around it, which are the parts that can
silently go wrong: the allowlist, precedence against the existing detectors,
and the self-exclusion that stops the recorder detecting its own capture.
"""
import unittest
from unittest import mock

import meeting_recorder as m


class DetectMeetingMicTests(unittest.TestCase):
    def setUp(self):
        # No browser tabs, no meeting processes: isolate the mic path.
        self.tabs = mock.patch.object(m, "browser_tabs", return_value=[])
        self.procs = mock.patch.object(m, "active_processes", return_value=["Finder", "Slack"])
        self.tabs.start()
        self.procs.start()
        self.addCleanup(self.tabs.stop)
        self.addCleanup(self.procs.stop)

    def test_slack_holding_mic_is_a_huddle(self):
        with mock.patch.object(m, "mic_input_holders", return_value=[(42, "Slack")]):
            self.assertEqual(m.detect_meeting(), "Slack Huddle")

    def test_slack_merely_running_is_not_a_meeting(self):
        """The whole reason for the mic signal: Slack is always in `ps`."""
        with mock.patch.object(m, "mic_input_holders", return_value=[]):
            self.assertIsNone(m.detect_meeting())

    def test_unwatched_mic_user_is_ignored(self):
        """Dictation or Voice Memos must not start a recording."""
        with mock.patch.object(m, "mic_input_holders", return_value=[(7, "VoiceMemos")]):
            self.assertIsNone(m.detect_meeting())

    def test_browser_tab_wins_over_mic(self):
        """A Meet tab should keep its own label even if Slack holds the mic."""
        with mock.patch.object(m, "browser_tabs",
                               return_value=[("Safari", "https://meet.google.com/abc", "Standup")]), \
             mock.patch.object(m, "mic_input_holders", return_value=[(42, "Slack")]):
            self.assertIn("Standup", m.detect_meeting())

    def test_mic_detection_can_be_disabled(self):
        with mock.patch.object(m, "MIC_DETECT", False), \
             mock.patch.object(m, "mic_input_holders", return_value=[(42, "Slack")]) as holders:
            self.assertIsNone(m.detect_meeting())
            holders.assert_not_called()


class MicInputHoldersTests(unittest.TestCase):
    """mic_input_holders() parses probe output and drops our own capture."""

    def _run_with_probe_output(self, stdout: str, returncode: int = 0):
        result = mock.Mock(returncode=returncode, stdout=stdout, stderr="")
        with mock.patch.object(m, "ensure_mic_probe_built", return_value="/fake/mic-probe"), \
             mock.patch.object(m, "run", return_value=result):
            return m.mic_input_holders(use_cache=False)

    def test_parses_pid_and_name(self):
        self.assertEqual(self._run_with_probe_output("42\tSlack\n"), [(42, "Slack")])

    def test_excludes_own_capture(self):
        """Without this the recorder sees itself and never stops recording."""
        out = "42\tSlack\n99\tffmpeg\n100\tsck-recorder\n729\treplayd\n"
        self.assertEqual(self._run_with_probe_output(out), [(42, "Slack")])

    def test_probe_failure_is_not_fatal(self):
        """Mic detection is additive; losing it must not lose other meetings."""
        self.assertEqual(self._run_with_probe_output("", returncode=1), [])

    def test_malformed_lines_are_skipped(self):
        self.assertEqual(self._run_with_probe_output("junk\n\n42\tSlack\nxx\tBad\n"), [(42, "Slack")])


if __name__ == "__main__":
    unittest.main()
