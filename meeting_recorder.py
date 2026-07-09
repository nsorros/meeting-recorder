#!/usr/bin/env python3
"""Background meeting recorder for macOS.

Detects browser/app meetings, asks before recording, records a configured
audio input with ffmpeg, transcribes with Whisper, then optionally cleans the
transcript with `claude -p`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(os.environ.get("MEETING_RECORDER_DIR", "~/Meetings/Recordings")).expanduser()
LOG = Path(os.environ.get("MEETING_RECORDER_LOG", "~/Library/Logs/meeting-recorder.log")).expanduser()
LAUNCH_AGENT_LABEL = "com.nsorros.meeting-recorder"
LAUNCH_AGENT_PATH = Path(f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist").expanduser()
AUDIO_DEVICE = os.environ.get("MEETING_RECORDER_AUDIO_DEVICE", "").strip()
POLL_SECONDS = int(os.environ.get("MEETING_RECORDER_POLL_SECONDS", "10"))
END_GRACE_SECONDS = int(os.environ.get("MEETING_RECORDER_END_GRACE_SECONDS", "45"))
WHISPER_MODEL = os.environ.get("MEETING_RECORDER_WHISPER_MODEL", "turbo")
LANGUAGE = os.environ.get("MEETING_RECORDER_LANGUAGE", "")
CLAUDE_MODEL = os.environ.get("MEETING_RECORDER_CLAUDE_MODEL", "")
DISABLE_CLAUDE = os.environ.get("MEETING_RECORDER_DISABLE_CLAUDE", "").lower() in {"1", "true", "yes"}

BROWSER_APPS = {
    "Google Chrome": "chrome",
    "Chromium": "chrome",
    "Brave Browser": "chrome",
    "Microsoft Edge": "chrome",
    "Arc": "chrome",
    "Safari": "safari",
}

MEETING_HINTS = (
    "meet.google.com",
    "teams.microsoft.com",
    "zoom.us/wc",
    "app.zoom.us",
    "webex.com",
    "whereby.com",
)

PROCESS_HINTS = (
    ("zoom.us", "Zoom"),
    ("Microsoft Teams", "Microsoft Teams"),
    ("com.microsoft.teams", "Microsoft Teams"),
)

TITLE_HINTS = (
    "Google Meet",
    "Meet - ",
    "Zoom Meeting",
    "Microsoft Teams",
    "Teams meeting",
)


def display_path(path: Path) -> str:
    try:
        home = Path.home()
        return "~/" + str(path.resolve().relative_to(home))
    except Exception:
        return str(path)


def log(message: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")


def log_section(title: str, **fields: object) -> None:
    log(f"--- {title} ---")
    for key, value in fields.items():
        log(f"{key}: {value}")


def run(cmd: list[str], *, timeout: int = 20, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=check)


def command_exists(name: str) -> bool:
    return subprocess.run(["/usr/bin/env", "sh", "-c", f"command -v {shlex.quote(name)}"],
                          capture_output=True).returncode == 0


def osascript(script: str, timeout: int = 20) -> str:
    proc = run(["osascript", "-e", script], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout.strip()


def notify(title: str, text: str) -> None:
    try:
        osascript(f'display notification {applescript_quote(text)} with title {applescript_quote(title)}')
    except Exception as exc:
        log(f"notification failed: {exc}")


def alert(title: str, text: str) -> None:
    try:
        osascript(
            "display alert "
            + applescript_quote(title)
            + " message "
            + applescript_quote(text)
            + ' as informational buttons {"OK"} default button "OK"',
            timeout=20,
        )
    except Exception as exc:
        log(f"alert failed: {exc}")


def applescript_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def ask_to_record(reason: str) -> bool:
    message = (
        f"I found what looks like a live meeting:\n\n{reason}\n\n"
        "Do you want me to record it?\n\n"
        "When the meeting ends, I will save the audio, run Whisper transcription, "
        "and optionally clean the notes with Claude."
    )
    script = (
        "display dialog "
        + applescript_quote(message)
        + ' buttons {"Not this meeting", "Start recording"} default button "Start recording" '
          'cancel button "Not this meeting" with title "Meeting Recorder" giving up after 30'
    )
    log_section("meeting detected", reason=reason)
    try:
        out = osascript(script, timeout=35)
    except Exception as exc:
        log(f"record prompt failed or declined: {exc}")
        return False
    accepted = "button returned:Start recording" in out and "gave up:true" not in out
    log(f"user response: {'accepted recording' if accepted else 'declined or timed out'}")
    return accepted


def active_processes() -> list[str]:
    proc = run(["ps", "axo", "comm="], timeout=10)
    if proc.returncode != 0:
        return []
    return [Path(line.strip()).name for line in proc.stdout.splitlines() if line.strip()]


def browser_tabs() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for app, kind in BROWSER_APPS.items():
        if kind == "chrome":
            script = f'''
                if application {applescript_quote(app)} is running then
                  tell application {applescript_quote(app)}
                    set rows to {{}}
                    repeat with w in windows
                      repeat with t in tabs of w
                        set end of rows to (URL of t as text) & " ||| " & (title of t as text)
                      end repeat
                    end repeat
                    return rows as text
                  end tell
                end if
            '''
        else:
            script = f'''
                if application {applescript_quote(app)} is running then
                  tell application {applescript_quote(app)}
                    set rows to {{}}
                    repeat with w in windows
                      repeat with t in tabs of w
                        set end of rows to (URL of t as text) & " ||| " & (name of t as text)
                      end repeat
                    end repeat
                    return rows as text
                  end tell
                end if
            '''
        try:
            out = osascript(script, timeout=8)
        except Exception as exc:
            log(f"could not inspect {app}: {exc}")
            continue
        for row in re.split(r",\s*", out):
            if " ||| " in row:
                url, title = row.split(" ||| ", 1)
                found.append((app, url.strip(), title.strip()))
    return found


def detect_meeting() -> str | None:
    for app, url, title in browser_tabs():
        haystack = f"{url} {title}".lower()
        if any(hint.lower() in haystack for hint in MEETING_HINTS):
            return f"{app}: {title or url}"
        if any(hint.lower() in haystack for hint in TITLE_HINTS):
            return f"{app}: {title or url}"

    processes = active_processes()
    for process in processes:
        for hint, label in PROCESS_HINTS:
            if hint.lower() in process.lower():
                return label
    return None


def list_audio_devices() -> str:
    proc = run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""], timeout=20)
    return (proc.stderr + proc.stdout).strip()


def visible_audio_devices(devices: str) -> list[str]:
    in_audio = False
    found: list[str] = []
    for line in devices.splitlines():
        lower = line.lower()
        if "avfoundation audio devices:" in lower:
            in_audio = True
            continue
        if "avfoundation video devices:" in lower:
            in_audio = False
            continue
        if in_audio and re.search(r"\[\d+\]\s+.+", line):
            found.append(line.strip())
    return found


def audio_device_arg() -> str:
    if AUDIO_DEVICE:
        return f":{AUDIO_DEVICE}"
    return ":0"


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return value[:80] or "meeting"


def start_recording(reason: str) -> tuple[subprocess.Popen[bytes], Path]:
    ROOT.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = ROOT / f"{now}_{slug(reason)}.m4a"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-f",
        "avfoundation",
        "-i",
        audio_device_arg(),
        "-vn",
        "-acodec",
        "aac",
        "-b:a",
        "128k",
        str(path),
    ]
    log_section(
        "recording started",
        meeting=reason,
        audio_file=display_path(path),
        audio_input=audio_device_arg(),
    )
    log("ffmpeg command: " + shlex.join(cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(3)
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        raise RuntimeError(f"ffmpeg exited early: {stderr[-2000:]}")
    return proc, path


def stop_recording(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        log("stopping recording")
        if proc.stdin:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
        proc.wait(timeout=12)
    except Exception:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()


def transcribe_audio(audio: Path) -> Path:
    out_dir = audio.with_suffix("")
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["whisper", str(audio), "--model", WHISPER_MODEL, "--output_dir", str(out_dir), "--output_format", "all"]
    if LANGUAGE:
        cmd.extend(["--language", LANGUAGE])
    log_section("transcription started", audio_file=display_path(audio), output_dir=display_path(out_dir), engine="whisper")
    log("whisper command: " + shlex.join(cmd))
    proc = subprocess.run(cmd, text=True, capture_output=True)
    (out_dir / "whisper.log").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"whisper failed; see {out_dir / 'whisper.log'}")

    raw_txt = out_dir / f"{audio.stem}.txt"
    if not raw_txt.exists():
        candidates = list(out_dir.glob("*.txt"))
        if not candidates:
            raise RuntimeError(f"whisper produced no txt output in {out_dir}")
        raw_txt = candidates[0]

    final_md = out_dir / f"{audio.stem}.meeting.md"
    if DISABLE_CLAUDE:
        log("claude cleanup disabled; writing basic markdown transcript")
        write_basic_meeting_md(raw_txt, final_md, audio)
    else:
        clean_with_claude(raw_txt, final_md, audio)
    log_section("transcription finished", final_notes=display_path(final_md), raw_transcript=display_path(raw_txt))
    return final_md


def write_basic_meeting_md(raw_txt: Path, final_md: Path, audio: Path) -> None:
    final_md.write_text(
        f"# Meeting Transcript\n\nAudio: `{audio}`\n\nRaw transcript: `{raw_txt}`\n\n"
        + raw_txt.read_text(encoding="utf-8", errors="replace"),
        encoding="utf-8",
    )


def clean_with_claude(raw_txt: Path, final_md: Path, audio: Path) -> None:
    prompt = f"""
