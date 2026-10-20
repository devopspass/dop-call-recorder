"""Interpret Microsoft Teams Accessibility trees (X1; trees come from axhelper/ as JSON).

Pure functions, no macOS dependency: input is the node dicts emitted by `axhelper dump|snapshot`
({"role","title","desc","value","help","id","dom","class","children":[...]}), output is
{"in_call","title","self","roster","speaking","windows"}.

Evidence status (see README X1): self name and idle window titles are VERIFIED against a real Teams 26261 window;
in-call title/roster/active-speaker rules below are HEURISTICS written from the Teams web-UI a11y conventions
("Name, Muted", "(You)", `speaking` DOM classes) and still need a real multi-person call to confirm.
All regexes are module constants so they can be tuned after looking at `ax_samples.jsonl` of a real call.
"""
from __future__ import annotations

import re
from typing import Iterator

IDLE_TITLE_RE = re.compile(
    r"^(Chat|Activity|Calendar|Calls|Teams|Files|OneDrive|Apps|Copilot|Search|Settings|Store|Home|Notes|Planner|Viva \w+)\s*\|")
GENERIC_TITLE_RE = re.compile(r"^(microsoft teams|teams|calling|call|meeting|\s*)$", re.I)
CALL_CONTROL_RE = re.compile(r"(?i)^(leave|hang up|end call|end meeting|leave meeting|leave call)\b")
ROSTER_LABEL_RE = re.compile(r"(?i)^(participants?|people|attendees|roster|in this meeting|in the meeting|participant list)\b")
SPEAKING_TEXT_RE = re.compile(r"(?i)\b(is speaking|speaking now|active speaker|currently speaking)\b|\(speaking\)|,\s*speaking\b")
SPEAKING_CLASS_RE = re.compile(r"(?i)(^|[\s_-])(is-?speaking|speaking|active-?speaker)($|[\s_-])")
SELF_RE = re.compile(r"^(?:Chat\s+)?(?P<n>[^|()]+?)\s*\(You\)\s*$")
STATUS = (r"muted|unmuted|organizer|co-organizer|presenter|attendee|guest|external|speaking|is speaking|video on|video off|"
          r"camera on|camera off|hand raised|raised hand|pinned|spotlighted|screen sharing|sharing|presenting|connecting|"
          r"in the lobby|lobby|calling|ringing|on hold|available|busy|away|offline|do not disturb|be right back|"
          r"\(you\)|you|mic on|mic off|microphone on|microphone off|video is on|video is off|fill frame|fit to frame|"
          r"has context menu|context menu is available|has control of production tools|unknown")
STATUS_RE = re.compile(rf"^(?:{STATUS})$", re.I)
UI_WORDS = re.compile(r"(?i)^(mute|unmute|camera|share|people|chat|leave|more|raise|react|view|apps|notes|participants?|"
                      r"meeting|call|microphone|speaker|record|captions|settings|close|back|search|invite|add)$")
SUFFIX_RE = re.compile(r"(?i)\s*(\(you\)|\(guest\)|\(external\)|\(unverified\)|\(organizer\)|\(presenter\))\s*$")


UI_PREFIX_RE = re.compile(r"(?i)^(meeting compact view|compact view|meeting|call|sharing control|calling)\s*\|\s*")
DENY_NAME_RE = re.compile(r"(?i)\b(show|hide|only|filter|filters|mute|unmute|options|menu|controls?|participants?|people|"
                          r"everyone|view|stream|video|camera|mic|microphone|captions|reactions?|raise|share|chat)\b")
MYSELF_RE = re.compile(r"^Myself video,\s*(?P<rest>.+)$")


def _text(n: dict) -> str:
    return (n.get("title") or n.get("desc") or n.get("value") or "").strip()


def walk(n: dict, anc: tuple = ()) -> Iterator[tuple[dict, tuple]]:
    yield n, anc
    for c in n.get("children", []) or []:
        yield from walk(c, anc + (n,))


def display_name(raw: str, first_last: bool = True) -> str:
    """'Doe, Jane (You)' -> 'Jane Doe' (Teams shows 'Surname, Name')."""
    s = raw.strip()
    while True:
        t = SUFFIX_RE.sub("", s)
        if t == s:
            break
        s = t
    s = re.sub(r"\s+", " ", s).strip(" ,")
    if first_last and s.count(",") == 1:
        a, b = (x.strip() for x in s.split(","))
        if a and b:
            s = f"{b} {a}"
    return s


