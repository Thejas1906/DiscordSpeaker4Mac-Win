// dsaudiotap - native macOS system / per-application audio capture using Core Audio process taps
// (macOS 14.2+; AudioHardwareCreateProcessTap). Writes interleaved little-endian float32 stereo to
// stdout at the output device's native sample rate, after one JSON header line on stderr.
// No third-party driver is needed. Requires the "System Audio Recording" permission.
//
//   dsaudiotap capture [--pid N]...    capture all system audio, or only the given processes
//   dsaudiotap list-apps               JSON list of processes known to Core Audio
//
// Exit codes: 0 clean, 1 error, 2 permission/unsupported, 3 no matching process, 75 default output changed.
import Foundation
import CoreAudio
import AudioToolbox
import AppKit

setvbuf(stdout, nil, _IOFBF, 1 << 16)

func err(_ s: String) { FileHandle.standardError.write((s + "\n").data(using: .utf8)!) }
func die(_ s: String, _ code: Int32 = 1) -> Never { err("{\"error\":\"\(s)\"}"); exit(code) }

func addr(_ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func getScalar<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, _ zero: T, qualifier: UnsafeRawPointer? = nil, qsize: UInt32 = 0) -> T? {
    var a = addr(sel); var size = UInt32(MemoryLayout<T>.size); var v = zero
    return AudioObjectGetPropertyData(obj, &a, qsize, qualifier, &size, &v) == noErr ? v : nil
}

func getString(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var a = addr(sel); var size = UInt32(MemoryLayout<CFString?>.size); var s: CFString? = nil
    let st = withUnsafeMutablePointer(to: &s) { AudioObjectGetPropertyData(obj, &a, 0, nil, &size, $0) }
    return st == noErr ? (s as String?) : nil
}

func getIDs(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> [AudioObjectID] {
    var a = addr(sel); var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(obj, &a, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func defaultOutput() -> AudioObjectID? {
    getScalar(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice, AudioObjectID(0))
}

func jsonEscape(_ s: String) -> String {
    var o = ""
    for u in s.unicodeScalars {
        switch u { case "\"": o += "\\\""; case "\\": o += "\\\\"; case "\n": o += "\\n"
        default: o += u.value < 0x20 ? "" : String(u) }
    }
    return o
}

func processList() -> [(obj: AudioObjectID, pid: Int32, bundle: String, name: String, output: Bool)] {
    getIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList).compactMap { o in
        guard let pid = getScalar(o, kAudioProcessPropertyPID, pid_t(0)) else { return nil }
        let bundle = getString(o, kAudioProcessPropertyBundleID) ?? ""
        let running = (getScalar(o, kAudioProcessPropertyIsRunningOutput, UInt32(0)) ?? 0) != 0
        let name = NSRunningApplication(processIdentifier: pid)?.localizedName ?? bundle
        return (o, pid, bundle, name.isEmpty ? "pid \(pid)" : name, running)
    }
}

let args = Array(CommandLine.arguments.dropFirst())
guard let cmd = args.first else { die("usage: dsaudiotap capture [--pid N]... | list-apps", 64) }

if cmd == "list-apps" {
    let items = processList().map {
        "{\"pid\":\($0.pid),\"bundle\":\"\(jsonEscape($0.bundle))\",\"name\":\"\(jsonEscape($0.name))\",\"playing\":\($0.output)}"
    }
    print("[" + items.joined(separator: ",") + "]")
    exit(0)
}
guard cmd == "capture" else { die("unknown command", 64) }

var pids: [Int32] = []
var i = 1
while i < args.count { if args[i] == "--pid", i + 1 < args.count, let p = Int32(args[i + 1]) { pids.append(p); i += 2 } else { i += 1 } }

guard #available(macOS 14.2, *) else { die("process taps need macOS 14.2 or newer", 2) }

// Which device clocks the aggregate? NOT the default output by default: a Bluetooth headset drops to a 16 kHz
// "call" profile as soon as any app (e.g. Discord) opens its microphone, and the capture would follow it down
// to 16 kHz (muffled). The tap itself is independent of the output device, so clock from the built-in output,
// which stays at 44.1/48 kHz, and fall back to the default output only if there is none.
func builtInOutput() -> AudioObjectID? {
    for d in getIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices) {
        var a = addr(kAudioDevicePropertyStreams, kAudioObjectPropertyScopeOutput); var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(d, &a, 0, nil, &size) == noErr, size > 0 else { continue }
        if getScalar(d, kAudioDevicePropertyTransportType, UInt32(0)) == kAudioDeviceTransportTypeBuiltIn { return d }
    }
    return nil
}
let useDefaultClock = args.contains("--clock-default")
guard let outDev = (useDefaultClock ? defaultOutput() : (builtInOutput() ?? defaultOutput())),
      let outUID = getString(outDev, kAudioDevicePropertyDeviceUID) else { die("no output device to clock from", 1) }

// ---- tap ----------------------------------------------------------------
let tapDesc: CATapDescription
if pids.isEmpty {
    tapDesc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
} else {
    let procs = processList()
    let objs = pids.compactMap { p in procs.first(where: { $0.pid == p })?.obj }
    if objs.isEmpty { die("no matching audio process", 3) }
    tapDesc = CATapDescription(stereoMixdownOfProcesses: objs)
}
let tapUUID = UUID()
tapDesc.uuid = tapUUID
tapDesc.name = "DiscordSpeaker"
tapDesc.isPrivate = true
tapDesc.muteBehavior = .unmuted        // the user keeps hearing their audio locally

var tapID = AudioObjectID(kAudioObjectUnknown)
var st = AudioHardwareCreateProcessTap(tapDesc, &tapID)
if st != noErr { die("AudioHardwareCreateProcessTap failed (\(st)); grant System Audio Recording permission", 2) }

let aggUID = UUID().uuidString
let aggDesc: [String: Any] = [
    kAudioAggregateDeviceNameKey: "DiscordSpeaker Tap",
    kAudioAggregateDeviceUIDKey: aggUID,
    kAudioAggregateDeviceMainSubDeviceKey: outUID,
    kAudioAggregateDeviceIsPrivateKey: true,
    kAudioAggregateDeviceIsStackedKey: false,
    kAudioAggregateDeviceTapAutoStartKey: true,
    kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
    kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: tapUUID.uuidString]],
]
var aggID = AudioObjectID(kAudioObjectUnknown)
st = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggID)
if st != noErr { AudioHardwareDestroyProcessTap(tapID); die("AudioHardwareCreateAggregateDevice failed (\(st))", 1) }

