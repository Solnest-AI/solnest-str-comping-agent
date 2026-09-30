"""No brand yet: Claude must ASK the student for their website and wait. Hermetic.

"Ask the student for their company website" alone was not enough (2026-09-29).
A headless "set this up" with another copy's branding.json on disk read that
file and branded from its site without asking. Every place that hands the
branding step to Claude must say to wait for the answer and never guess.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import agent  # noqa: E402
import check_setup as cs  # noqa: E402
import config  # noqa: E402

ASK = '"What is your company website?"'
WAIT = "wait for their answer"
NO_GUESS = "another copy's branding.json"


def _says_ask_and_wait(text: str) -> None:
    assert ASK in text
    assert WAIT in text
    assert NO_GUESS in text


def test_check_setup_exit_4_asks_and_waits(tmp_path, monkeypatch, capsys):
    kit_dir = tmp_path / "str-secrets-connections"
    kit_dir.mkdir()
    (kit_dir / "CONNECTIONS.md").write_text("", encoding="utf-8")
    (kit_dir / "fan-out-env.sh").write_text("", encoding="utf-8")
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    monkeypatch.setattr(cs.kit, "find_kit", lambda: kit_dir)
    monkeypatch.setattr(cs.kit, "find_kits", lambda: [kit_dir])
    monkeypatch.setattr(cs, "PROBES", {n: (lambda key: "ok") for n in cs.REQUIRED})
    monkeypatch.setattr(cs, "STAMP", tmp_path / "stamp.json")
    monkeypatch.setattr(cs, "other_copy", lambda name: ("", ""))
    monkeypatch.setattr(cs, "BRANDING", tmp_path / "branding.json")   # never written

    assert cs.main(["--no-open"]) == 4
    _says_ask_and_wait(capsys.readouterr().out)


def test_the_run_itself_stops_and_asks_and_waits(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "_BRANDING_PATH", tmp_path / "branding.json")
    assert not agent._require_branding()
    _says_ask_and_wait(capsys.readouterr().out)


def test_the_instructions_claude_reads_say_ask_and_wait():
    for doc in ("CLAUDE.md", ".claude/skills/str-comping-agent/SKILL.md"):
        text = " ".join((ROOT / doc).read_text(encoding="utf-8").split())
        assert "wait for their answer" in text, doc
        assert "Never fill it in yourself" in text, doc
