// DOP Call Recorder recorder (M2). Built with swiftc, see build.sh.
import Foundation
import AVFoundation
import CoreAudio
import AppKit
import Darwin

let appSupport: String = {
    let env = ProcessInfo.processInfo.environment
    if let e = env["DOP_STATE_DIR"] ?? env["DOP_CR_STATE_DIR"], !e.isEmpty { return NSString(string: e).expandingTildeInPath }
    return NSString(string: "~/Library/Application Support/dop-call-recorder").expandingTildeInPath
}()
let pidFile = appSupport + "/recorder.pid"
let logFile = appSupport + "/recorder.log"
let SR = 16000.0
let CHUNK_SECONDS = Int(ProcessInfo.processInfo.environment["DOP_CR_CHUNK_SECONDS"] ?? "") ?? 300

func jlog(_ event: String, _ extra: [String: Any] = [:]) {
    var d = extra
    d["ts"] = ISO8601DateFormatter().string(from: Date())
    d["event"] = event
    d["src"] = "recorder"
    if let data = try? JSONSerialization.data(withJSONObject: d, options: [.sortedKeys]),
       let s = String(data: data, encoding: .utf8) {
        FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
        try? FileManager.default.createDirectory(atPath: appSupport, withIntermediateDirectories: true)
        if let h = FileHandle(forWritingAtPath: logFile) ?? { FileManager.default.createFile(atPath: logFile, contents: nil); return FileHandle(forWritingAtPath: logFile) }() {
            h.seekToEndOfFile(); h.write((s + "\n").data(using: .utf8)!); h.closeFile()
        }
    }
}

// MARK: Core Audio helpers
func prop<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector, scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ def: T) -> T {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var v = def
    var size = UInt32(MemoryLayout<T>.size)
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &v)
    return st == noErr ? v : def
}

func cfStringProp(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var s: Unmanaged<CFString>? = nil
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &s)
    guard st == noErr, let s = s else { return nil }
    return s.takeRetainedValue() as String
}

struct AudioProc { var objectID: AudioObjectID; var pid: pid_t; var bundleID: String; var running: Bool; var input: Bool; var output: Bool }

func audioProcesses() -> [AudioProc] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyProcessObjectList, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids.map { id in
        let pid: pid_t = prop(id, kAudioProcessPropertyPID, -1)
        let bid = cfStringProp(id, kAudioProcessPropertyBundleID) ?? ""
        let run: UInt32 = prop(id, kAudioProcessPropertyIsRunning, 0)
        let inp: UInt32 = prop(id, kAudioProcessPropertyIsRunningInput, 0)
        let out: UInt32 = prop(id, kAudioProcessPropertyIsRunningOutput, 0)
        return AudioProc(objectID: id, pid: pid, bundleID: bid, running: run != 0, input: inp != 0, output: out != 0)
    }
}

func parentPID(_ pid: pid_t) -> pid_t {
    var info = proc_bsdinfo()
    let n = proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &info, Int32(MemoryLayout<proc_bsdinfo>.size))
    return n > 0 ? pid_t(info.pbi_ppid) : 0
}

func procPath(_ pid: pid_t) -> String {
    var buf = [CChar](repeating: 0, count: 4096)
    return proc_pidpath(pid, &buf, UInt32(buf.count)) > 0 ? String(cString: buf) : ""
}

/// Bundle id that "owns" a process: Core Audio's own value, else the enclosing .app main bundle (helpers resolve to their parent app).
func ownerBundle(_ p: AudioProc) -> String {
    return p.bundleID
}

/// Does a Core Audio process belong to one of the wanted bundle ids (exact, or helper "<id>.helper*", or lives inside the main app via process ancestry)?
func matches(_ p: AudioProc, wanted: [String], mainPIDs: [String: [pid_t]]) -> Bool {
    for w in wanted {
        if p.bundleID == w || p.bundleID.hasPrefix(w + ".") || w == "pid:\(p.pid)" { return true }
        var cur = p.pid; var hops = 0
        while cur > 1 && hops < 8 {
            if w == "pid:\(cur)" { return true }
            if (mainPIDs[w] ?? []).contains(cur) { return true }
            cur = parentPID(cur); hops += 1
        }
    }
    return false
}

