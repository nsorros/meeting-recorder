"""Repetition loops in ASR output, and the retry that rescues them.

Both engines occasionally lock onto a phrase and emit it until the chunk ends.
Nothing about it looks like an error — OpenRouter returns 200 with plausible
text — so it went unnoticed until 2026-09-02, when a wrap-up call was filed as a
recap built on 493 copies of "He's good.", and 2026-08-26, where an interleaved
loop flipped speaker labels and lost who owned the budget action item.

The fixtures are trimmed from those real captures, because the three incidents
have three different signatures and a threshold nobody can re-check is one that
silently drifts: 2026-09-02 is one long run buried mid-chunk in otherwise-good
conversation, 2026-08-28 is four discrete runs, and 2026-08-26 has a longest run
of *one* and is caught only by the unique-sentence ratio.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import meeting_recorder as m


# Trimmed from ~/Meetings/Recordings/2026-09-02_17-52-39_Addgene-wrap-up-Nick-Sim.
# The loop sits 81 sentences into a chunk whose head and tail are real talk —
# which is why the fix re-transcribes the chunk instead of discarding it.
SEP_02_HEAD = ("For some reason, they're starting early, but uh he's not fluent at all. "
               "So, yeah. I think he's good.")
SEP_02_LOOP = " ".join(["He's good."] * 493)
SEP_02_TAIL = "He're going to get there. He's doing english quite clearly. It's going to be fine."

# Trimmed from 2026-08-28_11-09-48_Standup: four separate runs of the same word,
# 1249 long at the worst, rather than one continuous block.
AUG_28_HEAD = ("Χαίρετε. Μα πού πήγε το το finand; Γαμώτο. "
               "Δεν σ' ακούω by the way. Τώρα σ' ακούω.")
AUG_28 = AUG_28_HEAD + " " + " ".join([" ".join(["Ναι."] * 60), AUG_28_HEAD] * 4)

# Trimmed from 2026-08-26_10-11-48_Standup: an eight-sentence block the model
# repeated 163 times. No two neighbours match, so the longest run is 1 and only
# the unique ratio sees it.
AUG_26_BLOCK = ("Ε, άρα το budget δεν θα το συζητήσουμε σήμερα. "
                "Έχω budget, το έχω στο όμως για να το κάνω. "
                "Το κάνω μια γρήγορη έτσι. Να πρώτο bullet point. "
                "Ότι ωραία. Ναι. Οπότε να κάνουμε τώρα stand up. "
                "Τι να κάνουμε τόση ώρα.")
AUG_26 = " ".join([AUG_26_BLOCK] * 163)

# A real healthy excerpt, for the other side of the line. Across the 65-transcript
# archive the healthy captures top out at a run of 5 and 0.83 unique.
HEALTHY = ("Good morning. So where did we land on the deployment? "
           "We pushed it last night and it looks stable. "
           "Okay. Any errors in the logs since then? "
           "A couple of timeouts, nothing that woke anyone up. "
           "Right, let's leave it and check again after standup. "
           "Sounds good. I'll write up the migration notes this afternoon. "
           "Perfect, thanks. Anything else before we wrap? No, that's me. Yeah.")


class DegenerationReportTests(unittest.TestCase):
    """One report, two tests, because neither alone catches all three incidents."""

    def test_healthy_conversation_is_clean(self):
        report = m.degeneration_report(HEALTHY)
        self.assertFalse(report.flagged)
        self.assertLess(report.longest_run, m.DEGENERATION_MAX_RUN)
        self.assertGreater(report.unique_ratio, m.DEGENERATION_MIN_UNIQUE_RATIO)

    def test_mid_chunk_run_is_caught_with_good_talk_around_it(self):
        """2026-09-02: the run is buried in a chunk that is otherwise fine."""
        report = m.degeneration_report(f"{SEP_02_HEAD} {SEP_02_LOOP} {SEP_02_TAIL}")
        self.assertTrue(report.flagged)
        self.assertGreaterEqual(report.longest_run, 493)
        self.assertEqual(report.token, "he's good.")

    def test_four_discrete_runs_are_caught(self):
        """2026-08-28: the loop restarts rather than running once."""
        report = m.degeneration_report(AUG_28)
        self.assertTrue(report.flagged)
        self.assertEqual(report.token, "ναι.")

    def test_interleaved_repetition_is_caught_by_ratio_alone(self):
        """2026-08-26, the one that cost us the budget action item.

        This is the whole reason there are two tests: the repeats alternate, so
        the run test sees nothing at all.
        """
        report = m.degeneration_report(AUG_26)
        self.assertTrue(report.flagged)
        self.assertEqual(report.longest_run, 1)
        self.assertLess(report.unique_ratio, m.DEGENERATION_MIN_UNIQUE_RATIO)

    def test_a_short_quiet_chunk_is_not_a_loop(self):
        """The unit floor. Ten sentences of "Yeah." is a quiet minute, not a
        failure, and re-transcribing it locally would be wasted work."""
        report = m.degeneration_report("Yeah. Yeah. Yeah. Yeah. Yeah. Yeah. Yeah. Yeah. Yeah. Yeah.")
        self.assertFalse(report.flagged)
        self.assertLess(report.units, m.DEGENERATION_MIN_UNITS)

    def test_empty_text_is_not_a_loop(self):
        self.assertFalse(m.degeneration_report("").flagged)
        self.assertFalse(m.degeneration_report("   \n\n  ").flagged)

    def test_case_and_whitespace_do_not_hide_a_loop(self):
        """The captures alternate "He's good." and "He's good. " across line
        breaks; comparing raw strings would score that as distinct sentences."""
        report = m.degeneration_report("He's good.\nhe's  good. " * 30)
        self.assertTrue(report.flagged)
        self.assertEqual(report.longest_run, 60)

    def test_an_unpunctuated_loop_is_still_a_loop(self):
        """Whisper writes one line per segment and does not always punctuate — a
        real 30s slice came back as eight bare lines. Splitting on ". ! ?" alone
        would make a whole unpunctuated transcript one unit and see no loop in
        it, which is exactly the Whisper output the final gate is there to catch.
        """
        report = m.degeneration_report("\n".join(["yeah I think so"] * 400))
        self.assertTrue(report.flagged)
        self.assertEqual(report.longest_run, 400)

    def test_lines_of_real_talk_are_not_a_loop(self):
        """The other half of splitting on lines: unpunctuated but varied output
        must stay clean, or every local Whisper run fails the gate."""
        report = m.degeneration_report(
            "you were also updating the cloud\nand the file to\n"
            "in which case it sounds logical\nas we're learning more about the project\n"
            "we're sort of\ncreating the constraints that we want\nyeah\n"
            "i feel like that's\nhow a lot of my work is now\n"
            "getting my head around a project quickly\nand then getting those things in\n"
            "because otherwise the reviews can get really tedious")
        self.assertFalse(report.flagged)

    def test_detail_names_the_phrase_and_its_share(self):
        detail = m.degeneration_detail(m.degeneration_report(SEP_02_LOOP))
        self.assertIn("he's good.", detail)
        self.assertIn("493x", detail)


class ArchiveSeparationTests(unittest.TestCase):
    """The thresholds are numbers off a real corpus, so keep the corpus's shape.

    Every capture in the archive was scored when these constants were chosen. If
    someone widens a threshold, these are the margins that should stop them.
    """

    def test_thresholds_sit_between_the_two_populations(self):
        self.assertGreater(m.DEGENERATION_MAX_RUN, 5, "healthy captures reach a run of 5")
        self.assertLess(m.DEGENERATION_MAX_RUN, 24, "the mildest degenerate run is 24")
        self.assertGreater(m.DEGENERATION_MIN_UNIQUE_RATIO, 0.28,
                           "the most varied ratio-caught capture is 0.28 unique")
        self.assertLess(m.DEGENERATION_MIN_UNIQUE_RATIO, 0.83,
                        "the least varied healthy capture is 0.83 unique")


class ChunkRetryTests(unittest.TestCase):
    """A flagged chunk is re-transcribed locally, and the clean text wins.

    The mp3 is already on disk from the transcode, so the retry is a local
    Whisper pass over a few minutes of audio and no re-encode.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = Path(tmp.name)
        self.mp3 = self.tmpdir / "chunk_000.mp3"
        self.mp3.write_bytes(b"\x00" * 4096)
        quiet = mock.patch.object(m, "log")
        quiet.start()
        self.addCleanup(quiet.stop)

    def _whisper_returning(self, text):
        """Stand in for whisper_transcribe_file, writing the txt it would write."""
        def fake(audio, out_dir, **kwargs):
            out_dir.mkdir(parents=True, exist_ok=True)
            txt = out_dir / f"{audio.stem}.txt"
            txt.write_text(text, encoding="utf-8")
            return txt
        return fake

    def test_clean_local_text_replaces_the_looped_chunk(self):
        with mock.patch.object(m, "whisper_transcribe_file",
                               side_effect=self._whisper_returning(HEALTHY)):
            self.assertEqual(m.whisper_retry_chunk(self.mp3, self.tmpdir, 0), HEALTHY)

    def test_the_retry_never_writes_job_state(self):
        """A chunk retry mid-OpenRouter-run must not flip the menu bar to
        "whisper" or rewind the progress bar — the reason the side-effect-free
        helper was split out of transcribe_with_whisper."""
        with mock.patch.object(m, "whisper_transcribe_file",
                               side_effect=self._whisper_returning(HEALTHY)), \
             mock.patch.object(m, "write_transcribe_engine") as engine, \
             mock.patch.object(m, "write_transcribe_progress") as progress:
            m.whisper_retry_chunk(self.mp3, self.tmpdir, 0)
        engine.assert_not_called()
        progress.assert_not_called()

    def test_a_local_retry_that_also_loops_is_refused(self):
        with mock.patch.object(m, "whisper_transcribe_file",
                               side_effect=self._whisper_returning(SEP_02_LOOP)):
            self.assertIsNone(m.whisper_retry_chunk(self.mp3, self.tmpdir, 0))

    def test_a_failed_retry_keeps_the_openrouter_text(self):
        """None means "keep what we had". A retry that cannot run is not a
        reason to lose the chunk."""
        with mock.patch.object(m, "whisper_transcribe_file",
                               side_effect=RuntimeError("whisper failed")):
            self.assertIsNone(m.whisper_retry_chunk(self.mp3, self.tmpdir, 0))

    def test_an_empty_retry_keeps_the_openrouter_text(self):
        with mock.patch.object(m, "whisper_transcribe_file",
                               side_effect=self._whisper_returning("  \n ")):
            self.assertIsNone(m.whisper_retry_chunk(self.mp3, self.tmpdir, 0))


