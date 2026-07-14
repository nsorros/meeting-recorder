#!/usr/bin/env python3
# <xbar.title>Meeting Recorder</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.author>nsorros</xbar.author>
# <xbar.desc>Start, stop, and monitor the local meeting recorder.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


HOME = Path.home()
MREC = HOME / "code/meeting-recorder/mrec"
RECORDINGS = HOME / "Meetings/Recordings"
LOG = HOME / "Library/Logs/meeting-recorder.log"
PID_FILE = HOME / ".local/state/meeting-recorder/manual-recording.pid"
STATUS_FILE = HOME / ".local/state/meeting-recorder/watcher-status"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=5)


def pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def manual_state() -> dict[str, str] | None:
    if not PID_FILE.exists():
        return None
    state: dict[str, str] = {}
    for line in PID_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            state[key] = value
    try:
        pid = int(state.get("pid", ""))
    except ValueError:
        return None
    return state if pid_running(pid) else None


def watcher_running() -> bool:
    proc = run([str(MREC), "status"])
    return proc.returncode == 0 and "running" in proc.stdout


def watcher_status() -> dict[str, str]:
    """Live watcher state (status/meeting/since), or {} if absent or stale."""
    if not STATUS_FILE.exists():
        return {}
    state: dict[str, str] = {}
    for line in STATUS_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            state[key] = value
    try:
        if not pid_running(int(state.get("pid", ""))):
            return {}
    except ValueError:
        pass
    return state


def _age(mtime: float) -> str:
    secs = int(time.time() - mtime)
    if secs < 3600:
        return f"{max(secs // 60, 0)}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def latest_recording() -> Path | None:
    """Newest recorded audio file, or None."""
    if not RECORDINGS.exists():
        return None
    files = [p for p in RECORDINGS.glob("*.wav") if p.is_file()]
    files += [p for p in RECORDINGS.glob("*.m4a") if p.is_file()]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def latest_transcript() -> Path | None:
    """Newest cleaned meeting transcript (<stem>.meeting.md), or None."""
    if not RECORDINGS.exists():
        return None
    md = [p for p in RECORDINGS.glob("*/*.meeting.md") if p.is_file()]
    return max(md, key=lambda p: p.stat().st_mtime, default=None)


def elapsed(since: str) -> str:
    try:
        secs = int(time.time()) - int(since)
    except ValueError:
        return ""
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def engine_plan() -> dict:
    """Which engine the next transcription will use, per `mrec engine`.

    Passes --max-age so this once-a-minute plugin reads a cached balance instead
    of calling the API every refresh. Best-effort: on any failure the menu simply
    omits the line rather than breaking.
    """
    try:
        proc = subprocess.run([str(MREC), "engine", "--json", "--max-age", "900"],
                              text=True, capture_output=True, timeout=20)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}
    except Exception:
        return {}


def print_engine_section() -> None:
    plan = engine_plan()
    if not plan:
        return
    model = plan.get("model", "")
    if plan.get("engine") == "openrouter":
        label = model.split("/")[-1]
    else:
        label = f"local whisper ({model})"
    line = f"Transcribes with: {label}"
    credits = plan.get("credits")
    if credits:
        line += f"  ·  ${credits['remaining']:.2f} left"
    # "|" would be read as the start of xbar's parameter list.
    print(line.replace("|", "/"))
    if plan.get("warning"):
        print(f"⚠️ {plan.get('reason', 'check engine')}".replace("|", "/") + " | color=red")


def menu_title(text: str, sfimage: str, color: str | None = None) -> None:
    line = f"{text} | sfimage={sfimage}"
    if color:
        line += f" sfcolor={color}"
    print(line)


def item(title: str, command: str, *params: str, refresh: bool = True, terminal: bool = False) -> None:
    bits = [f"bash={command}", f"terminal={'true' if terminal else 'false'}"]
    for i, param in enumerate(params, start=1):
        bits.append(f"param{i}={param}")
    if refresh:
        bits.append("refresh=true")
    print(f"{title} | " + " ".join(bits))


def main() -> None:
    manual = manual_state()
    watcher = watcher_running()
    status = watcher_status() if watcher else {}
    state = status.get("status", "")

    # Menu bar title signals the current state at a glance.
    if manual or state == "recording":
        since = manual.get("started_at", "") if manual else status.get("since", "")
        mins = elapsed(since)
        menu_title(f"Rec {mins}".strip(), "record.circle.fill", color="red")
    elif state == "transcribing":
        menu_title("Transcribing…", "ellipsis.circle")
    elif watcher:
        menu_title("Listening", "waveform")
    else:
        menu_title("Off", "waveform.slash")

    print("---")
    if manual or state == "recording":
        meeting = manual.get("label", "manual recording") if manual else status.get("meeting", "meeting")
        print(f"🔴 Recording: {meeting}")
    elif state == "transcribing":
        print("⏳ Transcribing last meeting…")
    elif watcher:
        print("🎧 Listening for a meeting")
    print(f"Watcher: {'running' if watcher else 'stopped'}")
    print_engine_section()

    # Stop whatever is recording (auto or manual) — the primary action when live.
    if manual or state == "recording":
        item("⏹ Stop recording and transcribe", str(MREC), "stop-recording")

    print("---")
    if watcher:
        item("Stop watcher", str(MREC), "stop")
    else:
        item("Start watcher at login", str(MREC), "start")

    print("---")
    if manual:
        print(f"Manual recording: {manual.get('label', 'manual-meeting')}")
        print(f"Started: {manual.get('started_at', 'unknown')}")
        if manual.get("audio"):
            print(f"Audio: {manual['audio']}")
    else:
        print("Manual recording: stopped")
        item("Start manual recording", str(MREC), "record-start", "menubar-manual")

    print("---")
    last_rec = latest_recording()
    last_tx = latest_transcript()
    if last_rec or last_tx:
        if last_rec:
            print(f"Last recording: {last_rec.stem}  ({_age(last_rec.stat().st_mtime)})")
            item("▸ Play recording", "/usr/bin/open", str(last_rec), refresh=False)
        if last_tx:
            print(f"Last transcript: {last_tx.parent.name}  ({_age(last_tx.stat().st_mtime)})")
            # Via mrec, not `open`: a bare .md goes to whatever app claims markdown
            # (an editor), while this renders summary + transcript for reading.
            item("▸ Open transcript", str(MREC), "open-transcript", str(last_tx), refresh=False)
        print("---")
    item("Open recordings folder", "/usr/bin/open", str(RECORDINGS), refresh=False)
    item("Open log", "/usr/bin/open", str(LOG), refresh=False)
    item("Run doctor in Terminal", str(MREC), "doctor", refresh=False, terminal=True)
    print("---")
    print(f"Log: {LOG}")
    print(f"Recordings: {RECORDINGS}")


if __name__ == "__main__":
    main()
