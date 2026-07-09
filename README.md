# Meeting Recorder

Local macOS meeting recorder:

1. watches for Google Meet, Teams, Zoom, Webex, or Whereby in browsers/apps
2. asks before recording
3. records the configured macOS audio input with `ffmpeg` as `.wav`
4. transcribes with local `whisper`
5. optionally asks `claude -p` to clean the raw transcript into notes

`claude -p` is not used as the speech recognizer. Whisper does the audio transcription. Claude is only the cleanup/summary pass.

## Commands

```sh
~/code/meeting-recorder/mrec doctor
~/code/meeting-recorder/mrec watch
~/code/meeting-recorder/mrec record test-meeting
~/code/meeting-recorder/mrec record-start menubar-manual
~/code/meeting-recorder/mrec record-stop
~/code/meeting-recorder/mrec transcribe ~/Meetings/Recordings/example.m4a
```

Recordings and transcripts are written under:

```sh
~/Meetings/Recordings
```

Logs are written to:

```sh
~/Library/Logs/meeting-recorder.log
```

## Audio Setup

For "whatever goes into my ears and into the meeting", macOS needs a loopback or aggregate input device. The usual setup is:

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

- waveform `MR`: watcher state / idle
- filled record circle `REC`: manual recording running

Menu actions:

- start/stop the login watcher
- start a manual recording
- stop the manual recording and transcribe it
- open recordings
- open logs
- run doctor

## Configuration

Environment variables:

- `MEETING_RECORDER_AUDIO_DEVICE`: AVFoundation audio index or name. Default: auto — a loopback/aggregate device (BlackHole, Aggregate, Loopback, …) if one is present, otherwise index `0`. Prefer a **name** over an index: AVFoundation indices are not stable across reboots/device changes, so `0` can silently become a webcam mic instead of your built-in mic.
- `MEETING_RECORDER_SAMPLE_RATE`: output WAV sample rate. Default: `48000`.
- `MEETING_RECORDER_ALLOW_MIC_ONLY`: set to `1` to silence the warning shown when recording a plain microphone instead of a loopback device.
- `MEETING_RECORDER_DIR`: output directory. Default: `~/Meetings/Recordings`.
- `MEETING_RECORDER_LOG`: log path. Default: `~/Library/Logs/meeting-recorder.log`.
- `MEETING_RECORDER_WHISPER_MODEL`: Whisper model. Default: `turbo`.
- `MEETING_RECORDER_LANGUAGE`: optional Whisper language.
- `MEETING_RECORDER_CONDITION_ON_PREVIOUS_TEXT`: `True`/`False`. Default: `False`. Keeping this `False` stops Whisper repeating the previous line (the "Thank you… Thank you…" loops) across silences.
- `MEETING_RECORDER_NO_SPEECH_THRESHOLD`: probability above which a segment is treated as silence and dropped. Default: `0.6`.
- `MEETING_RECORDER_HALLUCINATION_SILENCE_THRESHOLD`: seconds — skip silent stretches longer than this when a hallucination is detected (needs word timestamps, which the tool enables automatically). Default: `2`. Set to empty to disable.
- `MEETING_RECORDER_DISABLE_CLAUDE`: set to `1` to skip Claude cleanup.
- `MEETING_RECORDER_CLAUDE_MODEL`: optional Claude model alias.
- `MEETING_RECORDER_POLL_SECONDS`: meeting detection interval. Default: `10`.
- `MEETING_RECORDER_END_GRACE_SECONDS`: time to wait after meeting disappears before stopping. Default: `45`.
- `MEETING_RECORDER_CHECK_IN_SECONDS`: while recording, ask whether to keep going after this many seconds. Default: `1800` (30 minutes). Set to `0` to disable. Dismissing or ignoring this prompt now **keeps recording** — only clicking "Stop and transcribe" stops it, so an unanswered check-in can no longer cut a meeting short.

## Recording Reliability

The tool records new meetings as `.wav` rather than `.m4a`. WAV files are larger, but they are much safer for long recordings because they remain easier to recover if the process is stopped unexpectedly. Older `.m4a` recordings without a finalized MP4 `moov` atom may not be transcribable.

### Capture the whole meeting, not just your mic

If `MEETING_RECORDER_AUDIO_DEVICE` points at a plain microphone (or falls back to the default), the recording captures **your side clearly and the far side barely** — the other participants only reach the mic as faint speaker bleed. Worse, a browser meeting and `ffmpeg` both holding the same built-in mic can drop a large fraction of the audio. Run `mrec doctor`: it now reports whether the selected device is a loopback (records everyone) or microphone-only (warns), and the watcher posts a notification if it starts a mic-only recording. For full coverage, follow **Audio Setup** above to create a BlackHole aggregate device and point `MEETING_RECORDER_AUDIO_DEVICE` at it by name.
