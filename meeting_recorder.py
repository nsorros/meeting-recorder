#!/usr/bin/env python3
"""Background meeting recorder for macOS.

Detects browser/app meetings, asks before recording, records a configured
audio input with ffmpeg, transcribes with OpenRouter (cloud; fast and cheap)
falling back to local Whisper when there is no key/network/credits, then
optionally cleans the transcript with `claude -p`.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(os.environ.get("MEETING_RECORDER_DIR", "~/Meetings/Recordings")).expanduser()
LOG = Path(os.environ.get("MEETING_RECORDER_LOG", "~/Library/Logs/meeting-recorder.log")).expanduser()
LAUNCH_AGENT_LABEL = "com.nsorros.meeting-recorder"
LAUNCH_AGENT_PATH = Path(f"~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist").expanduser()
STATE_DIR = Path(os.environ.get("MEETING_RECORDER_STATE_DIR", "~/.local/state/meeting-recorder")).expanduser()
MANUAL_PID_FILE = STATE_DIR / "manual-recording.pid"
# Published so the menu bar can show a real state (recording / transcribing /
# watching) instead of a static label. Written by the watcher, read by the plugin.
WATCHER_STATUS_FILE = STATE_DIR / "watcher-status"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
# Notification logo: osascript notifications are locked to the generic script
# icon, so when terminal-notifier is installed we route through it with -appIcon
# to show the Meeting Recorder mic logo. `mrec install-app` sets this up.
NOTIFIER_BUNDLE_ID = "com.nsorros.meeting-recorder"
# A tiny native Swift app (notifier/main.swift) posts notifications via the modern
# UserNotifications framework, so they carry its bundle icon (our mic logo) on the
# left — which osascript/terminal-notifier cannot do on current macOS. Built by
# `mrec install-app`. Set MEETING_RECORDER_NO_LOGO=1 to force plain osascript.
NOTIFIER_SRC = Path(__file__).resolve().parent / "notifier" / "main.swift"
NOTIFIER_APP = Path("~/Applications/Meeting Recorder Notifier.app").expanduser()
NOTIFIER_BIN = NOTIFIER_APP / "Contents" / "MacOS" / "notifier"
USE_LOGO = os.environ.get("MEETING_RECORDER_NO_LOGO", "").lower() not in {"1", "true", "yes"}
AUDIO_DEVICE = os.environ.get("MEETING_RECORDER_AUDIO_DEVICE", "").strip()
# Capture sample rate for the output WAV. The bare built-in mic sometimes gets
# negotiated down to 24 kHz with dropouts; pinning the output rate keeps files
# consistent. Default 48 kHz (native for most devices and loopbacks).
SAMPLE_RATE = os.environ.get("MEETING_RECORDER_SAMPLE_RATE", "48000").strip()
# Substrings that identify a loopback/aggregate device able to capture the
# meeting audio (both sides), as opposed to a bare microphone that only hears
# the local speaker. Used to auto-pick a device and to warn in doctor.
LOOPBACK_HINTS = ("blackhole", "aggregate", "loopback", "soundflower", "vb-cable", "vb-audio", "multi-output", "existential")
# ffmpeg's avfoundation capture under-delivers samples (~12% even on a bare mic,
# worse under load), which time-compresses audio and drifts timestamps. Wall-clock
# input timestamps + async resampling pad genuine capture gaps with silence so the
# recording keeps real-time length and honest timing. Set to 0 to disable.
AUDIO_SYNC = os.environ.get("MEETING_RECORDER_AUDIO_SYNC", "1").lower() not in {"0", "false", "no"}
# Set to 1/true to silence the "recording microphone only" warning.
ALLOW_MIC_ONLY = os.environ.get("MEETING_RECORDER_ALLOW_MIC_ONLY", "").lower() in {"1", "true", "yes"}
# --- Capture backend -----------------------------------------------------
# "auto" (default): capture system audio + mic via ScreenCaptureKit (a native
#   macOS API — no BlackHole, no Multi-Output device, survives reboots), and
#   fall back to the ffmpeg/avfoundation loopback path if the helper can't build
#   or run (e.g. Screen Recording permission not yet granted).
# "screencapturekit"/"sck": force ScreenCaptureKit; error out instead of falling back.
# "ffmpeg": force the legacy ffmpeg + loopback/aggregate device path.
CAPTURE_BACKEND = os.environ.get("MEETING_RECORDER_CAPTURE_BACKEND", "auto").strip().lower()
# Skip microphone capture in the ScreenCaptureKit path (system audio only).
SCK_NO_MIC = os.environ.get("MEETING_RECORDER_SCK_NO_MIC", "").lower() in {"1", "true", "yes"}
RECORDER_SRC = Path(__file__).resolve().parent / "recorder" / "main.swift"
RECORDER_BIN = Path(
    os.environ.get("MEETING_RECORDER_SCK_BIN", str(Path(__file__).resolve().parent / "bin" / "sck-recorder"))
).expanduser()
POLL_SECONDS = int(os.environ.get("MEETING_RECORDER_POLL_SECONDS", "10"))
END_GRACE_SECONDS = int(os.environ.get("MEETING_RECORDER_END_GRACE_SECONDS", "45"))
CHECK_IN_SECONDS = int(os.environ.get("MEETING_RECORDER_CHECK_IN_SECONDS", "1800"))
WHISPER_MODEL = os.environ.get("MEETING_RECORDER_WHISPER_MODEL", "turbo")
LANGUAGE = os.environ.get("MEETING_RECORDER_LANGUAGE", "")
# Anti-hallucination settings. Meeting audio is often ~half silence (one party
# listening, screen-share pauses), and vanilla Whisper fills those gaps with
# repeated priors like "Thank you" / ".". These defaults suppress that:
#  - condition_on_previous_text False breaks the repeat-the-last-line feedback loop
#  - word_timestamps True enables hallucination_silence_threshold
#  - hallucination_silence_threshold skips silent stretches (seconds) where a
#    hallucination is detected. Set it empty to disable.
WHISPER_CONDITION_ON_PREVIOUS_TEXT = os.environ.get("MEETING_RECORDER_CONDITION_ON_PREVIOUS_TEXT", "False")
WHISPER_NO_SPEECH_THRESHOLD = os.environ.get("MEETING_RECORDER_NO_SPEECH_THRESHOLD", "0.6")
WHISPER_HALLUCINATION_SILENCE_THRESHOLD = os.environ.get("MEETING_RECORDER_HALLUCINATION_SILENCE_THRESHOLD", "2").strip()
CLAUDE_MODEL = os.environ.get("MEETING_RECORDER_CLAUDE_MODEL", "")
DISABLE_CLAUDE = os.environ.get("MEETING_RECORDER_DISABLE_CLAUDE", "").lower() in {"1", "true", "yes"}
# --- Transcription engine ------------------------------------------------
# "openrouter" (default): transcode to 16 kHz mono mp3 and transcribe via an
# OpenRouter audio-capable model (Gemini Flash by default) — seconds per file
# and ~$0.12/hr of audio, vs. minutes-to-hours for local Whisper on CPU. Falls
# back automatically to local Whisper when there is no API key, no network, no
# credits (HTTP 401/402/403), or any request error. Set to "whisper" to force
# local-only.
TRANSCRIBE_ENGINE = os.environ.get("MEETING_RECORDER_TRANSCRIBE_ENGINE", "openrouter").strip().lower()
OPENROUTER_BASE_URL = os.environ.get(
    "MEETING_RECORDER_OPENROUTER_BASE_URL",
    os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
).strip()
OPENROUTER_MODEL = os.environ.get("MEETING_RECORDER_OPENROUTER_MODEL", "google/gemini-2.5-flash").strip()
# Audio is chunked into segments this many seconds long so request bodies stay
# small (Gemini bills ~25 audio tokens/sec; 600s ≈ 15k tokens, ~2.4 MB mp3).
OPENROUTER_CHUNK_SECONDS = int(os.environ.get("MEETING_RECORDER_OPENROUTER_CHUNK_SECONDS", "600"))
OPENROUTER_TIMEOUT = int(os.environ.get("MEETING_RECORDER_OPENROUTER_TIMEOUT", "300"))
OPENROUTER_MAX_RETRIES = int(os.environ.get("MEETING_RECORDER_OPENROUTER_MAX_RETRIES", "3"))
OPENROUTER_PROMPT = os.environ.get(
    "MEETING_RECORDER_OPENROUTER_PROMPT",
    "Transcribe this meeting audio verbatim. Output only the transcript text, with no "
    "commentary, headings, or timestamps. If there is no intelligible speech, output nothing.",
)
# Optional dotenv-style file to read OPENROUTER_API_KEY from when it is not in
# the environment (e.g. the ant app's ~/code/ant/.env). Only the key line is read.
OPENROUTER_ENV_FILE = os.environ.get("MEETING_RECORDER_OPENROUTER_ENV_FILE", "").strip()
# Silence guard: a reboot can reset the default output device away from the
# Multi-Output/BlackHole loopback, so ffmpeg captures pure digital silence.
# Recordings at or below this mean dBFS abort with an actionable error instead
# of wasting minutes/hours "transcribing" nothing. Empty disables the guard.
_silence_db_raw = os.environ.get("MEETING_RECORDER_SILENCE_DB", "-80").strip()
SILENCE_DB = float(_silence_db_raw) if _silence_db_raw else None
# Speaker labels. The Claude cleanup always attributes turns to speakers from
# context (names if mentioned, else consistent role labels). For true acoustic
# per-person diarization, set MEETING_RECORDER_DIARIZE=1 with whisperx installed
# and a Hugging Face token (see README > Speaker Labels); when that succeeds its
# speaker-tagged transcript is fed to the cleanup instead of the plain one.
DIARIZE = os.environ.get("MEETING_RECORDER_DIARIZE", "").lower() in {"1", "true", "yes"}
HF_TOKEN = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""

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


def pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_manual_state() -> dict[str, str] | None:
    if not MANUAL_PID_FILE.exists():
        return None
    state: dict[str, str] = {}
    for line in MANUAL_PID_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            state[key] = value
    try:
        pid = int(state.get("pid", ""))
    except ValueError:
        return None
    if not pid_is_running(pid):
        MANUAL_PID_FILE.unlink(missing_ok=True)
        return None
    return state


def osascript(script: str, timeout: int = 20) -> str:
    proc = run(["osascript", "-e", script], timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout.strip()


def write_watcher_status(status: str, meeting: str = "") -> None:
    """Publish the watcher's live state for the menu bar (recording / transcribing / watching)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        WATCHER_STATUS_FILE.write_text(
            f"status={status}\nmeeting={meeting}\nsince={int(time.time())}\npid={os.getpid()}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        log(f"could not write watcher status: {exc}")


