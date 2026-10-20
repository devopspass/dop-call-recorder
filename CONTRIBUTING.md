# Contributing

Issues and MRs welcome. Keep changes small and tested. Developer workflow (setup, tests, apps, signing/TCC caveats, local brew test):
[.github/skills/dop-recorder-development/SKILL.md](.github/skills/dop-recorder-development/SKILL.md). Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Releases: [RELEASING.md](RELEASING.md).

- Run `.venv/bin/python -m pytest -q` before sending a PR (tests never touch your real state: see the skill).
- Update `CHANGELOG.md` (Unreleased) for user-visible changes.
- PR titles: `feat|fix|docs|chore|refactor|ci: short description`.
- Do not commit transcripts, audio, personal vocabulary or on-screen text dumps (`ax dump`).
- Be kind: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
