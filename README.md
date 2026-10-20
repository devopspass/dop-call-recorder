# DOP Call Recorder

Records your calls on macOS (Microsoft Teams verified; Zoom / Slack huddles configurable) and transcribes them **offline** with Whisper
on Apple silicon. Two audio streams (you / others), speaker names from Teams, searchable Markdown + VTT transcripts. No cloud, no telemetry.

## Requirements
Apple silicon Mac, macOS 14.2+, [Homebrew](https://brew.sh), Xcode Command Line Tools (`xcode-select --install`, the small Swift app is built on your Mac), ~3 GB free disk (model ~1.5 GB).

## Install
```bash
brew tap devopspass/tap
brew install devopspass/tap/dop-call-recorder   # a few minutes: builds the app locally
dop-recorder setup                              # downloads the Whisper model, copies the app to ~/Applications, asks for permissions
dop-recorder start                              # background agent + menu-bar icon (--no-menubar: no icon)
```
Grant in System Settings > Privacy & Security: **Microphone**, **Screen & System Audio Recording**, **Accessibility** (speaker names, optional) and Notifications - all for the one app "DOP Call Recorder".
`dop-recorder doctor` shows what is missing and how to fix it.

## Use
Join a call as usual: a notification says recording started; after the call a transcript appears in `~/Transcripts/YYYY/YYYY-MM-DD_HHMM_<title>/`
(`transcript.md`, `transcript.vtt`, `meta.json`, `call.m4a`).

| Command | What |
|---|---|
| `dop-recorder status` | state, transcription progress, queue |
| `dop-recorder pause` / `resume` | pause recording (menu bar: Ctrl+Opt+Cmd+P) |
| `dop-recorder search "term" [--since 7d --speaker NAME]` | full-text search over transcripts |
| `dop-recorder reprocess <call>` | re-transcribe after editing the vocabulary |
| `dop-recorder doctor` / `permissions` | diagnostics with fix hints |
| `dop-recorder stop` | stop the agent |

Config: `config.toml` (watched apps, `output_dir`, `retention_days`, language). Custom words: copy `vocabulary.example.yaml` and point `vocabulary_file` at it.

## Privacy and consent
Audio, transcripts and the model stay on your Mac; the only network access is the one-time model download from OpenAI's CDN (sha256-verified).
There is no telemetry. **You are responsible for recording consent**: tell participants and follow local law and company policy. A "recording started" notification is always shown.
Raw audio is deleted after transcription; `call.m4a` is deleted after `retention_days` (default 30; transcripts are kept).

## Known limits
- The apps are **ad-hoc signed** (no Apple Developer ID): after every `brew upgrade` macOS forgets their permissions; re-enable them (`dop-recorder start` tells you). Accessibility may need remove and re-add of the app.
- Only Microsoft Teams is verified; speaker detection needs the Accessibility grant, otherwise lines are `Me` / `Others`.
- Apple silicon only.

## Data on disk
| What | Where |
|---|---|
| Transcripts | `~/Transcripts` (never deleted by uninstall) |
| State, sessions, model | `~/Library/Application Support/dop-call-recorder/` |
| Logs | `~/Library/Logs/dop-call-recorder/` |
| App, LaunchAgents | `~/Applications/DOP Call Recorder/DOPCallRecorder.app`, `~/Library/LaunchAgents/com.devopspass.call-recorder*.plist` |

## Uninstall
`dop-recorder uninstall [--purge] [--purge-models]`, then `brew uninstall dop-call-recorder`. Remove the app entries in Privacy & Security manually if you like.

## More
[Architecture](docs/ARCHITECTURE.md) · [Internals and benchmarks](docs/INTERNALS.md) · [Contributing](CONTRIBUTING.md) · [Releasing](RELEASING.md) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md) · [Third-party notices](THIRD_PARTY_NOTICES.md) · MIT [License](LICENSE)
