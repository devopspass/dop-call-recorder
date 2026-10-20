# DOP Call Recorder - product spec

## Goal
A macOS agent that detects a call in a configured app (default Microsoft Teams; Zoom / Slack huddles via the `apps` bundle-id list), records it
as two streams (others via a per-app audio tap, you via the microphone) and, AFTER the call, transcribes it offline into
`~/Transcripts/YYYY/YYYY-MM-DD_HHMM_<title>/` (`transcript.md` with YAML front matter, `transcript.vtt`, `meta.json`, `call.m4a`).

## Principles
| Principle | Meaning |
|---|---|
| Offline | no cloud; the only network access is the one-time model download (OpenAI CDN, sha256-verified) |
| Consent | running the agent = user consent; a "recording started" notification is always shown; the user informs participants |
| No live transcription, no hooks into other tools | transcription runs after the call at low priority |
| Fail-safe extras | Accessibility (speaker names, title) is optional; without it lines are `Me` / `Others` |
| Reversible | `dop-recorder uninstall`; transcripts are never deleted by uninstall |
| Isolated | `DOP_STATE_DIR` moves all state, logs, models, apps and LaunchAgents |

## Transcription
Whisper `large-v3-turbo`, MLX on Apple silicon (no torch); Silero VAD (webrtcvad fallback); hallucination guards (`condition_on_previous_text=false`,
no-speech / compression-ratio thresholds, repeated-segment dedupe); 5-minute chunks with crash recovery; quality gate with retry; per-segment language
detection; custom vocabulary (`vocabulary.yaml`: `terms` -> initial prompt, `corrections` -> deterministic replace).

## Features
Call detector with hysteresis; recorder app; pipeline state machine (idle > recording > finalizing > queued > transcribing > done | failed);
FTS5 search (`search`, `list`, `reprocess`); LaunchAgent, notifications, menu-bar icon, pause hotkey, `status` with ETA, `doctor` / `permissions`,
caffeinate while busy, disk guard, retention (`retention_days`, raw WAVs deleted after transcription), log rotation, structured JSON logs.
Speaker names + call title via Accessibility; speaker ring / learned class fallbacks.

## Non-goals
Live captions, cloud upload, diarization of in-room speakers (planned experiment), Intel Macs, Windows/Linux.

## Platform
Apple silicon, macOS 14.2+ (audio tap API), Homebrew install that builds the Swift apps locally (ad-hoc signed).

Architecture: [ARCHITECTURE.md](ARCHITECTURE.md). Milestone notes and benchmarks: [INTERNALS.md](INTERNALS.md).
