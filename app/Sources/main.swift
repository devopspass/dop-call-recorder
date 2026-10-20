import Foundation

// One bundle (one macOS permission identity) with three modes, chosen by the first argument:
//   recorder commands (audio capture), Accessibility commands (speaker names, screenshots), default = menu-bar app.
let argv = Array(CommandLine.arguments.dropFirst())
let recorderCommands: Set<String> = ["list-apps", "start", "stop", "check-permissions", "request-mic", "probe-tap"]
let axCommands: Set<String> = ["check", "request", "dump", "snapshot", "poll", "shot"]
if let c = argv.first {
    if recorderCommands.contains(c) { recorderMain(argv) }
    if axCommands.contains(c) { axMain(argv) }
}
menubarMain()
