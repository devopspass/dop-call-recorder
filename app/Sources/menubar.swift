// Menu-bar status app for DOP Call Recorder (M5). Reads status.json written by `dop-recorder run`, writes control.json (pause).
// State dir: $DOP_STATE_DIR or ~/Library/Application Support/dop-call-recorder.
// `menubar --print` prints the title/menu that would be shown for the current status and exits (headless smoke test).
import Cocoa
import Carbon.HIToolbox
import UserNotifications

let stateDir: String = {
    if let e = ProcessInfo.processInfo.environment["DOP_STATE_DIR"] ?? ProcessInfo.processInfo.environment["DOP_CR_STATE_DIR"], !e.isEmpty { return NSString(string: e).expandingTildeInPath }
    return NSString(string: "~/Library/Application Support/dop-call-recorder").expandingTildeInPath
}()
let statusFile = stateDir + "/status.json"
let controlFile = stateDir + "/control.json"
let staleSeconds = 20.0

func readJSON(_ path: String) -> [String: Any] {
    guard let d = FileManager.default.contents(atPath: path),
          let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return [:] }
    return o
}

func isPaused() -> Bool { (readJSON(controlFile)["paused"] as? Bool) ?? false }

func setPaused(_ p: Bool) {
    try? FileManager.default.createDirectory(atPath: stateDir, withIntermediateDirectories: true)
    let o: [String: Any] = ["paused": p, "by": "menubar", "ts": Date().timeIntervalSince1970]
    if let d = try? JSONSerialization.data(withJSONObject: o) { try? d.write(to: URL(fileURLWithPath: controlFile), options: .atomic) }
}

func dur(_ s: Double?) -> String {
    guard let s = s else { return "?" }
    let i = Int(s); let h = i / 3600, m = (i % 3600) / 60, sec = i % 60
    return h > 0 ? "\(h)h\(String(format: "%02d", m))m" : (m > 0 ? "\(m)m\(String(format: "%02d", sec))s" : "\(sec)s")
}

let healthFile = stateDir + "/health.json"
let notifiedFile = stateDir + "/health_notified.json"
let ctRoot: String = {
    if let p = Bundle.main.path(forResource: "dop_cr_root", ofType: nil), let t = try? String(contentsOfFile: p, encoding: .utf8) { return t.trimmingCharacters(in: .whitespacesAndNewlines) }
    return ""
}()
let recorderBin: String = {
    if let e = ProcessInfo.processInfo.environment["DOP_RECORDER_BIN"], !e.isEmpty { return e }
    let dev = ctRoot + "/.venv/bin/dop-recorder"
    if FileManager.default.isExecutableFile(atPath: dev) { return dev }
    for p in ["/opt/homebrew/bin/dop-recorder", "/usr/local/bin/dop-recorder"] where FileManager.default.isExecutableFile(atPath: p) { return p }
    return "dop-recorder"
}()
let notifSettingsURL = "x-apple.systempreferences:com.apple.Notifications-Settings.extension"

struct HealthRow { var id: String; var group: String; var name: String; var status: String; var detail: String; var required: Bool
                   var actionLabel: String?; var actionType: String?; var actionCommand: [String]; var actionURL: String?; var actionText: String? }

func statusIcon(_ s: String) -> String { s == "ok" ? "✅" : (s == "fail" ? "❌" : "⚠️") }