class OpenRouterChunkLoopTests(unittest.TestCase):
    """The retry is wired into the chunk loop, not just available beside it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.out_dir = Path(tmp.name) / "out"
        self.out_dir.mkdir()
        self.audio = Path(tmp.name) / "meeting.wav"
        self.audio.write_bytes(b"\x00" * 4096)
        for name, kwargs in (("log", {}), ("log_section", {}), ("record_openrouter_cost", {}),
                             ("write_transcribe_engine", {}), ("write_transcribe_progress", {}),
                             ("openrouter_api_key", {"return_value": "key"}),
                             ("command_exists", {"return_value": True}),
                             ("audio_duration_seconds", {"return_value": 600.0})):
            patch = mock.patch.object(m, name, **kwargs)
            patch.start()
            self.addCleanup(patch.stop)
        # ffmpeg is the transcode; write the mp3 it would have written.
        def fake_ffmpeg(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"\x00" * 4096)
            return mock.Mock(returncode=0)
        run = mock.patch.object(m.subprocess, "run", side_effect=fake_ffmpeg)
        run.start()
        self.addCleanup(run.stop)

    def test_a_looped_chunk_is_retried_and_the_local_text_filed(self):
        looped = f"{SEP_02_HEAD} {SEP_02_LOOP} {SEP_02_TAIL}"
        with mock.patch.object(m, "openrouter_transcribe_chunk", return_value=(looped, {})), \
             mock.patch.object(m, "whisper_retry_chunk", return_value=HEALTHY) as retry:
            raw = m.transcribe_with_openrouter(self.audio, self.out_dir)
        retry.assert_called_once()
        self.assertEqual(raw.read_text(encoding="utf-8").strip(), HEALTHY)

    def test_a_healthy_chunk_is_never_retried(self):
        with mock.patch.object(m, "openrouter_transcribe_chunk", return_value=(HEALTHY, {})), \
             mock.patch.object(m, "whisper_retry_chunk") as retry:
            raw = m.transcribe_with_openrouter(self.audio, self.out_dir)
        retry.assert_not_called()
        self.assertEqual(raw.read_text(encoding="utf-8").strip(), HEALTHY)

    def test_a_refused_retry_keeps_the_chunk_rather_than_dropping_it(self):
        """Half a meeting is worth more than none; the final gate decides."""
        looped = f"{SEP_02_HEAD} {SEP_02_LOOP} {SEP_02_TAIL}"
        with mock.patch.object(m, "openrouter_transcribe_chunk", return_value=(looped, {})), \
             mock.patch.object(m, "whisper_retry_chunk", return_value=None):
            raw = m.transcribe_with_openrouter(self.audio, self.out_dir)
        self.assertIn("He's good.", raw.read_text(encoding="utf-8"))


class FinalGateTests(unittest.TestCase):
    """Nothing gets built on a looped transcript.

    On 2026-09-02 the cleanup faithfully amplified 493 copies to 843 in the filed
    notes. A visible failure that keeps the audio beats a recap that reads as if
    it were true.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audio = Path(tmp.name) / "meeting.wav"
        self.audio.write_bytes(b"\x00" * 4096)
        for patch in (mock.patch.object(m, "log"),
                      mock.patch.object(m, "log_section"),
                      mock.patch.object(m, "SILENCE_DB", None),
                      mock.patch.object(m, "DIARIZE", False)):
            patch.start()
            self.addCleanup(patch.stop)

    def _transcribed_as(self, text):
        def fake(audio, out_dir):
            out_dir.mkdir(parents=True, exist_ok=True)
            raw = out_dir / f"{audio.stem}.txt"
            raw.write_text(text, encoding="utf-8")
            return raw
        return fake

    def test_a_looped_transcript_fails_loudly_instead_of_being_cleaned_up(self):
        with mock.patch.object(m, "TRANSCRIBE_ENGINE", "whisper"), \
             mock.patch.object(m, "transcribe_with_whisper",
                               side_effect=self._transcribed_as(SEP_02_LOOP)), \
             mock.patch.object(m, "clean_with_claude") as cleanup:
            with self.assertRaises(RuntimeError) as caught:
                m.transcribe_audio(self.audio)
        self.assertIn("he's good.", str(caught.exception))
        cleanup.assert_not_called()

    def test_the_gate_also_covers_local_whisper(self):
        """Four of the nine bad captures in the archive are Whisper's, so the
        gate sits in transcribe_audio rather than inside the OpenRouter path."""
        with mock.patch.object(m, "TRANSCRIBE_ENGINE", "whisper"), \
             mock.patch.object(m, "transcribe_with_whisper",
                               side_effect=self._transcribed_as(AUG_26)), \
             mock.patch.object(m, "clean_with_claude"):
            with self.assertRaises(RuntimeError):
                m.transcribe_audio(self.audio)

    def test_a_clean_transcript_passes_through_to_cleanup(self):
        with mock.patch.object(m, "TRANSCRIBE_ENGINE", "whisper"), \
             mock.patch.object(m, "transcribe_with_whisper",
                               side_effect=self._transcribed_as(HEALTHY)), \
             mock.patch.object(m, "event_for_recording", return_value=None), \
             mock.patch.object(m, "clean_with_claude") as cleanup:
            m.transcribe_audio(self.audio)
        cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()
