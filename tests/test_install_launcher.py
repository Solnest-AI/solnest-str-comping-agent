"""The user-level launcher makes the skill reachable from any Claude Code session.

Headless test, 2026-09-28: a session opened outside the cloned folder never
saw the project skill, improvised comps from web searches, produced no report
and spent $6.48.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import install_launcher as il  # noqa: E402


def test_launcher_points_at_this_folder_with_the_skills_own_trigger(tmp_path):
    path = il.install(home=tmp_path)
    text = path.read_text(encoding="utf-8")
    assert path == tmp_path / ".claude/skills/str-comping-agent/SKILL.md"
    assert "name: str-comping-agent" in text
    assert ROOT.resolve().as_posix() in text
    project = (ROOT / ".claude/skills/str-comping-agent/SKILL.md").read_text(encoding="utf-8")
    description = next(line for line in project.splitlines() if line.startswith("description:"))
    assert description in text, "launcher must trigger on the same phrases as the skill"
    assert "\\" not in text.split("---", 2)[2].split("If that folder")[0], "Windows path must use forward slashes"


def test_rerun_rewrites_its_own_launcher(tmp_path):
    il.install(home=tmp_path, root=Path("C:/old/place"))
    path = il.install(home=tmp_path)
    assert "C:/old/place" not in path.read_text(encoding="utf-8")


def test_never_overwrites_a_skill_it_did_not_write(tmp_path):
    target = tmp_path / ".claude/skills/str-comping-agent/SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("someone else's skill", encoding="utf-8")
    with pytest.raises(SystemExit):
        il.install(home=tmp_path)
    assert target.read_text(encoding="utf-8") == "someone else's skill"