def clear_watcher_status() -> None:
    try:
        WATCHER_STATUS_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        log(f"could not clear watcher status: {exc}")


def notify(title: str, text: str) -> None:
    # The native notifier app (built by `mrec install-app`) posts via
    # UserNotifications so its bundle icon — our mic logo — shows on the left.
    # osascript is the fallback: always works but locked to the generic script
    # icon. (An AppleScript applet can't: on modern macOS its notifications are
    # silently dropped as an unregistered client.)
    if USE_LOGO and NOTIFIER_BIN.exists():
        try:
            subprocess.run([str(NOTIFIER_BIN), "--title", title, "--message", text],
                           capture_output=True, timeout=10)
            return
        except Exception as exc:
            log(f"native notifier failed ({exc}); falling back to osascript")
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


def short_meeting_label(reason: str) -> str:
    """Turn a raw detection string into a short, human platform label."""
    low = reason.lower()
    for hint, name in (
        ("meet.google", "Google Meet"), ("google meet", "Google Meet"),
        ("zoom", "Zoom"), ("teams", "Microsoft Teams"),
        ("webex", "Webex"), ("whereby", "Whereby"),
    ):
        if hint in low:
            return name
    # Fallback: the app name before the first colon.
    return reason.split(":", 1)[0].strip() or "Meeting"


