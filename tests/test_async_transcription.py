"""Transcription runs detached from the watcher.

The failure this guards against is silent and expensive: transcription used to run
inline in the watch loop, so the recorder was deaf for its whole duration (76
minutes on 2026-08-12, when an empty OpenRouter balance dropped a 22-minute
meeting onto local Whisper) and a meeting starting in that window was never
recorded at all. These cover the split itself, the fallback that must never skip
a recording, and the queue that keeps two Whisper runs off the CPU at once.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import meeting_recorder as m


class _JobStateTestCase(unittest.TestCase):
    """Redirect the job-state directory at a temp dir for each test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jobs_dir = Path(tmp.name) / "transcribe-jobs"
        patch = mock.patch.object(m, "TRANSCRIBE_JOBS_DIR", self.jobs_dir)
        patch.start()
        self.addCleanup(patch.stop)
        # Per-process memo of "when did my job start"; stale entries would leak
        # a `since` across tests.
        m._transcribe_job_state.clear()
        self.addCleanup(m._transcribe_job_state.clear)
        quiet = mock.patch.object(m, "swiftbar_refresh")
        quiet.start()
        self.addCleanup(quiet.stop)
        # Liveness is decided per test; the default is "these pids are ordinary
        # live processes" so no test shells out to ps.
        live = mock.patch.object(m, "process_is_zombie", return_value=False)
        live.start()
        self.addCleanup(live.stop)

    def write_job(self, pid: int, state: str, meeting: str = "other", since: int = 0) -> None:
        """Fabricate another process's job file."""
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        (self.jobs_dir / str(pid)).write_text(
            f"pid={pid}\nstate={state}\naudio=/tmp/{meeting}.wav\n"
            f"meeting={meeting}\nsince={since}\n",
            encoding="utf-8",
        )


class WatcherDoesNotBlockTests(_JobStateTestCase):
    def setUp(self):
        super().setUp()
        self.audio = Path(tempfile.mkdtemp()) / "meeting.wav"
        self.audio.write_bytes(b"x" * 2048)

    def test_recording_is_handed_to_a_background_process(self):
        """The whole point: the caller returns without transcribing anything."""
        with mock.patch.object(m, "spawn_transcribe_job", return_value=True) as spawn, \
             mock.patch.object(m, "transcribe_audio") as transcribe, \
             mock.patch.object(m, "notify"):
            m.transcribe_recording(self.audio, label="Meeting ended.")
        spawn.assert_called_once_with(self.audio)
        transcribe.assert_not_called()

    def test_falls_back_to_inline_when_the_job_cannot_start(self):
        """A recording that can't be handed off must still be transcribed, not lost."""
        with mock.patch.object(m, "spawn_transcribe_job", return_value=False), \
             mock.patch.object(m, "transcribe_audio", return_value=Path("/tmp/x.meeting.md")) as transcribe, \
             mock.patch.object(m, "transcript_ready_dialog"), \
             mock.patch.object(m, "notify"):
            m.transcribe_recording(self.audio, label="Meeting ended.")
        transcribe.assert_called_once_with(self.audio)

    def test_async_can_be_switched_off(self):
        with mock.patch.object(m, "ASYNC_TRANSCRIBE", False), \
             mock.patch.object(m, "spawn_transcribe_job") as spawn, \
             mock.patch.object(m, "transcribe_audio", return_value=Path("/tmp/x.meeting.md")), \
             mock.patch.object(m, "transcript_ready_dialog"), \
             mock.patch.object(m, "notify"):
            m.transcribe_recording(self.audio, label="Meeting ended.")
        spawn.assert_not_called()

    def test_inline_fallback_does_not_renice_the_caller(self):
        """os.nice() is irreversible: nicing here would slow the watcher forever."""
        with mock.patch.object(m, "transcribe_audio", return_value=Path("/tmp/x.meeting.md")), \
             mock.patch.object(m, "transcript_ready_dialog"), \
             mock.patch.object(os, "nice") as nice:
            m.run_transcribe_job(self.audio, detached=False)
        nice.assert_not_called()

    def test_tiny_audio_is_not_transcribed(self):
        empty = self.audio.with_name("empty.wav")
        empty.write_bytes(b"x")
        with mock.patch.object(m, "spawn_transcribe_job") as spawn, \
             mock.patch.object(m, "transcribe_audio") as transcribe:
            m.transcribe_recording(empty, label="Meeting ended.")
        spawn.assert_not_called()
        transcribe.assert_not_called()


