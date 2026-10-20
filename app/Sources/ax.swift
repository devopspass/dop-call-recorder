// axhelper: reads the Microsoft Teams UI through the macOS Accessibility API (X1).
//   axhelper check                      -> {"trusted":bool}  (no prompt)
//   axhelper request                    -> prompts for Accessibility permission (adds the app to the list)
//   axhelper dump [--out F] [--depth N] [--max N] [--bundle ID]   full AX tree of all app windows (JSON)
//   axhelper snapshot [--out F]         one {"title","roster","speakers","self",...} JSON line
//   axhelper poll --out F [--interval S] [--stop-file F] [--watch-pid P] [--max-seconds N]
//                                       overwrite F (atomically) with the latest snapshot every S s (default 1) until the
//                                       stop-file exists / SIGTERM / watched pid exits / max-seconds elapsed
//   axhelper shot --png F               one screenshot of the main display (so Screen Recording belongs to this app)
//   poll ... --shot-req F --shot-png P  also serves screenshot requests: when F appears, capture to P (atomically) and delete F
// Launch through LaunchServices (`open -n -a ... --args ...`) so the permission belongs to this app bundle.
import Cocoa
import ApplicationServices

let defaultBundle = "com.microsoft.teams2"

func attr(_ e: AXUIElement, _ name: String) -> AnyObject? {
    var v: CFTypeRef?
    return AXUIElementCopyAttributeValue(e, name as CFString, &v) == .success ? (v as AnyObject?) : nil
}
func str(_ e: AXUIElement, _ name: String) -> String? {
    guard let v = attr(e, name) else { return nil }
    if let s = v as? String { return s.isEmpty ? nil : s }
    if let n = v as? NSNumber { return n.stringValue }
    return nil
}
func children(_ e: AXUIElement) -> [AXUIElement] { (attr(e, "AXChildren") as? [AXUIElement]) ?? [] }

var enabledPids = Set<pid_t>()
func enableAX(_ app: AXUIElement) {
    var pid: pid_t = 0
    AXUIElementGetPid(app, &pid)
    let already = (attr(app, "AXEnhancedUserInterface") as? Bool) == true || enabledPids.contains(pid)
    defer { enabledPids.insert(pid); if !already { Thread.sleep(forTimeInterval: 1.0) } }   // Chromium builds the web tree asynchronously
    AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
    AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
}

func teamsApps(_ bundle: String) -> [NSRunningApplication] {
    NSWorkspace.shared.runningApplications.filter { ($0.bundleIdentifier ?? "") == bundle }
}

struct Node {
    var role: String; var subrole: String?; var title: String?; var desc: String?; var value: String?
    var help: String?; var ident: String?; var dom: String?; var cls: String?; var rdesc: String?
    var selected: String?; var kids: [Node]
    func json() -> [String: Any] {
        var d: [String: Any] = ["role": role]
        for (k, v) in [("subrole", subrole), ("title", title), ("desc", desc), ("value", value), ("help", help),
                       ("id", ident), ("dom", dom), ("class", cls), ("rdesc", rdesc), ("selected", selected)] {
            if let v = v { d[k] = v }
        }
        if !kids.isEmpty { d["children"] = kids.map { $0.json() } }
        return d
    }
}

var budget = 0
var gTerminated = false
func walk(_ e: AXUIElement, _ depth: Int, _ maxDepth: Int) -> Node {
    budget -= 1
    var cls: String? = nil
    if let c = attr(e, "AXDOMClassList") as? [String] { cls = c.joined(separator: " ") }
    var n = Node(role: str(e, "AXRole") ?? "?", subrole: str(e, "AXSubrole"), title: str(e, "AXTitle"),
                 desc: str(e, "AXDescription"), value: str(e, "AXValue"), help: str(e, "AXHelp"),
                 ident: str(e, "AXIdentifier"), dom: str(e, "AXDOMIdentifier"), cls: cls,
                 rdesc: str(e, "AXRoleDescription"), selected: str(e, "AXSelected"), kids: [])
    if depth < maxDepth && budget > 0 {
        for c in children(e) { if budget <= 0 { break }; n.kids.append(walk(c, depth + 1, maxDepth)) }
    }
    return n
}

func windows(_ app: NSRunningApplication) -> [AXUIElement] {
    let el = AXUIElementCreateApplication(app.processIdentifier)
    enableAX(el)
    return (attr(el, "AXWindows") as? [AXUIElement]) ?? []
}

// ---- flatten for heuristics
struct Flat { var role: String; var text: [String]; var node: Node }
func flatten(_ n: Node, _ out: inout [Node]) { out.append(n); for k in n.kids { flatten(k, &out) } }
func texts(_ n: Node) -> [String] {
    var r: [String] = []
    for s in [n.title, n.desc, n.value, n.help] { if let s = s, !s.isEmpty { r.append(s) } }
    return r
}
func allText(_ n: Node) -> [String] { var r = texts(n); for k in n.kids { r += allText(k) }; return r }

