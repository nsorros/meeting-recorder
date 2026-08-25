"""The capture intermediates, and the rule that they never outlive a capture.

ScreenCaptureKit writes `<stem>.system.wav` and `<stem>.mic.wav` on the way to the
single `<stem>.wav` the pipeline expects. A clean run mixes and deletes them, and for
months that was the only run under test — so the three ways a capture can fail all
returned before the delete, and one declined Screen Recording prompt left an 11 KB
`.system.wav` on disk.

That leftover is not clutter. Its stem parses as a recording of its own, so the
backfill minted it a calendar sidecar and the meetings app drew it as a fourth
attendee-carrying copy of a meeting that happened once. These tests pin both halves:
the intermediates are recognised by name, and every path out of a capture removes them.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import meeting_recorder as m


class IsCapturePartTests(unittest.TestCase):
    def test_recognises_both_intermediates(self):
        self.assertTrue(m.is_capture_part(Path("2026-08-19_18-01-46_demo.system.wav")))
        self.assertTrue(m.is_capture_part(Path("2026-08-19_18-01-46_demo.mic.wav")))

    def test_a_real_recording_is_not_a_part(self):
        self.assertFalse(m.is_capture_part(Path("2026-08-19_18-06-51_demo.wav")))

    def test_a_dot_in_the_slug_is_not_a_part(self):
        """`finant.ai-Alex-Loizou` is a real slug in the archive, and `Path.suffix`
        will happily call `.ai-Alex-Loizou` an extension. Only the two known
        intermediate names count, which is why this matches a list and not a regex."""
        audio = Path("2026-08-19_10-32-13_finant.ai-Alex-Loizou-fundraising-chat.wav")
        self.assertFalse(m.is_capture_part(audio))

    def test_a_capture_part_is_never_looked_up_in_the_calendar(self):
        """`event_for_recording` writes a hit back to disk, so a lookup here is how a
        `.system.event.json` gets minted. It must not get as far as the calendar."""
        with mock.patch.object(m, "read_event_sidecar", return_value=None), \
             mock.patch.object(m, "best_event_at") as ask:
            got = m.event_for_recording(Path("2026-08-19_18-01-46_demo.system.wav"))
        self.assertIsNone(got)
        ask.assert_not_called()


class MixCapturePartsTests(unittest.TestCase):
    """Every exit from `_mix_capture_parts` leaves only `final` behind."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.system_wav = root / "2026-08-19_18-01-46_demo.system.wav"
        self.mic_wav = root / "2026-08-19_18-01-46_demo.mic.wav"
        self.final = root / "2026-08-19_18-01-46_demo.wav"

    def _write(self, path: Path, size: int) -> None:
        path.write_bytes(b"\0" * size)

    def assert_parts_gone(self):
        self.assertFalse(self.system_wav.exists(), "system intermediate outlived the mix")
        self.assertFalse(self.mic_wav.exists(), "mic intermediate outlived the mix")

    def test_both_empty_still_cleans_up(self):
        """The declined-permission shape: a header-only stub under the 1 KB floor.
        It counts as 'no audio', which used to mean 'return before the delete'."""
        self._write(self.system_wav, 44)
        self._write(self.mic_wav, 44)
        with mock.patch.object(m, "run") as run:
            m._mix_capture_parts([self.system_wav, self.mic_wav], self.final)
        run.assert_not_called()
        self.assert_parts_gone()

    def test_a_successful_mix_cleans_up(self):
        self._write(self.system_wav, 4096)
        self._write(self.mic_wav, 4096)
        self.final.write_bytes(b"\0" * 8192)  # ffmpeg is mocked; stand in for its output
        with mock.patch.object(m, "run", return_value=mock.Mock(returncode=0)):
            m._mix_capture_parts([self.system_wav, self.mic_wav], self.final)
        self.assert_parts_gone()
        self.assertTrue(self.final.exists())

    def test_a_failed_mix_rescues_the_far_side_then_cleans_up(self):
        self._write(self.system_wav, 4096)
        self._write(self.mic_wav, 2048)
        failed = mock.Mock(returncode=1, stderr="amix exploded", stdout="")
        with mock.patch.object(m, "run", return_value=failed):
            m._mix_capture_parts([self.system_wav, self.mic_wav], self.final)
        self.assert_parts_gone()
        self.assertEqual(self.final.stat().st_size, 4096, "far-side audio was not rescued")

    def test_a_failed_mix_rescues_the_mic_when_that_is_all_there_was(self):
        """Nobody came through on the far side, and the mix failed anyway. Half a
        meeting beats none, and the cleanup would otherwise take the only copy."""
        self._write(self.system_wav, 44)
        self._write(self.mic_wav, 2048)
        failed = mock.Mock(returncode=1, stderr="", stdout="transcode failed")
        with mock.patch.object(m, "run", return_value=failed):
            m._mix_capture_parts([self.system_wav, self.mic_wav], self.final)
        self.assert_parts_gone()
        self.assertEqual(self.final.stat().st_size, 2048, "mic audio was not rescued")


class StartScreenCaptureKitCleanupTests(unittest.TestCase):
    """The path that actually bit: the helper dies inside the first three seconds."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "2026-08-19_18-01-46_demo.wav"

    def test_an_early_exit_clears_the_parts_but_keeps_the_log(self):
        stem = self.path.with_suffix("").name
        system_wav = self.path.with_name(stem + ".system.wav")
        mic_wav = self.path.with_name(stem + ".mic.wav")
        sck_log = self.path.with_name(stem + ".sck.log")

        def popen(cmd, **kwargs):
            # What the helper does before giving up: opens its output and writes a
            # header, then exits 4 because the user declined the TCC prompt.
            system_wav.write_bytes(b"\0" * 44)
            kwargs["stderr"].write("sck-recorder: startCapture failed: The user "
                                   "declined TCCs for application, window, display "
                                   "capture\n")
            return mock.Mock(poll=mock.Mock(return_value=4), returncode=4)

        with mock.patch.object(m, "ensure_recorder_built", return_value=Path("/bin/true")), \
             mock.patch.object(m.sys, "platform", "darwin"), \
             mock.patch.object(m.time, "sleep"), \
             mock.patch.object(m.subprocess, "Popen", side_effect=popen), \
             mock.patch.object(m, "SCK_NO_MIC", False):
            proc = m._start_screencapturekit("finant/Springcoast demo", self.path)

        self.assertIsNone(proc, "a failed start must fall back, not return a process")
        self.assertFalse(system_wav.exists(), "the orphan this whole fix is about")
        self.assertFalse(mic_wav.exists())
        self.assertTrue(sck_log.exists(), "the log is the only record of why it failed")


if __name__ == "__main__":
    unittest.main()
