"""Who was in the room, carried from the calendar into the cleanup prompt.

The recorder already asked the calendar what a meeting was *called*; it threw the
guest list away. These cover keeping it: the shape written beside a recording, the
way a name is recovered from an email address, and the prompt block that hands the
roster to the model without licensing it to deal the names out.

The risk being guarded is a confident wrong answer. A model given six names and a
six-voice transcript will pair them off, so the roster must be phrased as evidence
rather than as an answer key — and a declined invitation must not reach it at all.
"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import meeting_recorder as m


def _event(summary="Standup", start="2026-08-20T10:00:00+03:00",
           end="2026-08-20T10:30:00+03:00", attendees=None, **extra):
    ev = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        "attendees": attendees if attendees is not None else [],
    }
    ev.update(extra)
    return ev


class NamesFromAddresses(unittest.TestCase):
    def test_local_part_becomes_a_plausible_name(self):
        self.assertEqual(m.humanise_email("persefoni@finant.ai"), "Persefoni")
        self.assertEqual(m.humanise_email("fernando.ortiz@x.com"), "Fernando Ortiz")
        self.assertEqual(m.humanise_email("nick-sorros+cal@x.com"), "Nick Sorros Cal")

    def test_digits_are_dropped_and_existing_caps_survive(self):
        self.assertEqual(m.humanise_email("dan2@x.com"), "Dan")
        self.assertEqual(m.humanise_email("McDonald@x.com"), "McDonald")

    def test_an_address_with_no_usable_local_part_is_returned_whole(self):
        self.assertEqual(m.humanise_email("123@x.com"), "123@x.com")
        self.assertEqual(m.humanise_email(""), "")

    def test_a_display_name_carrying_its_own_address_is_stripped(self):
        # Seen live: Google puts "Name <email>" in displayName for some guests, and
        # unstripped that address ends up printed in the middle of a spoken line.
        self.assertEqual(
            m._display_name("Persefoni Noulika <persefoni.noulika@googlemail.com>"),
            "Persefoni Noulika")
        self.assertEqual(m._display_name("Patrik Bless"), "Patrik Bless")


class TrimmedEvent(unittest.TestCase):
    def test_keeps_the_roster_and_marks_which_names_are_guesses(self):
        trimmed = m.trim_event(_event(attendees=[
            {"email": "nick@finant.ai", "self": True, "organizer": True,
             "responseStatus": "accepted"},
            {"email": "p@finant.ai", "displayName": "Persefoni Noulika",
             "responseStatus": "needsAction"},
        ]))
        nick, persefoni = trimmed["attendees"]
        self.assertEqual(nick["name"], "Nick")
        self.assertTrue(nick["guessed_name"], "a name we derived must say so")
        self.assertTrue(nick["self"])
        self.assertTrue(nick["organizer"])
        self.assertEqual(persefoni["name"], "Persefoni Noulika")
        self.assertFalse(persefoni["guessed_name"], "Google gave us this one")

    def test_meeting_rooms_are_not_people(self):
        trimmed = m.trim_event(_event(attendees=[
            {"email": "room-a@resource.calendar.google.com", "resource": True},
            {"email": "nick@finant.ai"},
        ]))
        self.assertEqual([a["email"] for a in trimmed["attendees"]], ["nick@finant.ai"])

    def test_a_long_agenda_is_clipped_not_pasted_whole(self):
        trimmed = m.trim_event(_event(description="x" * 5000))
        self.assertLessEqual(len(trimmed["description"]), m.EVENT_DESCRIPTION_MAX)


class RosterBlock(unittest.TestCase):
    def _block(self, **kw):
        return m.roster_block(m.trim_event(_event(**kw)))

    def test_declined_guests_are_left_out(self):
        # Someone who said no is a name the model would otherwise be free to hang a
        # voice on, and it would sound entirely plausible.
        block = self._block(attendees=[
            {"email": "here@x.com", "responseStatus": "accepted"},
            {"email": "elsewhere@x.com", "responseStatus": "declined"},
        ])
        self.assertIn("Here", block)
        self.assertNotIn("Elsewhere", block)

    def test_unanswered_invitations_are_kept(self):
        # "needsAction" and turning up anyway is the normal case, not an absence.
        self.assertIn("Maybe", self._block(
            attendees=[{"email": "maybe@x.com", "responseStatus": "needsAction"}]))

    def test_no_attendees_means_no_block_at_all(self):
        self.assertEqual(self._block(attendees=[]), "")
        self.assertEqual(m.roster_block(None), "")

    def test_the_block_says_the_roster_is_not_an_answer_key(self):
        block = self._block(attendees=[{"email": "a@x.com"}, {"email": "b@x.com"}])
        self.assertIn("invited", block)
        self.assertIn("may never say a word", block)
        self.assertIn("not on\nit may well be in the room", block)
        self.assertIn("guessed from", block)

    def test_the_recording_laptop_is_identified(self):
        block = self._block(attendees=[{"email": "nick@finant.ai", "self": True}])
        self.assertIn("the person whose laptop is recording", block)


class CleanupPrompt(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.raw = Path(tmp.name) / "raw.txt"
        self.raw.write_text("shall we start", encoding="utf-8")

    def test_the_roster_reaches_the_prompt(self):
        prompt = m.build_cleanup_prompt(
            self.raw, Path("/x/a.wav"),
            event=m.trim_event(_event(summary="Demo", attendees=[
                {"email": "matteo@x.com"}])))
        self.assertIn("Matteo", prompt)
        self.assertIn('calendar entry "Demo"', prompt)
        self.assertIn("shall we start", prompt)

    def test_a_recording_with_no_invite_is_unchanged(self):
        # The prompt without an event must be exactly what it was before rosters
        # existed: no empty heading, no dangling instruction about a list that
        # is not there.
        prompt = m.build_cleanup_prompt(self.raw, Path("/x/a.wav"), event=None)
        self.assertNotIn("Invited to this meeting", prompt)
        self.assertNotIn("roster", prompt.lower())
        self.assertIn("Speaker labels:", prompt)

    def test_diarized_input_still_gets_the_roster(self):
        diarized = self.raw.with_name("d.txt")
        diarized.write_text("[SPEAKER_00] hello", encoding="utf-8")
        prompt = m.build_cleanup_prompt(
            self.raw, Path("/x/a.wav"), diarized_txt=diarized,
            event=m.trim_event(_event(attendees=[{"email": "matteo@x.com"}])))
        self.assertIn("SPEAKER_00", prompt)
        self.assertIn("Matteo", prompt)


class RecordingStems(unittest.TestCase):
    def test_a_recording_name_yields_its_moment_and_slug(self):
        when, name = m.parse_recording_stem("2026-08-20_10-18-00_Standup")
        self.assertEqual((when.year, when.month, when.day), (2026, 8, 20))
        self.assertEqual((when.hour, when.minute), (10, 18))
        self.assertEqual(name, "Standup")

    def test_a_name_with_no_slug_still_parses(self):
        when, name = m.parse_recording_stem("2026-08-20_10-18-00")
        self.assertEqual(name, "")

    def test_anything_else_is_a_miss_rather_than_a_wrong_time(self):
        self.assertIsNone(m.parse_recording_stem("notes"))
        self.assertIsNone(m.parse_recording_stem("2026-13-45_99-99-99_x"))

    def test_slugs_match_through_the_80_char_truncation(self):
        self.assertTrue(m._slug_matches("Standup", "Standup"))
        self.assertTrue(m._slug_matches("Demo-session-finant", "Demo-session"))
        self.assertFalse(m._slug_matches("Standup", "Lunch"))
        self.assertFalse(m._slug_matches("Standup", ""))
        # Two characters in common is a coincidence, not a match.
        self.assertFalse(m._slug_matches("Standup", "St"))


class MatchingAnEvent(unittest.TestCase):
    def setUp(self):
        for name, value in (("CALENDAR_LOOKUP", True),):
            patch = mock.patch.object(m, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(m, "command_exists", return_value=True)
        patch.start()
        self.addCleanup(patch.stop)

    def _at(self, events, when, **kw):
        with mock.patch.object(m, "_fetch_calendar_events", return_value=events):
            return m.best_event_at(dt.datetime.fromisoformat(when), **kw)

    def test_an_open_meet_room_code_beats_everything(self):
        events = [
            _event(summary="Wrong", hangoutLink="https://meet.google.com/aaa-bbb-ccc"),
            _event(summary="Right", hangoutLink="https://meet.google.com/xyz-xyz-xyz"),
        ]
        best = self._at(events, "2026-08-20T10:05:00+03:00", open_codes={"xyz-xyz-xyz"})
        self.assertEqual(best["summary"], "Right")

    def test_the_recording_slug_decides_when_no_tab_is_open(self):
        # This is the only evidence left when the lookup happens after the fact,
        # which is the whole of the archive.
        events = [_event(summary="Lunch"), _event(summary="Standup")]
        best = self._at(events, "2026-08-20T10:05:00+03:00", want_slug="Standup")
        self.assertEqual(best["summary"], "Standup")

    def test_events_outside_the_window_are_not_considered(self):
        events = [_event(summary="Standup")]
        self.assertIsNone(self._at(events, "2026-08-20T15:00:00+03:00"))

    def test_an_all_day_entry_never_matches(self):
        # It has no useful name and would overlap every recording made that day.
        events = [{"summary": "Holiday", "start": {"date": "2026-08-20"},
                   "end": {"date": "2026-08-21"}}]
        self.assertIsNone(self._at(events, "2026-08-20T10:05:00+03:00"))

    def test_the_later_start_wins_a_tie(self):
        events = [
            _event(summary="Long block", start="2026-08-20T09:00:00+03:00",
                   end="2026-08-20T12:00:00+03:00"),
            _event(summary="The actual call", start="2026-08-20T10:00:00+03:00"),
        ]
        best = self._at(events, "2026-08-20T10:05:00+03:00")
        self.assertEqual(best["summary"], "The actual call")


class Sidecars(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.audio = Path(tmp.name) / "2026-08-20_10-18-00_Standup.wav"
        self.audio.write_bytes(b"")

    def test_written_beside_the_audio_and_read_back(self):
        event = m.trim_event(_event(attendees=[{"email": "p@finant.ai"}]))
        m.write_event_sidecar(self.audio, event)
        self.assertTrue(self.audio.with_name(self.audio.stem + ".event.json").exists())
        self.assertEqual(m.read_event_sidecar(self.audio), event)

    def test_nothing_is_written_when_there_was_no_match(self):
        m.write_event_sidecar(self.audio, None)
        self.assertIsNone(m.read_event_sidecar(self.audio))

    def test_a_corrupt_sidecar_reads_as_absent_rather_than_raising(self):
        m.event_sidecar_for(self.audio).write_text("{not json", encoding="utf-8")
        self.assertIsNone(m.read_event_sidecar(self.audio))

    def test_the_cached_answer_is_used_without_asking_the_calendar(self):
        m.write_event_sidecar(self.audio, m.trim_event(_event(summary="Cached")))
        with mock.patch.object(m, "best_event_at") as lookup:
            found = m.event_for_recording(self.audio)
        lookup.assert_not_called()
        self.assertEqual(found["summary"], "Cached")

    def test_a_miss_is_never_cached(self):
        # "Nothing was scheduled" and "the network was down" arrive here as the same
        # None, and writing the second one down would make a blip permanent.
        with mock.patch.object(m, "best_event_at", return_value=None):
            self.assertIsNone(m.event_for_recording(self.audio))
        self.assertFalse(m.event_sidecar_for(self.audio).exists())

    def test_a_hit_is_cached_so_the_trip_is_made_once(self):
        with mock.patch.object(m, "best_event_at", return_value=_event(summary="Found")):
            m.event_for_recording(self.audio)
        self.assertEqual(json.loads(m.event_sidecar_for(self.audio).read_text())["summary"],
                         "Found")

    def test_lookup_can_be_refused_for_callers_that_cannot_wait(self):
        # A page listing the whole archive must not make one network call per row.
        with mock.patch.object(m, "best_event_at") as lookup:
            self.assertIsNone(m.event_for_recording(self.audio, lookup=False))
        lookup.assert_not_called()

    def test_an_unrecognisable_filename_is_not_looked_up(self):
        odd = self.audio.with_name("notes.wav")
        odd.write_bytes(b"")
        with mock.patch.object(m, "best_event_at") as lookup:
            self.assertIsNone(m.event_for_recording(odd))
        lookup.assert_not_called()


class NamingStillWorks(unittest.TestCase):
    def test_the_title_and_the_roster_come_from_one_lookup(self):
        event = m.trim_event(_event(summary="Standup", attendees=[{"email": "p@x.com"}]))
        with mock.patch.object(m, "calendar_meeting_event", return_value=event) as look:
            name, got = m.meeting_context("Safari: Meet")
        self.assertEqual(look.call_count, 1, "asking twice is two chances to differ")
        self.assertEqual(name, "Standup")
        self.assertIs(got, event)

    def test_a_calendar_miss_falls_back_to_the_tab_title(self):
        with mock.patch.object(m, "calendar_meeting_event", return_value=None):
            name, event = m.meeting_context("Safari: Meet – Standup")
        self.assertIsNone(event)
        self.assertIn("Standup", name)

    def test_a_calendar_that_raises_never_stops_a_recording(self):
        with mock.patch.object(m, "calendar_meeting_event", side_effect=RuntimeError("no net")):
            name, event = m.meeting_context("Safari: Meet – Standup")
        self.assertIsNone(event)
        self.assertTrue(name)


if __name__ == "__main__":
    unittest.main()
