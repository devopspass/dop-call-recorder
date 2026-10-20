"""M4b: SQLite FTS5 index over the transcripts on disk + search / list.

The index is derived data: it is rebuilt/updated from <output_dir>/YYYY/<call>/transcript.md (front matter +
`**[HH:MM:SS] Speaker:** text` turns). Location: cfg["search"]["index_path"] or <output_dir>/.index.sqlite.
Incremental: a call is re-read only when transcript.md mtime/size changed; calls whose folder vanished
(retention cleanup) are dropped. Per-turn language comes from the heuristic in dop_call_recorder.langid (md has no per-turn lang).
Hook for the pipeline: `index_call(cfg, call_dir)` (never raises).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from dop_call_recorder import langid, log

_log = log.get("search")

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls(
  path TEXT PRIMARY KEY, title TEXT, date TEXT, time TEXT, start TEXT, duration TEXT, duration_seconds REAL,
  apps TEXT, languages TEXT, quality TEXT, mtime REAL, size INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS seg USING fts5(
  text, path UNINDEXED, ts UNINDEXED, line UNINDEXED, speaker UNINDEXED, lang UNINDEXED,
  tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS info(k TEXT PRIMARY KEY, v TEXT);
"""
VERSION = "1"
TURN_RE = re.compile(r"^\*\*\[(\d+):(\d\d):(\d\d)\] (.+?):\*\* (.*)$")


class SearchError(Exception):
    pass


def index_path(cfg: dict) -> Path:
    p = (cfg.get("search") or {}).get("index_path") or ""
    return Path(p).expanduser() if p else Path(cfg["output_dir"]) / ".index.sqlite"


def connect(cfg: dict, create: bool = True) -> sqlite3.Connection:
    p = index_path(cfg)
    if create:
        p.parent.mkdir(parents=True, exist_ok=True)
    elif not p.exists():
        raise SearchError(f"no index at {p}; run `dop-recorder index` first")
    db = sqlite3.connect(p, timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


# ---------------------------------------------------------------- parsing
def _val(v: str):
    v = v.strip()
    try:
        return json.loads(v)
    except ValueError:
        return v


def parse_transcript(path: Path) -> tuple[dict, list[dict]]:
    """-> (front matter dict, [{ts, line, speaker, text}])"""
    lines = path.read_text(encoding="utf-8").splitlines()
    fm: dict = {}
    body_from = 0
    if lines and lines[0].strip() == "---":
        for i, ln in enumerate(lines[1:], 1):
            if ln.strip() == "---":
                body_from = i + 1
                break
            k, _, v = ln.partition(":")
            if _:
                fm[k.strip()] = _val(v)
    turns = []
    for i in range(body_from, len(lines)):
        m = TURN_RE.match(lines[i])
        if m:
            h, mi, s, spk, text = m.groups()
            turns.append({"ts": int(h) * 3600 + int(mi) * 60 + int(s), "line": i + 1, "speaker": spk, "text": text})
    return fm, turns


def call_dirs(cfg: dict) -> list[Path]:
    root = Path(cfg["output_dir"])
    return sorted({p.parent for p in list(root.glob("*/*/transcript.md")) + list(root.glob("*/transcript.md"))})


def _rel(cfg: dict, d: Path) -> str:
    return str(Path(d).resolve().relative_to(Path(cfg["output_dir"]).resolve()))


# ---------------------------------------------------------------- indexing
def _upsert(db: sqlite3.Connection, cfg: dict, d: Path) -> int:
    tp = d / "transcript.md"
    st = tp.stat()
    fm, turns = parse_transcript(tp)
    meta = {}
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    rel = _rel(cfg, d)
    start = str(fm.get("start") or meta.get("start") or "")
    day = str(fm.get("date") or meta.get("date") or start[:10] or d.name[:10])
    title = fm.get("title") or meta.get("title") or d.name
    apps = list(fm.get("apps") or meta.get("apps") or []) + list(meta.get("app_bundle_ids") or [])
    langs = fm.get("languages") or meta.get("languages") or []
    call_lang = fm.get("language") or (langs[0] if langs else None)
    db.execute("DELETE FROM seg WHERE path=?", (rel,))
    db.execute("DELETE FROM calls WHERE path=?", (rel,))
    db.execute("INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
        rel, str(title), day, start[11:16], start, str(fm.get("duration") or meta.get("duration") or ""),
        float(fm.get("duration_seconds") or meta.get("duration_seconds") or 0),
        " ".join(str(a) for a in apps).lower(), " ".join(langs).lower(), str(fm.get("quality") or "good"),
        st.st_mtime, st.st_size))
    rows = [(str(title), rel, 0, 1, "(title)", call_lang or "")]
    for t in turns:
        rows.append((t["text"], rel, t["ts"], t["line"], t["speaker"], langid.detect(t["text"], call_lang) or call_lang or ""))
    db.executemany("INSERT INTO seg(text,path,ts,line,speaker,lang) VALUES(?,?,?,?,?,?)", rows)
    return len(turns)


