"""config.toml loading with defaults. Other milestones may add keys; unknown keys are preserved."""
from __future__ import annotations

import copy
import os
import re
import tomllib
from pathlib import Path

from dop_call_recorder import paths

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "output_dir": "~/Transcripts",
    "retention_days": 30,
    "apps": ["com.microsoft.teams2"],
    "backend": "auto",  # auto = mlx-whisper if importable and converted model exists, else openai-whisper
    "model": "large-v3-turbo",
    "model_url": "",  # optional mirror: {url}/mlx-<model>/{config.json,weights.safetensors}
    "model_dir": "",  # "" -> paths.models_dir()
    "vocabulary_file": "vocabulary.yaml",
    "language": "",
    "transcribe": {
        "vad": "silero",
        "no_speech_threshold": 0.6,
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
        "condition_on_previous_text": False,
        "temperature": [0.0, 0.2, 0.4],
    },
    "work_dir": "",  # "" -> <state dir>/sessions
    "pipeline": {
        "window_minutes": 30, "overlap_seconds": 15, "nice": 10,
        "chapter_gap_seconds": 60, "chapter_max_minutes": 10,
        "echo_window_seconds": 2.0, "stale_heartbeat_seconds": 30,
    },
    "device": "cpu",  # openai-whisper only: cpu | mps
    "mlx": {"model_path": ""},  # default <model_dir>/mlx-<model>
    "whispercpp": {"binary": "", "model_path": "", "beam_size": 1},  # default <model_dir>/ggml-<model>.bin
    "preprocess": {"vad_trim": False, "loudnorm": False, "denoise": False},
    "silence": {"end_of_call_seconds": 60, "min_call_seconds": 30},
    "timeouts": {"detector_poll_seconds": 2, "transcribe_timeout_seconds": 7200},
    "logging": {"file": "", "level": "INFO", "max_mb": 10, "backups": 5},
    "disk": {"warn_gb": 5, "critical_gb": 1, "raw_max_age_days": 7, "check_seconds": 30},  # M5
    "notify": {"enabled": True, "on_start": True, "on_done": True, "on_error": True, "on_warn": True, "on_info": True, "sound": ""},
    "agent": {"status_seconds": 2, "caffeinate": True},
    "ax": {"enabled": True, "interval_seconds": 1.0, "my_name": "", "max_seconds": 21600, "sample_seconds": 10, "max_samples": 30},  # X1
}


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def config_path() -> Path:
    return paths.config_path()


def load(path: str | Path | None = None) -> dict:
    p = Path(path).expanduser() if path else config_path()
    cfg = copy.deepcopy(DEFAULTS)
    if p.exists():
        with open(p, "rb") as f:
            _merge(cfg, tomllib.load(f))
    cfg["_path"] = str(p)
    # pre-rename configs: transparently point old default locations at the migrated ones
    if str(cfg.get("work_dir", "")).rstrip("/") == "~/Library/Application Support/calls_transcriber/sessions":
        cfg["work_dir"] = DEFAULTS["work_dir"]
    lf = cfg.get("logging", {}).get("file", "")
    if lf.startswith("~/Library/Logs/calls_transcriber/"):
        lf = cfg["logging"]["file"] = lf.replace("calls_transcriber", "dop-call-recorder", 1)
    if lf.startswith("~/Library/Logs/dop-call-recorder/") and lf.endswith("/ct.log"):
        cfg["logging"]["file"] = lf[: -len("ct.log")] + "dop-call-recorder.log"
    if not cfg.get("model_dir"):
        cfg["model_dir"] = str(paths.models_dir())
    if not cfg.get("work_dir") or str(cfg["work_dir"]).rstrip("/") == "~/Library/Application Support/dop-call-recorder/sessions":
        cfg["work_dir"] = str(paths.work_dir())
    if not cfg["logging"].get("file"):
        cfg["logging"]["file"] = str(paths.logs_dir() / "dop-call-recorder.log")
    for key in ("output_dir", "model_dir", "work_dir"):
        cfg[key] = str(Path(cfg[key]).expanduser())
    vf = cfg.get("vocabulary_file") or ""
    if vf:
        vp = Path(vf).expanduser()
        cfg["vocabulary_file"] = str(vp if vp.is_absolute() else p.parent / vp)
    return cfg