func mainPIDs(for wanted: [String]) -> [String: [pid_t]] {
    var m: [String: [pid_t]] = [:]
    for a in NSWorkspace.shared.runningApplications {
        if let b = a.bundleIdentifier, wanted.contains(b) { m[b, default: []].append(a.processIdentifier) }
    }
    return m
}

// MARK: list-apps
func listApps(all: Bool) {
    let procs = audioProcesses()
    var apps: [String: [String: Any]] = [:]
    let running = NSWorkspace.shared.runningApplications
    let byPid = Dictionary(uniqueKeysWithValues: running.map { ($0.processIdentifier, $0) })
    for p in procs {
        if p.pid == getpid() { continue }
        if !all && !(p.input || p.output) { continue }
        // find the owning main app: walk ancestors until a regular NSRunningApplication
        var owner: NSRunningApplication? = byPid[p.pid]
        var cur = p.pid; var hops = 0
        while (owner == nil || owner!.activationPolicy != .regular) && cur > 1 && hops < 8 {
            cur = parentPID(cur); hops += 1
            if let a = byPid[cur], a.activationPolicy == .regular { owner = a; break }
        }
        let key = owner?.bundleIdentifier ?? (p.bundleID.isEmpty ? "pid:\(p.pid)" : p.bundleID)
        var e = apps[key] ?? [
            "bundle_id": key,
            "name": owner?.localizedName ?? procPath(p.pid).split(separator: "/").last.map(String.init) ?? key,
            "main_pid": owner.map { Int($0.processIdentifier) } ?? Int(p.pid),
            "pids": [Int](), "audio_bundle_ids": [String](), "is_running_input": false, "is_running_output": false,
        ]
        var pids = e["pids"] as! [Int]; pids.append(Int(p.pid)); e["pids"] = pids
        var abs = e["audio_bundle_ids"] as! [String]
        if !p.bundleID.isEmpty && !abs.contains(p.bundleID) { abs.append(p.bundleID) }
        e["audio_bundle_ids"] = abs
        if p.input { e["is_running_input"] = true }
        if p.output { e["is_running_output"] = true }
        apps[key] = e
    }
    let out: [String: Any] = [
        "generated_at": ISO8601DateFormatter().string(from: Date()),
        "apps": apps.values.sorted { ($0["name"] as! String) < ($1["name"] as! String) },
    ]
    let data = try! JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])
    print(String(data: data, encoding: .utf8)!)
}

// MARK: WAV chunk writer (16 kHz mono s16le)
final class ChunkWriter {
    let dir: String, prefix: String
    let lock = NSLock()
    var fd: Int32 = -1
    var index = 0
    var samplesInChunk = 0
    var totalSamples = 0
    var chunks: [[String: Any]] = []
    var chunkStart = Date()
    var peak: Int16 = 0
    var lastActive: Date? = nil
    init(dir: String, prefix: String) { self.dir = dir; self.prefix = prefix; openChunk() }

