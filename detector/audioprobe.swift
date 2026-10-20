// audioprobe: prints one JSON object with per-process audio activity and per-device "running somewhere" state.
import CoreAudio
import CoreGraphics
import AppKit
import Foundation

func addr(_ s: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: s, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}
func ids(_ obj: AudioObjectID, _ s: AudioObjectPropertySelector) -> [AudioObjectID] {
    var a = addr(s); var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(obj, &a, 0, nil, &size) == 0, size > 0 else { return [] }
    var out = [AudioObjectID](repeating: 0, count: Int(size) / 4)
    guard AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &out) == 0 else { return [] }
    return out
}
func u32(_ obj: AudioObjectID, _ s: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> UInt32 {
    var a = addr(s, scope); var v: UInt32 = 0; var size: UInt32 = 4
    _ = AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &v); return v
}
func str(_ obj: AudioObjectID, _ s: AudioObjectPropertySelector) -> String {
    var a = addr(s); var v: Unmanaged<CFString>?; var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(obj, &a, 0, nil, &size, &v) == 0, let c = v else { return "" }
    return c.takeRetainedValue() as String
}
func hasStreams(_ dev: AudioObjectID, _ scope: AudioObjectPropertyScope) -> Bool {
    var a = addr(kAudioDevicePropertyStreams, scope); var size: UInt32 = 0
    return AudioObjectGetPropertyDataSize(dev, &a, 0, nil, &size) == 0 && size > 0
}
func esc(_ s: String) -> String {
    let d = try? JSONSerialization.data(withJSONObject: [s]); let j = String(data: d ?? Data(), encoding: .utf8) ?? "[\"\"]"
    return String(j.dropFirst().dropLast())
}
let sys = AudioObjectID(kAudioObjectSystemObject)
var procs: [String] = []
for p in ids(sys, kAudioHardwarePropertyProcessObjectList) {
    let pid = u32(p, kAudioProcessPropertyPID)
    let bid = str(p, kAudioProcessPropertyBundleID)
    let i = u32(p, kAudioProcessPropertyIsRunningInput), o = u32(p, kAudioProcessPropertyIsRunningOutput)
    procs.append("{\"pid\":\(pid),\"bundle\":\(esc(bid)),\"input\":\(i != 0),\"output\":\(o != 0)}")
}
var devs: [String] = []
for d in ids(sys, kAudioHardwarePropertyDevices) {
    let running = u32(d, kAudioDevicePropertyDeviceIsRunningSomewhere) != 0
    let name = str(d, kAudioObjectPropertyName)
    let uid = str(d, kAudioDevicePropertyDeviceUID)
    let tr = u32(d, kAudioDevicePropertyTransportType)
    let hasIn = hasStreams(d, kAudioObjectPropertyScopeInput), hasOut = hasStreams(d, kAudioObjectPropertyScopeOutput)
    devs.append("{\"name\":\(esc(name)),\"uid\":\(esc(uid)),\"transport\":\(tr),\"running\":\(running),\"in\":\(hasIn),\"out\":\(hasOut)}")
}
var wins: [String] = []
var bundleCache: [Int32: String] = [:]
for w in (CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]] ?? []) {
    guard let pid = w[kCGWindowOwnerPID as String] as? Int32, let name = w[kCGWindowName as String] as? String, !name.isEmpty else { continue }
    if bundleCache[pid] == nil { bundleCache[pid] = NSRunningApplication(processIdentifier: pid)?.bundleIdentifier ?? "" }
    let on = (w[kCGWindowIsOnscreen as String] as? Bool) ?? false
    let layer = (w[kCGWindowLayer as String] as? Int) ?? 0
    wins.append("{\"pid\":\(pid),\"bundle\":\(esc(bundleCache[pid]!)),\"title\":\(esc(name)),\"layer\":\(layer),\"onscreen\":\(on)}")
}
let defIn = u32(sys, kAudioHardwarePropertyDefaultInputDevice)
print("{\"processes\":[\(procs.joined(separator: ","))],\"devices\":[\(devs.joined(separator: ","))],\"windows\":[\(wins.joined(separator: ","))],\"default_input\":\(esc(str(defIn, kAudioDevicePropertyDeviceUID)))}")