class TranscribeJobStateTests(_JobStateTestCase):
    def test_dead_jobs_are_pruned(self):
        """A job killed mid-run would otherwise wedge the queue and the menu bar."""
        self.write_job(pid=999999, state="transcribing")
        with mock.patch.object(m, "pid_is_running", return_value=False):
            self.assertEqual(m.transcribe_jobs(), [])
        self.assertFalse((self.jobs_dir / "999999").exists())

    def test_a_second_job_in_the_same_process_publishes_again(self):
        """The inline fallback runs inside the long-lived watcher, so the per-process
        memo has to reset on clear or the next transcription publishes nothing."""
        audio = Path("/tmp/meeting.wav")
        m.write_transcribe_job(audio, "transcribing")
        m.clear_transcribe_job()
        m.write_transcribe_job(audio, "transcribing")
        self.assertTrue((self.jobs_dir / str(os.getpid())).exists())

    def test_a_zombie_job_counts_as_dead(self):
        """kill(pid, 0) succeeds for an exited-but-unreaped job. Trusting it would
        leave the queue shut and the menu bar stuck on "Transcribing…" forever."""
        self.write_job(pid=4242, state="transcribing")
        with mock.patch.object(m, "pid_is_running", return_value=True), \
             mock.patch.object(m, "process_is_zombie", return_value=True):
            self.assertEqual(m.transcribe_jobs(), [])
        self.assertFalse((self.jobs_dir / "4242").exists())

    def test_start_time_survives_a_state_change(self):
        """The queue orders by `since`; a moving timestamp would reshuffle it."""
        audio = Path("/tmp/meeting.wav")
        m.write_transcribe_job(audio, "queued")
        first = (self.jobs_dir / str(os.getpid())).read_text()
        m.write_transcribe_job(audio, "transcribing")
        second = (self.jobs_dir / str(os.getpid())).read_text()
        since = [line for line in first.splitlines() if line.startswith("since=")]
        self.assertEqual(since, [line for line in second.splitlines() if line.startswith("since=")])
        self.assertIn("state=transcribing", second)


class TranscribeQueueTests(_JobStateTestCase):
    def setUp(self):
        super().setUp()
        self.audio = Path("/tmp/mine.wav")
        alive = mock.patch.object(m, "pid_is_running", return_value=True)
        alive.start()
        self.addCleanup(alive.stop)

    def test_first_job_starts_immediately(self):
        with mock.patch.object(m, "time") as clock:
            clock.time.return_value = 1000.0
            self.assertTrue(m._claim_transcribe_slot(self.audio))
        self.assertIn("state=transcribing", (self.jobs_dir / str(os.getpid())).read_text())

    def test_waits_while_another_job_transcribes(self):
        """Two local Whisper runs at once finish later than the same two in sequence."""
        self.write_job(pid=4242, state="transcribing", meeting="earlier")
        m.write_transcribe_job(self.audio, "queued")
        with mock.patch.object(m, "time") as clock:
            # Straight past the deadline: one look, then give up.
            clock.time.side_effect = [0.0, 10 ** 9]
            self.assertFalse(m._claim_transcribe_slot(self.audio))
        self.assertIn("state=queued", (self.jobs_dir / str(os.getpid())).read_text())

    def test_older_queued_job_goes_first(self):
        """Ordering is oldest-first, and every waiter must agree on the winner."""
        self.write_job(pid=4242, state="queued", meeting="earlier", since=1)
        m._transcribe_job_state["since"] = 500
        m.write_transcribe_job(self.audio, "queued")
        with mock.patch.object(m, "time") as clock:
            clock.time.side_effect = [0.0, 10 ** 9]
            self.assertFalse(m._claim_transcribe_slot(self.audio))

    def test_concurrency_can_be_raised(self):
        """Network-bound OpenRouter transcriptions have no reason to queue."""
        self.write_job(pid=4242, state="transcribing", meeting="earlier")
        m.write_transcribe_job(self.audio, "queued")
        with mock.patch.object(m, "TRANSCRIBE_CONCURRENCY", 2), \
             mock.patch.object(m, "time") as clock:
            clock.time.return_value = 1000.0
            self.assertTrue(m._claim_transcribe_slot(self.audio))


if __name__ == "__main__":
    unittest.main()
