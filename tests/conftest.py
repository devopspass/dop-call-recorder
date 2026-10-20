import os

import pytest

os.environ.setdefault("DOP_CR_AX_DISABLE", "1")  # never talk to the real Accessibility helper from tests


@pytest.fixture(autouse=True)
def _isolated_self_cache(tmp_path, monkeypatch):
    from dop_call_recorder import axpoll
    monkeypatch.setattr(axpoll, "SELF_CACHE", tmp_path / "ax_self_cache.json")


@pytest.fixture(autouse=True)
def _no_ambient_state_dir(monkeypatch, tmp_path):
    # never read/write the real per-user state (e.g. a learned speaker_class.json); tests may override with their own dir
    monkeypatch.setenv("DOP_STATE_DIR", str(tmp_path / "state"))


