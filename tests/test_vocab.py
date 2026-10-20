from dop_call_recorder import config as c


def _cfg(tmp_path, text):
    f = tmp_path / "v.txt"
    f.write_text(text, encoding="utf-8")
    return {"vocabulary_file": str(f)}


def test_prompt_comments_and_corrections_terms(tmp_path):
    cfg = _cfg(tmp_path, "Flexcube, Oracle  # c\n# full comment\nmerch request -> Merge Request\n")
    assert c.load_vocabulary(cfg) == "Flexcube, Oracle, Merge Request"


def test_corrections_whole_word_case_insensitive(tmp_path):
    cfg = _cfg(tmp_path, "merch request | march request -> Merge Request\nterra ground -> TerraGrunt\n")
    assert c.apply_corrections(cfg, "Open a Merch  Request, then Terra Ground; unmerch requests") == \
        "Open a Merge Request, then TerraGrunt; unmerch requests"
    assert c.apply_corrections({}, "x") == "x"


def test_overflow_drops_earliest_keeps_last(tmp_path):
    terms = [f"Term{i:03d}word" for i in range(200)]
    cfg = _cfg(tmp_path, "\n".join(terms))
    p = c.load_vocabulary(cfg)
    assert p.endswith("Term199word") and "Term000word" not in p
    assert sum(c.est_tokens(t) + 1 for t in p.split(", ")) <= c.PROMPT_TOKEN_BUDGET


def test_extra_terms_last_and_dedupe(tmp_path):
    cfg = _cfg(tmp_path, "Alpha\nBeta\n")
    assert c.load_vocabulary(cfg, ["alpha", "Anna Kowalski"]) == "Beta, alpha, Anna Kowalski"


def test_corrections_reload_on_file_change(tmp_path):
    cfg = _cfg(tmp_path, "aa -> BB\n")
    assert c.apply_corrections(cfg, "aa") == "BB"
    import os, time
    f = tmp_path / "v.txt"
    f.write_text("aa -> CC\n")
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert c.apply_corrections(cfg, "aa") == "CC"


def test_yaml_format(tmp_path):
    f = tmp_path / "v.yaml"
    f.write_text("terms:\n  - Flexcube\n  - Oracle\ncorrections:\n  TerraGrunt: [terra grant, terra ground]\n  LogMiner: log miner\n",
                 encoding="utf-8")
    cfg = {"vocabulary_file": str(f)}
    assert c.load_vocabulary(cfg) == "TerraGrunt, LogMiner, Flexcube, Oracle"  # explicit terms last = top priority
    assert c.apply_corrections(cfg, "it is Terra Grant and log miner") == "it is TerraGrunt and LogMiner"


def test_repo_vocabulary_yaml_valid():
    cfg = c.load()
    assert cfg["vocabulary_file"].endswith("vocabulary.yaml")
    assert "Terraform" in c.load_vocabulary(cfg)
    assert c.apply_corrections(cfg, "deploy with terror form") == "deploy with Terraform"