PROMPT_TOKEN_BUDGET = 200  # Whisper keeps only the LAST ~223 prompt tokens; stay below it
_SEP = "->"


def _read_vocab(cfg: dict) -> tuple[list[str], list[tuple[list[str], str]]]:
    """Parse the vocabulary file -> (terms, corrections).
    *.yaml/*.yml: `terms: [..]` + `corrections: {Right: [wrong, ..]}` (Right is also a prompt term).
    Anything else = legacy text: one term per line (`#` comments); `wrong | wrong2 -> Right` = correction."""
    vf = cfg.get("vocabulary_file")
    if not vf or not Path(vf).exists():
        return [], []
    text = Path(vf).read_text(encoding="utf-8")
    terms: list[str] = []
    corr: list[tuple[list[str], str]] = []
    if Path(vf).suffix.lower() in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{vf}: expected a mapping with `terms:` and/or `corrections:`")
        explicit = [str(t).strip() for t in data.get("terms") or [] if str(t).strip()]
        for right, wrongs in (data.get("corrections") or {}).items():
            if isinstance(wrongs, str):
                wrongs = [wrongs]
            wrongs = [str(w).strip() for w in wrongs or [] if str(w).strip()]
            if wrongs and str(right).strip():
                corr.append((wrongs, str(right).strip()))
                terms.append(str(right).strip())
        return terms + explicit, corr  # explicit terms last = highest prompt priority
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if _SEP in line:
            left, right = (x.strip() for x in line.split(_SEP, 1))
            wrongs = [w.strip() for w in left.split("|") if w.strip()]
            if wrongs and right:
                corr.append((wrongs, right))
                terms.append(right)
            continue
        terms.append(line.rstrip(",;"))
    return terms, corr


def est_tokens(text: str) -> int:
    """Cheap, deliberately pessimistic token estimate (no tokenizer needed)."""
    return len(text) // 3 + 2


def fit_prompt(terms: list[str], budget: int = PROMPT_TOKEN_BUDGET) -> tuple[str, list[str]]:
    """Dedupe (last occurrence wins) and keep the LAST terms that fit `budget`. Returns (prompt, dropped)."""
    seen: set[str] = set()
    uniq: list[str] = []
    for t in reversed(terms):
        k = t.casefold()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    uniq.reverse()
    kept: list[str] = []
    used = 0
    for t in reversed(uniq):
        c = est_tokens(t) + 1
        if used + c > budget:
            break
        kept.append(t)
        used += c
    kept.reverse()
    return ", ".join(kept), uniq[: len(uniq) - len(kept)]


def load_vocabulary(cfg: dict, extra_terms: list[str] | tuple[str, ...] = ()) -> str:
    """vocabulary file (+ per-call extra terms, e.g. roster names, highest priority = last) -> initial_prompt.
    Over budget -> the earliest terms are dropped (a warning is logged), so put important terms last."""
    terms, _ = _read_vocab(cfg)
    prompt, dropped = fit_prompt(terms + [t for t in extra_terms if t and t.strip()])
    if dropped:
        from dop_call_recorder import log
        log.event(log.get("config"), "vocabulary_truncated", dropped=len(dropped), first_dropped=dropped[:5])
    return prompt


_CORR_CACHE: dict = {}


def _corrections(cfg: dict) -> list[tuple[re.Pattern, str]]:
    vf = cfg.get("vocabulary_file")
    if not vf or not Path(vf).exists():
        return []
    key = (vf, Path(vf).stat().st_mtime_ns)
    if key not in _CORR_CACHE:
        rules = []
        for wrongs, right in _read_vocab(cfg)[1]:
            for w in sorted(wrongs, key=len, reverse=True):
                pat = r"\s+".join(re.escape(x) for x in w.split())
                rules.append((re.compile(rf"(?<!\w){pat}(?!\w)", re.IGNORECASE), right))
        _CORR_CACHE.clear()
        _CORR_CACHE[key] = rules
    return _CORR_CACHE[key]


def apply_corrections(cfg: dict, text: str) -> str:
    """Deterministic find/replace (whole words, case-insensitive) from the `corrections` of the vocabulary file."""
    for rx, right in _corrections(cfg):
        text = rx.sub(lambda _m, r=right: r, text)
    return text