    func header(dataBytes: UInt32) -> Data {
        var d = Data()
        func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
        d.append("RIFF".data(using: .ascii)!); u32(36 + dataBytes); d.append("WAVEfmt ".data(using: .ascii)!)
        u32(16); u16(1); u16(1); u32(16000); u32(32000); u16(2); u16(16)
        d.append("data".data(using: .ascii)!); u32(dataBytes)
        return d
    }
    func openChunk() {
        let name = String(format: "%@_%04d.wav", prefix, index)
        fd = open(dir + "/" + name, O_WRONLY | O_CREAT | O_TRUNC, 0o644)
        let h = header(dataBytes: 0); h.withUnsafeBytes { _ = write(fd, $0.baseAddress, h.count) }
        samplesInChunk = 0; chunkStart = Date()
        chunks.append(["file": name, "start": ISO8601DateFormatter().string(from: chunkStart), "samples": 0])
    }
    func patchHeader() {
        guard fd >= 0 else { return }
        let h = header(dataBytes: UInt32(samplesInChunk * 2))
        h.withUnsafeBytes { _ = pwrite(fd, $0.baseAddress, h.count, 0) }
        chunks[chunks.count - 1]["samples"] = samplesInChunk
    }
    func append(_ s: UnsafeBufferPointer<Int16>) {
        lock.lock(); defer { lock.unlock() }
        var off = 0
        while off < s.count {
            let room = CHUNK_SECONDS * Int(SR) - samplesInChunk
            let n = min(room, s.count - off)
            _ = write(fd, s.baseAddress! + off, n * 2)
            for i in 0..<n { let a = s[off + i].magnitude; if a > UInt16(peak.magnitude) { peak = Int16(clamping: a) } ; if a > 300 { lastActive = Date() } }
            samplesInChunk += n; totalSamples += n; off += n
            if samplesInChunk >= CHUNK_SECONDS * Int(SR) { patchHeader(); close(fd); index += 1; openChunk() }
        }
    }
    /// Keep the file timeline aligned with wall-clock: pad silence when no data arrived (no tap yet, device switch gap).
    func padSilence(upTo seconds: Double) {
        let missing = Int(seconds * SR) - totalSamples
        if missing > Int(SR / 2) {
            let z = [Int16](repeating: 0, count: missing)
            z.withUnsafeBufferPointer { append($0) }
        }
    }
    func heartbeat() { lock.lock(); patchHeader(); lock.unlock() }
    func finish() { lock.lock(); patchHeader(); if fd >= 0 { fsync(fd); close(fd); fd = -1 }; lock.unlock() }
}

/// Converts arbitrary-format PCM to 16 kHz mono s16 and feeds a writer.
final class Resampler {
    let writer: ChunkWriter
    var converter: AVAudioConverter?
    var inFmt: AVAudioFormat?
    let outFmt = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: SR, channels: 1, interleaved: true)!
    init(writer: ChunkWriter) { self.writer = writer }
    func feed(_ buf: AVAudioPCMBuffer) {
        if inFmt == nil || inFmt! != buf.format { inFmt = buf.format; converter = AVAudioConverter(from: buf.format, to: outFmt) }
        guard let conv = converter else { return }
        let cap = AVAudioFrameCount(Double(buf.frameLength) * SR / buf.format.sampleRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return }
        var supplied = false
        var err: NSError?
        conv.convert(to: out, error: &err) { _, st in
            if supplied { st.pointee = .noDataNow; return nil }
            supplied = true; st.pointee = .haveData; return buf
        }
        if out.frameLength > 0, let p = out.int16ChannelData {
            writer.append(UnsafeBufferPointer(start: p[0], count: Int(out.frameLength)))
        }
    }
}

// MARK: Recorder
final class Recorder {
    let wanted: [String], outDir: String, fallbackAll: Bool
    var mode = "per-app"          // per-app | system-fallback
    let others: ChunkWriter, me: ChunkWriter
    let othersRS: Resampler, meRS: Resampler
    var engine = AVAudioEngine()
    var tapID: AudioObjectID = 0, aggID: AudioObjectID = 0
    var ioProc: AudioDeviceIOProcID?
    var tapPIDs: Set<pid_t> = []
    var tapFormat: AVAudioFormat?
    var aggRate = 0.0
    let ioQueue = DispatchQueue(label: "dop-cr.io", qos: .userInteractive)
    let ctl = DispatchQueue(label: "dop-cr.ctl")
    var timer: DispatchSourceTimer?
    let started = Date()
    var events: [[String: Any]] = []
    var stopping = false
    var lastOthersBytes = 0

    init(wanted: [String], outDir: String, fallbackAll: Bool) {
        self.wanted = wanted; self.outDir = outDir; self.fallbackAll = fallbackAll
        others = ChunkWriter(dir: outDir, prefix: "others"); me = ChunkWriter(dir: outDir, prefix: "me")
        othersRS = Resampler(writer: others); meRS = Resampler(writer: me)
    }

    func note(_ ev: String, _ extra: [String: Any] = [:]) {
        var e = extra; e["event"] = ev; e["ts"] = ISO8601DateFormatter().string(from: Date())
        events.append(e); if events.count > 200 { events.removeFirst() }
        jlog(ev, extra)
    }

