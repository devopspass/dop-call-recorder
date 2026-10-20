---
name: dop-recorder-development
description: Use when: developing, testing, building or releasing DOP Call Recorder (dop-recorder CLI, dop_call_recorder package, Swift apps recorder/axhelper/menubar, Homebrew formula, launchd agent). Covers dev flow, rules and gotchas (TCC resets, state-dir isolation, brew vs .venv agent).
---
# DOP Call Recorder development

Layout and data flow: `docs/ARCHITECTURE.md`. Release steps: `RELEASING.md`. Not a Luminor/Corebanking tool.

## Dev flow
| Task | Command |
|---|---|
| Venv | `python3.13 -m venv .venv && .venv/bin/pip install --no-deps --require-hashes -r requirements.lock && .venv/bin/pip install --no-deps -e . pytest` |
| Tests | `DOP_STATE_DIR=$(mktemp -d) .venv/bin/python -m pytest -q` (tests/conftest.py also isolates; never let tests touch the real state) |
| Swift app | `app/build.sh` -> `app/build/DOPCallRecorder.app` (one bundle: recorder + Accessibility helper + menu bar; modes by first argument), `scripts/build-probe.sh` for the detector probe |
| Isolated run | `DOP_STATE_DIR=/tmp/x dop-recorder setup\|start --dry-run` (state, logs, models, apps, LaunchAgents, launchd label all move) |
| Local brew test | `scripts/tap-install-local.sh` (builds tarball, local tap `devopspass/tap`), then `brew install --build-from-source devopspass/tap/dop-call-recorder`, `brew test ...` |
| Release tarball | `scripts/make-release.sh` (excludes .venv, .bench, build, dist, .git; ships the example vocabulary) |

## Rules
- **Ask before rebuilding / re-signing / reinstalling any Swift app or `brew reinstall`**: ad-hoc signature = identity is the code hash, so every rebuild resets the app's TCC grants (Microphone, System Audio, Accessibility, Screen Recording, Notifications: all one identity `com.devopspass.call-recorder`).
- Accessibility after a rebuild: `tccutil reset Accessibility com.devopspass.call-recorder`, then remove and re-add the app with "+".
- Launch native apps with `open -n` (LaunchServices) so the permission belongs to the bundle, never by exec'ing the binary.
- The launchd agent runs the **brew** one (`/opt/homebrew/bin/dop-recorder`, from the Cellar) once installed; `.venv` and brew can differ: check `dop-recorder --version` and `which dop-recorder`, `brew reinstall` is needed for brew to see code changes. Plist templates in `dop_call_recorder/data/` are re-rendered on `start`.
- `DOP_STATE_DIR` leaking from a shell into tests makes them write to the wrong place: always set it explicitly or unset it.
- Deps: edit `requirements.in`, regenerate with `scripts/make-lock.sh`. mlx-whisper is installed `--no-deps` (its metadata requires torch, never imported). Pillow must stay in the lock (screenshots).
- Formula `test do` runs the full pytest suite from `pkgshare/tests` with pytest installed to a temp dir; keep tests free of network and of real state.
- No `Co-authored-by` trailers. Other sessions may edit the repo: re-read before editing, small edits.
- Don't commit transcripts, audio, `ax dump` output, or personal vocabulary.

## Debugging
`dop-recorder doctor`, `dop-recorder permissions --json`, logs in `~/Library/Logs/dop-call-recorder/` (`dop-call-recorder.log`, `detector.jsonl`, `launchd.*.log`), per-call data in `<state>/sessions/<id>/`.
