# Meeting Recorder

Local macOS meeting recorder:

1. watches for Google Meet, Teams, Zoom, Webex, or Whereby in browsers/apps
2. asks before recording
3. records the configured macOS audio input with `ffmpeg` as `.wav`
4. transcribes via **OpenRouter** (cloud; seconds per file) with automatic fallback to local `whisper`
5. optionally asks `claude -p` to clean the raw transcript into notes

`claude -p` is not used as the speech recognizer. OpenRouter (or local Whisper) does the audio transcription. Claude is only the cleanup/summary pass.

## Commands

```sh
~/code/meeting-recorder/mrec doctor
~/code/meeting-recorder/mrec engine
~/code/meeting-recorder/mrec watch
~/code/meeting-recorder/mrec record test-meeting
~/code/meeting-recorder/mrec record-start menubar-manual
~/code/meeting-recorder/mrec record-stop
~/code/meeting-recorder/mrec open-transcript
~/code/meeting-recorder/mrec transcribe ~/Meetings/Recordings/example.m4a
~/code/meeting-recorder/mrec install-app
~/code/meeting-recorder/mrec build-recorder
```

Recordings and transcripts are written under:

```sh
~/Meetings/Recordings
```

Logs are written to:

```sh
~/Library/Logs/meeting-recorder.log
```

## Transcription Engine

Transcription defaults to **OpenRouter** (`google/gemini-2.5-flash`): the `.wav` is
transcoded to 16 kHz mono mp3, chunked, and transcribed in seconds for roughly
$0.12 per hour of audio (`google/gemini-2.5-flash-lite` is ~$0.035/hr). It falls
back to **local Whisper** automatically when there is no API key, no network, no
credits, or any request error — so a run never depends on connectivity.

```sh
# engine: "openrouter" (default) or "whisper" to force local-only
export MEETING_RECORDER_TRANSCRIBE_ENGINE=openrouter
export MEETING_RECORDER_OPENROUTER_MODEL=google/gemini-2.5-flash
# provide the key directly...
export MEETING_RECORDER_OPENROUTER_API_KEY=sk-or-v1-...
# ...or point at a dotenv file to read OPENROUTER_API_KEY from (e.g. the ant app)
export MEETING_RECORDER_OPENROUTER_ENV_FILE=~/code/ant/.env
```

The background watcher does not inherit your shell environment: `mrec start` bakes
`MEETING_RECORDER_TRANSCRIBE_ENGINE`, `MEETING_RECORDER_OPENROUTER_ENV_FILE` and
`MEETING_RECORDER_OPENROUTER_MODEL` into the LaunchAgent plist, so export them
before running it (and run it again after changing them). The **key itself is never
written to the plist** — that file is world-readable, so point at an env file
instead.

### Which engine will actually run

Because the fallback is silent, a dead API key or an empty balance shows up only
as slower, worse transcripts. `mrec engine` answers the question up front — it
walks the same chain as a real run (engine setting → key → credits) and reports
what would happen now, exiting non-zero on a downgrade or a low balance:

```sh
$ mrec engine
next transcription: openrouter:google/gemini-2.5-flash  [key present, credits available]
openrouter credits: $7.33 left of $130.00 (~61h of audio)
```

Once the balance hits zero OpenRouter returns HTTP 402 and every recording falls
back to local Whisper, so `mrec engine` warns below
`MEETING_RECORDER_LOW_CREDIT_USD` (default `$2`) while there is still time to top
up. The same line appears in `mrec doctor` and in the menu bar, which reads a
cached balance (`--max-age`) rather than calling the API every refresh.

A **silence guard** aborts transcription with an actionable error when a recording
is digital silence (mean ≤ `MEETING_RECORDER_SILENCE_DB`, default `-80` dBFS) —
the usual cause is the system output device being reset away from the
Multi-Output/BlackHole loopback after a reboot, so nothing reaches the recorder.
Set `MEETING_RECORDER_SILENCE_DB=` (empty) to disable the check.

## Audio Setup

There are two capture backends, selected by `MEETING_RECORDER_CAPTURE_BACKEND` (default `auto`):

### ScreenCaptureKit (default, recommended)

`auto` captures **system audio + microphone** through ScreenCaptureKit — a native macOS API. There is **nothing to install**: no BlackHole, no Aggregate/Multi-Output device, and no default-output setting that a reboot can silently reset to plain speakers. This is how Notion/Granola capture meetings.

The only prerequisite is a one-time permission grant. Build the helper and grant permission once:

```sh
mrec build-recorder
# then: System Settings > Privacy & Security > Screen Recording > enable the app that runs mrec
```

