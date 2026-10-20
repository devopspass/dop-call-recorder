# Changelog

## 1.0.0
- Release prep: LICENSE (MIT), short README (internals moved to `docs/INTERNALS.md`), ARCHITECTURE/CONTRIBUTING/RELEASING/SECURITY/CoC/THIRD_PARTY_NOTICES, dev skill, CI workflow, neutral `vocabulary.example.yaml` shipped in release tarballs, Xcode CLT preflight in the formula, legacy `calls-transcriber` migration removed.
- **One app instead of three**: `DOPCallRecorder.app` (`app/`, bundle id `com.devopspass.call-recorder`) hosts recorder, Accessibility helper and menu bar: one set of macOS permission prompts. `setup` removes the old three apps; re-grant permissions once. Tests no longer read the real state dir.
- Speaker-ring screenshots are served by the AX helper app (`shot`, `poll --shot-req`): it owns Screen Recording, fixing `ring_capture_failed` for the python agent. Formula `test do` runs the unit-test suite.

- Homebrew formula (local tap `devopspass/tap`): builds the three ad-hoc signed apps with swiftc, hash-locked torch-free Python venv, deps `ffmpeg` + `python@3.13`, arm64, macOS 14.2+.
- New CLI: `dop-recorder setup | start | stop | uninstall`; `DOP_STATE_DIR` isolates state, logs, models, apps and LaunchAgents.
- Model: Azure CDN `.pt` (sha256-verified) converted locally to MLX format, no torch; Silero VAD via bundled ONNX.
- Removed `install.sh`, `uninstall.sh`, `packaging/` and the `ct` alias.