/// Rows from dop-recorder's health.json (`dop-recorder permissions --write`) plus the Notifications row, which only this app can query.
/// notif: "authorized" | "denied" | "not_determined" | "unknown"
func healthRows(notif: String) -> [HealthRow] {
    var rows: [HealthRow] = []
    for o in (readJSON(healthFile)["items"] as? [[String: Any]]) ?? [] {
        let a = o["action"] as? [String: Any]
        rows.append(HealthRow(id: (o["id"] as? String) ?? "", group: (o["group"] as? String) ?? "", name: (o["name"] as? String) ?? "?",
                              status: (o["status"] as? String) ?? "warn", detail: (o["detail"] as? String) ?? "", required: (o["required"] as? Bool) ?? false,
                              actionLabel: a?["label"] as? String, actionType: a?["type"] as? String, actionCommand: (a?["command"] as? [String]) ?? [],
                              actionURL: a?["url"] as? String, actionText: a?["text"] as? String))
    }
    let ns: (String, String) = {
        switch notif {
        case "authorized": return ("ok", "banners allowed")
        case "denied": return ("fail", "notifications are blocked: you won't see the recording-start banner")
        case "not_determined": return ("warn", "not asked yet")
        default: return ("warn", "unknown")
        }
    }()
    let nr = HealthRow(id: "notifications", group: "Permissions", name: "Notifications (menu app)", status: ns.0, detail: ns.1, required: true,
                       actionLabel: ns.0 == "ok" ? nil : "Open Notifications settings…", actionType: ns.0 == "ok" ? nil : "url", actionCommand: [], actionURL: notifSettingsURL, actionText: nil)
    if let i = rows.lastIndex(where: { $0.group == "Permissions" }) { rows.insert(nr, at: i + 1) } else { rows.insert(nr, at: 0) }
    return rows
}

func healthSummary(_ rows: [HealthRow]) -> (title: String, attention: Bool, ids: [String]) {
    if rows.count <= 1 { return ("⚠️ no health data yet", false, []) }
    let f = rows.filter { $0.status == "fail" }.count, w = rows.filter { $0.status == "warn" }.count
    let ids = rows.filter { $0.required && $0.status == "fail" }.map { $0.id }
    let t = (f == 0 && w == 0) ? "✅ All OK" : [f > 0 ? "❌ \(f) missing" : "", w > 0 ? "⚠️ \(w) warning" : ""].filter { !$0.isEmpty }.joined(separator: "  ")
    return (t, !ids.isEmpty, ids)
}

struct View { var state: String; var icon: String; var lines: [String]; var paused: Bool; var running: Bool; var lastTranscript: String?; var outputDir: String }

func compute(attention: Bool = false) -> View {
    let st = readJSON(statusFile)
    let ts = (st["ts"] as? Double) ?? 0
    var state = (st["state"] as? String) ?? "stopped"
    let running = !st.isEmpty && Date().timeIntervalSince1970 - ts < staleSeconds && state != "stopped"
    let paused = isPaused()
    if !running { state = "stopped" }
    let icons = ["idle": "🎙", "recording": "🔴", "transcribing": "⏳", "paused": "⏸", "error": "⚠️", "stopped": "⚪️", "attention": "🟠"]
    var icon = icons[state] ?? "🎙"
    if running && paused && state == "idle" { icon = icons["paused"]!; state = "paused" }
    if attention && (state == "idle" || state == "stopped") { icon = icons["attention"]!; state = "attention" }
    var lines: [String] = []
    switch state {
    case "attention": lines.append(running ? (paused ? "Paused — " : "Idle — ") + "permission missing, see Permissions & health" : "Agent not running; permission missing, see Permissions & health")
    case "stopped": lines.append("Agent not running (start `dop-recorder run` or load the LaunchAgent)")
    case "recording":
        let a = st["active"] as? [String: Any]
        lines.append("Recording: \((a?["title"] as? String) ?? (a?["app"] as? String) ?? "call") — \(dur(a?["elapsed_s"] as? Double))")
    case "transcribing":
        if let p = st["progress"] as? [String: Any] {
            let f = ((p["fraction"] as? Double) ?? 0) * 100
            lines.append("Transcribing: \(Int(f))%, ETA \(dur(p["eta_s"] as? Double))")
        } else { lines.append("Transcribing: queued") }
    case "error": lines.append("Error: \(((st["last_error"] as? [String: Any])?["message"] as? String) ?? "see dop-call-recorder.log")")
    default: lines.append(paused ? "Paused" : "Idle — listening for calls")
    }
    if let q = st["queue_len"] as? Int, q > 0 { lines.append("Queue: \(q)") }
    var last: String? = nil
    if let lt = st["last_transcript"] as? [String: Any] { last = lt["path"] as? String }
    let out = (st["output_dir"] as? String) ?? NSString(string: "~/Transcripts").expandingTildeInPath
    return View(state: state, icon: icon, lines: lines, paused: paused, running: running, lastTranscript: last, outputDir: out)
}