def update(cfg: dict, rebuild: bool = False) -> dict:
    """Incremental index of all calls on disk. -> {"indexed", "unchanged", "removed", "calls"}"""
    db = connect(cfg)
    try:
        if rebuild or (db.execute("SELECT v FROM info WHERE k='version'").fetchone() or [None])[0] != VERSION:
            db.executescript("DELETE FROM seg; DELETE FROM calls;")
            db.execute("INSERT OR REPLACE INTO info VALUES('version',?)", (VERSION,))
        known = {r["path"]: (r["mtime"], r["size"]) for r in db.execute("SELECT path,mtime,size FROM calls")}
        seen, indexed = set(), 0
        for d in call_dirs(cfg):
            rel = _rel(cfg, d)
            seen.add(rel)
            st = (d / "transcript.md").stat()
            if known.get(rel) == (st.st_mtime, st.st_size):
                continue
            try:
                _upsert(db, cfg, d)
                indexed += 1
            except Exception as e:  # one broken transcript must not block the rest
                log.event(_log, "index_failed", call=rel, error=f"{type(e).__name__}: {e}"[:200])
        gone = [p for p in known if p not in seen]
        for p in gone:
            db.execute("DELETE FROM seg WHERE path=?", (p,))
            db.execute("DELETE FROM calls WHERE path=?", (p,))
        db.commit()
        total = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
        res = {"indexed": indexed, "unchanged": len(seen) - indexed, "removed": len(gone), "calls": total}
        log.event(_log, "index_updated", **res)
        return res
    finally:
        db.close()


def index_call(cfg: dict, call_dir: str | Path) -> bool:
    """Pipeline hook: (re)index one finished call. Never raises."""
    try:
        db = connect(cfg)
        try:
            _upsert(db, cfg, Path(call_dir))
            db.commit()
        finally:
            db.close()
        return True
    except Exception as e:
        log.event(_log, "index_call_failed", call=str(call_dir), error=f"{type(e).__name__}: {e}"[:200])
        return False


# ---------------------------------------------------------------- search
def parse_since(s: str | None) -> str | None:
    """'7d' | '2w' | '3m' | 'YYYY-MM-DD' -> 'YYYY-MM-DD' lower bound (inclusive)."""
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*([dwm])", s.strip().lower())
    if m:
        n, u = int(m.group(1)), m.group(2)
        return (date.today() - timedelta(days=n * {"d": 1, "w": 7, "m": 30}[u])).isoformat()
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise SearchError(f"bad --since {s!r}: use 7d, 2w, 3m or YYYY-MM-DD") from None


def _friendly(q: str, e: Exception) -> SearchError:
    msg = str(e)
    hint = ('use "exact phrase", prefix*, AND/OR/NOT, NEAR(a b); wrap terms containing - : . / in double quotes '
            '(e.g. "CB-12345")')
    return SearchError(f"cannot parse query {q!r} ({msg}); {hint}")


MARK_A, MARK_B = "\x01", "\x02"


