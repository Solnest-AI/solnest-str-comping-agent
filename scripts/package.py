#!/usr/bin/env python3
"""Build a distribution zip of the comping agent.

Usage: python scripts/package.py
Output: dist/str-comping-agent.zip
"""

import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
ZIP_NAME = "str-comping-agent.zip"

# Files/dirs to EXCLUDE from the distribution
EXCLUDE = {
    ".git",
    ".gitnexus",
    "__pycache__",
    ".pytest_cache",
    ".env",              # real keys — never distribute
    "output",            # generated reports
    "dist",              # the output of this script
    "docs/superpowers",  # dev planning docs
    "scripts",           # this script itself
    ".claude",           # Claude Code local config (includes GitNexus skills)
    ".remember",         # Claude Code local memory
    "AGENTS.md",         # GitNexus agent config
    "docs",              # dev planning docs, specs, plans
    "node_modules",
    "solneststays-full.png",  # dev asset
}

# File extensions to exclude
EXCLUDE_EXT = {".pyc", ".pyo"}


def should_include(path: Path, root: Path) -> bool:
    """Check if a file should be included in the zip."""
    rel = path.relative_to(root)
    parts = rel.parts

    # Check directory exclusions
    for part in parts:
        if part in EXCLUDE:
            return False

    # Check extension exclusions
    if path.suffix in EXCLUDE_EXT:
        return False

    return True


def build_zip():
    """Build the distribution zip."""
    DIST_DIR.mkdir(exist_ok=True)
    zip_path = DIST_DIR / ZIP_NAME

    # Remove old zip if exists
    if zip_path.exists():
        zip_path.unlink()

    file_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(PROJECT_ROOT.rglob("*")):
            if not path.is_file():
                continue
            if not should_include(path, PROJECT_ROOT):
                continue

            rel_path = path.relative_to(PROJECT_ROOT)
            arcname = f"str-comping-agent/{rel_path}"

            # Keep the original Solnest branding.json — users will customize
            # during setup via CLAUDE.md's branding flow
            if rel_path.name == "branding.example.json":
                continue  # skip the blank example — not needed in distribution

            zf.write(path, arcname)
            file_count += 1

        # .env.example is already included by the rglob — no need to add again

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"\n{'=' * 50}")
    print("  Distribution zip created!")
    print(f"  File: {zip_path.resolve()}")
    print(f"  Size: {size_mb:.1f} MB")
    print(f"  Files: {file_count}")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    build_zip()
