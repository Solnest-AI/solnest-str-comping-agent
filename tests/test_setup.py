"""setup.py is the first thing a student runs. It must agree with the docs.

CLAUDE.md, README.md and SETUP.md all say one key is required (AirROI) and
that there is no Anthropic step. setup.py marked Firecrawl and Anthropic as
REQUIRED, printed 'Still missing required keys' and returned 1 to a student
who had done exactly what the docs said.
"""

from __future__ import annotations

from pathlib import Path

import setup

_ROOT = Path(__file__).parent.parent


def test_only_airroi_is_required():
    required = {k for k, _label, req, _where, _hint in setup.KEYS if req}
    assert required == {"AIRROI_API_KEY"}


def test_airbtics_is_no_longer_asked_for():
    assert "AIRBTICS_API_KEY" not in {k for k, *_ in setup.KEYS}
    assert "airbtics" not in (_ROOT / ".env.example").read_text().lower()


def test_setup_completes_with_only_an_airroi_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(setup, "ENV_EXAMPLE", tmp_path / ".env.example")
    (tmp_path / ".env.example").write_text((_ROOT / ".env.example").read_text())

    answers = iter(["ar_test_key_000000000000"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers, ""))

    assert setup.main() == 0

    out = capsys.readouterr().out
    assert "Still missing required keys" not in out
    assert "<- REQUIRED" not in out and "← REQUIRED" not in out

    env = (tmp_path / ".env").read_text()
    assert "AIRROI_API_KEY=ar_test_key_000000000000" in env
    assert "AIRBTICS" not in env
