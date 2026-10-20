# Releasing

Versioning: SemVer. `dop_call_recorder/__init__.py::__version__` and `pyproject.toml` must match.

## Checklist
1. `rg -i "luminor|aliaksei|/Users/"` over the release content is clean (the release tarball ships `vocabulary.example.yaml` as `vocabulary.yaml`).
2. `.venv/bin/python -m pytest -q` green; `requirements.lock` current (`scripts/make-lock.sh`); update `THIRD_PARTY_NOTICES.md` if deps changed.
3. Move CHANGELOG "Unreleased" to the new version; bump version in both files.
4. `scripts/make-release.sh` builds `dist/dop-call-recorder-<ver>.tar.gz` and prints sha256.
5. Local brew test (rebuilds the apps and resets their macOS grants - expect to re-grant): `scripts/tap-install-local.sh && brew install --build-from-source devopspass/tap/dop-call-recorder && brew test devopspass/tap/dop-call-recorder`; then `dop-recorder setup`, `doctor`, one real call.
6. Tag `vX.Y.Z`, create the GitHub Release, attach the tarball.
7. In the `homebrew-tap` repo set `url`/`sha256` in `Formula/dop-call-recorder.rb` (url = release asset), `brew style` + `brew audit --strict --new devopspass/tap/dop-call-recorder`, push.
8. Clean-machine check: `brew tap` + `brew install` + `setup` + `doctor` on a user without your state.

## Known limits (state them in release notes)
Ad-hoc signed apps: every upgrade resets macOS permissions. Only Microsoft Teams is verified. Apple silicon, macOS 14.2+.
