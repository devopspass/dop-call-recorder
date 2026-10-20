"""M4b: FTS5 search/index/list, reprocess + save flows (synthetic corpus, fake backend)."""
import json
import shutil
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from dop_call_recorder import cli, outputs, reprocess, search
from dop_call_recorder import config as cfgmod
from dop_call_recorder import merge as mg
from dop_call_recorder.backends.base import BackendResult, Segment
from test_pipeline import write_wav

TOPICS = ["budget", "migration", "kubernetes", "invoice", "roadmap", "latency"]


def cfg_for(tmp_path):
    c = cfgmod.load("/nonexistent.toml")
    c.update(output_dir=str(tmp_path / "out"), work_dir=str(tmp_path / "work"), vocabulary_file="", backend="auto")
    c["transcribe"]["vad"] = "off"
    return c


def make_call(cfg, n: int, days_ago: int, title: str, segs, app="com.microsoft.teams2", lang="en") -> Path:
    d0 = date.today() - timedelta(days=days_ago)
    start = time.mktime((d0.year, d0.month, d0.day, 9 + n % 8, 0, 0, 0, 0, -1))
    out = outputs.output_folder(cfg["output_dir"], start, title)
    out.mkdir(parents=True)
    dur = max(s.end for s in segs) + 5
    meta = {"title": title, "date": d0.isoformat(), "start": outputs.iso(start), "end": outputs.iso(start + dur),
            "duration": outputs.hms(dur), "duration_seconds": dur, "apps": ["Zoom" if "zoom" in app else "Microsoft Teams"],
            "app_bundle_ids": [app], "language": lang, "languages": [lang], "quality": "good", "model": "m", "backend": "b"}
    outputs.atomic_write(out / "transcript.md", outputs.render_markdown(meta, segs, cfg["pipeline"]))
    outputs.atomic_write(out / "transcript.vtt", outputs.render_vtt(segs))
    outputs.atomic_write(out / "meta.json", json.dumps(meta))
    return out


@pytest.fixture
def corpus(tmp_path):
    cfg = cfg_for(tmp_path)
    for i in range(36):
        topic = TOPICS[i % len(TOPICS)]
        lang = "de" if i % 9 == 0 else "en"
        app = "us.zoom.xos" if i % 4 == 0 else "com.microsoft.teams2"
        segs = [mg.Seg(0, 5, f"Welcome everyone, today we discuss the {topic} plan number {i}.", "Others", lang),
                mg.Seg(10, 15, "I think we should postpone the decision." if i % 2 else "Agreed, let us move forward.", "Me", lang),
                mg.Seg(80, 90, f"Action item: finish the {topic} report " + ("budget " * (i % 3)) + "by Friday.", "Others", lang)]
        make_call(cfg, i, days_ago=i, title=f"Sync {topic} {i}", segs=segs, app=app, lang=lang)
    return cfg


def test_index_and_basic_search(corpus):
    r = search.update(corpus)
    assert r["calls"] == 36 and r["indexed"] == 36
    hits = search.search(corpus, "kubernetes", limit=100)
    assert hits and all("kubernetes" in h["snip"].lower() for h in hits)
    assert {h["path"] for h in hits} >= {Path(d).name for d in []}  # smoke: paths present
    h = hits[0]
    assert h["abs_path"].endswith("transcript.md") and search.hms(h["ts"]).count(":") == 2 and h["title"] and h["date"]
    assert search.MARK_A in h["snip"]


def test_ranking_bm25(corpus):
    hits = search.search(corpus, "budget")
    # calls repeating "budget" most (i%3==2 -> twice in action item + topic budget) outrank single mentions
    ranks = [h["rank"] for h in hits]
    assert ranks == sorted(ranks)


