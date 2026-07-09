# Meeting Recorder

Local macOS meeting recorder:

1. watches for Google Meet, Teams, Zoom, Webex, or Whereby in browsers/apps
2. asks before recording
3. records the configured macOS audio input with `ffmpeg`
4. transcribes with local `whisper`
5. optionally asks `claude -p` to clean the raw transcript into notes

`claude -p` is not used as the speech recognizer. Whisper does the audio transcription. Claude is only the cleanup/summary pass.

## Commands

```sh
~/code/meeting-recorder/mrec doctor
~/code/meeting-recorder/mrec watch
~/code/meeting-recorder/mrec record test-meeting
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

## Configuration

Environment variables:

- `MEETING_RECORDER_AUDIO_DEVICE`: AVFoundation audio index or name. Default: `0`.
- `MEETING_RECORDER_DIR`: output directory. Default: `~/Meetings/Recordings`.
- `MEETING_RECORDER_LOG`: log path. Default: `~/Library/Logs/meeting-recorder.log`.
- `MEETING_RECORDER_WHISPER_MODEL`: Whisper model. Default: `turbo`.
- `MEETING_RECORDER_LANGUAGE`: optional Whisper language.
- `MEETING_RECORDER_DISABLE_CLAUDE`: set to `1` to skip Claude cleanup.
- `MEETING_RECORDER_CLAUDE_MODEL`: optional Claude model alias.
- `MEETING_RECORDER_POLL_SECONDS`: meeting detection interval. Default: `10`.
- `MEETING_RECORDER_END_GRACE_SECONDS`: time to wait after meeting disappears before stopping. Default: `45`.