REAL_STATUS_RE = re.compile(r"(?i)^(muted|unmuted|organizer|co-organizer|presenter|attendee|guest|external|in the lobby|"
                            r"context menu is available|has context menu|video is on|video is off)$")


def entry_name(text: str, speaking_ok: bool = True) -> str | None:
    """Participant name from an a11y label like 'Doe, Jane, Muted' / 'Echo, Context menu is available'.
    Needs a real status token (or an 'is speaking' marker when speaking_ok); UI strings are rejected."""
    text = re.sub(r"^Myself video,\s*", "", text.strip())
    t = SPEAKING_TEXT_RE.sub("", text).strip(" ,")
    had = speaking_ok and t != text.strip(" ,")
    parts = [p.strip() for p in t.split(",")]
    real = False
    while parts and STATUS_RE.match(parts[-1]):
        real = real or bool(REAL_STATUS_RE.match(parts[-1]))
        parts.pop()
    name = ", ".join(p for p in parts if p)
    if not (real or had) or not name or len(name) > 80 or "\n" in name or name.endswith(".") or len(parts) > 2:
        return None
    if UI_WORDS.match(name) or DENY_NAME_RE.search(name):
        return None
    return display_name(name)


def _key(n: str) -> str:
    return " ".join(sorted(n.lower().replace(",", " ").split()))


def _plain_name(t: str) -> str | None:
    """Row text that is just a person name ('Surname, Name' / 'Name Surname', optional '(You)'/role suffix)."""
    t = re.sub(r"(?i),?\s*(organizer|presenter|attendee|guest|external)$", "", t.strip())
    t = SUFFIX_RE.sub("", t)
    if not t or len(t) > 60 or "\n" in t or t.count(",") > 1 or DENY_NAME_RE.search(t) or UI_WORDS.match(t) or any(c.isdigit() for c in t):
        return None
    return display_name(t) if re.match(r"^[^\W\d_][\w .'’-]*(, [\w .'’-]+)?$", t) else None


def _is_call_window(win: dict) -> bool:
    for n, _ in walk(win):
        if n.get("role") in ("AXButton", "AXPopUpButton", "AXMenuButton") and CALL_CONTROL_RE.match(_text(n)):
            return True
    return False


def clean_title(win_title: str) -> str | None:
    """'Meeting compact view | Echo | Microsoft Teams' -> 'Echo'."""
    t = re.sub(r"\s*\|\s*Microsoft Teams\s*$", "", win_title or "").strip()
    while UI_PREFIX_RE.match(t) and UI_PREFIX_RE.sub("", t, count=1).strip():
        t = UI_PREFIX_RE.sub("", t, count=1).strip()
    return None if GENERIC_TITLE_RE.match(t) or IDLE_TITLE_RE.match(win_title or "") else t


def find_self(trees: list[dict]) -> str | None:
    for win in trees:
        for n, _ in walk(win):
            t = _text(n)
            if "(You)" in t and len(t) < 120:
                m = SELF_RE.match(t.replace("\n", " "))
                if m:
                    return display_name(m.group("n"))
    return None


UNMUTED_RE = re.compile(r"(?i)\bmuted\b")


def tiles_of(calls: list[dict]) -> list[dict]:
    """Gallery tiles in AX (= visual row-major) order: [{name, muted, self, classes}]; first occurrence of a name wins.
    `classes` = CSS class tokens of the tile's container: the outermost ancestor that holds exactly one tile."""
    items = []
    for w in calls:
        for n, anc in walk(w):
            if n.get("role") == "AXMenuItem":
                t = _text(n)
                if t and len(t) < 160 and entry_name(t):
                    items.append((n, anc, t))
    per_node: dict[int, int] = {}
    for n, anc, _ in items:
        for a in anc + (n,):
            per_node[id(a)] = per_node.get(id(a), 0) + 1
    seen, out = set(), []
    for n, anc, t in items:
        nm = entry_name(t)
        if _key(nm) in seen:
            continue
        seen.add(_key(nm))
        cont = n
        for a in reversed(anc):
            if per_node.get(id(a)) != 1:
                break
            cont = a
        toks: set[str] = set()
        for m, _ in walk(cont):
            toks.update((m.get("class") or "").split())
        parts = [p.strip().lower() for p in t.split(",")]
        out.append({"name": nm, "muted": "muted" in parts, "self": bool(MYSELF_RE.match(t)), "classes": sorted(toks)})
    return out