def ask_to_record(reason: str) -> bool:
    message = f"{short_meeting_label(reason)} detected. Record it?"
    script = (
        "display dialog "
        + applescript_quote(message)
        + ' buttons {"Dismiss", "Record"} default button "Record" '
          'cancel button "Dismiss" with title "Meeting Recorder" giving up after 30'
    )
    log_section("meeting detected", reason=reason)
    try:
        out = osascript(script, timeout=35)
    except Exception as exc:
        log(f"record prompt failed or declined: {exc}")
        return False
    accepted = "button returned:Record" in out and "gave up:true" not in out
    log(f"user response: {'accepted recording' if accepted else 'declined or timed out'}")
    return accepted


def ask_continue_recording(reason: str, audio: Path) -> bool:
    message = (
        f"I am still recording this meeting:\n\n{reason}\n\n"
        f"Audio file:\n{display_path(audio)}\n\n"
        "Should I keep recording?"
    )
    # No cancel button and any dialog failure defaults to KEEP recording: a
    # dismissed or timed-out check-in must never silently end a live meeting.
    # Only an explicit "Stop and transcribe" click stops.
    script = (
        "display dialog "
        + applescript_quote(message)
        + ' buttons {"Stop and transcribe", "Keep recording"} default button "Keep recording" '
          'with title "Meeting Recorder" giving up after 60'
    )
    log_section("recording check-in", reason=reason, audio_file=display_path(audio))
    try:
        out = osascript(script, timeout=70)
    except Exception as exc:
        log(f"check-in dialog dismissed ({exc}); keeping recording")
        return True
    stop = "button returned:Stop and transcribe" in out
    log(f"check-in response: {'stop and transcribe' if stop else 'keep recording'}")
    return not stop


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


_AUDIO_INDEX_RE = re.compile(r"\[(\d+)\]\s+(.+)")


def audio_device_catalog() -> list[tuple[int, str]]:
    """(index, name) for every AVFoundation audio input device."""
    catalog: list[tuple[int, str]] = []
    for line in visible_audio_devices(list_audio_devices()):
        match = _AUDIO_INDEX_RE.search(line)
        if match:
            catalog.append((int(match.group(1)), match.group(2).strip()))
    return catalog


def is_loopback_name(name: str) -> bool:
    low = name.lower()
    return any(hint in low for hint in LOOPBACK_HINTS)


def resolve_audio_device() -> tuple[str, str, bool]:
    """Return (avfoundation_arg, human_name, is_loopback).

    If MEETING_RECORDER_AUDIO_DEVICE is set it wins. Otherwise auto-pick a
    loopback/aggregate device if one exists (so we capture the whole meeting,
    not just the local mic); failing that, fall back to default index 0.
    """
    catalog = audio_device_catalog()
    names = {idx: name for idx, name in catalog}
    if AUDIO_DEVICE:
        if AUDIO_DEVICE.isdigit():
            name = names.get(int(AUDIO_DEVICE), AUDIO_DEVICE)
        else:
            name = AUDIO_DEVICE
        return f":{AUDIO_DEVICE}", name, is_loopback_name(name)
    for idx, name in catalog:
        if is_loopback_name(name):
            return f":{idx}", name, True
    return ":0", names.get(0, "default input (index 0)"), is_loopback_name(names.get(0, ""))


def audio_device_arg() -> str:
    return resolve_audio_device()[0]


def slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return value[:80] or "meeting"


def ensure_recorder_built() -> Path:
    """Compile recorder/main.swift into the sck-recorder helper if needed.

    Rebuilds when the binary is missing or older than the source. Raises on any
    failure so callers can fall back to the ffmpeg path."""
    if not RECORDER_SRC.exists():
        raise RuntimeError(f"missing recorder source: {RECORDER_SRC}")
    if RECORDER_BIN.exists() and RECORDER_BIN.stat().st_mtime >= RECORDER_SRC.stat().st_mtime:
        return RECORDER_BIN
    if not command_exists("xcrun"):
        raise RuntimeError("Xcode command line tools required (xcrun not found); run: xcode-select --install")
    RECORDER_BIN.parent.mkdir(parents=True, exist_ok=True)
    res = run(
        ["xcrun", "swiftc", "-O", str(RECORDER_SRC), "-o", str(RECORDER_BIN),
         "-framework", "ScreenCaptureKit", "-framework", "AVFoundation", "-framework", "CoreMedia"],
        timeout=180,
    )
    if res.returncode != 0:
        raise RuntimeError(f"swiftc failed: {res.stderr.strip() or res.stdout.strip()}")
    RECORDER_BIN.chmod(0o755)
    return RECORDER_BIN


def _start_screencapturekit(reason: str, path: Path) -> subprocess.Popen[bytes] | None:
    """Start the ScreenCaptureKit helper. Returns None (so the caller can fall
    back) if the platform, build, or first few seconds of capture fail."""
    if sys.platform != "darwin":
        return None
    try:
        binary = ensure_recorder_built()
    except Exception as exc:
        log(f"sck: could not build recorder helper ({exc})")
        return None

    system_wav = path.with_name(path.stem + ".system.wav")
    mic_wav = path.with_name(path.stem + ".mic.wav")
    cmd = [str(binary), "--output", str(system_wav), "--sample-rate", SAMPLE_RATE]
    if not SCK_NO_MIC:
        cmd += ["--mic-output", str(mic_wav)]
    log_section(
        "recording started",
        meeting=reason,
        backend="screencapturekit",
        audio_file=display_path(path),
        capture=("system audio only" if SCK_NO_MIC else "system audio + microphone (both sides)"),
    )
    log("sck command: " + shlex.join(cmd))

    sck_log_path = path.with_name(path.stem + ".sck.log")
    sck_log = open(sck_log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=sck_log)
    time.sleep(3)
    if proc.poll() is not None:
        sck_log.close()
        err = ""
        try:
            err = sck_log_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            pass
        log(f"sck: recorder exited early (rc={proc.returncode}): {err}")
        return None

    proc.mr_backend = "screencapturekit"  # type: ignore[attr-defined]
    proc.mr_parts = [system_wav, mic_wav]  # type: ignore[attr-defined]
    proc.mr_final = path  # type: ignore[attr-defined]
    proc.mr_sck_log = sck_log  # type: ignore[attr-defined]  # keep the file handle alive
    return proc