You are cleaning a machine-generated transcript of a meeting.

Input audio path: {audio}
Raw transcript path: {raw_txt}

Produce markdown with:
- Title inferred from content if possible
- Date/time from the filename if useful
- Cleaned transcript, preserving meaning and uncertainty
- Decisions
- Action items with owner if identifiable
- Open questions

Do not invent details not supported by the raw transcript.

Raw transcript:
{raw_txt.read_text(encoding="utf-8", errors="replace")}
""".strip()
    cmd = ["claude", "-p"]
    if CLAUDE_MODEL:
        cmd.extend(["--model", CLAUDE_MODEL])
    log_section("claude cleanup started", raw_transcript=display_path(raw_txt), final_notes=display_path(final_md))
    proc = subprocess.run(cmd, input=prompt, text=True, capture_output=True, timeout=60 * 30)
    (final_md.with_suffix(".claude.log")).write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        log(f"claude cleanup failed with exit code {proc.returncode}; writing fallback transcript")
        final_md.write_text(
            "# Claude cleanup failed\n\n"
            f"Audio: `{audio}`\n\nRaw transcript: `{raw_txt}`\n\n"
            "```text\n" + raw_txt.read_text(encoding="utf-8", errors="replace") + "\n```\n",
            encoding="utf-8",
        )
        return
    final_md.write_text(proc.stdout, encoding="utf-8")


def watch() -> None:
    log_section(
        "watcher started",
        recordings=display_path(ROOT),
        log=display_path(LOG),
        poll_seconds=POLL_SECONDS,
        end_grace_seconds=END_GRACE_SECONDS,
        audio_input=audio_device_arg(),
        whisper_model=WHISPER_MODEL,
        claude_cleanup="disabled" if DISABLE_CLAUDE else "enabled",
    )
    notify("Meeting Recorder", "Watching for Meet, Zoom, Teams, Webex, and Whereby.")
    active = False
    declined_reason: str | None = None
    while True:
        reason = detect_meeting()
        if not reason:
            declined_reason = None
            time.sleep(POLL_SECONDS)
            continue
        if declined_reason == reason:
            time.sleep(POLL_SECONDS)
            continue
        if not ask_to_record(reason):
            declined_reason = reason
            notify("Meeting Recorder", "Okay, I will ignore this meeting until it disappears.")
            time.sleep(POLL_SECONDS)
            continue

        active = True
        proc: subprocess.Popen[bytes] | None = None
        audio: Path | None = None
        try:
            proc, audio = start_recording(reason)
            notify("Meeting Recorder", f"Recording started. Audio will save to {display_path(audio)}")
            last_seen = time.time()
            while True:
                current = detect_meeting()
                if current:
                    last_seen = time.time()
                elif time.time() - last_seen >= END_GRACE_SECONDS:
                    log(f"meeting no longer detected for {END_GRACE_SECONDS}s; stopping recording")
                    break
                time.sleep(POLL_SECONDS)
        except Exception as exc:
            log(f"recording error: {exc}")
            alert(
                "Meeting Recorder could not start recording",
                f"{exc}\n\nRun ~/code/meeting-recorder/mrec doctor to check audio permissions and devices.",
            )
        finally:
            if proc:
                stop_recording(proc)
            active = False

        if audio and audio.exists() and audio.stat().st_size > 1024:
            try:
                notify("Meeting Recorder", "Meeting ended. Transcribing now.")
                final = transcribe_audio(audio)
                alert(
                    "Meeting transcript ready",
                    f"Saved notes:\n{display_path(final)}\n\nAudio:\n{display_path(audio)}",
                )
                log(f"transcript saved: {final}")
            except Exception as exc:
                log(f"transcription error: {exc}")
                alert(
                    "Meeting transcription failed",
                    f"{exc}\n\nThe audio may still be saved at:\n{display_path(audio)}\n\nLog:\n{display_path(LOG)}",
                )
        elif audio:
            log(f"skipping transcription; missing or tiny audio file: {audio}")

        if active:
            time.sleep(POLL_SECONDS)


def record_once(label: str) -> None:
    proc, audio = start_recording(label)
    print(f"Recording to {display_path(audio)}")
    print("Press Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_recording(proc)
        print(f"\nSaved {display_path(audio)}")
        print("Transcribing with Whisper...")
        try:
            final = transcribe_audio(audio)
        except KeyboardInterrupt:
            print("\nTranscription interrupted. The audio is still saved.")
            print(f"Audio: {display_path(audio)}")
            return
        except Exception as exc:
            print(f"\nTranscription failed: {exc}")
            print(f"Audio: {display_path(audio)}")
            print(f"Log: {display_path(LOG)}")
            return
        print(f"Transcript: {display_path(final)}")


def doctor() -> int:
    ok = True
    for binary in ("ffmpeg", "whisper", "claude", "osascript"):
        exists = command_exists(binary)
        print(f"{binary}: {'ok' if exists else 'missing'}")
        ok = ok and exists
    print(f"recordings: {ROOT}")
    print(f"log: {LOG}")
    print(f"audio input: {audio_device_arg()} ({'MEETING_RECORDER_AUDIO_DEVICE' if AUDIO_DEVICE else 'default index 0'})")
    print("\nAVFoundation devices:")
    devices = list_audio_devices()
    print(devices or "(none visible)")
    audio_devices = visible_audio_devices(devices)
    if not audio_devices:
        print("\nffmpeg cannot see audio devices. Grant microphone permission to Terminal/iTerm and rerun doctor.")
        ok = False
    if "BlackHole" not in devices and "Loopback" not in devices and "VB-Cable" not in devices:
        print("\nNo loopback device was obvious. For speaker + mic capture, create an Aggregate Device with BlackHole and your mic, then set MEETING_RECORDER_AUDIO_DEVICE to its AVFoundation index or name.")
    return 0 if ok else 1


def install_launch_agent() -> Path:
    plist = LAUNCH_AGENT_PATH
    script = Path(__file__).resolve()
    env_vars = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/nsorros/.local/bin:/Users/nsorros/.pyenv/shims",
        "MEETING_RECORDER_DIR": str(ROOT),
        "MEETING_RECORDER_LOG": str(LOG),
        "MEETING_RECORDER_WHISPER_MODEL": WHISPER_MODEL,
        "MEETING_RECORDER_POLL_SECONDS": str(POLL_SECONDS),
        "MEETING_RECORDER_END_GRACE_SECONDS": str(END_GRACE_SECONDS),
    }
    if AUDIO_DEVICE:
        env_vars["MEETING_RECORDER_AUDIO_DEVICE"] = AUDIO_DEVICE
    if LANGUAGE:
        env_vars["MEETING_RECORDER_LANGUAGE"] = LANGUAGE
    if CLAUDE_MODEL:
        env_vars["MEETING_RECORDER_CLAUDE_MODEL"] = CLAUDE_MODEL
    if DISABLE_CLAUDE:
        env_vars["MEETING_RECORDER_DISABLE_CLAUDE"] = "1"

    env_xml = "\n".join(
        f"    <key>{escape(key)}</key>\n    <string>{escape(value)}</string>"
        for key, value in env_vars.items()
    )
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCH_AGENT_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sys.executable}</string>
    <string>{script}</string>
    <string>watch</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{LOG}</string>
  <key>StandardErrorPath</key>
  <string>{LOG}</string>
  <key>EnvironmentVariables</key>
  <dict>
{env_xml}
  </dict>
</dict>
</plist>
""", encoding="utf-8")
    return plist


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def launch_service() -> str:
    return f"{launch_domain()}/{LAUNCH_AGENT_LABEL}"