def parse(trees: list[dict]) -> dict:
    """trees: AX window trees of the Teams app -> state dict (never raises on odd trees)."""
    out = {"in_call": False, "title": None, "self": None, "roster": [], "speaking": [], "windows": [], "tiles": []}
    try:
        out["windows"] = [w.get("title") or "" for w in trees]
        out["self"] = find_self(trees)
        calls = [w for w in trees if _is_call_window(w)]
        if not calls:
            return out
        out["in_call"] = True
        roster: list[str] = []
        speaking: list[str] = []
        for w in calls:
            out["title"] = out["title"] or clean_title(w.get("title") or "")
            for n, anc in walk(w):
                t = _text(n)
                if t and len(t) < 160:
                    nm = entry_name(t)
                    if nm:
                        roster.append(nm)
                        if MYSELF_RE.match(t):  # own tile: 'Myself video, Surname, Name, Unmuted, ...'
                            out["self"] = out["self"] or nm
                        if SPEAKING_TEXT_RE.search(t):
                            speaking.append(nm)
                cls = n.get("class") or ""
                if cls and SPEAKING_CLASS_RE.search(cls):
                    cand = entry_name(t) if t else None
                    if not cand:  # name lives on a descendant or ancestor
                        for m, _ in walk(n):
                            cand = entry_name(_text(m)) if _text(m) else None
                            if cand:
                                break
                    if not cand:
                        for a in reversed(anc[-3:]):
                            cand = entry_name(_text(a)) if _text(a) else None
                            if cand:
                                break
                    if cand:
                        speaking.append(cand)
                if t and ROSTER_LABEL_RE.match(t) and n.get("role") in ("AXList", "AXOutline", "AXTable", "AXGroup"):
                    for m, _ in walk(n):  # rows of a labelled participants list
                        if m.get("role") in ("AXRow", "AXCell") or m.get("role") == "AXStaticText":
                            nm = entry_name(_text(m)) if _text(m) else None
                            if not nm and m.get("role") == "AXRow":
                                nm = _plain_name(_text(m))
                            if nm:
                                roster.append(nm)
        seen = list(dict.fromkeys(roster))
        out["roster"] = seen
        out["tiles"] = tiles_of(calls)
        # an 'active speaker' only counts if it is a known participant (never a UI label)
        known = {_key(n) for n in seen}
        out["speaking"] = [n for n in dict.fromkeys(speaking) if _key(n) in known]
    except Exception as e:  # unreadable tree must never break the pipeline
        out["error"] = f"{type(e).__name__}: {e}"
    return out


ATOMIC_CLASS_RE = re.compile(r"^(f[0-9a-z]{4,10}|___[0-9a-z]+|r[0-9a-z]{6,}|fui-(Button|Flex|Primitive|Toolbar|Menu\w*))$")


def semantic_classes(cls: str) -> str:
    """DOM class list without Fluent UI atomic-CSS hashes (keeps tokens such as 'speaking', 'ui-text')."""
    return " ".join(c for c in cls.split() if not ATOMIC_CLASS_RE.match(c))


def compact_sample(trees: list[dict], limit: int = 800) -> list[str]:
    """Outline of the call windows for ax_samples.jsonl (tuning data for real calls): every node with text or
    with a non-atomic DOM class. Windows without call controls are never included."""
    lines: list[str] = []
    for w in trees:
        if not _is_call_window(w):
            continue
        lines.append(f"# window: {w.get('title')}")
        for n, anc in walk(w):
            t = _text(n)
            cls = semantic_classes(n.get("class") or "")
            if t or cls:
                lines.append(f"{len(anc)} {n.get('role')} | {t[:120]!r} | {n.get('dom', '')} | {cls[:120]}")
            if len(lines) >= limit:
                return lines
    return lines
