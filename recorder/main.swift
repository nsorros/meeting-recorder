// sck-recorder — capture system audio (and optionally the microphone) via
// ScreenCaptureKit, writing each source to its own WAV. Records until it
// receives SIGINT or SIGTERM, then finalizes the file(s) and exits 0.
//
// This is the "easy path" alternative to the ffmpeg + BlackHole loopback
// capture: ScreenCaptureKit reads system audio directly through a native
// macOS API, so there is no virtual audio device to install, no Multi-Output
// device to build, and nothing that a reboot can silently reset. The only
// prerequisite is a one-time Screen Recording permission grant.
//
// Usage:
//   sck-recorder --output <system.wav> [--mic-output <mic.wav>] [--sample-rate 48000]
//
// The Python driver (meeting_recorder.py) mixes the two WAVs into a single
// file with ffmpeg once recording stops; keeping them separate here keeps this
// helper simple and avoids any in-process sample-mixing math.

import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreMedia

func emit(_ msg: String) {
    FileHandle.standardError.write(("sck-recorder: " + msg + "\n").data(using: .utf8)!)
}

func fail(_ msg: String, code: Int32 = 1) -> Never {
    emit(msg)
    exit(code)
}

extension Array {
    subscript(safe idx: Int) -> Element? { indices.contains(idx) ? self[idx] : nil }
}

// --- parse arguments ---------------------------------------------------------
var systemOut: String?
var micOut: String?
var sampleRate = 48000

do {
    let args = Array(CommandLine.arguments.dropFirst())
    var i = 0
    while i < args.count {
        switch args[i] {
        case "--output", "--system-output":
            systemOut = args[safe: i + 1]; i += 2
        case "--mic-output":
            micOut = args[safe: i + 1]; i += 2
        case "--sample-rate":
            if let v = args[safe: i + 1], let n = Int(v) { sampleRate = n }
            i += 2
        default:
            i += 1
        }
    }
}

guard let systemOutPath = systemOut else {
    fail("missing --output <system.wav>")
}

@available(macOS 13.0, *)
final class Recorder: NSObject, SCStreamOutput, SCStreamDelegate {
    let systemURL: URL
    let micURL: URL?
    let sampleRate: Int
    let writeQueue = DispatchQueue(label: "sck-recorder.write")

    var stream: SCStream?
    var systemFile: AVAudioFile?
    var micFile: AVAudioFile?
    var systemFrames: Int64 = 0
    var micFrames: Int64 = 0
    var stopped = false

    init(systemURL: URL, micURL: URL?, sampleRate: Int) {
        self.systemURL = systemURL
        self.micURL = micURL
        self.sampleRate = sampleRate
    }

    var wantMic: Bool { micURL != nil }