    func start() throws {
        try startMic()
        setupTap()
        // device-change listeners
        for sel in [kAudioHardwarePropertyDefaultInputDevice, kAudioHardwarePropertyDefaultOutputDevice] {
            var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &addr, ctl) { [weak self] _, _ in
                guard let self = self, !self.stopping else { return }
                self.note("default_device_changed", ["selector": sel == kAudioHardwarePropertyDefaultInputDevice ? "input" : "output"])
                self.ctl.asyncAfter(deadline: .now() + 0.5) {
                    if sel == kAudioHardwarePropertyDefaultInputDevice { self.restartMic() } else { self.rebuildTap(reason: "output_device_change") }
                }
            }
        }
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: nil, queue: nil) { [weak self] _ in
            self?.ctl.async { self?.restartMic() }
        }
        let t = DispatchSource.makeTimerSource(queue: ctl)
        t.schedule(deadline: .now() + 1, repeating: 2)
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume(); timer = t
        writeState()
    }

    // Mic
    func startMic() throws {
        let input = engine.inputNode
        let fmt = input.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0 else { throw NSError(domain: "dop-call-recorder", code: 1, userInfo: [NSLocalizedDescriptionKey: "no input device / mic permission denied"]) }
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 4096, format: fmt) { [weak self] buf, _ in self?.meRS.feed(buf) }
        engine.prepare()
        try engine.start()
        note("mic_started", ["sample_rate": fmt.sampleRate, "channels": Int(fmt.channelCount)])
    }
    func restartMic() {
        if stopping { return }
        engine.stop(); engine.inputNode.removeTap(onBus: 0)
        engine = AVAudioEngine()
        for attempt in 1...5 {
            do { try startMic(); note("mic_restarted", ["attempt": attempt]); return }
            catch { note("mic_restart_failed", ["attempt": attempt, "error": "\(error)"]); Thread.sleep(forTimeInterval: 1) }
        }
    }

    // Tap
    func currentTargets() -> [AudioProc] {
        let mp = mainPIDs(for: wanted)
        return audioProcesses().filter { matches($0, wanted: wanted, mainPIDs: mp) && $0.pid != getpid() }
    }
    func destroyTap() {
        if let p = ioProc, aggID != 0 { AudioDeviceStop(aggID, p); AudioDeviceDestroyIOProcID(aggID, p) }
        ioProc = nil
        if aggID != 0 { AudioHardwareDestroyAggregateDevice(aggID); aggID = 0 }
        if tapID != 0 { AudioHardwareDestroyProcessTap(tapID); tapID = 0 }
    }
    func fallbackToSystem(_ why: String) {
        guard mode == "per-app" else { return }
        mode = "system-fallback"
        note("auto_fallback_system_tap", ["reason": why])
        setupTap()
    }
    func setupTap() {
        destroyTap()
        var desc: CATapDescription
        let targets = currentTargets()
        if mode == "per-app" {
            if targets.isEmpty { tapPIDs = []; return }   // nothing to tap yet; tick() retries as processes appear
            desc = CATapDescription(stereoMixdownOfProcesses: targets.map { $0.objectID })
            tapPIDs = Set(targets.map { $0.pid })
        } else {
            let me = audioProcesses().filter { $0.pid == getpid() }.map { $0.objectID }
            desc = CATapDescription(stereoGlobalTapButExcludeProcesses: me)
            tapPIDs = []
        }
        desc.uuid = UUID(); desc.muteBehavior = .unmuted; desc.isPrivate = true
        var tid: AudioObjectID = 0
        var st = AudioHardwareCreateProcessTap(desc, &tid)
        if mode == "per-app" && ProcessInfo.processInfo.environment["DOP_CR_TEST_FAIL_TAP"] != nil { AudioHardwareDestroyProcessTap(tid); st = -1 }
        guard st == noErr else { note("tap_create_failed", ["status": Int(st), "mode": mode]); fallbackToSystem("tap_create_failed"); return }
        tapID = tid
        let outDev: AudioObjectID = prop(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice, 0)
        let outUID = cfStringProp(outDev, kAudioDevicePropertyDeviceUID) ?? ""
        let aggDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "dop-cr-recorder-tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: desc.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]],
        ]
        var agg: AudioObjectID = 0
        st = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &agg)
        guard st == noErr else { note("aggregate_create_failed", ["status": Int(st)]); AudioHardwareDestroyProcessTap(tid); tapID = 0; fallbackToSystem("aggregate_create_failed"); return }
        aggID = agg
        var asbd: AudioStreamBasicDescription = prop(tid, kAudioTapPropertyFormat, AudioStreamBasicDescription())
        // The tap format can report a stale rate (e.g. 48k while a Bluetooth headset is in 24k HFP mode): trust the aggregate device's actual rate.
        let devRate: Double = prop(agg, kAudioDevicePropertyNominalSampleRate, 0.0)
        if devRate > 0 { asbd.mSampleRate = devRate }
        aggRate = devRate
        var ra = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyNominalSampleRate, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        AudioObjectAddPropertyListenerBlock(agg, &ra, ctl) { [weak self] _, _ in
            guard let self = self, !self.stopping, self.aggID == agg else { return }
            self.rebuildTap(reason: "sample_rate_change")
        }
        guard let fmt = AVAudioFormat(streamDescription: &asbd) else { note("tap_format_invalid"); return }
        tapFormat = fmt
        let rs = othersRS
        st = AudioDeviceCreateIOProcIDWithBlock(&ioProc, agg, ioQueue) { _, inData, _, _, _ in
            guard let b = AVAudioPCMBuffer(pcmFormat: fmt, bufferListNoCopy: inData, deallocator: nil) else { return }
            rs.feed(b)
        }
        guard st == noErr else { note("ioproc_failed", ["status": Int(st)]); fallbackToSystem("ioproc_failed"); return }
        st = AudioDeviceStart(agg, ioProc)
        note(st == noErr ? "tap_started" : "tap_start_failed", ["status": Int(st), "mode": mode, "pids": tapPIDs.map { Int($0) }.sorted(), "flags": Int(asbd.mFormatFlags), "bpf": Int(asbd.mBytesPerFrame), "interleaved": fmt.isInterleaved, "rate": fmt.sampleRate, "channels": Int(fmt.channelCount), "output_uid": outUID])
    }
    func rebuildTap(reason: String) { note("tap_rebuild", ["reason": reason]); setupTap() }

    func tick() {
        if stopping { return }
        if mode == "per-app" {
            let now = Set(currentTargets().map { $0.pid })
            // only NEW processes (e.g. Teams starts a call helper) need a rebuild; vanished ones are harmless
            if !now.isEmpty && !now.isSubset(of: tapPIDs) { rebuildTap(reason: "new_process") }
        }
        let el = Date().timeIntervalSince(started)
        others.padSilence(upTo: el - 1); me.padSilence(upTo: el - 1)
        others.heartbeat(); me.heartbeat()
        writeState()
    }

    func writeState() {
        func w(_ c: ChunkWriter) -> [String: Any] {
            c.lock.lock(); defer { c.lock.unlock() }
            return ["chunks": c.chunks, "total_seconds": Double(c.totalSamples) / SR, "peak": Int(c.peak),
             "last_active": c.lastActive.map { ISO8601DateFormatter().string(from: $0) } ?? NSNull()]
        }
        let st: [String: Any] = [
            "pid": getpid(), "started": ISO8601DateFormatter().string(from: started),
            "heartbeat": ISO8601DateFormatter().string(from: Date()), "heartbeat_epoch": Date().timeIntervalSince1970,
            "state": stopping ? "stopped" : "recording", "mic_permission": AVCaptureDevice.authorizationStatus(for: .audio).rawValue == 3 ? "authorized" : "NOT_AUTHORIZED", "mode": mode, "apps": wanted,
            "tap_active": tapID != 0, "tap_pids": tapPIDs.map { Int($0) }.sorted(),
            "chunk_seconds": CHUNK_SECONDS, "sample_rate": 16000,
            "others": w(others), "me": w(me), "events": events,
        ]
        if let d = try? JSONSerialization.data(withJSONObject: st, options: [.prettyPrinted, .sortedKeys]) {
            let tmp = outDir + "/state.json.tmp"
            try? d.write(to: URL(fileURLWithPath: tmp))
            _ = rename(tmp, outDir + "/state.json")
        }
    }

    func stop() {
        stopping = true
        timer?.cancel()
        engine.stop(); engine.inputNode.removeTap(onBus: 0)
        destroyTap()
        ioQueue.sync {}
        others.finish(); me.finish()
        writeState()
        note("stopped", ["others_seconds": Double(others.totalSamples) / SR, "me_seconds": Double(me.totalSamples) / SR])
    }
}

