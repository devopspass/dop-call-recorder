# DOP Call Recorder - internals and history (milestone notes, benchmarks, X1 evidence)

User docs: [README](../README.md). Architecture: [ARCHITECTURE.md](ARCHITECTURE.md). Old-name and `install.sh` notes below are historical.

Local macOS call recorder + transcriber ("DOP" = DevOps Pass). Project folder `~/DEVEL/dop-call-recorder`. **Version 1.0.0.**

## Install (Homebrew, Apple Silicon, macOS 14.2+)

```bash
brew tap devopspass/tap            # local tap for now: scripts/make-release.sh && scripts/tap-install-local.sh
brew install devopspass/tap/dop-call-recorder   # builds the 3 Swift apps on your Mac (needs Xcode Command Line Tools)
dop-recorder setup                 # one time: downloads the Whisper model, copies apps to ~/Applications, asks for permissions
dop-recorder start                 # background agent + menu-bar icon (--no-menubar: no icon)
```

Dependencies pulled by brew: `ffmpeg`, `python@3.13`. No `brew services`: you start/stop it yourself (`dop-recorder start|stop`).

| Topic | Details |
|---|---|
| Signing | Apps are **ad-hoc signed** (no Apple Developer account). They are built locally, so there is no quarantine flag / Gatekeeper prompt. |
| Permissions | Microphone, Screen & System Audio Recording, Accessibility (and Notifications). `dop-recorder permissions` shows what is missing; `start` refuses to run until required ones are granted. |
| After `brew upgrade` | Ad-hoc signatures change, so macOS **forgets the grants**: re-enable "DOP Call Recorder" apps in System Settings → Privacy & Security; `dop-recorder start` tells you when this is needed. |
| Model | `large-v3-turbo`, downloaded on `setup` from the Azure CDN (OpenAI `.pt`, sha256-verified) and converted locally to MLX format, no torch needed. Order: existing local mlx dir → `model_url` from config → Azure CDN. Nothing is fetched from HuggingFace or other mirrors. Stored in `~/Library/Application Support/dop-call-recorder/models/`. |
| Privacy | Everything stays on your Mac: audio, transcripts and the model run offline; the only network access is the one-time model download. Recording other people requires their consent under your local law/company policy. |
| Uninstall | `dop-recorder uninstall [--purge] [--purge-models]` (stops agents, removes apps; `--purge` also state/logs, `--purge-models` the model), then `brew uninstall dop-call-recorder`. Transcripts are never touched. |
| Testing in isolation | `DOP_STATE_DIR=/tmp/x dop-recorder setup|start --dry-run`: state, logs, models, apps, LaunchAgents and the launchd label all move under that dir; your real agent is not touched. |
| Licences | Not decided yet (not published). Whisper weights: MIT (OpenAI); mlx / mlx-whisper: MIT; Silero VAD: MIT. |

Dev checkout (no brew): `python3.13 -m venv .venv && .venv/bin/pip install --no-deps --require-hashes -r requirements.lock && .venv/bin/pip install --no-deps -e .`, then build the apps with `recorder/build.sh`, `axhelper/build.sh`, `menubar/build.sh` and the probe with `scripts/build-probe.sh`; `.venv/bin/dop-recorder setup|start|stop|uninstall` work the same as the brew ones (apps are found in `*/build/`). Python: brew installs 3.13; the code accepts 3.12+ but only 3.13 is locked/tested. The former `install.sh`/`uninstall.sh`/`packaging/*.template` were removed in 1.0.0 (replaced by `setup`/`start`/`uninstall` and `dop_call_recorder/data/*.plist.template`). Env vars: `DOP_STATE_DIR` (isolation), `DOP_HOME`, plus the `DOP_CR_*` ones below. Sections below are internals/history; where they mention `install.sh`, read `dop-recorder setup`.

## Naming / rename notes (formerly "Calls Transcriber" / `calls_transcriber`)

| What | Name |
|---|---|
| Display name | DOP Call Recorder |
| Apps (`*/build/`) | `DOPCallRecorderMenu.app` (menu bar, `com.devopspass.call-recorder.menubar`), `DOPCallRecorderRecorder.app` (audio, `...recorder`), `DOPCallRecorderAX.app` (Accessibility, `...axhelper`) |
| CLI | `dop-recorder` (only command; the former `ct` alias was removed). Python package: `dop_call_recorder` (`python -m dop_call_recorder` works too; the old `ct` package no longer exists) |
| State dir | `~/Library/Application Support/dop-call-recorder/` (status/control/recorder.log, `sessions/`, `ax_self.json`) |
| Logs | `~/Library/Logs/dop-call-recorder/` |
| LaunchAgents | `com.devopspass.call-recorder`, `com.devopspass.call-recorder.menubar` |

**Historical:** before 1.0 the project was called "Calls Transcriber"; an automatic one-time migration of the old state/log dirs existed and was removed for the public release.

**Env vars** are prefixed `DOP_CR_` (was `CT_`): `DOP_CR_STATE_DIR`, `DOP_CR_CONFIG`, `DOP_CR_NOTIFY_CMD`, `DOP_CR_LAUNCHAGENTS_DIR`, `DOP_CR_WITH_MLX`, `DOP_CR_AX_DISABLE`, `DOP_CR_CHUNK_SECONDS`, `DOP_CR_NO_MIGRATE`. Main log file: `~/Library/Logs/dop-call-recorder/dop-call-recorder.log` (old `ct.log*` renamed automatically).

