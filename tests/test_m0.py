from pathlib import Path

from dop_call_recorder import config
from dop_call_recorder.transcribe import Segment, dedupe


def test_dedupe_limits_runs():
    segs = [Segment(i, i + 1, "Thank you.") for i in range(6)] + [Segment(7, 8, "Other")]
    out = dedupe(segs)
    assert [s.text for s in out] == ["Thank you.", "Thank you.", "Other"]


def test_dedupe_ignores_empty_and_punctuation():
    assert len(dedupe([Segment(0, 1, " "), Segment(1, 2, "Hi!"), Segment(2, 3, "hi")])) == 2


def test_config_merge_and_relative_vocab(tmp_path):
    (tmp_path / "v.txt").write_text("# c\nFlexcube,\nLogMiner # x\n\n")
    (tmp_path / "c.toml").write_text('model = "small"\nvocabulary_file = "v.txt"\n[transcribe]\nvad = "off"\n')
    cfg = config.load(tmp_path / "c.toml")
    assert cfg["model"] == "small"
    assert cfg["transcribe"]["vad"] == "off"
    assert cfg["transcribe"]["no_speech_threshold"] == 0.6  # default preserved
    assert Path(cfg["vocabulary_file"]) == tmp_path / "v.txt"
    assert config.load_vocabulary(cfg) == "Flexcube, LogMiner"
    assert not cfg["output_dir"].startswith("~")
