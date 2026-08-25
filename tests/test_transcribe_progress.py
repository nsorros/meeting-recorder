"""Live progress for a running transcription, and which engine is running it.

An hour of audio takes local Whisper well over an hour on this machine, and the
menu bar used to say only "Transcribing…" for all of it — no way to tell a slow
job from a wedged one. Worse, the "Transcribes with:" line reported the *planned*
engine, so a job that fell back from OpenRouter to local Whisper mid-run (a DNS
failure did exactly that on 2026-08-25) left the menu confidently naming an
engine that was not running. These cover the progress signal each engine gives,
the throttling that keeps it from thrashing the menu bar, and the rendering.
"""
import importlib.util
import io
import itertools
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import meeting_recorder as m

_PLUGIN = Path(__file__).resolve().parent.parent / "menu-bar" / "meeting-recorder.1m.py"


def load_plugin():
    """Import the xbar plugin, whose filename is not a legal module name."""
    spec = importlib.util.spec_from_file_location("menubar_plugin", _PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WhisperSegmentParsingTests(unittest.TestCase):
    """Whisper's verbose output is the only progress the CLI offers."""

    def test_minutes_and_seconds(self):
        self.assertAlmostEqual(
            m.whisper_segment_end("[00:12.000 --> 00:15.480]  some words"), 15.48)

    def test_hours_appear_once_there_are_any(self):
        """format_timestamp only emits the hour field when it is non-zero, so both
        shapes turn up in the same log the moment a meeting passes an hour."""
        self.assertAlmostEqual(
            m.whisper_segment_end("[01:02:03.500 --> 01:04:05.250]  later words"), 3845.25)

    def test_other_output_is_not_progress(self):
        for line in ("Detecting language using up to the first 30 seconds",
                     "Detected language: English", ""):
            self.assertIsNone(m.whisper_segment_end(line))


class _JobTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jobs_dir = Path(tmp.name) / "transcribe-jobs"
        patch = mock.patch.object(m, "TRANSCRIBE_JOBS_DIR", self.jobs_dir)
        patch.start()
        self.addCleanup(patch.stop)
        m._transcribe_job_state.clear()
        self.addCleanup(m._transcribe_job_state.clear)
        quiet = mock.patch.object(m, "swiftbar_refresh")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.audio = Path("/tmp/2026-08-25_15-02-44_Standup.wav")

    def published(self) -> dict[str, str]:
        text = (self.jobs_dir / str(os.getpid())).read_text()
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


class ProgressPublishingTests(_JobTestCase):
    def test_progress_reaches_the_job_file(self):
        m.write_transcribe_job(self.audio, "transcribing")
        m.write_transcribe_engine("whisper", "turbo")
        # First report anchors the rate; the second, 600s of wall clock later, has
        # covered another 600s of audio — real time keeping pace with audio time.
        with mock.patch.object(m.time, "time", side_effect=[100.0, 700.0]):
            m.write_transcribe_progress(600.0, 3600.0)
            m.write_transcribe_progress(1200.0, 3600.0)
        job = self.published()
        self.assertEqual(job["percent"], "33")
        self.assertEqual(job["eta"], "2400")

    def test_the_first_update_has_a_percentage_even_with_no_eta_yet(self):
        """One data point is not a rate. The percentage is still worth showing."""
        with mock.patch.object(m.time, "time", return_value=100.0):
            m.write_transcribe_job(self.audio, "transcribing")
            m.write_transcribe_engine("whisper", "turbo")
            m.write_transcribe_progress(600.0, 3600.0)
        job = self.published()
        self.assertEqual(job["percent"], "16")
        self.assertNotIn("eta", job)

    def test_model_loading_time_is_left_out_of_the_eta(self):
        """Whisper decodes nothing for its first ~45s. Counting that as transcription
        made a 45-second clip's first ETA read twelve minutes."""
        m.write_transcribe_job(self.audio, "transcribing")
        with mock.patch.object(m.time, "time", side_effect=[0.0, 100.0, 200.0]):
            m.write_transcribe_engine("whisper", "turbo")   # t=0: model starts loading
            m.write_transcribe_progress(30.0, 3600.0)       # t=100: first segment out
            m.write_transcribe_progress(130.0, 3600.0)      # t=200: 100s of audio in 100s
        # Real time from here on, so ~3470s left — not the ~7000s a rate that
        # included the load would predict.
        self.assertEqual(self.published()["eta"], "3470")

    def test_progress_never_reads_100(self):
        """Whisper hits the last segment well before it writes its output files;
        a menu bar parked at 100% for minutes reads as a hung job."""
        m.write_transcribe_job(self.audio, "transcribing")
        m.write_transcribe_progress(3600.0, 3600.0)
        self.assertEqual(self.published()["percent"], "99")

    def test_a_flood_of_updates_is_throttled(self):
        """Short audio moves a percent every fraction of a second, and every
        publish is a file write plus a menu bar refresh."""
        m.write_transcribe_job(self.audio, "transcribing")
        with mock.patch.object(m.time, "time", return_value=100.0):
            m.write_transcribe_progress(10.0, 100.0)
            for done in range(11, 40):
                m.write_transcribe_progress(float(done), 100.0)
        self.assertEqual(self.published()["percent"], "10")

    def test_publishing_progress_keeps_the_rest_of_the_state(self):
        """A partial write would blank the meeting name or the start time, and the
        queue orders jobs by that start time."""
        m.write_transcribe_job(self.audio, "transcribing")
        before = self.published()
        with mock.patch.object(m.time, "time", return_value=10 ** 9):
            m.write_transcribe_progress(600.0, 3600.0)
        after = self.published()
        for key in ("state", "audio", "meeting", "since"):
            self.assertEqual(before[key], after[key])

    def test_an_unpublished_job_stays_unpublished(self):
        """`mrec transcribe <file>` run by hand has no job file; progress must not
        conjure one for a process the menu bar never knew about."""
        m.write_transcribe_progress(600.0, 3600.0)
        self.assertFalse((self.jobs_dir / str(os.getpid())).exists())

    def test_unknown_duration_reports_nothing(self):
        """ffprobe can fail on an odd file; a divide-by-zero must not take the
        transcription down with it."""
        m.write_transcribe_job(self.audio, "transcribing")
        m.write_transcribe_progress(600.0, 0.0)
        self.assertNotIn("percent", self.published())


class EnginePublishingTests(_JobTestCase):
    def test_the_running_engine_is_published(self):
        m.write_transcribe_job(self.audio, "transcribing")
        m.write_transcribe_engine("whisper", "turbo")
        job = self.published()
        self.assertEqual((job["engine"], job["model"]), ("whisper", "turbo"))

    def test_falling_back_resets_progress(self):
        """The fallback engine starts the audio again from zero; keeping the old
        percent would freeze the menu bar until it caught back up."""
        m.write_transcribe_job(self.audio, "transcribing")
        m.write_transcribe_engine("openrouter", "google/gemini-2.5-flash")
        with mock.patch.object(m.time, "time", return_value=10 ** 9):
            m.write_transcribe_progress(1800.0, 3600.0)
        self.assertEqual(self.published()["percent"], "50")
        m.write_transcribe_engine("whisper", "turbo")
        job = self.published()
        self.assertNotIn("percent", job)
        self.assertEqual(job["engine"], "whisper")


class WhisperStreamingTests(_JobTestCase):
    """The output has to be read as it is produced. Collecting it at the end (the
    old subprocess.run) is why an hour-long job showed nothing for its whole run."""

    def _run(self, lines: list[str], returncode: int = 0):
        out_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)
        proc = mock.MagicMock()
        proc.stdout = iter(lines)
        proc.wait.return_value = returncode
        (out_dir / f"{self.audio.stem}.txt").write_text("transcript", encoding="utf-8")
        with mock.patch.object(m.subprocess, "Popen", return_value=proc) as popen, \
             mock.patch.object(m, "audio_duration_seconds", return_value=3600.0), \
             mock.patch.object(m, "log"), mock.patch.object(m, "log_section"):
            result = m.transcribe_with_whisper(self.audio, out_dir)
        return result, out_dir, popen

    def test_segments_publish_progress_as_they_arrive(self):
        m.write_transcribe_job(self.audio, "transcribing")
        lines = ["Detected language: English\n",
                 "[00:00.000 --> 00:30.000]  hello\n",
                 "[30:00.000 --> 36:00.000]  later\n"]
        # A minute of wall clock per call, so the rate limiter lets both through.
        with mock.patch.object(m.time, "time", side_effect=itertools.count(1000.0, 60.0)):
            self._run(lines)
        job = self.published()
        self.assertEqual(job["percent"], "60")
        self.assertEqual(job["engine"], "whisper")

    def test_output_is_logged_as_it_streams(self):
        """A run killed mid-flight used to leave no log at all — the buffered
        version only wrote one after whisper exited."""
        _, out_dir, _ = self._run(["[00:00.000 --> 00:30.000]  hello\n"])
        self.assertIn("hello", (out_dir / "whisper.log").read_text())

    def test_a_failed_run_still_raises(self):
        with self.assertRaises(RuntimeError):
            self._run(["boom\n"], returncode=1)

    def test_whisper_is_run_unbuffered(self):
        """Without this its per-segment print() sits in a 4 KB pipe buffer and the
        progress arrives in bursts — or, on a quiet meeting, not until the end."""
        _, _, popen = self._run(["[00:00.000 --> 00:30.000]  hello\n"])
        self.assertEqual(popen.call_args.kwargs["env"]["PYTHONUNBUFFERED"], "1")


class MenuBarRenderingTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.jobs_dir = Path(tmp.name) / "transcribe-jobs"
        self.jobs_dir.mkdir(parents=True)
        for name, value in (("JOBS_DIR", self.jobs_dir), ("RECORDINGS", Path(tmp.name) / "rec"),
                            ("PID_FILE", Path(tmp.name) / "none"),
                            ("STATUS_FILE", Path(tmp.name) / "none")):
            patch = mock.patch.object(self.plugin, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        for name in ("watcher_running",):
            patch = mock.patch.object(self.plugin, name, return_value=True)
            patch.start()
            self.addCleanup(patch.stop)
        quiet = mock.patch.object(self.plugin, "engine_plan", return_value={})
        quiet.start()
        self.addCleanup(quiet.stop)

    def write_job(self, **fields) -> None:
        fields.setdefault("pid", os.getpid())
        fields.setdefault("state", "transcribing")
        body = "".join(f"{key}={value}\n" for key, value in fields.items())
        (self.jobs_dir / str(fields["pid"])).write_text(body, encoding="utf-8")

    def render(self) -> str:
        buffer = io.StringIO()
        with mock.patch.object(self.plugin, "pid_running", return_value=True), \
             mock.patch.object(self.plugin, "process_is_zombie", return_value=False), \
             redirect_stdout(buffer):
            self.plugin.main()
        return buffer.getvalue()

    def test_the_title_carries_the_percentage(self):
        self.write_job(meeting="Standup", since=0, percent=42, eta=1500,
                       engine="whisper", model="turbo")
        self.assertIn("Transcribing 42%", self.render().splitlines()[0])

    def test_the_job_line_names_the_engine_and_the_time_left(self):
        self.write_job(meeting="Standup", since=0, percent=42, eta=1500,
                       engine="whisper", model="turbo")
        detail = [line for line in self.render().splitlines() if "local whisper" in line]
        self.assertEqual(len(detail), 1)
        self.assertIn("42%", detail[0])
        self.assertIn("~25m left", detail[0])

    def test_a_job_with_no_progress_yet_still_renders(self):
        """Progress only starts once the first segment lands — a couple of minutes
        into a long meeting, and never at all for an engine that never reports."""
        self.write_job(meeting="Standup", since=0)
        rendered = self.render()
        self.assertIn("Transcribing…", rendered.splitlines()[0])
        self.assertIn("⏳ Transcribing: Standup", rendered)

    def test_the_plan_line_is_not_confused_with_the_running_engine(self):
        """The bug this fixes: gemini in the menu while whisper did the work."""
        self.write_job(meeting="Standup", since=0, percent=42, engine="whisper", model="turbo")
        with mock.patch.object(self.plugin, "engine_plan",
                               return_value={"engine": "openrouter",
                                             "model": "google/gemini-2.5-flash"}):
            rendered = self.render()
        self.assertIn("Next transcription: gemini-2.5-flash", rendered)
        self.assertIn("local whisper (turbo)", rendered)

    def test_recording_keeps_the_lead_and_shows_the_transcription_behind_it(self):
        self.write_job(meeting="Standup", since=0, percent=42)
        with mock.patch.object(self.plugin, "watcher_status",
                               return_value={"status": "recording", "meeting": "Later call",
                                             "since": "0", "pid": str(os.getpid())}):
            title = self.render().splitlines()[0]
        self.assertTrue(title.startswith("Rec "))
        self.assertIn("⧗42%", title)


if __name__ == "__main__":
    unittest.main()