def search(cfg: dict, query: str, since: str | None = None, app: str | None = None, speaker: str | None = None,
           lang: str | None = None, limit: int = 20, auto_update: bool = True) -> list[dict]:
    if not query or not query.strip():
        raise SearchError("empty query")
    if auto_update:
        update(cfg)
    db = connect(cfg, create=False)
    try:
        where, args = ["seg MATCH ?"], ['text : (' + query + ')']
        if since_d := parse_since(since):
            where.append("c.date >= ?"); args.append(since_d)
        if app:
            where.append("c.apps LIKE ?"); args.append(f"%{app.lower()}%")
        if speaker:
            where.append("seg.speaker = ? COLLATE NOCASE"); args.append(speaker)
        if lang:
            where.append("(seg.lang = ? COLLATE NOCASE OR c.languages LIKE ?)"); args += [lang, f"%{lang.lower()}%"]
        sql = (f"SELECT c.path, c.title, c.date, c.time, c.quality, seg.ts, seg.line, seg.speaker, seg.lang, "
               f"snippet(seg, 0, '{MARK_A}', '{MARK_B}', '…', 24) AS snip, bm25(seg) AS rank "
               f"FROM seg JOIN calls c ON c.path = seg.path WHERE " + " AND ".join(f"({w})" for w in where) +
               " ORDER BY rank, c.date DESC, seg.ts LIMIT ?")
        try:
            rows = db.execute(sql, args + [max(1, limit)]).fetchall()
        except sqlite3.OperationalError as e:
            raise _friendly(query, e) from None
        root = Path(cfg["output_dir"])
        out = []
        for r in rows:
            d = dict(r)
            d["abs_path"] = str(root / r["path"] / "transcript.md")
            out.append(d)
        return out
    finally:
        db.close()


def hms(t: int) -> str:
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def highlight(snip: str, color: bool) -> str:
    a, b = ("\033[1;33m", "\033[0m") if color else ("**", "**")
    return snip.replace(MARK_A, a).replace(MARK_B, b).replace("\n", " ")


def link(hit: dict, kind: str = "vscode") -> str:
    if kind == "file":
        return Path(hit["abs_path"]).as_uri()
    return f"vscode://file{hit['abs_path']}:{hit['line']}"


def format_hits(hits: list[dict], color: bool = False, open_kind: str | None = None) -> str:
    if not hits:
        return "no matches"
    out = []
    for i, h in enumerate(hits, 1):
        who = "" if h["speaker"] == "(title)" else f" {h['speaker']}"
        lang = f" ({h['lang']})" if h["lang"] else ""
        out.append(f"{i:>2}. {h['title']} — {h['date']} {h['time']}  [{hms(h['ts'])}]{who}{lang}")
        out.append(f"    {highlight(h['snip'], color)}")
        out.append(f"    {h['abs_path']}" if not open_kind else f"    {link(h, open_kind)}")
    return "\n".join(out)


# ---------------------------------------------------------------- list
def list_calls(cfg: dict, since: str | None = None, limit: int = 50) -> list[dict]:
    update(cfg)
    db = connect(cfg, create=False)
    try:
        args: list = []
        sql = "SELECT path,title,date,time,duration,quality,languages,apps FROM calls"
        if since_d := parse_since(since):
            sql += " WHERE date >= ?"; args.append(since_d)
        sql += " ORDER BY start DESC, path DESC LIMIT ?"
        return [dict(r) for r in db.execute(sql, args + [limit]).fetchall()]
    finally:
        db.close()


def format_calls(rows: list[dict]) -> str:
    if not rows:
        return "no calls"
    head = ["Date", "Time", "Duration", "Quality", "Lang", "Title", "Path"]
    data = [[r["date"], r["time"], r["duration"], r["quality"], r["languages"].replace(" ", ","), r["title"], r["path"]]
            for r in rows]
    w = [max(len(x[i]) for x in [head] + data) for i in range(len(head))]
    fmt = lambda x: "  ".join(c.ljust(w[i]) for i, c in enumerate(x)).rstrip()  # noqa: E731
    return "\n".join([fmt(head), fmt(["-" * n for n in w])] + [fmt(x) for x in data])