// MARK: CLI
func usage() -> Never {
    FileHandle.standardError.write("""
    usage: recorder list-apps [--all]
           recorder start --apps <bundle,ids> --out <dir> [--system-fallback]
           recorder stop
           recorder check-permissions|request-mic|probe-tap [--out FILE]   (JSON; run via `open -W -n` so TCC is this bundle's)
    """.data(using: .utf8)!)
    exit(2)
}

// MARK: permission checks (run via `open -W -n recorder.app --args <cmd> --out FILE` so TCC is attributed to this bundle)
func micStatusName() -> String {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: return "authorized"
    case .denied: return "denied"
    case .restricted: return "restricted"
    default: return "not_determined"
    }
}

/// 1-second global process tap: noErr + non-silent buffers = audio capture proven; silent = unknown (nothing playing, or denied).
func probeTap(seconds: Double = 1.0) -> [String: Any] {
    let me = audioProcesses().filter { $0.pid == getpid() }.map { $0.objectID }
    let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: me)
    desc.uuid = UUID(); desc.muteBehavior = .unmuted; desc.isPrivate = true
    var tid: AudioObjectID = 0
    var st = AudioHardwareCreateProcessTap(desc, &tid)
    guard st == noErr else { return ["tap": "create_failed", "status": Int(st)] }
    defer { AudioHardwareDestroyProcessTap(tid) }
    let outDev: AudioObjectID = prop(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultOutputDevice, 0)
    let outUID = cfStringProp(outDev, kAudioDevicePropertyDeviceUID) ?? ""
    let aggDesc: [String: Any] = [
        kAudioAggregateDeviceNameKey: "dop-cr-permission-probe", kAudioAggregateDeviceUIDKey: UUID().uuidString,
        kAudioAggregateDeviceMainSubDeviceKey: outUID, kAudioAggregateDeviceIsPrivateKey: true, kAudioAggregateDeviceIsStackedKey: false,
        kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
        kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: desc.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]],
    ]
    var agg: AudioObjectID = 0
    st = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &agg)
    guard st == noErr else { return ["tap": "aggregate_failed", "status": Int(st)] }
    defer { AudioHardwareDestroyAggregateDevice(agg) }
    var asbd: AudioStreamBasicDescription = prop(tid, kAudioTapPropertyFormat, AudioStreamBasicDescription())
    guard let fmt = AVAudioFormat(streamDescription: &asbd) else { return ["tap": "format_invalid"] }
    let lock = NSLock(); var peak: Float = 0; var frames = 0
    var proc: AudioDeviceIOProcID?
    let q = DispatchQueue(label: "probe")
    st = AudioDeviceCreateIOProcIDWithBlock(&proc, agg, q) { _, inData, _, _, _ in
        guard let b = AVAudioPCMBuffer(pcmFormat: fmt, bufferListNoCopy: inData, deallocator: nil), let ch = b.floatChannelData else { return }
        var p: Float = 0
        for c in 0..<Int(b.format.channelCount) { for i in 0..<Int(b.frameLength) { p = max(p, abs(ch[c][i])) } }
        lock.lock(); peak = max(peak, p); frames += Int(b.frameLength); lock.unlock()
    }
    guard st == noErr, let pr = proc else { return ["tap": "ioproc_failed", "status": Int(st)] }
    AudioDeviceStart(agg, pr)
    Thread.sleep(forTimeInterval: seconds)
    AudioDeviceStop(agg, pr); AudioDeviceDestroyIOProcID(agg, pr)
    lock.lock(); defer { lock.unlock() }
    return ["tap": frames == 0 ? "no_buffers" : (peak > 0.0005 ? "audio" : "silent"), "peak": Double(peak), "frames": frames]
}