def start_recording(reason: str) -> tuple[subprocess.Popen[bytes], Path]:
    ROOT.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = ROOT / f"{now}_{slug(reason)}.wav"

    if CAPTURE_BACKEND in ("auto", "screencapturekit", "sck"):
        proc = _start_screencapturekit(reason, path)
        if proc is not None:
            return proc, path
        if CAPTURE_BACKEND != "auto":
            raise RuntimeError(
                "ScreenCaptureKit backend unavailable (build failed or Screen Recording "
                "permission not granted); see the log. Run 'mrec doctor' for details."
            )
        log("ScreenCaptureKit unavailable; falling back to ffmpeg/avfoundation loopback capture")

    return _start_ffmpeg(reason, path), path


def _start_ffmpeg(reason: str, path: Path) -> subprocess.Popen[bytes]:
    device_arg, device_name, is_loopback = resolve_audio_device()
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y"]
    if AUDIO_SYNC:
        cmd += ["-use_wallclock_as_timestamps", "1"]
    cmd += ["-f", "avfoundation", "-i", device_arg, "-vn"]
    if AUDIO_SYNC:
        cmd += ["-af", "aresample=async=1:first_pts=0"]
    cmd += ["-acodec", "pcm_s16le", "-ar", SAMPLE_RATE, str(path)]
    log_section(
        "recording started",
        meeting=reason,
        audio_file=display_path(path),
        audio_input=f"{device_arg} ({device_name})",
        capture=("loopback (both sides)" if is_loopback else "microphone only"),
    )
    if not is_loopback and not ALLOW_MIC_ONLY:
        log(
            f"WARNING: capturing '{device_name}', a microphone — you will get your own voice but "
            "little of the far side. For full meeting audio, set up a BlackHole aggregate device "
            "and MEETING_RECORDER_AUDIO_DEVICE (see README / run 'mrec doctor')."
        )
        notify("Meeting Recorder", "Recording mic only — the far side may be missing. Run mrec doctor to set up loopback capture.")
    log("ffmpeg command: " + shlex.join(cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    if proc.poll() is not None:
        raise RuntimeError("ffmpeg exited early; run mrec doctor to check audio permissions and device selection")
    proc.mr_backend = "ffmpeg"  # type: ignore[attr-defined]
    return proc


def stop_recording(proc: subprocess.Popen[bytes]) -> None:
    if getattr(proc, "mr_backend", "ffmpeg") == "screencapturekit":
        _stop_screencapturekit(proc)
        return
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


def _stop_screencapturekit(proc: subprocess.Popen[bytes]) -> None:
    """Signal the helper to finalize its WAV files, then mix system+mic into the
    single output file the transcription pipeline expects."""
    if proc.poll() is None:
        try:
            log("stopping recording (screencapturekit)")
            proc.send_signal(signal.SIGINT)
            proc.wait(timeout=20)
        except Exception:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    sck_log = getattr(proc, "mr_sck_log", None)
    if sck_log is not None:
        try:
            sck_log.close()
        except OSError:
            pass
    parts = getattr(proc, "mr_parts", None)
    final = getattr(proc, "mr_final", None)
    if parts and final:
        _mix_capture_parts(parts, final)


def _mix_capture_parts(parts: list[Path], final: Path) -> None:
    """Mix the system-audio and mic WAVs into a single mono file for transcription.

    amix with normalize=0 keeps both sources at full level (they rarely peak at
    once, and Whisper/Gemini are tolerant of the occasional overlap). If only one
    source produced audio we just transcode that. On any failure we preserve the
    system-audio track (the far side) so a meeting is never silently lost."""
    system_wav, mic_wav = parts[0], parts[1] if len(parts) > 1 else None
    have_sys = system_wav.exists() and system_wav.stat().st_size > 1024
    have_mic = bool(mic_wav) and mic_wav.exists() and mic_wav.stat().st_size > 1024

    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y"]
    if have_sys and have_mic:
        cmd += [
            "-i", str(system_wav), "-i", str(mic_wav),
            "-filter_complex", "amix=inputs=2:duration=longest:dropout_transition=0:normalize=0",
        ]
    elif have_sys:
        cmd += ["-i", str(system_wav)]
    elif have_mic:
        cmd += ["-i", str(mic_wav)]
    else:
        log("sck: no audio captured (system and mic both empty) — check Screen Recording permission / audio output")
        return
    cmd += ["-ac", "1", "-ar", SAMPLE_RATE, "-acodec", "pcm_s16le", str(final)]

    res = run(cmd, timeout=180)
    if res.returncode != 0:
        log(f"sck: mixing failed ({res.stderr.strip() or res.stdout.strip()})")
        if have_sys and not final.exists():
            shutil.copyfile(system_wav, final)  # don't lose the far-side audio
        return
    for part in (system_wav, mic_wav):
        if part:
            try:
                part.unlink()
            except OSError:
                pass


def audio_duration_seconds(audio: Path) -> float | None:
    """Return the audio duration in seconds via ffprobe, or None if unknown."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
            text=True, capture_output=True, timeout=60,
        )
        return float(proc.stdout.strip())
    except Exception:
        return None


def audio_mean_volume_db(audio: Path) -> float | None:
    """Return the mean volume in dBFS via ffmpeg volumedetect, or None if unmeasurable.

    Digital silence reads as ~-91 dB (the 16-bit noise floor); real speech is
    typically -60..-20 dB.
    """
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio),
             "-af", "volumedetect", "-f", "null", "-"],
            text=True, capture_output=True, timeout=600,
        )
    except Exception:
        return None
    m = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", proc.stderr)
    return float(m.group(1)) if m else None


def transcribe_audio(audio: Path) -> Path:
    out_dir = audio.with_suffix("")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Silence guard: fail fast (and usefully) instead of transcribing nothing.
    if SILENCE_DB is not None:
        mean_db = audio_mean_volume_db(audio)
        if mean_db is not None and mean_db <= SILENCE_DB:
            raise RuntimeError(
                f"recording is silent (mean {mean_db:.1f} dB) — no meeting audio was captured. "
                "Set the system audio output to the Multi-Output Device so loopback (BlackHole) "
                "receives sound, then re-record. (Set MEETING_RECORDER_SILENCE_DB= to disable "
                "this check.)"
            )

    raw_txt: Path | None = None
    if TRANSCRIBE_ENGINE == "openrouter":
        try:
            raw_txt = transcribe_with_openrouter(audio, out_dir)
        except Exception as exc:
            log(f"openrouter transcription failed ({exc}); falling back to local whisper")
    if raw_txt is None:
        raw_txt = transcribe_with_whisper(audio, out_dir)

    diarized_txt = diarize_with_whisperx(audio, out_dir) if DIARIZE else None

    final_md = out_dir / f"{audio.stem}.meeting.md"
    if DISABLE_CLAUDE:
        log("claude cleanup disabled; writing basic markdown transcript")
        write_basic_meeting_md(diarized_txt or raw_txt, final_md, audio)
    else:
        clean_with_claude(raw_txt, final_md, audio, diarized_txt=diarized_txt)
    log_section("transcription finished", final_notes=display_path(final_md), raw_transcript=display_path(raw_txt))
    return final_md


def transcribe_with_whisper(audio: Path, out_dir: Path) -> Path:
    """Local Whisper transcription. Returns the raw .txt transcript path."""
    cmd = ["whisper", str(audio), "--model", WHISPER_MODEL, "--output_dir", str(out_dir), "--output_format", "all"]
    # Suppress silence hallucinations (see WHISPER_* config above).
    cmd.extend([
        "--word_timestamps", "True",
        "--condition_on_previous_text", WHISPER_CONDITION_ON_PREVIOUS_TEXT,
        "--no_speech_threshold", WHISPER_NO_SPEECH_THRESHOLD,
    ])
    if WHISPER_HALLUCINATION_SILENCE_THRESHOLD:
        cmd.extend(["--hallucination_silence_threshold", WHISPER_HALLUCINATION_SILENCE_THRESHOLD])
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
    return raw_txt


def openrouter_api_key() -> str:
    """Resolve the OpenRouter key from the environment, or an optional env file."""
    for var in ("MEETING_RECORDER_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    if OPENROUTER_ENV_FILE:
        try:
            for line in Path(OPENROUTER_ENV_FILE).expanduser().read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def openrouter_transcribe_chunk(mp3: Path, key: str) -> str:
    """Transcribe one mp3 chunk via the OpenRouter chat/completions audio API.

    Raises on auth/credit failures (401/402/403) and on exhausted retries so the
    caller can fall back to local Whisper.
    """
    data_b64 = base64.b64encode(mp3.read_bytes()).decode("ascii")
    prompt = OPENROUTER_PROMPT
    if LANGUAGE:
        prompt += f" The audio language is {LANGUAGE}."
    body = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "input_audio", "input_audio": {"data": data_b64, "format": "mp3"}},
        ]}],
        "temperature": 0,
    }).encode("utf-8")
    url = OPENROUTER_BASE_URL.rstrip("/") + "/chat/completions"
    last_exc: Exception | None = None
    for attempt in range(OPENROUTER_MAX_RETRIES):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=OPENROUTER_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if payload.get("error"):
                raise RuntimeError(str(payload["error"])[:300])
            return payload["choices"][0]["message"].get("content") or ""
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            # Auth/credit problems won't fix themselves — fail now to trigger fallback.
            if exc.code in (401, 402, 403):
                raise RuntimeError(f"openrouter HTTP {exc.code}: {detail}")
            last_exc = RuntimeError(f"openrouter HTTP {exc.code}: {detail}")
        except Exception as exc:
            last_exc = exc
        time.sleep(2 * (attempt + 1))
    raise last_exc or RuntimeError("openrouter request failed")


def transcribe_with_openrouter(audio: Path, out_dir: Path) -> Path:
    """Cloud transcription via OpenRouter. Returns the raw .txt transcript path.

    Transcodes to 16 kHz mono mp3 and sends the audio in OPENROUTER_CHUNK_SECONDS
    segments, concatenating the results. Raises on any failure so the caller can
    fall back to local Whisper.
    """
    key = openrouter_api_key()
    if not key:
        raise RuntimeError("no OpenRouter API key (set MEETING_RECORDER_OPENROUTER_API_KEY, "
                           "OPENROUTER_API_KEY, or MEETING_RECORDER_OPENROUTER_ENV_FILE)")
    if not command_exists("ffmpeg") or not command_exists("ffprobe"):
        raise RuntimeError("ffmpeg/ffprobe are required for OpenRouter transcription")

    log_section("transcription started", audio_file=display_path(audio),
                output_dir=display_path(out_dir), engine=f"openrouter:{OPENROUTER_MODEL}")
    duration = audio_duration_seconds(audio) or 0.0
    nchunks = max(1, int(math.ceil(duration / OPENROUTER_CHUNK_SECONDS))) if duration else 1
    parts: list[str] = []
    with tempfile.TemporaryDirectory(prefix="mrec-or-") as tmp:
        tmpdir = Path(tmp)
        for idx in range(nchunks):
            start = idx * OPENROUTER_CHUNK_SECONDS
            mp3 = tmpdir / f"chunk_{idx:03d}.mp3"
            enc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "error", "-y",
                 "-ss", str(start), "-t", str(OPENROUTER_CHUNK_SECONDS), "-i", str(audio),
                 "-ac", "1", "-ar", "16000", "-b:a", "32k", str(mp3)],
                text=True, capture_output=True,
            )
            if enc.returncode != 0 or not mp3.exists() or mp3.stat().st_size < 512:
                continue  # trailing/empty segment
            text = openrouter_transcribe_chunk(mp3, key).strip()
            if text:
                parts.append(text)
            log(f"openrouter chunk {idx + 1}/{nchunks} done ({len(text)} chars)")
    if not parts:
        raise RuntimeError("openrouter returned no transcript text")
    raw_txt = out_dir / f"{audio.stem}.txt"
    raw_txt.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return raw_txt


def diarize_with_whisperx(audio: Path, out_dir: Path) -> Path | None:
    """Optional true acoustic diarization via the whisperx CLI.

    Returns a path to a transcript whose lines are prefixed with speaker tags
    (e.g. ``[SPEAKER_00]``), or None if whisperx is unavailable, no HF token is
    set, or the run fails. Never raises — diarization is best-effort and the
    plain Whisper transcript remains the fallback.
    """
    if not command_exists("whisperx"):
        log("diarization requested but whisperx is not installed; using plain transcript (see README > Speaker Labels)")
        return None
    if not HF_TOKEN:
        log("diarization requested but no Hugging Face token (HF_TOKEN) is set; using plain transcript")
        return None
    dia_dir = out_dir / "diarization"
    dia_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        "whisperx", str(audio),
        "--model", WHISPER_MODEL,
        "--diarize",
        "--hf_token", HF_TOKEN,
        "--output_dir", str(dia_dir),
        "--output_format", "txt",
    ]
    if LANGUAGE:
        cmd.extend(["--language", LANGUAGE])
    log_section("diarization started", audio_file=display_path(audio), engine="whisperx")
    log("whisperx command: " + shlex.join(c if c != HF_TOKEN else "<hf_token>" for c in cmd))
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=60 * 60)
    except Exception as exc:
        log(f"diarization failed to run ({exc}); using plain transcript")
        return None
    (dia_dir / "whisperx.log").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        log(f"whisperx exited {proc.returncode}; using plain transcript (see {display_path(dia_dir / 'whisperx.log')})")
        return None
    candidates = sorted(dia_dir.glob("*.txt"))
    if not candidates:
        log("whisperx produced no txt output; using plain transcript")
        return None
    log(f"diarization succeeded: {display_path(candidates[0])}")
    return candidates[0]


def write_basic_meeting_md(raw_txt: Path, final_md: Path, audio: Path) -> None:
    final_md.write_text(
        f"# Meeting Transcript\n\nAudio: `{audio}`\n\nRaw transcript: `{raw_txt}`\n\n"
        + raw_txt.read_text(encoding="utf-8", errors="replace"),
        encoding="utf-8",
    )


def clean_with_claude(raw_txt: Path, final_md: Path, audio: Path, diarized_txt: Path | None = None) -> None:
    transcript_text = raw_txt.read_text(encoding="utf-8", errors="replace")
    if diarized_txt is not None:
        speaker_note = (
            "The transcript below already carries acoustic speaker tags (e.g. [SPEAKER_00]) "
            "from diarization. Keep those turn boundaries. Map each tag to a real name if the "
            "content makes it unambiguous (e.g. someone is addressed by name or introduces "
            "themselves); otherwise keep a stable label like \"Speaker 1 (host)\". State the mapping "
            "in a short Speakers legend at the top."
        )
        transcript_text = diarized_txt.read_text(encoding="utf-8", errors="replace")
    else:
        speaker_note = (
            "Attribute each turn to a speaker. The transcript has no speaker tags, so infer them "
            "from turn-taking and content: use a person's real name when it is clearly identifiable "
            "(mentioned, introduced, or addressed), otherwise a consistent role label like "
            "\"Speaker 1 (host/presenter)\" or \"Speaker 2 (client)\". Do NOT guess specific names "
            "without support. Add a short Speakers legend at the top listing who each label is."
        )
    prompt = f"""
You are cleaning a machine-generated transcript of a meeting.

Input audio path: {audio}
Raw transcript path: {raw_txt}

Speaker labels: {speaker_note}

Produce markdown with:
- Title inferred from content if possible
- Date/time from the filename if useful
- Speakers legend (label -> who they are, as far as identifiable)
- Cleaned transcript as attributed turns in the form "**Name:** text", preserving meaning and uncertainty
- Decisions
- Action items with owner if identifiable
- Open questions

Do not invent details not supported by the raw transcript. Attribution should be
your best supported reading; flag it as uncertain rather than fabricating names.

Raw transcript:
{transcript_text}
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
        capture_backend=CAPTURE_BACKEND,
        audio_input=audio_device_arg(),
        whisper_model=WHISPER_MODEL,
        claude_cleanup="disabled" if DISABLE_CLAUDE else "enabled",
    )
    notify("Meeting Recorder", "Watching for Meet, Zoom, Teams, Webex, and Whereby.")
    active = False
    declined_reason: str | None = None
    write_watcher_status("watching")
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
            write_watcher_status("recording", short_meeting_label(reason))
            notify("Meeting Recorder", f"Recording {short_meeting_label(reason)}.")
            last_seen = time.time()
            last_check_in = time.time()
            while True:
                current = detect_meeting()
                if current:
                    last_seen = time.time()
                elif time.time() - last_seen >= END_GRACE_SECONDS:
                    log(f"meeting no longer detected for {END_GRACE_SECONDS}s; stopping recording")
                    break
                if CHECK_IN_SECONDS > 0 and time.time() - last_check_in >= CHECK_IN_SECONDS:
                    if not ask_continue_recording(reason, audio):
                        log("recording stopped by check-in prompt")
                        break
                    last_check_in = time.time()
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
                write_watcher_status("transcribing", short_meeting_label(reason))
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

        write_watcher_status("watching")
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


def record_daemon(label: str) -> int:
    stop_requested = False

    def stop_handler(signum: int, frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, stop_handler)
    signal.signal(signal.SIGTERM, stop_handler)

    proc, audio = start_recording(label)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MANUAL_PID_FILE.write_text(
        f"pid={os.getpid()}\nlabel={label}\naudio={audio}\nstarted_at={dt.datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )
    notify("Meeting Recorder", f"Manual recording started: {label}")
    try:
        while not stop_requested:
            time.sleep(1)
    finally:
        stop_recording(proc)
        MANUAL_PID_FILE.unlink(missing_ok=True)

    if audio.exists() and audio.stat().st_size > 1024:
        try:
            notify("Meeting Recorder", "Manual recording stopped. Transcribing now.")
            final = transcribe_audio(audio)
            alert("Manual transcript ready", f"Saved notes:\n{display_path(final)}\n\nAudio:\n{display_path(audio)}")
            return 0
        except Exception as exc:
            log(f"manual transcription error: {exc}")
            alert("Manual transcription failed", f"{exc}\n\nAudio:\n{display_path(audio)}\n\nLog:\n{display_path(LOG)}")
            return 1
    log(f"manual recording missing or tiny: {audio}")
    return 1


def manual_recording_start(label: str) -> int:
    state = read_manual_state()
    if state:
        print(f"Manual recording already running: pid {state.get('pid')} ({state.get('label', 'manual')})")
        return 0
    log_file = LOG.open("a", encoding="utf-8")
    cmd = [sys.executable, str(Path(__file__).resolve()), "record-daemon", label]
    subprocess.Popen(cmd, stdout=log_file, stderr=log_file, start_new_session=True)
    print(f"Started manual recording: {label}")
    print(f"Log: {display_path(LOG)}")
    return 0


def manual_recording_stop() -> int:
    state = read_manual_state()
    if not state:
        print("No manual recording is running.")
        return 0
    pid = int(state["pid"])
    os.kill(pid, signal.SIGTERM)
    print(f"Stopping manual recording pid {pid}. It will transcribe after the audio finalizes.")
    return 0


def doctor() -> int:
    ok = True
    for binary in ("ffmpeg", "whisper", "claude", "osascript"):
        exists = command_exists(binary)
        print(f"{binary}: {'ok' if exists else 'missing'}")
        ok = ok and exists
    print(f"recordings: {ROOT}")
    print(f"log: {LOG}")

    print(f"\ncapture backend: {CAPTURE_BACKEND}")
    if CAPTURE_BACKEND in ("auto", "screencapturekit", "sck"):
        if command_exists("xcrun"):
            print("xcrun (swiftc): ok")
            try:
                binary = ensure_recorder_built()
                print(f"sck-recorder: built at {display_path(binary)}")
                print("ScreenCaptureKit: ✅ system audio + mic, no BlackHole/Multi-Output needed.")
                print("         First run needs a one-time Screen Recording permission grant")
                print('         (System Settings > Privacy & Security > Screen Recording).')
            except Exception as exc:
                print(f"sck-recorder: ⚠️  could not build ({exc})")
                if CAPTURE_BACKEND == "auto":
                    print("         Will fall back to the ffmpeg/loopback path below.")
                else:
                    ok = False
        else:
            print("xcrun (swiftc): missing — install Xcode command line tools (xcode-select --install)")
            if CAPTURE_BACKEND == "auto":
                print("         Will fall back to the ffmpeg/loopback path below.")
            else:
                ok = False

    print("\nAVFoundation devices:")
    devices = list_audio_devices()
    print(devices or "(none visible)")
    audio_devices = visible_audio_devices(devices)
    if not audio_devices:
        print("\nffmpeg cannot see audio devices. Grant microphone permission to Terminal/iTerm and rerun doctor.")
        ok = False

    device_arg, device_name, is_loopback = resolve_audio_device()
    source = "MEETING_RECORDER_AUDIO_DEVICE" if AUDIO_DEVICE else ("auto-detected loopback" if is_loopback else "default index 0")
    print(f"\naudio input: {device_arg} — {device_name} [{source}]")
    print(f"sample rate: {SAMPLE_RATE} Hz")
    if is_loopback:
        print("capture: ✅ loopback/aggregate — records the whole meeting (both sides).")
    else:
        print("capture: ⚠️  MICROPHONE ONLY — you will get your own voice but little of the far side.")
        print("         Google Meet, Zoom etc. also contend for this mic, which can drop ~half the audio.")
        print("         Fix: install BlackHole 2ch, build an Aggregate Device (mic + BlackHole) in Audio")
        print("         MIDI Setup, route meeting output to it, then set MEETING_RECORDER_AUDIO_DEVICE to")
        print("         that device's name/index and run 'mrec start' again. See README > Audio Setup.")
    return 0 if ok else 1


def build_notifier_app() -> Path:
    """Compile notifier/main.swift into a signed .app whose bundle icon is the mic
    logo, so its notifications show that logo. Ad-hoc signing only — no Apple
    Developer account needed. Raises on failure (no swiftc, compile error, etc.)."""
    if not NOTIFIER_SRC.exists():
        raise RuntimeError(f"missing notifier source: {NOTIFIER_SRC}")
    if not command_exists("xcrun"):
        raise RuntimeError("Xcode command line tools required (xcrun not found); run: xcode-select --install")
    icon_svg = ASSETS_DIR / "icon.svg"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        binary = tmp_path / "notifier"
        compile_res = run(["xcrun", "swiftc", str(NOTIFIER_SRC), "-o", str(binary),
                           "-framework", "Cocoa", "-framework", "UserNotifications"], timeout=180)
        if compile_res.returncode != 0:
            raise RuntimeError(f"swiftc failed: {compile_res.stderr.strip() or compile_res.stdout.strip()}")

        NOTIFIER_APP.parent.mkdir(parents=True, exist_ok=True)
        if NOTIFIER_APP.exists():
            shutil.rmtree(NOTIFIER_APP)
        (NOTIFIER_APP / "Contents" / "MacOS").mkdir(parents=True)
        (NOTIFIER_APP / "Contents" / "Resources").mkdir(parents=True)
        shutil.copyfile(binary, NOTIFIER_BIN)
        NOTIFIER_BIN.chmod(0o755)

        # Bundle icon (best-effort; the app still works without it).
        if icon_svg.exists() and command_exists("rsvg-convert") and command_exists("iconutil"):
            iconset = tmp_path / "icon.iconset"
            iconset.mkdir()
            for size in (16, 32, 128, 256, 512):
                for scale, suffix in ((1, f"{size}x{size}"), (2, f"{size}x{size}@2x")):
                    px = str(size * scale)
                    run(["rsvg-convert", "-w", px, "-h", px, str(icon_svg),
                         "-o", str(iconset / f"icon_{suffix}.png")], timeout=30)
            icns = tmp_path / "AppIcon.icns"
            if run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], timeout=60).returncode == 0:
                shutil.copyfile(icns, NOTIFIER_APP / "Contents" / "Resources" / "AppIcon.icns")

        (NOTIFIER_APP / "Contents" / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            '  <key>CFBundleExecutable</key><string>notifier</string>\n'
            f'  <key>CFBundleIdentifier</key><string>{NOTIFIER_BUNDLE_ID}</string>\n'
            '  <key>CFBundleName</key><string>Meeting Recorder</string>\n'
            '  <key>CFBundleDisplayName</key><string>Meeting Recorder</string>\n'
            '  <key>CFBundleIconFile</key><string>AppIcon</string>\n'
            '  <key>CFBundlePackageType</key><string>APPL</string>\n'
            '  <key>CFBundleShortVersionString</key><string>1.0</string>\n'
            '  <key>LSUIElement</key><true/>\n'
            '</dict></plist>\n',
            encoding="utf-8",
        )
        run(["codesign", "--force", "--deep", "-s", "-", str(NOTIFIER_APP)], timeout=60)
        lsregister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
        if Path(lsregister).exists():
            run([lsregister, "-f", str(NOTIFIER_APP)], timeout=30)
    return NOTIFIER_APP