The two sources are recorded to separate WAVs and mixed to a single mono file with ffmpeg when the meeting ends. Set `MEETING_RECORDER_SCK_NO_MIC=1` to capture system audio only. If the helper can't build or permission isn't granted, `auto` transparently falls back to the ffmpeg path below (force it with `MEETING_RECORDER_CAPTURE_BACKEND=ffmpeg`, or require ScreenCaptureKit with `screencapturekit`).

### ffmpeg + BlackHole loopback (fallback, portable)

For "whatever goes into my ears and into the meeting" via ffmpeg, macOS needs a loopback or aggregate input device. The usual setup is:

1. Install BlackHole 2ch.
2. Open Audio MIDI Setup.
3. Create an Aggregate Device containing your microphone and BlackHole.
4. Route meeting output to BlackHole, or use a Multi-Output Device if you also want to hear it.
5. Run `mrec doctor` and set the detected device:

```sh
export MEETING_RECORDER_AUDIO_DEVICE="BlackHole 2ch"
```

You can also use the AVFoundation input index shown by `mrec doctor`, for example:

```sh
export MEETING_RECORDER_AUDIO_DEVICE="1"
```

If `ffmpeg` cannot see any audio devices, grant microphone permission to your terminal app in System Settings.

## Claude Cleanup

By default, the raw Whisper transcript is cleaned with:

```sh
claude -p
```

Disable that and keep the raw transcript wrapped in markdown:

```sh
export MEETING_RECORDER_DISABLE_CLAUDE=1
```

Choose a Claude model:

```sh
export MEETING_RECORDER_CLAUDE_MODEL=sonnet
```

## Reading Transcripts

```sh
mrec open-transcript                    # the newest meeting
mrec open-transcript path/to/x.meeting.md
```

Opens the meeting rendered in your **browser** — the notes/summary first, then the
full transcript below, with a jump link between them. Where the cleanup wrote a
separate `<stem>-cleaned.md` that speaker-attributed version is used; otherwise the
raw `<stem>.txt` is shown as prose.

This exists because `open`-ing a `.md` hands it to whichever app has claimed
markdown on your Mac — typically an *editor* (VS Code), which is the wrong tool for
reading a 30 KB transcript. Rendering is done with the `markdown` module, falling
back to `pandoc`, then to preformatted text. Pages are written to
`~/.local/state/meeting-recorder/rendered/` and are safe to delete.

## Speaker Labels

The cleanup pass attributes each turn to a speaker and adds a **Speakers legend**
at the top of the notes. With no acoustic diarization it infers speakers from
turn-taking and content — using real names when they are clearly identifiable and
consistent role labels (`Speaker 1 (host)`, `Speaker 2 (client)`) otherwise. This
is a best-supported reading, not ground truth.