func emit(_ o: [String: Any], out: String?) {
    var d = o; d["bundle"] = Bundle.main.bundleIdentifier ?? "?"
    let data = (try? JSONSerialization.data(withJSONObject: d, options: [.sortedKeys])) ?? Data()
    if let out = out { try? data.write(to: URL(fileURLWithPath: out)) } else { print(String(data: data, encoding: .utf8) ?? "{}") }
}

var signalSources: [DispatchSourceSignal] = []

/// Recorder commands (`start`, `stop`, `list-apps`, `check-permissions`, `request-mic`, `probe-tap`).
func recorderMain(_ args: [String]) -> Never {
    guard let cmd = args.first else { usage() }
    func opt(_ name: String) -> String? { if let i = args.firstIndex(of: name), i + 1 < args.count { return args[i + 1] }; return nil }

    switch cmd {
    case "check-permissions":
        emit(["mic": micStatusName()], out: opt("--out")); exit(0)
    case "request-mic":
        let sem = DispatchSemaphore(value: 0)
        if AVCaptureDevice.authorizationStatus(for: .audio) == .notDetermined { AVCaptureDevice.requestAccess(for: .audio) { _ in sem.signal() }; _ = sem.wait(timeout: .now() + 60) }
        emit(["mic": micStatusName()], out: opt("--out")); exit(0)
    case "probe-tap":
        emit(probeTap(), out: opt("--out")); exit(0)
    case "list-apps":
        listApps(all: args.contains("--all"))
    case "stop":
        guard let s = try? String(contentsOfFile: pidFile, encoding: .utf8), let pid = pid_t(s.trimmingCharacters(in: .whitespacesAndNewlines)), kill(pid, 0) == 0 else {
            print("not running"); exit(0)
        }
        guard procPath(pid).contains("DOPCallRecorder") else {
            try? FileManager.default.removeItem(atPath: pidFile); print("not running (stale pidfile removed)"); exit(0)
        }
        kill(pid, SIGTERM)
        for _ in 0..<100 { if kill(pid, 0) != 0 { print("stopped"); exit(0) }; usleep(100_000) }
        print("timeout waiting for recorder to exit"); exit(1)
    case "start":
        guard let apps = opt("--apps"), let out = opt("--out") else { usage() }
        let wanted = apps.split(separator: ",").map { String($0).trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        let outDir = NSString(string: out).expandingTildeInPath
        try? FileManager.default.createDirectory(atPath: outDir, withIntermediateDirectories: true)
        try? FileManager.default.createDirectory(atPath: appSupport, withIntermediateDirectories: true)
        if let s = try? String(contentsOfFile: pidFile, encoding: .utf8), let p = pid_t(s.trimmingCharacters(in: .whitespacesAndNewlines)), kill(p, 0) == 0, p != getpid() {
            jlog("already_running", ["pid": Int(p)]); exit(3)
        }
        try? "\(getpid())".write(toFile: pidFile, atomically: true, encoding: .utf8)
        let rec = Recorder(wanted: wanted, outDir: outDir, fallbackAll: args.contains("--system-fallback"))
        if args.contains("--system-fallback") { rec.mode = "system-fallback" }

        func requestMic(_ cb: @escaping (Bool) -> Void) {
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized: cb(true)
            case .notDetermined: AVCaptureDevice.requestAccess(for: .audio, completionHandler: cb)
            default: cb(false)
            }
        }
        signal(SIGTERM, SIG_IGN); signal(SIGINT, SIG_IGN)
        let sigs = [SIGTERM, SIGINT].map { s -> DispatchSourceSignal in
            let src = DispatchSource.makeSignalSource(signal: s, queue: .main)
            src.setEventHandler { rec.stop(); try? FileManager.default.removeItem(atPath: pidFile); exit(0) }
            src.resume(); return src
        }
        signalSources = sigs
        requestMic { ok in
            DispatchQueue.main.async {
                if !ok { jlog("mic_permission_denied"); }
                do { try rec.start(); jlog("recording", ["out": outDir, "apps": wanted, "mode": rec.mode]) }
                catch { jlog("start_failed", ["error": "\(error)"]); try? FileManager.default.removeItem(atPath: pidFile); exit(1) }
            }
        }
        RunLoop.main.run()
    default:
        usage()
    }
    exit(0)
}