/// Menu-bar app (default mode: no command). `--print` / `--print-health` are headless smoke tests.
func menubarMain() -> Never {
    if CommandLine.arguments.contains("--print") {
        let rows0 = healthRows(notif: "unknown"); let hs0 = healthSummary(rows0.filter { $0.id != "notifications" })
        let v = compute(attention: hs0.attention)
        print("icon=\(v.icon)"); v.lines.forEach { print("line=\($0)") }
        print("paused=\(v.paused) running=\(v.running) last=\(v.lastTranscript ?? "-") out=\(v.outputDir)")
        exit(0)
    }

    // `menubar --print-health`: the Permissions & health submenu structure + resulting tray icon state (headless smoke test).
    if CommandLine.arguments.contains("--print-health") {
        let rows = healthRows(notif: "unknown")
        let hs = healthSummary(rows.filter { $0.id != "notifications" })
        print("submenu=Permissions & health  \(hs.title)")
        var g = ""
        for r in rows {
            if r.group != g { g = r.group; print("group=\(g)") }
            print("row=\(statusIcon(r.status)) \(r.name) — \(r.detail)" + (r.actionLabel.map { " | action=\($0) (\(r.actionType ?? "?"))" } ?? ""))
        }
        let v = compute(attention: hs.attention)
        print("state=\(v.state) attention=\(hs.attention) ids=\(hs.ids.joined(separator: ","))")
        print("footer=Re-check now | Open full report (dop-recorder doctor)")
        exit(0)
    }

    final class AppDelegate: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, NSMenuDelegate {
        var item: NSStatusItem!
        var timer: Timer?
        var view = compute()
        var notif = "unknown"
        var rows: [HealthRow] = []
        var healthBusy = false
        var notifiedOnce = false
        var pendingActions: [Int: HealthRow] = [:]
        func statusImage(for state: String) -> NSImage? {
            guard let p = Bundle.main.path(forResource: state, ofType: "png", inDirectory: "menubar"), let img = NSImage(contentsOfFile: p) else { return nil }
            if let p2 = Bundle.main.path(forResource: state + "@2x", ofType: "png", inDirectory: "menubar"), let r = NSImage(contentsOfFile: p2) { img.addRepresentation(r.representations[0]) }
            img.size = NSSize(width: 18, height: 18); img.isTemplate = false
            return img
        }
        var hotKeyRef: EventHotKeyRef?

        // Notifications queued by `dop-recorder` as JSON files in <stateDir>/notify; posted here so the banner uses this app's icon.
        func drainNotifications() {
            let dir = stateDir + "/notify"
            guard let files = try? FileManager.default.contentsOfDirectory(atPath: dir) else { return }
            for f in files.sorted() where f.hasSuffix(".json") {
                let path = dir + "/" + f
                let o = readJSON(path)
                let c = UNMutableNotificationContent()
                c.title = (o["title"] as? String) ?? "DOP Call Recorder"
                c.body = (o["message"] as? String) ?? ""
                if let snd = o["sound"] as? String, !snd.isEmpty { c.sound = UNNotificationSound(named: UNNotificationSoundName(snd)) }
                if let op = o["open"] as? String, !op.isEmpty { c.userInfo = ["open": op] }
                if let u = o["url"] as? String, !u.isEmpty { c.userInfo = ["url": u] }
                UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
                try? FileManager.default.removeItem(atPath: path)
            }
        }

        func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler done: @escaping () -> Void) {
            if let p = response.notification.request.content.userInfo["open"] as? String { NSWorkspace.shared.open(URL(fileURLWithPath: p)) }
            if let u = response.notification.request.content.userInfo["url"] as? String, let url = URL(string: u) { NSWorkspace.shared.open(url) }
            done()
        }
        func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent n: UNNotification, withCompletionHandler done: @escaping (UNNotificationPresentationOptions) -> Void) {
            done([.banner, .sound])
        }

        func applicationDidFinishLaunching(_ n: Notification) {
            item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
            UNUserNotificationCenter.current().delegate = self
            UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { [weak self] _, _ in self?.queryNotifications() }
            queryNotifications()
            refreshHealth(probe: false)
            Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.refreshHealth(probe: false); self?.queryNotifications() }
            refresh()
            timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
            Timer.scheduledTimer(withTimeInterval: 0.3, repeats: true) { [weak self] _ in self?.drainNotifications() }
            registerHotKey()
        }

        // Opening the menu re-checks right away so a just-fixed permission shows up without waiting for the timer.
        func menuWillOpen(_ menu: NSMenu) { refreshHealth(probe: false); queryNotifications() }

        func queryNotifications() {
            UNUserNotificationCenter.current().getNotificationSettings { [weak self] st in
                let v: String = { switch st.authorizationStatus { case .authorized, .provisional: return "authorized"; case .denied: return "denied"; case .notDetermined: return "not_determined"; default: return "unknown" } }()
                DispatchQueue.main.async { self?.notif = v; self?.refresh(); self?.maybeNotifyMissing() }
            }
        }

        /// Runs `dop-recorder permissions --write` (health.json) off the main thread; probe=true adds the 1 s system-audio tap probe.
        func refreshHealth(probe: Bool, after: (() -> Void)? = nil) {
            if healthBusy { return }
            healthBusy = true
            DispatchQueue.global().async { [weak self] in
                let p = Process()
                p.executableURL = URL(fileURLWithPath: recorderBin)
                p.arguments = ["permissions", "--write"] + (probe ? ["--probe"] : [])
                p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
                try? p.run(); p.waitUntilExit()
                DispatchQueue.main.async { self?.healthBusy = false; self?.refresh(); self?.maybeNotifyMissing(); after?() }
            }
        }

        /// One notification per distinct set of missing required permissions (remembered across restarts).
        func maybeNotifyMissing() {
            let hs = healthSummary(rows)
            let ids = hs.ids.sorted()
            guard hs.attention, notif != "unknown" || !ids.isEmpty else { return }
            let last = (readJSON(notifiedFile)["ids"] as? [String]) ?? []
            if last == ids { return }
            let names = rows.filter { ids.contains($0.id) }.map { $0.name }.joined(separator: ", ")
            let c = UNMutableNotificationContent()
            c.title = "DOP Call Recorder: permission missing"
            c.body = "\(names). Open the menu-bar icon > Permissions & health to fix."
            c.userInfo = ["url": ids.contains("notifications") ? notifSettingsURL : "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"]
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
            if let d = try? JSONSerialization.data(withJSONObject: ["ids": ids]) { try? d.write(to: URL(fileURLWithPath: notifiedFile), options: .atomic) }
        }

        func refresh() {
            rows = healthRows(notif: notif)
            let hs = healthSummary(rows)
            view = compute(attention: hs.attention)
            if let img = statusImage(for: view.state) { item.button?.image = img; item.button?.title = "" } else { item.button?.title = view.icon }
            let m = NSMenu()
            for l in view.lines { let i = NSMenuItem(title: l, action: nil, keyEquivalent: ""); i.isEnabled = false; m.addItem(i) }
            m.addItem(.separator())
            let lt = NSMenuItem(title: "Open last transcript", action: #selector(openLast), keyEquivalent: "")
            lt.target = self; lt.isEnabled = view.lastTranscript != nil; m.addItem(lt)
            let od = NSMenuItem(title: "Open transcripts folder", action: #selector(openFolder), keyEquivalent: ""); od.target = self; m.addItem(od)
            m.addItem(.separator())
            m.addItem(healthMenuItem(hs.title))
            m.addItem(.separator())
            let pr = NSMenuItem(title: view.paused ? "Resume recording  (⌃⌥⌘P)" : "Pause recording  (⌃⌥⌘P)", action: #selector(togglePause), keyEquivalent: ""); pr.target = self; m.addItem(pr)
            m.addItem(.separator())
            let q = NSMenuItem(title: "Quit menu icon", action: #selector(quit), keyEquivalent: "q"); q.target = self; m.addItem(q)
            m.delegate = self
            item.menu = m
        }

        func healthMenuItem(_ title: String) -> NSMenuItem {
            let top = NSMenuItem(title: "Permissions & health   \(title)", action: nil, keyEquivalent: "")
            let sub = NSMenu(); pendingActions = [:]
            var g = ""
            for (i, r) in rows.enumerated() {
                if r.group != g { if !g.isEmpty { sub.addItem(.separator()) }; g = r.group
                    let h = NSMenuItem(title: g.uppercased(), action: nil, keyEquivalent: ""); h.isEnabled = false; sub.addItem(h) }
                let it = NSMenuItem(title: "\(statusIcon(r.status)) \(r.name)", action: nil, keyEquivalent: "")
                it.toolTip = r.detail
                if r.status == "ok" { it.isEnabled = false; it.title += " — OK" } else {
                    let d = NSMenu()
                    let di = NSMenuItem(title: String(r.detail.prefix(110)), action: nil, keyEquivalent: ""); di.isEnabled = false; d.addItem(di)
                    if let l = r.actionLabel { let fx = NSMenuItem(title: l, action: #selector(runFix(_:)), keyEquivalent: ""); fx.target = self; fx.tag = i; d.addItem(fx); pendingActions[i] = r }
                    it.submenu = d
                }
                sub.addItem(it)
            }
            sub.addItem(.separator())
            let rc = NSMenuItem(title: "Re-check now (incl. audio probe)", action: #selector(recheck), keyEquivalent: ""); rc.target = self; sub.addItem(rc)
            let fr = NSMenuItem(title: "Open full report (dop-recorder doctor)", action: #selector(fullReport), keyEquivalent: ""); fr.target = self; sub.addItem(fr)
            top.submenu = sub
            return top
        }

        @objc func recheck() { refreshHealth(probe: true) }
        @objc func fullReport() {
            let script = "tell application \"Terminal\" to do script \"\(recorderBin) doctor\"\ntell application \"Terminal\" to activate"
            NSAppleScript(source: script)?.executeAndReturnError(nil)
        }
        @objc func runFix(_ sender: NSMenuItem) {
            guard let r = pendingActions[sender.tag] else { return }
            switch r.actionType {
            case "url": if let u = r.actionURL, let url = URL(string: u) { NSWorkspace.shared.open(url) }
            case "cli":
                DispatchQueue.global().async { [weak self] in
                    let p = Process(); p.executableURL = URL(fileURLWithPath: recorderBin); p.arguments = r.actionCommand
                    p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
                    try? p.run(); p.waitUntilExit()
                    DispatchQueue.main.async { self?.refreshHealth(probe: false) }
                }
            default:
                let a = NSAlert(); a.messageText = r.name; a.informativeText = r.actionText ?? r.detail
                a.addButton(withTitle: "OK"); a.addButton(withTitle: "Copy")
                NSApp.activate(ignoringOtherApps: true)
                if a.runModal() == .alertSecondButtonReturn { NSPasteboard.general.clearContents(); NSPasteboard.general.setString(r.actionText ?? r.detail, forType: .string) }
            }
        }

        @objc func openLast() {
            guard let p = view.lastTranscript else { return }
            let md = p + "/transcript.md"
            NSWorkspace.shared.open(URL(fileURLWithPath: FileManager.default.fileExists(atPath: md) ? md : p))
        }
        @objc func openFolder() { NSWorkspace.shared.open(URL(fileURLWithPath: view.outputDir)) }
        @objc func togglePause() { setPaused(!isPaused()); refresh() }
        @objc func quit() { NSApp.terminate(nil) }

        // Global hotkey ctrl+opt+cmd+P (Carbon; needs no permission)
        func registerHotKey() {
            var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
            InstallEventHandler(GetApplicationEventTarget(), { _, _, ud in
                let d = Unmanaged<AppDelegate>.fromOpaque(ud!).takeUnretainedValue()
                DispatchQueue.main.async { d.togglePause() }
                return noErr
            }, 1, &spec, Unmanaged.passUnretained(self).toOpaque(), nil)
            let id = EventHotKeyID(signature: OSType(0x4354_4d42), id: 1)
            RegisterEventHotKey(UInt32(kVK_ANSI_P), UInt32(controlKey | optionKey | cmdKey), id, GetApplicationEventTarget(), 0, &hotKeyRef)
        }
    }

    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
    exit(0)
}