For true per-person acoustic diarization (like Google Meet / Gemini), enable the
optional [whisperx](https://github.com/m-bain/whisperX) path:

```sh
pipx install whisperx            # or: pip install whisperx
export HF_TOKEN=hf_...            # Hugging Face token
# Accept the model licenses once at:
#   huggingface.co/pyannote/speaker-diarization-3.1
#   huggingface.co/pyannote/segmentation-3.0
export MEETING_RECORDER_DIARIZE=1
```

When enabled and available, whisperx produces a speaker-tagged transcript
(`[SPEAKER_00]` …) that is fed to the cleanup instead of the plain one. If
whisperx or the token is missing, the tool logs why and falls back to the plain
transcript — it never fails the run.

## Notification Logo

macOS locks `osascript` notifications to the generic script icon, so to show the
Meeting Recorder mic logo the tool builds a tiny native notifier app
(`notifier/main.swift`) that posts via the modern `UserNotifications` framework —
so the notification carries *its* bundle icon (the mic). Build it with:

```sh
~/code/meeting-recorder/mrec install-app
```

This compiles and ad-hoc-signs `~/Applications/Meeting Recorder Notifier.app`
(needs the Xcode command line tools — `xcode-select --install`; no Apple
Developer account required). `mrec start` builds it too. The first time, allow
"Meeting Recorder" once in **System Settings > Notifications**. If the app can't
be built, notifications fall back to `osascript` (reliable, generic icon); set
`MEETING_RECORDER_NO_LOGO=1` to force that path. Edit `assets/icon.svg` and rerun
`mrec install-app` to change the logo.

> The notifier is also published standalone at
> [github.com/nsorros/notifly](https://github.com/nsorros/notifly) — the same
> trick for giving *any* script's notifications a custom icon.

## Background Agent

After `mrec doctor` and `mrec record test` work:

```sh
~/code/meeting-recorder/mrec start
```

That installs a macOS LaunchAgent and starts the watcher immediately. It will also start automatically when you log in.

Check it:

```sh
~/code/meeting-recorder/mrec status
```

Stop it:

```sh
~/code/meeting-recorder/mrec stop
```

If you set environment variables such as `MEETING_RECORDER_AUDIO_DEVICE`, run `mrec start` again so the LaunchAgent plist is rewritten with the current values.

## Menu Bar

An xbar/SwiftBar-compatible menu plugin is included at:

```sh
~/code/meeting-recorder/menu-bar/meeting-recorder.1m.py
```

It is installed as:

```sh
~/Library/Application Support/xbar/plugins/meeting-recorder.1m.py
```

The menu shows:

- clock `Waiting`: watcher running, no meeting in progress
- filled record circle `Rec <elapsed>`: a recording is running
- `Transcribing…`: cleaning up the last meeting
- waveform-slash `Off`: watcher stopped
- **which engine the next transcription will use**, plus the OpenRouter balance (`Transcribes with: gemini-2.5-flash · $7.33 left`), turning red when it has degraded to Whisper or the balance is nearly out
- the **last recording** and **last transcript**, each with their age and a one-click action to play / open them

Menu actions:

- start/stop the login watcher
- start a manual recording
- stop the manual recording and transcribe it
- play the last recording / open the last transcript (rendered in the browser)
- open recordings
- open logs
- run doctor

## Meeting Names

Recordings are named after the meeting, best effort, in this order:

1. **Calendar** — the Google Calendar event happening right now, via the `gog`
   CLI. This gives the real human title (`Danil / Nick - 1 on 1`) instead of the
   generic tab (`Meet - abc-defg-hij`). A video event whose Meet room code matches
   an open tab wins; otherwise any event with a video link, otherwise the
   most-recently-started event overlapping now (with a few minutes' grace so
   joining early or tripping late still matches). Both of Nick's orgs (mantis +
   finant) are consulted by default.
2. **Tab title** — the browser tab title with platform noise, unread badges and
   Meet room codes stripped (`Weekly Sync | Google Meet` → `Weekly Sync`).
3. **Platform label** — `Google Meet` / `Zoom` / … when nothing better is known.

The lookup is best effort and never blocks recording for long: if `gog` is
missing, offline, or slow (past `MEETING_RECORDER_CALENDAR_TIMEOUT`), it silently
falls back to the tab title. Set `MEETING_RECORDER_CALENDAR=0` to skip the
calendar step entirely.

## Configuration

Environment variables:

- `MEETING_RECORDER_CAPTURE_BACKEND`: `auto` (default — ScreenCaptureKit system+mic capture, falling back to ffmpeg if it can't build/run), `screencapturekit`/`sck` (force ScreenCaptureKit, error instead of falling back), or `ffmpeg` (force the legacy loopback path).
- `MEETING_RECORDER_SCK_NO_MIC`: set to `1` to capture system audio only in the ScreenCaptureKit path (skip the microphone).
- `MEETING_RECORDER_SCK_BIN`: path to the compiled `sck-recorder` helper. Default: `bin/sck-recorder` next to the script (built on demand).
- `MEETING_RECORDER_AUDIO_DEVICE`: AVFoundation audio index or name (ffmpeg backend only). Default: auto — a loopback/aggregate device (BlackHole, Aggregate, Loopback, …) if one is present, otherwise index `0`. Prefer a **name** over an index: AVFoundation indices are not stable across reboots/device changes, so `0` can silently become a webcam mic instead of your built-in mic.
- `MEETING_RECORDER_SAMPLE_RATE`: output WAV sample rate. Default: `48000`.
- `MEETING_RECORDER_AUDIO_SYNC`: keep recordings at real-time length. Default: `1`. ffmpeg's avfoundation capture under-delivers samples (~12% even on a bare mic, worse under load), which time-compresses audio and drifts timestamps; this pads genuine capture gaps with silence via wall-clock timestamps + async resampling. Set to `0` to disable.
- `MEETING_RECORDER_ALLOW_MIC_ONLY`: set to `1` to silence the warning shown when recording a plain microphone instead of a loopback device.
- `MEETING_RECORDER_DIR`: output directory. Default: `~/Meetings/Recordings`.
- `MEETING_RECORDER_LOG`: log path. Default: `~/Library/Logs/meeting-recorder.log`.
- `MEETING_RECORDER_TRANSCRIBE_ENGINE`: `openrouter` (default) or `whisper` (force local-only). OpenRouter falls back to Whisper on any key/network/credit/API error.
- `MEETING_RECORDER_OPENROUTER_MODEL`: OpenRouter audio model. Default: `google/gemini-2.5-flash` (try `google/gemini-2.5-flash-lite` for ~3× cheaper).
- `MEETING_RECORDER_OPENROUTER_API_KEY` / `OPENROUTER_API_KEY`: the key. If neither is set, `MEETING_RECORDER_OPENROUTER_ENV_FILE` is read.
- `MEETING_RECORDER_OPENROUTER_ENV_FILE`: optional dotenv file to read `OPENROUTER_API_KEY` from (e.g. `~/code/ant/.env`).
- `MEETING_RECORDER_LOW_CREDIT_USD`: warn in `mrec engine` / `doctor` / the menu bar when the OpenRouter balance drops below this. At `$0` the API returns 402 and every transcription silently falls back to Whisper. Default: `2`. Set to `0` to disable.
- `MEETING_RECORDER_OPENROUTER_CREDITS_TIMEOUT`: timeout for the `/credits` balance check. Default: `10`s.
- `MEETING_RECORDER_OPENROUTER_CHUNK_SECONDS`: audio chunk length sent per request. Default: `600`.
- `MEETING_RECORDER_OPENROUTER_TIMEOUT` / `MEETING_RECORDER_OPENROUTER_MAX_RETRIES`: per-request timeout (`300`s) and retry count (`3`).
- `MEETING_RECORDER_OPENROUTER_PROMPT`: override the transcription instruction.
- `MEETING_RECORDER_SILENCE_DB`: mean dBFS at/below which a recording is treated as silent and transcription aborts with an actionable error. Default: `-80`. Empty disables.
- `MEETING_RECORDER_WHISPER_MODEL`: Whisper model (local engine / fallback). Default: `turbo`.
- `MEETING_RECORDER_LANGUAGE`: optional transcription language (both engines).
- `MEETING_RECORDER_CONDITION_ON_PREVIOUS_TEXT`: `True`/`False`. Default: `False`. Keeping this `False` stops Whisper repeating the previous line (the "Thank you… Thank you…" loops) across silences.
- `MEETING_RECORDER_NO_SPEECH_THRESHOLD`: probability above which a segment is treated as silence and dropped. Default: `0.6`.
- `MEETING_RECORDER_HALLUCINATION_SILENCE_THRESHOLD`: seconds — skip silent stretches longer than this when a hallucination is detected (needs word timestamps, which the tool enables automatically). Default: `2`. Set to empty to disable.
- `MEETING_RECORDER_NO_LOGO`: set to `1` to post notifications via osascript (generic icon) instead of the native notifier app. See **Notification Logo**.
- `MEETING_RECORDER_DISABLE_CLAUDE`: set to `1` to skip Claude cleanup.
- `MEETING_RECORDER_CLAUDE_MODEL`: optional Claude model alias.
- `MEETING_RECORDER_DIARIZE`: set to `1` to enable acoustic speaker diarization via whisperx (needs whisperx + `HF_TOKEN`). See **Speaker Labels**.
- `MEETING_RECORDER_CALENDAR`: set to `0` to skip the calendar lookup when naming a recording (see **Meeting Names**). Default: `1`.
- `MEETING_RECORDER_CALENDAR_ACCOUNTS`: comma-separated `client=account` pairs passed to `gog` for the calendar lookup (either side may be blank for gog's default). Default: `=,finant=nick@finant.ai` (mantis + finant).
- `MEETING_RECORDER_CALENDAR_TIMEOUT`: seconds each calendar query may take before falling back to the tab title. Default: `6`.
- `MEETING_RECORDER_CALENDAR_START_GRACE_MIN` / `MEETING_RECORDER_CALENDAR_END_GRACE_MIN`: minutes of slack before an event starts / after it ends that still count as "now". Defaults: `10` / `5`.
- `MEETING_RECORDER_GOG_BIN`: path to the `gog` CLI used for the calendar lookup. Default: `gog`.
- `MEETING_RECORDER_POLL_SECONDS`: meeting detection interval. Default: `10`.
- `MEETING_RECORDER_END_GRACE_SECONDS`: time to wait after meeting disappears before stopping. Default: `45`.
- `MEETING_RECORDER_CHECK_IN_SECONDS`: while recording, ask whether to keep going after this many seconds. Default: `1800` (30 minutes). Set to `0` to disable. Dismissing or ignoring this prompt now **keeps recording** — only clicking "Stop and transcribe" stops it, so an unanswered check-in can no longer cut a meeting short.

## Recording Reliability

The tool records new meetings as `.wav` rather than `.m4a`. WAV files are larger, but they are much safer for long recordings because they remain easier to recover if the process is stopped unexpectedly. Older `.m4a` recordings without a finalized MP4 `moov` atom may not be transcribable.

### Capture the whole meeting, not just your mic

If `MEETING_RECORDER_AUDIO_DEVICE` points at a plain microphone (or falls back to the default), the recording captures **your side clearly and the far side barely** — the other participants only reach the mic as faint speaker bleed. Worse, a browser meeting and `ffmpeg` both holding the same built-in mic can drop a large fraction of the audio. Run `mrec doctor`: it now reports whether the selected device is a loopback (records everyone) or microphone-only (warns), and the watcher posts a notification if it starts a mic-only recording. For full coverage, follow **Audio Setup** above to create a BlackHole aggregate device and point `MEETING_RECORDER_AUDIO_DEVICE` at it by name.