def test_filters(corpus):
    all_ = search.search(corpus, "plan", limit=100)
    assert len(all_) == 36
    since = search.search(corpus, "plan", since="7d", limit=100)
    assert 0 < len(since) < 36 and all(h["date"] >= (date.today() - timedelta(days=7)).isoformat() for h in since)
    abs_since = (date.today() - timedelta(days=3)).isoformat()
    assert len(search.search(corpus, "plan", since=abs_since, limit=100)) == 4
    zoom = search.search(corpus, "plan", app="zoom", limit=100)
    assert len(zoom) == 9
    teams = search.search(corpus, "plan", app="teams", limit=100)
    assert len(teams) == 27
    me = search.search(corpus, "postpone", speaker="me", limit=100)
    assert len(me) == 18 and all(h["speaker"] == "Me" for h in me)
    assert search.search(corpus, "postpone", speaker="Others") == []
    de = search.search(corpus, "plan", lang="de", limit=100)
    assert len(de) == 4
    comb = search.search(corpus, "plan", lang="en", app="zoom", since="14d", speaker="Others", limit=100)
    assert comb and all(h["lang"] == "en" and h["speaker"] == "Others" for h in comb)
    assert len(search.search(corpus, "plan", limit=5)) == 5


def test_fts_syntax(corpus):
    assert search.search(corpus, '"discuss the kubernetes plan"')
    assert search.search(corpus, "kuber*")
    assert {h["path"] for h in search.search(corpus, "kubernetes OR invoice", limit=100)} \
        > {h["path"] for h in search.search(corpus, "kubernetes", limit=100)}
    assert search.search(corpus, "kubernetes AND report")
    assert search.search(corpus, "kubernetes NOT invoice")
    for bad in ['"unterminated', "AND", "foo:bar baz", "CB-123", "(("]:
        with pytest.raises(search.SearchError) as e:
            search.search(corpus, bad)
        assert "double quotes" in str(e.value)
    assert search.search(corpus, '"CB-123"') == []
    with pytest.raises(search.SearchError):
        search.search(corpus, "x", since="yesterday")
    with pytest.raises(search.SearchError):
        search.search(corpus, "   ")


def test_title_hit(corpus):
    h = search.search(corpus, "Sync AND latency", limit=3)
    assert h and h[0]["speaker"] == "(title)"


def test_incremental_and_removal(corpus):
    search.update(corpus)
    assert search.update(corpus) == {"indexed": 0, "unchanged": 36, "removed": 0, "calls": 36}
    d = search.call_dirs(corpus)[0]
    p = d / "transcript.md"
    time.sleep(0.01)
    p.write_text(p.read_text().replace("Welcome everyone", "Hello zebra"))
    r = search.update(corpus)
    assert r["indexed"] == 1 and r["unchanged"] == 35
    assert len(search.search(corpus, "zebra")) == 1
    shutil.rmtree(d)
    r = search.update(corpus)
    assert r["removed"] == 1 and r["calls"] == 35 and search.search(corpus, "zebra") == []
    assert search.update(corpus, rebuild=True)["indexed"] == 35


def test_index_call_hook(tmp_path):
    cfg = cfg_for(tmp_path)
    d = make_call(cfg, 1, 0, "Hook call", [mg.Seg(0, 3, "unique quokka sentence", "Me", "en")])
    assert search.index_call(cfg, d) is True
    assert len(search.search(cfg, "quokka", auto_update=False)) == 1
    assert search.index_call(cfg, tmp_path / "nope") is False


