"""Tiny per-segment language heuristic (script + stopwords). Whisper detects language once per
transcription call (per window); this refines it per segment and flags wrong-language output.
Not a real LID model: short/ambiguous text falls back to the window language."""
from __future__ import annotations

import re

_STOP = {
    "en": "the and is are you that this with for have it not but what on we they was be of to in so just".split(),
    "de": "der die das und ist nicht ich sie wir mit für ein eine auch aber wie zu den dem".split(),
    "lt": "ir yra kad tai bet kaip aš tu mes jūs taip ne su iš kas čia jau dar tik".split(),
    "lv": "un ir ka tas bet kā es tu mēs jūs jā nē ar no kas šeit jau vēl tikai".split(),
    "et": "ja on ei see kui aga mina sina me te ka et siis või oli ma".split(),
    "pl": "i w nie to jest się na że ale jak do tak po co czy".split(),
}
CYRILLIC_LANGS = {"ru", "uk", "bg", "sr", "mk", "be"}


def script_shares(text: str) -> tuple[float, float]:
    """(cyrillic share, latin share) of the letters in text."""
    cyr = len(re.findall(r"[\u0400-\u04FF]", text))
    lat = len(re.findall(r"[A-Za-z\u00C0-\u017F]", text))
    n = cyr + lat
    return (cyr / n, lat / n) if n else (0.0, 0.0)


def detect(text: str, fallback: str | None = None) -> str | None:
    cyr, _ = script_shares(text)
    if cyr > 0.6:
        return fallback if fallback in CYRILLIC_LANGS else "ru"
    toks = re.findall(r"\w+", text.lower())
    if len(toks) < 4:
        return fallback
    scores = {k: sum(t in set(v) for t in toks) / len(toks) for k, v in _STOP.items()}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    (best, s1), (_, s2) = ranked[0], ranked[1]
    if s1 >= 0.12 and s1 >= 1.5 * s2:
        return best
    return fallback


def script_mismatch(text: str, lang: str | None) -> str | None:
    """Suggested language when the text's script contradicts `lang` (e.g. Cyrillic text labelled 'en'), else None."""
    if not lang or len(text) < 40:
        return None
    cyr, lat = script_shares(text)
    if lang in CYRILLIC_LANGS and lat > 0.8:
        return detect(text, None) or "en"
    if lang not in CYRILLIC_LANGS and cyr > 0.5:
        return "ru"
    return None