def build_recorder() -> int:
    """Compile the ScreenCaptureKit capture helper (sck-recorder)."""
    try:
        binary = ensure_recorder_built()
    except Exception as exc:
        print(f"Could not build sck-recorder: {exc}")
        print("The recorder will fall back to ffmpeg/BlackHole loopback capture.")
        return 1
    print(f"Built sck-recorder: {display_path(binary)}")
    print("First use needs a one-time Screen Recording permission grant:")
    print("  System Settings > Privacy & Security > Screen Recording > enable the app that runs it.")
    return 0


def install_notifier() -> int:
    """Build the native notifier app so notifications show the Meeting Recorder logo."""
    try:
        app = build_notifier_app()
    except Exception as exc:
        print(f"Could not build the notifier app: {exc}")
        print("Notifications still work via osascript (generic icon).")
        return 1
    print(f"Built notifier app: {app}")
    notify("Meeting Recorder", "Logo test — notifications now use the mic icon.")
    print("Sent a test notification. The first time, click Allow if macOS prompts")
    print('(System Settings > Notifications > "Meeting Recorder").')
    return 0


def launch_agent_path() -> str:
    """Build a PATH for the LaunchAgent that works for any user.

    launchd starts with a minimal PATH, so we seed the standard tool locations
    (derived from the running user's home, not a hardcoded one) and then union in
    the installing shell's PATH so custom tool locations — a non-standard
    Homebrew prefix, pyenv/asdf/conda shims, etc. — survive."""
    home = Path.home()
    seed = [
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        str(home / ".local" / "bin"), str(home / ".pyenv" / "shims"),
    ]
    ordered: list[str] = []
    seen: set[str] = set()
    for entry in seed + os.environ.get("PATH", "").split(os.pathsep):
        if entry and entry not in seen:
            seen.add(entry)
            ordered.append(entry)
    return os.pathsep.join(ordered)