    func start() {
        SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: false) { [weak self] content, error in
            guard let self = self else { return }
            if let error = error {
                fail("cannot get shareable content — is Screen Recording permission granted? (\(error.localizedDescription))", code: 2)
            }
            guard let display = content?.displays.first else {
                fail("no display available to attach the audio stream to", code: 3)
            }

            let filter = SCContentFilter(display: display, excludingWindows: [])
            let config = SCStreamConfiguration()
            config.capturesAudio = true
            config.sampleRate = self.sampleRate
            config.channelCount = 2
            config.excludesCurrentProcessAudio = true
            // We only want audio; keep the (mandatory) video path trivially small.
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 4)
            config.queueDepth = 6
            if #available(macOS 15.0, *), self.wantMic {
                config.captureMicrophone = true
            }

            let stream = SCStream(filter: filter, configuration: config, delegate: self)
            self.stream = stream
            do {
                try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: self.writeQueue)
                if #available(macOS 15.0, *), self.wantMic {
                    try stream.addStreamOutput(self, type: .microphone, sampleHandlerQueue: self.writeQueue)
                }
                // The stream must produce video frames to run; register a handler
                // and ignore them so the (tiny) frames don't back up the queue.
                try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: self.writeQueue)
            } catch {
                fail("failed to add stream output: \(error.localizedDescription)", code: 5)
            }

            stream.startCapture { error in
                if let error = error {
                    fail("startCapture failed: \(error.localizedDescription)", code: 4)
                }
                let mic = (self.wantMic && self.micAvailable) ? "+mic" : ""
                emit("recording started (system\(mic)) -> \(self.systemURL.path)")
            }
        }
    }

    var micAvailable: Bool {
        if #available(macOS 15.0, *) { return wantMic }
        return false
    }

    // MARK: SCStreamOutput

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }
        switch type {
        case .audio:
            write(sampleBuffer, toMic: false)
        default:
            if #available(macOS 15.0, *), type == .microphone {
                write(sampleBuffer, toMic: true)
            }
            // .screen frames intentionally ignored
        }
    }

    private func write(_ sb: CMSampleBuffer, toMic: Bool) {
        guard let fmtDesc = CMSampleBufferGetFormatDescription(sb),
              let asbdPtr = CMAudioFormatDescriptionGetStreamBasicDescription(fmtDesc) else { return }
        var asbd = asbdPtr.pointee
        guard let format = AVAudioFormat(streamDescription: &asbd) else { return }
        let frames = AVAudioFrameCount(CMSampleBufferGetNumSamples(sb))
        guard frames > 0, let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: frames) else { return }
        pcm.frameLength = frames
        let status = CMSampleBufferCopyPCMDataIntoAudioBufferList(
            sb, at: 0, frameCount: Int32(frames), into: pcm.mutableAudioBufferList)
        guard status == noErr else { return }

        do {
            if toMic {
                guard let micURL = micURL else { return }
                if micFile == nil { micFile = try makeFile(micURL, source: format) }
                try micFile?.write(from: pcm)
                micFrames += Int64(frames)
            } else {
                if systemFile == nil { systemFile = try makeFile(systemURL, source: format) }
                try systemFile?.write(from: pcm)
                systemFrames += Int64(frames)
            }
        } catch {
            emit("write error (\(toMic ? "mic" : "system")): \(error.localizedDescription)")
        }
    }

    private func makeFile(_ url: URL, source: AVAudioFormat) throws -> AVAudioFile {
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVSampleRateKey: source.sampleRate,
            AVNumberOfChannelsKey: source.channelCount,
            AVLinearPCMBitDepthKey: 16,
            AVLinearPCMIsFloatKey: false,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
        ]
        return try AVAudioFile(
            forWriting: url,
            settings: settings,
            commonFormat: source.commonFormat,
            interleaved: source.isInterleaved)
    }

    // MARK: SCStreamDelegate

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        emit("stream stopped with error: \(error.localizedDescription)")
        stop()
    }

    func stop() {
        writeQueue.async { [weak self] in
            guard let self = self, !self.stopped else { return }
            self.stopped = true
            let finish = {
                self.writeQueue.async {
                    // Releasing the AVAudioFile objects flushes and finalizes the
                    // WAV headers on disk.
                    self.systemFile = nil
                    self.micFile = nil
                    emit("stopped (system frames=\(self.systemFrames), mic frames=\(self.micFrames))")
                    exit(0)
                }
            }
            if let stream = self.stream {
                stream.stopCapture { _ in finish() }
            } else {
                finish()
            }
        }
    }
}

guard #available(macOS 13.0, *) else {
    fail("ScreenCaptureKit requires macOS 13 or newer", code: 6)
}

let recorder = Recorder(
    systemURL: URL(fileURLWithPath: systemOutPath),
    micURL: micOut.map { URL(fileURLWithPath: $0) },
    sampleRate: sampleRate)

// Route SIGINT/SIGTERM to a graceful stop that finalizes the WAV files.
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
sigint.setEventHandler { recorder.stop() }
sigint.resume()
let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
sigterm.setEventHandler { recorder.stop() }
sigterm.resume()

recorder.start()
dispatchMain()
