// mic-probe — report which processes currently hold a microphone input stream.
//
// Used by meeting-recorder to detect calls that leave no browser tab and no
// distinctive process behind: Slack huddles, Discord calls, FaceTime. Slack runs
// all day, so "is Slack running" says nothing; "is Slack holding the mic right
// now" is the signal that tracks an actual huddle.
//
// Prints one `pid<TAB>executable-name` line per process running an input stream,
// and exits 0 even when nothing is (empty output is a valid answer). Exit 1 is
// reserved for "this machine cannot answer the question", so the caller can tell
// "no huddle" apart from "detection is broken".
//
// Uses the CoreAudio process-object API (macOS 14.4+), which gives per-process
// attribution. The older kAudioDevicePropertyDeviceIsRunningSomewhere is device
// -wide: it cannot tell Slack from Voice Memos, nor from meeting-recorder's own
// capture, so it cannot drive this decision.
//
// Note this only reads audio-system bookkeeping — it opens no stream, so it does
// NOT require (or trigger) microphone permission.

import CoreAudio
import Darwin
import Foundation

func processObjectIDs() -> [AudioObjectID]? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else { return nil }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    if count == 0 { return [] }
    var ids = [AudioObjectID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &ids) == noErr else { return nil }
    return ids
}

func uint32Property(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    guard AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value) == noErr else { return nil }
    return value
}

func executableName(_ pid: pid_t) -> String {
    var buffer = [CChar](repeating: 0, count: 4096)
    guard proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 else { return "" }
    return (String(cString: buffer) as NSString).lastPathComponent
}

guard let objects = processObjectIDs() else {
    FileHandle.standardError.write(
        "mic-probe: CoreAudio process-object API unavailable (needs macOS 14.4+)\n".data(using: .utf8)!)
    exit(1)
}

var out = ""
for object in objects {
    guard let running = uint32Property(object, kAudioProcessPropertyIsRunningInput), running != 0 else { continue }
    guard let raw = uint32Property(object, kAudioProcessPropertyPID) else { continue }
    let pid = pid_t(bitPattern: raw)
    out += "\(pid)\t\(executableName(pid))\n"
}
FileHandle.standardOutput.write(out.data(using: .utf8)!)
exit(0)