def start_launch_agent() -> int:
    plist = install_launch_agent()
    subprocess.run(["launchctl", "bootout", launch_domain(), str(plist)], capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", launch_domain(), str(plist)], text=True, capture_output=True)
    if proc.returncode != 0:
        print(proc.stderr.strip() or proc.stdout.strip(), file=sys.stderr)
        print(f"Failed to start {LAUNCH_AGENT_LABEL}. Plist: {display_path(plist)}", file=sys.stderr)
        return proc.returncode
    subprocess.run(["launchctl", "enable", launch_service()], capture_output=True)
    subprocess.run(["launchctl", "kickstart", "-k", launch_service()], capture_output=True)
    print(f"Started {LAUNCH_AGENT_LABEL}")
    print(f"It will also start automatically at login.")
    print(f"Log: {display_path(LOG)}")
    return 0


def stop_launch_agent() -> int:
    proc = subprocess.run(["launchctl", "bootout", launch_domain(), str(LAUNCH_AGENT_PATH)], text=True, capture_output=True)
    if proc.returncode != 0 and "No such process" not in proc.stderr:
        print(proc.stderr.strip() or proc.stdout.strip(), file=sys.stderr)
        return proc.returncode
    print(f"Stopped {LAUNCH_AGENT_LABEL}")
    return 0


def status_launch_agent() -> int:
    proc = subprocess.run(["launchctl", "print", launch_service()], text=True, capture_output=True)
    if proc.returncode != 0:
        print(f"{LAUNCH_AGENT_LABEL}: not running")
        print(f"Plist: {'installed' if LAUNCH_AGENT_PATH.exists() else 'not installed'} at {display_path(LAUNCH_AGENT_PATH)}")
        return 1
    print(f"{LAUNCH_AGENT_LABEL}: running")
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith(("state =", "pid =", "last exit code =")):
            print(stripped)
    print(f"Log: {display_path(LOG)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect, record, and transcribe meetings on macOS.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("watch", help="run the background watcher in the foreground")
    sub.add_parser("doctor", help="check dependencies and visible audio devices")
    rec = sub.add_parser("record", help="record immediately until Ctrl-C")
    rec.add_argument("label", nargs="?", default="manual-meeting")
    tr = sub.add_parser("transcribe", help="transcribe an existing audio file")
    tr.add_argument("audio")
    sub.add_parser("install-launch-agent", help="write the LaunchAgent plist")
    sub.add_parser("start", help="install and start the login background watcher")
    sub.add_parser("stop", help="stop the login background watcher")
    sub.add_parser("status", help="show whether the login background watcher is running")
    args = parser.parse_args()

    if args.command == "watch":
        watch()
        return 0
    if args.command == "doctor":
        return doctor()
    if args.command == "record":
        record_once(args.label)
        return 0
    if args.command == "transcribe":
        print(transcribe_audio(Path(args.audio).expanduser()))
        return 0
    if args.command == "install-launch-agent":
        print(install_launch_agent())
        return 0
    if args.command == "start":
        return start_launch_agent()
    if args.command == "stop":
        return stop_launch_agent()
    if args.command == "status":
        return status_launch_agent()
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
