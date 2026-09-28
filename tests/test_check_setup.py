"""scripts/check_setup.py: the kit and both keys, before any paid run. Hermetic."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_setup as cs  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake kit, fake probes and a private stamp file."""
    kit_dir = tmp_path / "str-secrets-connections"
    kit_dir.mkdir()
    (kit_dir / "CONNECTIONS.md").write_text("", encoding="utf-8")
    (kit_dir / "fan-out-env.sh").write_text("", encoding="utf-8")
    probes = {"AIRROI_API_KEY": "ok", "FIRECRAWL_API_KEY": "ok"}
    calls = []

    def probe(name):
        def run(key):
            calls.append(name)
            return probes[name]
        return run

    monkeypatch.setattr(cs.kit, "find_kit", lambda: kit_dir)
    monkeypatch.setattr(cs, "PROBES", {n: probe(n) for n in cs.REQUIRED})
    monkeypatch.setattr(cs, "STAMP", tmp_path / "stamp.json")
    return kit_dir, probes, calls


def test_ready_when_both_keys_work_then_skips_paid_probes_for_a_day(world, capsys):
    kit_dir, _, calls = world
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    assert cs.main(["--no-open"]) == 0
    assert calls == ["AIRROI_API_KEY", "FIRECRAWL_API_KEY"]
    assert cs.main(["--no-open"]) == 0
    assert len(calls) == 2, "a fresh pass must not pay for another probe"
    assert "READY" in capsys.readouterr().out


def test_a_changed_key_is_probed_again(world):
    kit_dir, _, calls = world
    env = kit_dir / ".env"
    env.write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    cs.main(["--no-open"])
    env.write_text("AIRROI_API_KEY=new\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    cs.main(["--no-open"])
    assert len(calls) == 4


def test_blank_firecrawl_is_required_and_gets_a_line_to_paste_into(world, capsys):
    kit_dir, _, _ = world
    (kit_dir / ".env").write_text("STACK_PMS=hospitable\nAIRROI_API_KEY=a", encoding="utf-8")
    assert cs.main(["--no-open"]) == 2
    text = (kit_dir / ".env").read_text(encoding="utf-8")
    assert "STACK_PMS=hospitable\nAIRROI_API_KEY=a\nFIRECRAWL_API_KEY=\n" == text
    out = capsys.readouterr().out
    assert "FIRECRAWL_API_KEY: blank" in out and "never in the chat" in out


def test_a_rejected_key_is_not_ready(world):
    kit_dir, probes, _ = world
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    probes["AIRROI_API_KEY"] = "rejected"
    assert cs.main(["--no-open"]) == 2


def test_kit_never_run_or_missing_says_to_set_it_up(world, monkeypatch, capsys):
    assert cs.main(["--no-open"]) == 3            # kit folder, no .env
    monkeypatch.setattr(cs.kit, "find_kit", lambda: None)
    assert cs.main(["--no-open"]) == 3            # no kit at all
    assert "CONNECTIONS.md" in capsys.readouterr().out


def test_no_key_value_is_ever_printed(world, capsys):
    kit_dir, probes, _ = world
    (kit_dir / ".env").write_text("AIRROI_API_KEY=ar_SECRET_VALUE\nFIRECRAWL_API_KEY=fc-SECRET\n", encoding="utf-8")
    probes["FIRECRAWL_API_KEY"] = "rejected"
    cs.main(["--no-open"])
    out = capsys.readouterr().out
    assert "SECRET" not in out
