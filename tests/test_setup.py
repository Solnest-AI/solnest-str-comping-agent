"""setup.py is the first thing a student runs. It must agree with the docs.

CLAUDE.md, README.md and SETUP.md all say two keys are required (AirROI and
Firecrawl, since 2026-09-28) and that there is no Anthropic step. setup.py once
marked Anthropic as REQUIRED, printed 'Still missing required keys' and
returned 1 to a student who had done exactly what the docs said.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import setup

_ROOT = Path(__file__).parent.parent


def test_airroi_and_firecrawl_are_required_and_nothing_else():
    required = {k for k, _label, req, _where, _hint in setup.KEYS if req}
    assert required == {"AIRROI_API_KEY", "FIRECRAWL_API_KEY"}


def test_airbtics_is_no_longer_asked_for():
    assert "AIRBTICS_API_KEY" not in {k for k, *_ in setup.KEYS}
    assert "airbtics" not in (_ROOT / ".env.example").read_text(encoding="utf-8").lower()


def test_setup_completes_with_the_two_required_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup, "ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(setup, "ENV_EXAMPLE", tmp_path / ".env.example")
    (tmp_path / ".env.example").write_text((_ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")

    answers = iter(["ar_test_key_000000000000", "fc-test-key-000000000000"])
    monkeypatch.setattr("builtins.input", lambda _p="": next(answers, ""))

    assert setup.main() == 0

    out = capsys.readouterr().out
    assert "Still missing required keys" not in out
    assert "<- REQUIRED" not in out and "← REQUIRED" not in out

    env = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "AIRROI_API_KEY=ar_test_key_000000000000" in env
    assert "FIRECRAWL_API_KEY=fc-test-key-000000000000" in env
    assert "AIRBTICS" not in env


def test_setup_survives_a_cp1252_pipe(tmp_path):
    """Claude Code runs setup.py through a pipe; on Windows Python then writes
    cp1252, which cannot encode the ── and … it prints (2026-09-27). Forcing
    cp1252 reproduces that on every OS. Runs on a copy so no real .env is written."""
    shutil.copy(_ROOT / "setup.py", tmp_path)
    shutil.copy(_ROOT / "kit.py", tmp_path)   # setup.py parses .env with kit.read_env
    shutil.copy(_ROOT / ".env.example", tmp_path)
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    env.pop("PYTHONUTF8", None)
    r = subprocess.run([sys.executable, "setup.py"], cwd=tmp_path, env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=60)
    assert b"UnicodeEncodeError" not in r.stderr, r.stderr.decode("utf-8", "replace")[-500:]
    assert "Setup".encode() in r.stdout
