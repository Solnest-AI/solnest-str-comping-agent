#!/usr/bin/env python3
"""Install a user-level launcher so "run comps on ..." works from any folder.

Claude Code loads this project's skill only when a session is opened on this
folder. Users paste the GitHub link into the desktop app, Claude clones it into
a subfolder, and every later session starts somewhere else: a headless test of
exactly that (2026-09-28) never saw the skill, improvised comps from web
searches and raw MCP calls, produced no report and spent $6.48. The launcher
lives in ~/.claude/skills/ (loaded everywhere) and points back here.

    PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/install_launcher.py

Safe to re-run: it rewrites its own launcher (e.g. after the folder moves) and
never touches a skill of the same name that it did not write.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT_SKILL = ROOT / ".claude" / "skills" / "str-comping-agent" / "SKILL.md"
MARKER = "<!-- installed by solnest-str-comping-agent/scripts/install_launcher.py -->"


def _yaml_scalar_value(raw: str) -> str:
    """The text a one-line YAML scalar means, so it can be quoted once."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def launcher_text(root: Path = ROOT) -> str:
    source = PROJECT_SKILL.read_text(encoding="utf-8")
    m = re.search(r"^description:\s*(.+)$", source, re.MULTILINE)
    if not m:
        raise SystemExit(f"no description in {PROJECT_SKILL}")
    # Double-quoted, JSON-escaped: the description holds "quoted phrases" and
    # colons, either of which ends or breaks a plain YAML scalar and would leave
    # the launcher's front matter unparseable.
    description = json.dumps(_yaml_scalar_value(m.group(1)), ensure_ascii=False)
    home = root.as_posix()   # C:/Users/... works in Git Bash, PowerShell and Python
    return f"""---
name: str-comping-agent
description: {description}
---
{MARKER}

# STR Comping Agent (launcher)

The comping agent is installed at:

    {home}

Before doing anything else, `cd "{home}"` and read
`.claude/skills/str-comping-agent/SKILL.md` there, then follow it exactly. Run
every command from that folder. Do not build comps yourself from web searches
or AirROI MCP tools: the pipeline there does the data work and renders the
report.

If that folder no longer exists, tell the user to paste the GitHub link
(https://github.com/Solnest-AI/solnest-str-comping-agent) into Claude Code and
say "set this up" again.
"""


def install(home: Path | None = None, root: Path = ROOT) -> Path:
    target = (home or Path.home()) / ".claude" / "skills" / "str-comping-agent" / "SKILL.md"
    if target.exists() and MARKER not in target.read_text(encoding="utf-8", errors="replace"):
        raise SystemExit(f"{target} exists and was not written by this installer; leaving it alone.")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(launcher_text(root), encoding="utf-8", newline="\n")
    return target


if __name__ == "__main__":
    path = install()
    print(f"Launcher installed: {path}")
    print('From any Claude Code window: "run comps on <Airbnb link, Zillow link or address>".')
    sys.exit(0)