**Permissions must be re-granted once** (new bundle ids = new TCC identities; old entries cannot be reset by `tccutil` any more and can be removed manually in System Settings): Accessibility → *DOPCallRecorderAX* (`dop-recorder ax request`); Microphone + Screen & System Audio Recording → *DOPCallRecorderRecorder* (prompt on first recording); Notifications for the menu app/osascript.

## M3 — Call detector (`dop_call_recorder/detector.py`)

Run: `python -m dop_call_recorder.detector --watch` (or `dop-recorder detect --watch` once M0's cli wires `dop_call_recorder.detector.main`; `python -m dop_call_recorder.detector` without `--watch` prints one snapshot).
Log: `~/Library/Logs/dop-call-recorder/detector.jsonl` (sampled every 2 s; a `signals` line is written on change, on any positive sample, and as a 30 s heartbeat, plus `event` lines `start`/`end`).
Config: `apps` (bundle ids; helpers match by prefix, e.g. `com.microsoft.teams2.*`), `[detector]` start/end seconds + title regex.
Signals come from `scripts/audioprobe` (built automatically from `detector/audioprobe.swift` with `swiftc`; CoreAudio process objects, macOS 14.2+; CGWindowList titles).
Rule: **positive = proc_in OR (proc_out AND window)**; call START after 5 s continuous positive, END after 45 s continuous negative. Tests: `pytest tests/test_detector.py`.

| Signal | Source | Verified here (idle Teams, no call) | Reliability / caveats |
|---|---|---|---|
| `proc_in` — Teams process (any `com.microsoft.teams2*`) has running input stream | CoreAudio `kAudioProcessPropertyIsRunningInput` | false; probe verified with a test capture (ffmpeg showed `input:true` on its pid) | Best signal, no permissions needed, per-process. Unverified: whether Teams keeps the input stream open while muted (then `proc_out` + window cover it) |
| `proc_out` — Teams process outputs audio | `kAudioProcessPropertyIsRunningOutput` | false | Also true for notification sounds / media in Teams → only counts together with a call window |
| `window` — call-ish window title | `CGWindowListCopyWindowInfo` | false (only `Chat \| … \| Microsoft Teams` seen, deliberately ignored) | Supporting only; only on-screen windows count. Titles are visible without Screen Recording permission here; real call title format unverified; regex configurable; chat/tab titles excluded |
| `mic_any` — a physical input device running somewhere | `kAudioDevicePropertyDeviceIsRunningSomewhere` | false | App-agnostic (dictation, other apps) → informational, not part of the decision |
| Virtual device `Microsoft Teams Audio` (MSTeamsAudioDevice.driver, transport `virt`) | same | Idle: not running. **Becomes `running` when any app opens it** (an ffmpeg capture on device `:0` made it running) | Excluded from `mic_any` so it cannot cause false positives; decisions use per-process signals instead |
| Process presence only (`running`) | process/window list | true (Teams is always open) | Useless alone — Teams runs all day |

Current state on this machine: all three apps NO call (`positive=false`).
**Needs a real call** to confirm: join a Teams call (try muted and unmuted, with and without screen sharing), run `python -m dop_call_recorder.detector --watch`, and expect `start` ≤ ~7 s after joining and `end` ~45 s after leaving; then keep it logging for a day and check `detector.jsonl` for `positive` outside calls (false positives).

## M0 — Skeleton, config, model loader (`dop_call_recorder/`)

Setup: see "Install" above (brew) or the dev-checkout note (`.venv` from `requirements.lock`; torch/openai-whisper are optional extras `[openai]`, not installed by default).
Config: `config.toml` (override with `--config` / `$DOP_CR_CONFIG`); `dop-recorder config` prints the effective config. Keys: `output_dir, retention_days, apps, backend, model, model_dir, vocabulary_file, language`, `[transcribe]` guards, placeholder `[silence]`/`[timeouts]`/`[logging]`.
Model: `dop_call_recorder/model.py` → `whisper.load_model(model, download_root=model_dir)`; reuses `~/.cache/whisper/<model>.pt` (checksum-verified and loaded, no download; a corrupt file is re-downloaded, logged as `source: redownloaded`), otherwise falls back to OpenAI's Azure CDN (HuggingFace is blocked).
Guards (`dop_call_recorder/transcribe.py`): silero VAD (webrtcvad fallback; segments outside speech dropped, no speech → empty), `condition_on_previous_text=false`, no_speech/compression-ratio/logprob thresholds, consecutive-repeat dedupe. `vocabulary.yaml` (`terms:` list → `initial_prompt`; legacy `.txt` still works: one term per line, `#` comments). `corrections:` (`Right: [wrong, wrong2]`; legacy txt: `wrong | wrong2 -> Right`) are deterministic post-Whisper corrections (whole words, case-insensitive; applied per window, also on reprocess and on the quality-gate retry; `Right` is also added to the prompt). The prompt budget is ~200 tokens: Whisper keeps only the LAST tokens, so overflow drops the EARLIEST terms (logged as `vocabulary_truncated`) — put important terms last. Teams roster names (speaker_events.jsonl) are appended as the highest-priority prompt terms. Caveat: with `condition_on_previous_text=false` Whisper (mlx and openai) uses the prompt only for the first 30 s window, hence the corrections map.

**Speaker names (X2)**: `speaker_events.jsonl` rows carry `speaking` + `src`. Order of trust: `ax` (text/class marker in the AX tree; none in current Teams web UI) > `ring` (the blue/periwinkle ring Teams draws around the speaking tile, found in a screenshot every 2 s by `ringdetect.py`, mapped to a tile by walking the gallery grid and to a name via the AX tile order; a ringed tile that AX reports as muted is rejected) > `unmuted` (the only unmuted other participant, weak fallback). Needs Screen Recording permission for the process running the agent (`screencapture`); on failure it logs `ring_capture_failed`, backs off 60 s and uses the fallback. Disable with `[ax] ring_detect=false` or `DOP_CR_RING_DISABLE=1`. Centred partial last gallery rows are not mapped (fail-safe: no ring speaker).
Use: `.venv/bin/dop-recorder transcribe tests/sample.wav [--model small] [--json]`. Logs are JSON lines on stderr + `[logging].file`.
Verified: sample from `say`+ffmpeg transcribed offline (proxy set to a dead port) with the cached large-v3-turbo; vocabulary turns "log miner"/"EREST" into "LogMiner"/"eArrest"; 20 s silence → empty output.

**Learned speaking class (X3).** Teams' speaking ring is not text in the AX tree, but it may be a CSS class on the speaking tile. While someone other than you is unmuted, the agent takes a screenshot ring check every 3 s and records which class tokens of each tile's container sit on the ringed tile (≥90 %) and on almost no other tile (≤2 %). Once learned (≥12 samples from ≥3 different speakers) the class alone names the speaker, with no more continuous screenshots; one revalidation screenshot every 60 s, and 3 disagreements out of 5 drop the class and relearn. After 20 ringed samples (≥3 speakers) without a usable class the verdict "none" is kept for 7 days and the ring path continues. Verdicts are cached per Teams app version in `speaker_class.json` in the state dir (tokens and counters only, no names). `dop-recorder doctor` shows the state (and warns if Screen Recording is missing); `[ax] class_revalidate_seconds` tunes it.

## M2 — Recorder (`recorder/`, Swift, signed ad-hoc .app)

Build: `recorder/build.sh` → `recorder/build/DOPCallRecorderRecorder.app` (swiftc, no Xcode; bundle id `com.devopspass.call-recorder.recorder`, Info.plist has `NSMicrophoneUsageDescription` + `NSAudioCaptureUsageDescription`, ad-hoc signed, entitlement `device.audio-input`). CLI shim: `recorder/bin/recorder`.
```
recorder/bin/recorder list-apps [--all]                       # JSON; also `dop-recorder apps [--all]` (dop_call_recorder/recorder_bridge.py)
recorder/bin/recorder start --apps com.microsoft.teams2 --out <dir> [--system-fallback]
recorder/bin/recorder stop                                    # SIGTERM-safe: flushes + finalizes WAV headers, ~<1 s
```
- `list-apps`: Core Audio process objects (macOS 14.2+) grouped under the owning main app (walks the parent-pid chain, so Teams' helper/WebView processes resolve to `com.microsoft.teams2` with all pids). Default = only processes with an active input/output stream; `--all` = every audio client. Items: `bundle_id, name, main_pid, pids, audio_bundle_ids, is_running_input, is_running_output`. Needs no permissions.
- `start` (via `open`, so permissions are attributed to the .app; `start --direct` runs in the foreground): `--apps` takes bundle ids (matches exact id, `<id>.*` helpers, and any process whose ancestor is the running main app); for testing also `pid:<N>`. Writes into `--out`: **chunked** 16 kHz mono s16 WAVs `others_0000.wav, others_0001.wav…` (per-app tap output) and `me_0000.wav…` (default input mic), a new chunk every 300 s (`DOP_CR_CHUNK_SECONDS` env overrides, for tests), plus `state.json` (heartbeat every 2 s: pid, `heartbeat_epoch`, mode, tap pids, per-stream chunk list/seconds/peak/last_active, `mic_permission`, last 200 events). WAV headers are re-patched at every heartbeat so a crash leaves playable files. Both streams are padded with silence to stay aligned to wall-clock (e.g. before the call helper starts producing audio). Pid file/log: `~/Library/Application Support/dop-call-recorder/recorder.{pid,log}` (JSON lines).
- Per-app tap: `CATapDescription(stereoMixdownOfProcesses:)` + private aggregate device, rebuilt every time the set of matching processes changes (Teams spawns call helpers late) and on default-output-device change or device sample-rate change. `--system-fallback` = global tap excluding the recorder itself (use if per-app yields nothing).
- Mic: AVAudioEngine; restarted on default-input change / engine configuration change.
- Permissions (first use): macOS asks for **Microphone** and **System Audio Recording** for "DOP Call Recorder Recorder" (Settings → Privacy & Security). Ad-hoc signature = identity is the code hash, so **rebuilding may reset the grants** → re-approve after `build.sh`.
- Gotcha found: with a Bluetooth headset, opening the mic switches it to HFP (24 kHz); the tap's reported format stays 48 kHz → audio at 2× pitch/half length. Fixed by using the aggregate device's nominal rate and rebuilding on rate change.

Evidence (this machine, macOS 26.7.1, AirPods): two `afplay` processes (440 Hz and 1000 Hz tones), recorder tapping only one → `others` has 440 Hz amp 525, 1000 Hz amp 0.1 (**isolation works**); `--system-fallback` → both tones present (548 @1000 Hz). Default-output switch AirPods→speakers→AirPods mid-recording: recording continued, ~1 s gap each switch, tone intact, mic continued. `recorder stop` finalizes cleanly; 4 s chunk test → 4.0/4.0/2.9 s files.
Deliberate deviation from the SPEC wording: the single `others.wav`/`me.wav` are replaced by chunked `others_NNNN.wav`/`me_NNNN.wav` (listed in `state.json`) so a crash loses at most one chunk. If per-app tap creation fails, the recorder **auto-falls back** to a global tap excluding itself (event `auto_fallback_system_tap`); `bin/recorder start` waits up to 10 s for the first heartbeat and exits non-zero (3 = already running); `stop` verifies the pid really is the recorder before signalling. Test hook: `DOP_CR_TEST_FAIL_TAP=1` forces the fallback path.
**Decision: GO for per-app tap** (fallback kept as a switch). **Needs a real Teams call**: verify tap picks up Teams call audio (Teams helpers appear in `list-apps` only while in a call), with and without headset; confirm both permission prompts were approved.

## M1 — Backend comparison & pluggable backends (`dop_call_recorder/backends/`, `scripts/benchmark.py`)

**Backend selection**: `config.toml` `backend = "openai-whisper" | "mlx-whisper" | "whisper-cpp"` (also `dop-recorder transcribe --backend X`). Default stays `openai-whisper` (M0 behavior). Unavailable backends fail fast: `Backend.check()` runs before audio is read and the CLI prints `ct: error: ... <fix hint>` (exit 1) for a missing mlx package/model, missing whisper-cli/ggml model or unknown name. `dop_call_recorder/backends/{base,openai_whisper,mlx_whisper,whisper_cpp}.py`; `dop-recorder transcribe` → `dop_call_recorder/transcribe.py` → backend. openai-whisper accepts `device = "cpu"|"mps"`.
**Preprocessing** (`dop_call_recorder/audio.py`, config `[preprocess]`, all default off): `vad_trim` (silero spans + 0.3 s pad, gaps <1 s merged; segment times are mapped back to original time), `loudnorm` (ffmpeg EBU R128, -16 LUFS), `denoise` (ffmpeg `afftdn` before loudnorm). `load_audio` raises `AudioError` on missing/undecodable/empty input.
**Tooling**: `scripts/make_sample.py` (macOS `say`, 2 voices Samantha/Daniel, vocabulary terms, 3 silence stretches of 25-60 s, + noisy/quiet variant and a hard telephone/crosstalk variant via ffmpeg; writes `.bench/sample/{clean,noisy,hard}.wav` + `reference.txt`), `scripts/benchmark.py` (each combo in a fresh subprocess; prints a markdown table, appends JSON lines to `.bench/results.jsonl`), `scripts/convert_pt_to_mlx.py`.
**Setup of the non-default backends** (all offline from the cached `~/.cache/whisper/*.pt`; scratch/models in `.bench/`, deletable):
- mlx-whisper: `pip install mlx-whisper` (PyPI; extra `[mlx]`), then `python scripts/convert_pt_to_mlx.py ~/.cache/whisper/large-v3-turbo.pt ~/.cache/whisper/mlx-large-v3-turbo` (≈35 s, float16 `weights.npz` + `config.json`). Effort: one small script; only needed renaming `mlp.0/2`→`mlp1/2` and transposing conv weights (`mlx_whisper/whisper.py` layout). Output verified identical in quality to openai-whisper (WER table below). In 1.0.0 `dop-recorder setup` does the download + conversion automatically (torch-free converter in `dop_call_recorder/model_fetch.py`); mlx-whisper is installed with `--no-deps` from `requirements.lock` because its metadata requires torch without importing it.
- whisper.cpp: `brew install whisper-cpp` (Metal), `models/convert-pt-to-ggml.py` fetched from GitHub raw (not HF) and run with `<venv>/site-packages` as the assets dir (≈40 s, fp32 1.6 GB). Effort: moderate, one-off.
- No blocked source was needed for any backend.

### Results (Apple M-series, 10 cores, 25 GB; 11.8 min synthetic 2-speaker call, 1555 words, English forced; vocabulary prompt on)
RTF = (preprocess + ASR) / audio duration, model load excluded. Peak MB = max RSS (incl. whisper-cli child). "halluc" = 3rd+ identical consecutive segment, or segment with <50 % words from the reference.

| Backend (clean.wav) | pre | RTF | load s | peak MB | WER | halluc |
|---|---|---|---|---|---|---|
| openai-whisper turbo CPU | none | 0.188 | 4.8 | 5043 | 2.8 % | 0 |
| openai-whisper turbo CPU | vad+norm | 0.172 | 6.4 | 4995 | 2.9 % | 0 |
| openai-whisper turbo MPS | none | 0.124 | 10.8 | 3369 | 3.0 % | 0 |
| openai-whisper small CPU | none | 0.100 | 2.9 | 2951 | 3.3 % | 0 |
| openai-whisper small CPU | vad | 0.081 | 2.2 | 2798 | 2.7 % | 0 |
| **mlx-whisper turbo** | none | **0.074** | 4.5 (1.6 warm) | 2521 | 2.9 % | 0 |
| mlx-whisper turbo | vad | 0.062 | 1.6 | 2504 | 3.5 % | 0 |
| mlx-whisper turbo | vad+norm | 0.084 | 1.7 | 2623 | 2.9 % | 0 |
| whisper.cpp turbo (Metal, greedy) | none | 0.092 | ~0 (per-run) | 2070 | 3.0 % | 0 |
| whisper.cpp turbo | vad+norm | 0.098 | ~0 | 2014 | 3.1 % | 0 |

Noisy/quiet variant (pink noise, -14 dB): WER turbo CPU 3.3-3.6 %, MPS 3.4-3.6 %, mlx turbo 3.3-3.7 %, whisper.cpp 3.6-3.9 %, small 4.2-5.0 % (small clearly degrades; turbo variants don't). mlx RTF 0.075-0.085, hallucinations 0 everywhere. Full tables: `.bench/table_clean.md`, `.bench/table_noisy.md`, raw `.bench/results.jsonl` (rerun: `python scripts/benchmark.py --audio ... --backends ... --pre ...`).

Hard variant (`hard.wav`: 8 kHz telephone band, 0.7 s crosstalk echo at -9 dB, pink noise ~5 dB SNR, -12 dB gain; same text):

| Backend | pre | RTF | load s | peak MB | WER | vocab | halluc |
|---|---|---|---|---|---|---|---|
| openai-whisper turbo CPU | none | 0.268 | 6.3 | 5581 | 4.2 % | 72 % | 0 |
| openai-whisper turbo CPU | vad+norm | 0.241 | 11.0 | 4969 | 5.1 % | 72 % | 0 |
| mlx-whisper turbo | none | 0.077 | 2.2 | 2519 | 5.1 % | 74 % | 0 |
| mlx-whisper turbo | vad+norm | 0.097 | 2.5 | 2583 | 4.2 % | 74 % | 0 |

Findings:
- **Recommendation: mlx-whisper + large-v3-turbo** — about 2.5-3.5× faster than openai-whisper on CPU (RTF 0.074 vs 0.188 clean, 0.077 vs 0.268 hard; a 1 h call ≈ 4.5 min vs 11-16 min) and half the memory (2.5 GB vs 5-5.6 GB). Accuracy is equivalent: WER 2.9 % vs 2.8 % clean, and on the hard sample 4.2-5.1 % for both with no consistent winner (each best in one preprocessing setting). Differences of a few tenths of a point are within what one 1555-word run can resolve; no significance test was done, so read WER as "equal", not "better". whisper.cpp turbo is close on speed (RTF 0.09) and lowest in memory (2.0 GB) and also receives the vocabulary prompt (vocab recall 74-76 %, same as the others), but WER on the noisy variant was slightly higher (3.6-3.9 % vs 3.3-3.7 %), it reloads the model on every call, and it adds a subprocess + ggml conversion; it is a viable fallback, not the pick. openai-whisper MPS is faster than CPU on turbo (0.124) but slower than mlx and slow to load (≈10 s); `small` is cheaper but degrades on noise (4.2-5.0 % WER).
- **Preprocessing is not worth enabling by default**: no consistent accuracy gain on this sample (loudnorm: WER ±0.1 pp, costs ~0.03 RTF; denoise: helps vocab recall on noisy 77→80 % but drops segments/WER neutral; VAD trim: faster by ~15-20 % on silence-heavy audio, but WER 2.9→3.5 % for turbo (trimming context hurts slightly), not for small (3.3→2.7 %)). Silero spans are still used (always) to drop segments outside speech. Keep options off; enable `vad_trim` for long-silence calls if speed matters.
- Hallucinations: 0 with every combo, including the hard sample — but the sample has clean digital silence and TTS speech; real calls (crosstalk, music, keyboard noise) may differ. Guards remain on.
- Caveat: synthetic TTS speech is easy (WER 3 % is mostly proper-noun spelling: "Kargo"→"Cargo", "dev"→"DEB"); absolute numbers will be worse on real calls, only the relative comparison is reliable. **Proof on real call audio is still pending**, hence the default stays `openai-whisper`; to switch: set `backend = "mlx-whisper"` (model already at `~/.cache/whisper/mlx-large-v3-turbo`).
- Time to first load (measured in the benchmark worker): openai turbo 5-11 s, mlx 4.5 s cold (first ever) / 1.6-2.5 s warm; whisper.cpp loads inside each `whisper-cli` run, so its load time is included in its RTF, not reported separately.

## M4a — Pipeline (`dop_call_recorder/pipeline.py`, `windows.py`, `quality.py`, `merge.py`, `outputs.py`, `langid.py`)

Commands: `dop-recorder run` (foreground loop: detector → recorder → pipeline; recovers orphaned sessions first; `--recover-only` = recover + transcribe, then exit), `dop-recorder process <session-dir | recorded-folder> [--keep-raw] [--backend X]` (finalize + transcribe; for testing / retry of a `failed` session).
**State machine** (per call, persisted in `<work_dir>/<id>/session.json`, default `~/Library/Application Support/dop-call-recorder/sessions/`): `idle → recording → finalizing → queued → transcribing → done | failed` (`failed → queued` on re-process; illegal transitions raise). One recording at a time; transcription runs in a single low-priority worker (`os.nice`, `[pipeline].nice`) so a new call can be recorded while an old one is transcribed.
**Crash recovery** (`Pipeline.recover()`, run at `dop-recorder run` start): sessions in `recording/finalizing/queued/transcribing` whose `owner_pid` is dead are picked up; a still-running orphan recorder with fresh heartbeat is stopped cleanly, the chunks that exist are finalized (end time = last heartbeat) and queued. Raw chunks are deleted only after a successful transcription (kept on failure).
**Finalize**: chunks → `call.m4a` (ffmpeg concat + amix of me/others, AAC 64k mono); calls shorter than `[silence].min_call_seconds` are dropped (`skipped: too_short`).
**Transcribe**: me and others separately; backend from config — `backend = "auto"` (new default) = mlx-whisper if importable and `<model_dir>/mlx-<model>` exists, else openai-whisper (logged `backend_auto_fallback`); VAD, vocabulary prompt and hallucination guards as M0. Long audio is processed in `window_minutes` (30) windows overlapping by `overlap_seconds` (15), absolute timestamps; the seam is the overlap midpoint (segment belongs to the window containing its midpoint) plus similarity dedupe of straddling segments.
**Quality gate** (per stream): `empty_output` (≥60 s VAD speech but <0.2 words/s; no-VAD: ≥5 min and nothing), `repeated_lines` (≥25 % of segments in ≥5× repeated lines, or ≥15 % dropped by repeat-dedupe), `high_compression` (≥10 % long segments with zlib ratio >2.6), `wrong_language:<xx>` (Cyrillic text labelled non-Cyrillic or vice versa). On any issue: ONE automatic re-run with different settings (window ÷3, vad_trim+loudnorm, no vocabulary prompt, stricter no_speech/compression thresholds, forced language when wrong-language); the better result is kept, remaining issues → `quality: degraded` + `quality_issues` in meta.json/front matter.
**Merge**: Me/Others labels by stream; `Me` segments whose text (≥3 words) echoes an `Others` segment within `echo_window_seconds` (2 s) are dropped (no headphones → mic hears speakers; **use headphones** for best results); per-segment language = whisper's per-window language refined by a stopword/script heuristic (`langid.py`, not a real LID model — short text falls back to the window language); chapters = pause ≥60 s (chapter ≥2 min) or fixed 10-min interval; turns = same-speaker segments <4 s apart joined; timestamps `[HH:MM:SS]` relative to recording start.
**Outputs** (`output_dir/YYYY/YYYY-MM-DD_HHMM_<title>/`): `transcript.md` (YAML front matter: title,date,start,end,duration,duration_seconds,apps,language,languages,quality,model,backend), `transcript.vtt` (`<v Me|Others>`), `meta.json` (+ per-stream stats, quality issues, echo count), `call.m4a`; root `INDEX.md` (table, newest first) and `index.json` (one entry per call: date,time,start,duration,title,path,quality,snippet,apps,languages; re-processing replaces the entry). Title = detector window title (minus ` | Microsoft Teams`) or fallback `<App name> HHMM`.
**Testing without a call**: `scripts/fake_call.py make OUT --minutes 65` (macOS `say` voices Daniel=Others / Samantha=Me, `truth.json`, echo leak + a pause for chapters, chunked WAV exactly like the recorder), `record OUT --speed N --work-dir D` (growing chunks + heartbeat + session.json; `kill -9` it, then `dop-recorder run --recover-only`). `pytest tests/test_pipeline.py` (fast, FakeBackend; no models).
Not in M4a: `dop-recorder search/reprocess` (M4b), launchd/menu-bar/doctor/retention/disk guard/caffeinate (M5). Needs a real call: Teams window-title format for the title, recorder tap on real audio, real-world quality of the gate/echo thresholds.

## M4b — Search, reprocess, save (`dop_call_recorder/search.py`, `dop_call_recorder/reprocess.py`)

| Command | What |
|---|---|
| `dop-recorder index [--rebuild]` | incremental FTS5 index of `<output_dir>/**/transcript.md` -> `<output_dir>/.index.sqlite` (`[search] index_path` overrides). Pipeline calls `search.index_call(cfg, call_dir)` after each call; `dop-recorder search`/`dop-recorder list` also auto-update. |
| `dop-recorder search "<q>" [--since 7d\|2w\|3m\|YYYY-MM-DD] [--app teams] [--speaker Me\|Others\|Mixed\|<name>] [--lang en] [--limit N] [--json] [--open [vscode\|file]]` | bm25-ranked hits: title, date, `[HH:MM:SS]`, speaker, snippet with matches highlighted (bold/`**`). FTS5 syntax: `"phrase"`, `prefix*`, `AND/OR/NOT`, `NEAR(a b)`; terms with `-:./` must be quoted (`"CB-12345"`). Bad syntax -> exit 2 with a hint. Call titles are searchable too. `--lang` matches per-turn heuristic language or the call's languages. |
| `dop-recorder list [--since] [--limit] [--json]` | recent calls table |
| `dop-recorder reprocess <call-dir\|id> [--model] [--backend] [--vocab-file]` | re-transcribe from `call.m4a` with new settings; previous transcript kept as `transcript.prev.md`; meta/INDEX/index.json/search updated. call.m4a is a mono mix: Me/Others labels are recovered from the previous `transcript.vtt` by time overlap (unmatched -> `Mixed`). |
| `dop-recorder transcribe <file> --save [--title T]` | any audio/video (Teams cloud mp4, voice note) -> full call folder in output_dir (title from filename, speaker `Mixed`, call.m4a copy), indexed. |

Pipeline hook (the only change outside M4b files): `pipeline.write_outputs` calls `search.index_call` (never raises).

## M5 — Agent, notifications, menu bar, ops

| Thing | How |
|---|---|
| Install / remove | `dop-recorder setup [--no-prompt] [--no-model] [--no-menubar]` (copies the apps to `~/Applications/DOP Call Recorder/` without re-signing, stores their cdhash, downloads the model) → `dop-recorder start [--dry-run]` (plists rendered from `dop_call_recorder/data/*.template` → `~/Library/LaunchAgents/`, `plutil -lint`, gated on the health check; reports when permissions must be re-granted) → `dop-recorder stop` / `uninstall [--purge] [--purge-models]`; never touches `output_dir`. |
| LaunchAgent | `com.devopspass.call-recorder` runs `.venv/bin/dop-recorder run`, `KeepAlive`, `ExitTimeOut 60` (SIGTERM ⇒ recording is finalized + queued; the rest is recovered at next start). Logs: `~/Library/Logs/dop-call-recorder/{dop-call-recorder.log,detector.jsonl,launchd.out.log,launchd.err.log}`. Load: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.devopspass.call-recorder.plist`; stop: `launchctl bootout gui/$(id -u)/com.devopspass.call-recorder`. |
| Notifications | osascript banners: **recording started** (always on unless `[notify] on_start=false`), transcript ready (+ path), transcription/recording failures, paused/resumed, low disk. |
| Menu-bar icon | `menubar/build/DOPCallRecorderMenu.app` (Swift `NSStatusItem`): 🎙 idle · 🔴 recording · ⏳ transcribing (% + ETA) · ⏸ paused · ⚠️ error · ⚪️ agent down; menu: open last transcript / transcripts folder, Pause/Resume (global hotkey **⌃⌥⌘P**), Quit. Talks to the agent through `status.json` / `control.json` in `~/Library/Application Support/dop-call-recorder/` (override `$DOP_CR_STATE_DIR`). `menubar --print` prints the current view headlessly. |
| `dop-recorder pause` / `dop-recorder resume` | Pause stops a running recording cleanly (chunks finalized, queued, transcribed) and makes the detector's call starts be skipped; if the call is still going on at resume (including the call that was being recorded when you paused), recording starts again then. A watchdog (every 30 s) also re-starts recording whenever the detector reports a live call but nothing is recording (not paused/disk-blocked); failures notify as "Recording FAILED to start". |
| `dop-recorder status [--json]` | state, active session, transcription progress + ETA (audio-seconds based), queue length, disk, last transcript. Exit 3 if the agent is not running. |
| `dop-recorder doctor [--json]` | python/venv, deps, whisper `.pt` + MLX model, ffmpeg, recorder `.app` built+signed, mic / system-audio permission evidence, Teams installed, output_dir writable, disk, launchd state, agent process, detector probe, log dir; each non-OK line has a `fix:`. Exit 1 on failures. |
| Power | `caffeinate -i -w <pid>` is held while recording/transcribing (released on idle and on shutdown). |
| Disk guard | `[disk] warn_gb=5` ⇒ notification (every 6 h); `critical_gb=1` ⇒ recording is flushed+stopped, old raw WAVs auto-cleaned first (done sessions always, failed ones after `raw_max_age_days`; critical: all non-active), recording auto-resumes when space is back. |
| Retention | daily + at startup: `call.m4a` older than `retention_days` is deleted; transcript.md/vtt/meta.json and the index are kept; finished session dirs older than that are pruned. `retention_days = 0` disables. |
| Log rotation | `dop-call-recorder.log` and `detector.jsonl` rotate at 10 MB (5 backups, `[logging] max_mb/backups`); launchd out/err logs are copy-truncated hourly past 10 MB. |

Permissions (one-time): Microphone + "Screen & System Audio Recording" for **DOPCallRecorderRecorder**, notifications for osascript; `dop-recorder doctor` explains the current state. macOS offers no API to read these grants, so doctor reports the recorder's own last evidence and flags the system-audio grant as "verify with a real call".

## Permissions & health (menu-bar submenu, `dop-recorder permissions`)
Menu-bar icon > **Permissions & health  ✅ All OK / ❌ N missing / ⚠️ N warning**. Rows (grouped Permissions / Apps / Runtime / Resources) show ✅/⚠️/❌ + reason; a non-OK row opens a small submenu with the reason and a **Fix…** action (opens the exact System Settings pane or triggers the prompt / starts the agent). Footer: *Re-check now (incl. audio probe)*, *Open full report (dop-recorder doctor)*.
- Single source of truth = `dop-recorder doctor` checks (`dop_call_recorder/doctor.py`) aggregated by `dop_call_recorder/health.py`; `dop-recorder permissions [--json] [--probe] [--write]` prints/writes `<state>/health.json` (the menu app reads it; refreshed at start, every 2 min, on Re-check). `dop-recorder permissions fix <id>` runs the fix (microphone | system-audio | accessibility | agent | launchd).
- TCC is per bundle, so natives are probed BY the owning bundle via `open -W -n <app> --args <cmd> --out F`: recorder `check-permissions` (mic status), `request-mic` (prompt), `probe-tap` (1 s global process tap; `audio` = proven, `silent` = unknown, create failure = ❌); `axhelper check`/`request` (Accessibility); the menu app queries Notifications itself (`UNUserNotificationCenter.getNotificationSettings`).
- System-audio capture has no query API: ✅ only when the probe heard audio or a past call's others.wav had signal; otherwise ⚠️ "unknown, needs a real call" (probe is skipped while recording).
- Tray icon: new **attention** state (orange padlock badge, `icons/menubar8/attention*.png`, generated by `scripts/make_menu_icons.py`) shown when idle/stopped and a REQUIRED item is ❌ (Microphone, Recorder app, Notifications). One notification at startup per distinct set of missing required items (`<state>/health_notified.json`). Accessibility and the rest are warnings only.
- Smoke tests: `menubar/build/DOPCallRecorderMenu.app/Contents/MacOS/menubar --print-health` (submenu + icon state, headless), `dop-recorder permissions`, tests in `tests/test_health.py`.
- Rebuilding signs ad-hoc again and resets that app's TCC grants: recorder -> re-grant Microphone + System Audio; menu app -> Notifications; axhelper -> Accessibility.

## X1 — Real speaker names + call title via Accessibility (`axhelper/`, `dop_call_recorder/axparse.py`, `dop_call_recorder/axpoll.py`)

**What it does** (all optional and fail-safe — without the Accessibility grant the pipeline behaves exactly as before; logged once + `dop-recorder doctor` warning):
- `axhelper/build.sh` builds the ad-hoc signed helper **`axhelper/build/DOPCallRecorderAX.app`** (bundle id `com.devopspass.call-recorder.axhelper`, stable ⇒ the grant survives rebuilds). Launched through LaunchServices (`open -n`) so the permission belongs to the app bundle.
- **Grant once:** System Settings → Privacy & Security → **Accessibility** → enable *DOPCallRecorderAX* (`dop-recorder ax request` triggers the prompt; add with “+” if missing). Check: `dop-recorder ax check` / `dop-recorder doctor`.
- During a call `Pipeline.start_call` starts an `AxPoller` (~1 Hz) → `<session>/speaker_events.jsonl` (`{ts, speaking[], roster[], title, self}`), `ax_meta.json`, and sparse `ax_samples.jsonl` (≤12 compact text outlines of the call window — tuning data; windows without call controls are never sampled). Copied next to the transcript as `speaker_events.jsonl`.
- `dop_call_recorder/merge.py::assign_speakers`: **Me → own display name** (`[ax] my_name`, else Teams’ “Name (You)”); **Others → active speaker with the largest overlap** (±0.5 s slack, ≥0.25 s and >½ of the overlap, polling gaps >3 s are not evidence); exactly one other roster participant ⇒ all Others get that name; unknown ⇒ `Others`. Title = AX meeting title when the detector gave none (the detector `start` event also gets `title` from the AX call window at call start). `meta.json` gets `speakers` / `participants`.
- Tools: `dop-recorder ax check|request|snapshot|dump [--out F]` (`dump` = raw tree; contains on-screen Teams text — keep private). Config `[ax]` in `config.toml`.
- Not restarted: a running `dop-recorder run` keeps the old code until its next restart (then X1 is active).

**Evidence (real Teams 26261.303, WebView2, probed read-only with the Teams main window idle, no call open):**

| Item | Finding | Go / No-go |
|---|---|---|
| Tree reachable | Needs `AXEnhancedUserInterface`+`AXManualAccessibility` on the app element; the web tree is built asynchronously (first read = 19 shell nodes, ≈1 s later ~1.2 k nodes: `AXWindow > AXGroup(BrowserRootView) > … > AXWebArea`). Helper waits 1 s on first enable. A full read takes ~0.1 s. | **GO** |
| Window title | `AXWindow` title = `Chat \| <chat> \| Microsoft Teams` (same string CGWindowList already gives the detector) | n/a idle; in-call title **unverified** |
| Own display name | **Not** on the profile button (`AXButton "Your profile, status Available"`, dom id `idna-me-control-avatar-trigger`); but the self-chat row/label is `Doe, Jane (You)` (`AXRow`+`AXStaticText`) → parsed to `Jane Doe` | **GO** (verified live; fallback `[ax] my_name`) |
| Roster in a call | Heuristic: rows/tiles labelled `Surname, Name, <Muted\|Unmuted\|Organizer\|…>` and rows inside a list labelled Participants/People | **needs real call** |
| Meeting title in a call | Heuristic: title of the call window (non-`Chat\|…` window containing a Leave/Hang up button), `\| Microsoft Teams` stripped | **needs real call** |
| Active speaker | Heuristic: `is speaking`/`(speaking)` label, or `speaking`/`active-speaker` DOM class (`AXDOMClassList` is exposed) on a tile; name from node/descendant/ancestor | **needs real call** (Teams may not expose it at all ⇒ then only the “single other participant” and Me-name features apply) |

The idle-state fixtures in `tests/fixtures/ax_*.json` are shaped after the real tree but contain fake names; in-call fixtures are synthetic. **Tuning after the first real multi-person call:** look at `~/Library/Application Support/dop-call-recorder/sessions/<id>/ax_samples.jsonl` (or the copy in the transcript folder), then adjust the regex constants at the top of `dop_call_recorder/axparse.py`.

Known X1 limitation: once names are applied, `dop-recorder search --speaker Me` no longer matches your own lines (they carry your name; use `--speaker "<your name>"`).

### X1 — evidence from the first real Teams call (bot "Echo") and fixes

| Item | Real-call evidence | Status |
|---|---|---|
| Title | window title `Meeting compact view \| Echo \| Microsoft Teams`; UI-mode prefix + suffix are stripped → `Echo` (AX title now beats the detector's raw title) | GO |
| Own name | call tile `Myself video, Doe, Jane, Unmuted, video is on, …` → "Jane Doe"; also cached in `~/Library/Application Support/dop-call-recorder/ax_self.json` (from the idle window / earlier calls) as a fallback | GO |
| Other participants | tile `AXMenuItem 'Echo, Context menu is available'` → roster | GO (1 remote participant seen) |
| "Show only" false positive | a Teams filter label ("Show only … speaking …") matched the speaking regex; now UI words are denylisted, a real status token is required, and speakers must be in the roster; unsure → `Others` | FIXED |
| Active speaker | no indicator observed in the first sample (only one 60 s sample existed) | UNVERIFIED — `ax_samples.jsonl` now records a de-duplicated outline every ≥10 s (≤30) with non-atomic DOM classes, so the next call yields tuning data |

Test fixture: `tests/fixtures/ax_real_samples.jsonl` (real outline, private on-screen text of a test call).