var procID: AudioDeviceIOProcID? = nil
func cleanup() {
    if let p = procID { AudioDeviceStop(aggID, p); AudioDeviceDestroyIOProcID(aggID, p) }
    AudioHardwareDestroyAggregateDevice(aggID)
    AudioHardwareDestroyProcessTap(tapID)
}

guard let asbd = getScalar(tapID, kAudioTapPropertyFormat, AudioStreamBasicDescription()) else { cleanup(); die("cannot read tap format", 1) }
// The IOProc delivers at the aggregate device's clock (the output device's rate), which can differ from
// the tap's advertised format (e.g. 44.1 kHz Bluetooth output vs a 48 kHz tap format). Trust the device.
let rate = Int(getScalar(aggID, kAudioDevicePropertyNominalSampleRate, Float64(0)) ?? asbd.mSampleRate)
let tapCh = max(1, Int(asbd.mChannelsPerFrame))
err("{\"rate\":\(rate),\"channels\":2,\"format\":\"f32le\"}")

// ---- ring buffer (IOProc -> writer thread) ---------------------------------------
let ringFrames = rate * 2
let ring = UnsafeMutablePointer<Float>.allocate(capacity: ringFrames * 2)
var wpos = 0, rpos = 0, filled = 0
var lock = os_unfair_lock()
var dropped = 0

let ioQueue = DispatchQueue(label: "dsaudiotap.io", qos: .userInteractive)
st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, ioQueue) { _, inData, _, _, _ in
    let abl = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
    guard abl.count > 0 else { return }
    var frames = 0
    let interleaved = abl.count == 1
    if interleaved { frames = Int(abl[0].mDataByteSize) / (MemoryLayout<Float>.size * max(1, Int(abl[0].mNumberChannels))) }
    else { frames = Int(abl[0].mDataByteSize) / MemoryLayout<Float>.size }
    if frames == 0 { return }
    os_unfair_lock_lock(&lock)
    if filled + frames > ringFrames { dropped += 1; rpos = (rpos + frames) % ringFrames; filled -= frames }
    for f in 0..<frames {
        var l: Float = 0, r: Float = 0
        if interleaved {
            let ch = max(1, Int(abl[0].mNumberChannels))
            let p = abl[0].mData!.assumingMemoryBound(to: Float.self)
            l = p[f * ch]; r = ch > 1 ? p[f * ch + 1] : l
        } else {
            l = abl[0].mData!.assumingMemoryBound(to: Float.self)[f]
            r = abl.count > 1 ? abl[1].mData!.assumingMemoryBound(to: Float.self)[f] : l
        }
        ring[wpos * 2] = l; ring[wpos * 2 + 1] = r
        wpos = (wpos + 1) % ringFrames
    }
    filled += frames
    os_unfair_lock_unlock(&lock)
}
if st != noErr { cleanup(); die("AudioDeviceCreateIOProcIDWithBlock failed (\(st))", 1) }

// ---- exit paths -------------------------------------------------------------------
signal(SIGPIPE, SIG_IGN)
var signalSources: [DispatchSourceSignal] = []
for sig in [SIGTERM, SIGINT] {
    signal(sig, SIG_IGN)
    let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
    src.setEventHandler { cleanup(); exit(0) }
    src.resume()
    signalSources.append(src)
}
// Output device changed sample rate while capturing: restart so the Python side learns the new rate.
var rateAddr = addr(kAudioDevicePropertyNominalSampleRate)
AudioObjectAddPropertyListenerBlock(aggID, &rateAddr, .main) { _, _ in
    err("{\"event\":\"sample-rate-changed\"}"); cleanup(); exit(75)
}

st = AudioDeviceStart(aggID, procID)
if st != noErr { cleanup(); die("AudioDeviceStart failed (\(st))", 2) }

// writer thread: drain ring -> stdout
let writer = Thread {
    var buf = [Float](repeating: 0, count: 4800 * 2)
    while true {
        var n = 0
        os_unfair_lock_lock(&lock)
        n = min(filled, 4800)
        for f in 0..<n { buf[f * 2] = ring[rpos * 2]; buf[f * 2 + 1] = ring[rpos * 2 + 1]; rpos = (rpos + 1) % ringFrames }
        filled -= n
        os_unfair_lock_unlock(&lock)
        if n == 0 { usleep(3000); continue }
        let bytes = n * 2 * MemoryLayout<Float>.size
        let ok = buf.withUnsafeBytes { raw -> Bool in
            var off = 0
            while off < bytes {
                let w = write(1, raw.baseAddress! + off, bytes - off)
                if w <= 0 { return false }
                off += w
            }
            return true
        }
        if !ok { cleanup(); exit(0) }       // reader (parent) went away
    }
}
writer.qualityOfService = .userInteractive
writer.start()
_ = tapCh
RunLoop.main.run()
