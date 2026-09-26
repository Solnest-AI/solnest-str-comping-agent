"""The distribution zip ships the product and nothing owner-only.

Found 2026-09-26 in a clean-room install of the summit zip: it carried the dev
skills (GitNexus + auto-generated cluster skills that mention AirDNA), internal
handoff notes with owner paths, an old backup copy of agent.py, and a ledger
calibration script. Every attendee's Claude Code session would have loaded them.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("package", ROOT / "scripts" / "package.py")
package = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(package)


def test_dev_only_material_is_blocked():
    for rel in (
        "HANDOFF.md",
        "_pre-sync-backup-20260621-202528/agent.py",
        "_pre-sync-backup-20260621-202528/scrapers/airroi.py",
        ".claude/skills/gitnexus/gitnexus-cli/SKILL.md",
        ".claude/skills/generated/scrapers/SKILL.md",
        "docs/superpowers/plans/x.md",
        "scripts/calibrate_against_ledger.py",
        "solneststays-full.png",
        ".env",
        "branding.json",
    ):
        assert not package.is_safe(Path(rel)), rel


def test_the_product_still_ships():
    for rel in (
        "agent.py",
        "CLAUDE.md",
        "README.md",
        "SETUP.md",
        ".env.example",
        "branding.example.json",
        ".claude/skills/str-comping-agent/SKILL.md",
        "generators/narratives.py",
        "scrapers/airroi.py",
        "scripts/package.py",
        "templates/report.html.j2",
        "tests/test_package_contents.py",
    ):
        assert package.is_safe(Path(rel)), rel