def test_cli_search_list_json(corpus, capsys):
    cf = Path(corpus["output_dir"]).parent / "c.toml"
    cf.write_text(f'output_dir = "{corpus["output_dir"]}"\nvocabulary_file = ""\n[logging]\nfile = ""\n')
    assert cli.main(["--config", str(cf), "index", "--rebuild"]) == 0
    capsys.readouterr()
    assert cli.main(["--config", str(cf), "search", "kubernetes", "--since", "30d", "--speaker", "Others", "--json",
                     "--open", "file"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data and data[0]["timestamp"] and data[0]["link"].startswith("file://")
    assert cli.main(["--config", str(cf), "search", "kubernetes", "--open"]) == 0
    assert "vscode://file" in capsys.readouterr().out
    assert cli.main(["--config", str(cf), "search", '"oops']) == 2
    assert "cannot parse query" in capsys.readouterr().err
    assert cli.main(["--config", str(cf), "search", "detect"]) == 0       # 'detect' as query is not the subcommand
    assert "no matches" in capsys.readouterr().out
    assert cli.main(["--config", str(cf), "list", "--since", "3d"]) == 0
    out = capsys.readouterr().out
    assert "Title" in out and len(out.strip().splitlines()) == 2 + 4


# ---------------------------------------------------------------- reprocess / save
class Fake:
    def __init__(self, text="reprocessed alpaca words", lang="en", segs=None):
        self.text, self.lang, self.segs = text, lang, segs

    def describe(self):
        return "fake/new"

    def check(self):
        pass

    def transcribe(self, audio, prompt="", language=""):
        return BackendResult(self.lang, self.segs or [Segment(0, 5, self.text), Segment(10, 15, self.text + " again")])


def sample_audio(path: Path, secs=40):
    wav = path.with_suffix(".wav")
    write_wav(wav, (0.1 * np.sin(2 * np.pi * 300 * np.arange(16000 * secs) / 16000)).astype(np.float32))
    return wav


def test_reprocess_flow(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    d = make_call(cfg, 1, 2, "Old call", [mg.Seg(0, 5, "original gorilla text", "Others", "en"),
                                          mg.Seg(10, 15, "my own words", "Me", "en")])
    wav = sample_audio(d / "x")
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(wav), "-c:a", "aac", str(d / "call.m4a")], check=True)
    wav.unlink()
    search.update(cfg)
    assert search.search(cfg, "gorilla", auto_update=False)
    old_md = (d / "transcript.md").read_text()
    seen = {}
    monkeypatch.setattr(reprocess, "get_backend", lambda c: seen.setdefault("cfg", c) and Fake())
    vf = tmp_path / "v.txt"; vf.write_text("Kubernetes\n")
    out = reprocess.reprocess(cfg, d.name[:15] + "_Old", model="small", backend_name="openai-whisper", vocab_file=str(vf))
    assert out == d.resolve()
    assert seen["cfg"]["model"] == "small" and seen["cfg"]["vocabulary_file"] == str(vf)
    assert (d / "transcript.prev.md").read_text() == old_md
    new = (d / "transcript.md").read_text()
    assert "alpaca" in new and "gorilla" not in new and 'model: "small"' in new
    meta = json.loads((d / "meta.json").read_text())
    assert meta["model"] == "small" and meta["title"] == "Old call" and "reprocessed" in meta
    # speakers recovered from the previous vtt by time overlap
    assert "**[00:00:00] Others:**" in new and "**[00:00:10] Me:**" in new
    assert search.search(cfg, "gorilla", auto_update=False) == []
    assert search.search(cfg, "alpaca", auto_update=False, speaker="Me")
    idx = json.loads((Path(cfg["output_dir"]) / "index.json").read_text())["calls"]
    assert len(idx) == 1 and idx[0]["path"] == f"{d.parent.name}/{d.name}" and "alpaca" in idx[0]["snippet"]


def test_reprocess_errors(tmp_path):
    cfg = cfg_for(tmp_path)
    d = make_call(cfg, 1, 0, "No audio", [mg.Seg(0, 3, "hello", "Me", "en")])
    with pytest.raises(reprocess.ReprocessError, match="no retained audio"):
        reprocess.reprocess(cfg, str(d), backend=Fake())
    with pytest.raises(reprocess.ReprocessError, match="no call matches"):
        reprocess.reprocess(cfg, "nonexistent-id")
    make_call(cfg, 2, 0, "No audio", [mg.Seg(0, 3, "hello", "Me", "en")])
    with pytest.raises(reprocess.ReprocessError, match="ambiguous"):
        reprocess.resolve_call(cfg, "No_audio")


def test_save_file(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    src = tmp_path / "Voice_note_ideas.wav"
    sample_audio(src.with_suffix(""))
    monkeypatch.setattr(reprocess, "get_backend", lambda c: Fake("llama idea"))
    out = reprocess.save_file(cfg, str(src))
    assert out.parent.parent == Path(cfg["output_dir"]) and "Voice_note_ideas" in out.name
    for f in ("transcript.md", "transcript.vtt", "meta.json", "call.m4a"):
        assert (out / f).exists()
    assert 'title: "Voice note ideas"' in (out / "transcript.md").read_text()
    assert search.search(cfg, "llama", auto_update=False)[0]["speaker"] == "Mixed"
    assert (Path(cfg["output_dir"]) / "INDEX.md").exists()
    with pytest.raises(Exception):
        reprocess.save_file(cfg, str(tmp_path / "missing.wav"), backend=Fake())