def install_launch_agent() -> Path:
    plist = LAUNCH_AGENT_PATH
    script = Path(__file__).resolve()
    env_vars = {
        "PATH": launch_agent_path(),
        "MEETING_RECORDER_DIR": str(ROOT),
        "MEETING_RECORDER_LOG": str(LOG),
        "MEETING_RECORDER_WHISPER_MODEL": WHISPER_MODEL,
        "MEETING_RECORDER_POLL_SECONDS": str(POLL_SECONDS),
        "MEETING_RECORDER_END_GRACE_SECONDS": str(END_GRACE_SECONDS),
        "MEETING_RECORDER_SAMPLE_RATE": SAMPLE_RATE,
        "MEETING_RECORDER_AUDIO_SYNC": "1" if AUDIO_SYNC else "0",
        "MEETING_RECORDER_CAPTURE_BACKEND": CAPTURE_BACKEND,
    }
    if RECORDER_BIN != Path(__file__).resolve().parent / "bin" / "sck-recorder":
        env_vars["MEETING_RECORDER_SCK_BIN"] = str(RECORDER_BIN)
    if SCK_NO_MIC:
        env_vars["MEETING_RECORDER_SCK_NO_MIC"] = "1"
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
    try:
        build_notifier_app()
    except Exception as exc:
        log(f"could not build notifier app ({exc}); notifications will use the generic icon")
    if CAPTURE_BACKEND in ("auto", "screencapturekit", "sck"):
        try:
            ensure_recorder_built()
        except Exception as exc:
            log(f"could not build sck-recorder ({exc}); capture will fall back to ffmpeg/loopback")
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
    sub.add_parser("install-app", help="build the branded notifier app (logo for notifications)")
    sub.add_parser("build-recorder", help="compile the ScreenCaptureKit capture helper (sck-recorder)")
    sub.add_parser("start", help="install and start the login background watcher")
    sub.add_parser("stop", help="stop the login background watcher")
    sub.add_parser("status", help="show whether the login background watcher is running")
    manual_start = sub.add_parser("record-start", help="start a manual background recording")
    manual_start.add_argument("label", nargs="?", default="manual-meeting")
    sub.add_parser("record-stop", help="stop the manual background recording and transcribe it")
    daemon = sub.add_parser("record-daemon", help=argparse.SUPPRESS)
    daemon.add_argument("label", nargs="?", default="manual-meeting")
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
    if args.command == "install-app":
        return install_notifier()
    if args.command == "build-recorder":
        return build_recorder()
    if args.command == "start":
        return start_launch_agent()
    if args.command == "stop":
        return stop_launch_agent()
    if args.command == "status":
        return status_launch_agent()
    if args.command == "record-start":
        return manual_recording_start(args.label)
    if args.command == "record-stop":
        return manual_recording_stop()
    if args.command == "record-daemon":
        return record_daemon(args.label)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
