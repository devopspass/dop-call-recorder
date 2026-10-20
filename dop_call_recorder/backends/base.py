"""Backend interface: turn 16 kHz mono float32 audio into timed segments."""
from __future__ import annotations

from dataclasses import dataclass, field


class BackendError(Exception):
    """Backend unavailable/misconfigured (missing package, model or binary). Message is user-facing."""


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class BackendResult:
    language: str | None
    segments: list[Segment] = field(default_factory=list)


class Backend:
    """Subclass and implement load() + transcribe(). Guard options come from cfg['transcribe']."""

    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tc = cfg["transcribe"]

    def check(self) -> None:
        """Cheap fail-fast validation (no model load). Raise BackendError with a fix hint."""

    def load(self) -> None:  # idempotent; expensive part kept out of __init__ so it can be timed
        pass

    def transcribe(self, audio, prompt: str = "", language: str = "") -> BackendResult:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name