func jsonString(_ o: Any, pretty: Bool = false) -> String {
    let d = try? JSONSerialization.data(withJSONObject: o, options: pretty ? [.prettyPrinted, .sortedKeys] : [.sortedKeys])
    return d.flatMap { String(data: $0, encoding: .utf8) } ?? "{}"
}

// Runs screencapture from this bundle: LaunchServices-launched, so the Screen Recording grant is this app's.
func takeShot(_ path: String) {
    let tmp = path + ".tmp.png"
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    p.arguments = ["-x", "-t", "png", tmp]
    try? p.run(); p.waitUntilExit()
    if p.terminationStatus == 0, let a = try? FileManager.default.attributesOfItem(atPath: tmp), (a[.size] as? Int ?? 0) > 1000 {
        _ = try? FileManager.default.replaceItemAt(URL(fileURLWithPath: path), withItemAt: URL(fileURLWithPath: tmp))
        if !FileManager.default.fileExists(atPath: path) { try? FileManager.default.moveItem(atPath: tmp, toPath: path) }
    } else { try? FileManager.default.removeItem(atPath: tmp) }
}

/// Accessibility commands (`check`, `request`, `dump`, `snapshot`, `poll`, `shot`).
func axMain(_ argv: [String]) -> Never {
    // ---- args
    var args = argv
    let cmd = args.isEmpty ? "snapshot" : args.removeFirst()
    func opt(_ n: String) -> String? { if let i = args.firstIndex(of: n), i + 1 < args.count { return args[i + 1] }; return nil }
    let outPath = opt("--out")
    let bundle = opt("--bundle") ?? defaultBundle
    func emit(_ s: String) {
        if let p = outPath {
            try? (s + "\n").write(toFile: p, atomically: true, encoding: .utf8)   // poll: only the latest snapshot is kept
        } else { print(s) }
    }

    switch cmd {
    case "check":
        emit(jsonString(["trusted": AXIsProcessTrusted(), "bundle": Bundle.main.bundleIdentifier ?? "?"]))
    case "request":
        let o = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        emit(jsonString(["trusted": AXIsProcessTrustedWithOptions(o)]))
    case "shot":
        guard let png = opt("--png") else { exit(2) }
        try? FileManager.default.removeItem(atPath: png)
        takeShot(png)
        exit(FileManager.default.fileExists(atPath: png) ? 0 : 4)
    case "dump":
        guard AXIsProcessTrusted() else { emit(jsonString(["error": "not_trusted"])); exit(3) }
        let maxDepth = Int(opt("--depth") ?? "60") ?? 60
        budget = Int(opt("--max") ?? "20000") ?? 20000
        var res: [String: Any] = ["bundle": bundle, "ts": Date().timeIntervalSince1970]
        var wins: [[String: Any]] = []
        for app in teamsApps(bundle) {
            for w in windows(app) { var d = walk(w, 0, maxDepth).json(); d["pid"] = app.processIdentifier; wins.append(d) }
        }
        res["windows"] = wins
        res["nodes_left_budget"] = budget
        emit(jsonString(res, pretty: true))
    case "snapshot", "poll":
        let interval = Double(opt("--interval") ?? "1") ?? 1
        let stop = opt("--stop-file")
        let watchPid = Int32(opt("--watch-pid") ?? "")
        let maxSec = Double(opt("--max-seconds") ?? "") ?? 86400
        let shotReq = opt("--shot-req"), shotPng = opt("--shot-png")
        let started = Date()
        signal(SIGTERM) { _ in gTerminated = true }
        repeat {
            var rec: [String: Any] = ["ts": Date().timeIntervalSince1970, "pid": ProcessInfo.processInfo.processIdentifier]
            if !AXIsProcessTrusted() { rec["error"] = "not_trusted" }
            else {
                let apps = teamsApps(bundle)
                rec["running"] = !apps.isEmpty
                var dumps: [Node] = []
                for app in apps { for w in windows(app) { budget = 6000; dumps.append(walk(w, 0, 60)) } }
                rec["windows"] = dumps.map { ["title": $0.title ?? ""] as [String: Any] }
                rec["trees"] = dumps.map { $0.json() }   // raw trees are interpreted in Python (dop_call_recorder/axparse.py)
            }
            emit(jsonString(rec))
            if cmd == "snapshot" { break }
            let waitUntil = Date().addingTimeInterval(interval)
            while Date() < waitUntil && !gTerminated {   // sleep in slices so screenshot requests are served within ~0.1 s
                if let rq = shotReq, let png = shotPng, FileManager.default.fileExists(atPath: rq) {
                    try? FileManager.default.removeItem(atPath: png)
                    takeShot(png)
                    try? FileManager.default.removeItem(atPath: rq)
                }
                Thread.sleep(forTimeInterval: 0.1)
            }
            if let s = stop, FileManager.default.fileExists(atPath: s) { break }
            if let w = watchPid, kill(w, 0) != 0 && errno == ESRCH { break }
            if Date().timeIntervalSince(started) > maxSec { break }
        } while !gTerminated
    default:
        FileHandle.standardError.write("usage: axhelper check|request|dump|snapshot|poll\n".data(using: .utf8)!); exit(2)
    }
    exit(0)
}
