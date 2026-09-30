"""scripts/detach_from_github.sh keeps a student's copy from pushing to our GitHub.

At the 2026-09-29 summit students said "save" and their Claude tried to commit
and push to Solnest-AI's repo, because a `git clone` leaves the copy linked to
it. Setup Step 0 removes the link from an untouched copy, and must never touch
a developer's working copy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_ensure_env import _git_bash

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "detach_from_github.sh"
PUBLIC = "https://github.com/Solnest-AI/solnest-str-comping-agent.git"

pytestmark = pytest.mark.skipif(_git_bash() is None or shutil.which("git") is None,
                                reason="git and Git Bash needed")

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _student_copy(tmp_path: Path, origin: str = PUBLIC) -> Path:
    """What a `git clone` of the public repo looks like: main, tracking origin/main."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    work = tmp_path / "solnest-str-comping-agent"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    (work / "scripts").mkdir()
    shutil.copy(SCRIPT, work / "scripts" / SCRIPT.name)
    (work / "agent.py").write_text("print('hi')\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "push", "-q", "origin", "main")
    _git(work, "remote", "set-url", "origin", origin)
    return work


def _run(work: Path, **env: str) -> str:
    r = subprocess.run([_git_bash(), str(work / "scripts" / SCRIPT.name)], cwd=work,
                       env={**ENV, **env}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, "setup must never stop on this step"
    return r.stdout


def test_a_fresh_clone_is_detached(tmp_path):
    work = _student_copy(tmp_path)
    out = _run(work)
    assert not (work / ".git").exists() and "detached" in out
    assert (work / "agent.py").exists(), "only .git goes; the tool stays"
    assert "not attached" in _run(work), "running it again is a no-op"


def test_the_ssh_and_mixed_case_addresses_count(tmp_path):
    work = _student_copy(tmp_path, origin="git@github.com:solnest-ai/Solnest-STR-Comping-Agent")
    _run(work)
    assert not (work / ".git").exists()


@pytest.mark.parametrize("change", ["edit", "branch", "commit", "stash", "other_origin"])
def test_a_working_copy_is_never_touched(tmp_path, change):
    work = _student_copy(tmp_path, origin="https://github.com/someone/fork.git" if change == "other_origin" else PUBLIC)
    if change == "edit":
        (work / "agent.py").write_text("print('changed')\n", encoding="utf-8")
    elif change == "branch":
        _git(work, "branch", "feature")
    elif change == "commit":
        (work / "new.py").write_text("x = 1\n", encoding="utf-8")
        _git(work, "add", "new.py")
        _git(work, "commit", "-qm", "local work")
    elif change == "stash":
        (work / "agent.py").write_text("print('wip')\n", encoding="utf-8")
        _git(work, "stash", "-q")
    out = _run(work)
    assert (work / ".git").is_dir() and "kept attached" in out
    assert _git(work, "remote").strip() == "origin", "the remote stays too"


def test_a_developer_can_opt_out(tmp_path):
    work = _student_copy(tmp_path)
    (work / ".git" / "str-keep-attached").touch()
    assert "still attached" in _run(work) and (work / ".git").is_dir()
    (work / ".git" / "str-keep-attached").unlink()
    assert "still attached" in _run(work, STR_KEEP_GIT="1") and (work / ".git").is_dir()


def test_setup_detaches_first_and_the_skill_forbids_pushing():
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    setup = claude.split('## When the user says "set this up"', 1)[1]
    assert setup.index("detach_from_github.sh") < setup.index("### Step 1")
    assert "Never commit, push, fork" in setup
    skill = (ROOT / ".claude/skills/str-comping-agent/SKILL.md").read_text(encoding="utf-8")
    assert "Never `git commit`, push, fork" in skill
    assert "detach_from_github" not in skill, "running the skill must never strip a developer's git"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    note = readme.split("> **Claude, setting this up for someone:**", 1)[1].split("\n\n**", 1)[0]
    assert "archive/refs/heads/main.tar.gz" in note and "do **not** `git clone`" in note


def test_script_keeps_lf_endings():
    assert b"\r\n" not in SCRIPT.read_bytes()
