"""scripts/ensure_env.sh is how a fresh machine gets a working Python.

Users run everything from the Claude Code desktop app and never type into a
terminal. On a fresh Windows machine `python` is the Microsoft Store stub, so
the skill and CLAUDE.md run every command through the Python this script
builds (2026-09-28). The full fresh-machine path installs uv and downloads
Python, so it is verified by hand, not here: these tests stay hermetic.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "ensure_env.sh"


def test_script_keeps_lf_endings():
    """Git for Windows checks text out as CRLF unless .gitattributes says
    otherwise, and bash then fails on every line with a stray \r."""
    assert b"\r\n" not in SCRIPT.read_bytes()
    assert "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_script_parses():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, timeout=30)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")


def test_script_checks_every_runtime_requirement():
    """A package added to requirements.txt but not to the import check would
    let the fast path skip an install the run then crashes on."""
    import_names = {"beautifulsoup4": "bs4", "python-dotenv": "dotenv"}
    text = SCRIPT.read_text(encoding="utf-8")
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        pkg = re.split(r"[<>=!~\[ ]", line, maxsplit=1)[0]
        assert f'"{import_names.get(pkg, pkg)}"' in text, f"ensure_env.sh does not check {pkg}"


def test_instructions_never_run_bare_python():
    """Every command Claude is told to run goes through ensure_env.sh."""
    for doc in (ROOT / ".claude/skills/str-comping-agent/SKILL.md", ROOT / "CLAUDE.md"):
        for n, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            assert not re.match(r"\s*(python3?|pip3?) ", line), f"{doc.name}:{n} runs bare python: {line.strip()}"
