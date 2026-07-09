#!/usr/bin/env python3
# <xbar.title>Meeting Recorder</xbar.title>
# <xbar.version>v1.0</xbar.version>
# <xbar.author>nsorros</xbar.author>
# <xbar.desc>Start, stop, and monitor the local meeting recorder.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>

from __future__ import annotations

import os
import subprocess
from pathlib import Path


HOME = Path.home()
MREC = HOME / "code/meeting-recorder/mrec"
RECORDINGS = HOME / "Meetings/Recordings"
LOG = HOME / "Library/Logs/meeting-recorder.log"
PID_FILE = HOME / ".local/state/meeting-recorder/manual-recording.pid"


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


def menu_title(text: str, sfimage: str) -> None:
    print(f"{text} | sfimage={sfimage}")


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

    if manual:
        menu_title("REC", "record.circle.fill")
    elif watcher:
        menu_title("MR", "waveform")
    else:
        menu_title("MR", "waveform")

    print("---")
    print(f"Watcher: {'running' if watcher else 'stopped'}")
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
        item("Stop manual recording and transcribe", str(MREC), "record-stop")
    else:
        print("Manual recording: stopped")
        item("Start manual recording", str(MREC), "record-start", "menubar-manual")

    print("---")
    item("Open recordings folder", "/usr/bin/open", str(RECORDINGS), refresh=False)
    item("Open log", "/usr/bin/open", str(LOG), refresh=False)
    item("Run doctor in Terminal", str(MREC), "doctor", refresh=False, terminal=True)
    print("---")
    print(f"Log: {LOG}")
    print(f"Recordings: {RECORDINGS}")


if __name__ == "__main__":
    main()
