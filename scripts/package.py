#!/usr/bin/env python3
"""Build a distribution zip of the comping agent.

Usage:
    python scripts/package.py                 # -> dist/str-comping-agent.zip
    python scripts/package.py --out <path>    # also copy the zip to <path>

The file manifest comes from `git ls-files`, so the zip contains exactly the
tracked, published file set and nothing else. That matters: .gitignore is the
single place secrets and owner-specific files are excluded (.env, branding.json,
output/, docs/, AGENTS.md, STR-Agent-Updates-*.md), and sourcing the manifest
from git means this script can never drift out of sync with it.

An earlier version walked the filesystem with its own hand-maintained exclude
list. It missed .venv, .ruff_cache, the local backup dir, branding.json and the
client-data updates doc, producing a 44 MB zip that leaked real client revenue
figures and shipped owner-specific branding to end users. Don't reintroduce a manual
walk.
"""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DIST_DIR = PROJECT_ROOT / "dist"
ZIP_NAME = "str-comping-agent.zip"
TOP_LEVEL = "str-comping-agent"

# Belt-and-braces. Nothing matching these ever enters the zip, even if it
# somehow becomes tracked. Checked against each path part and the full
# relative path.
NEVER_SHIP = (
    ".env",
    "branding.json",
    "AGENTS.md",
    "branding-logo.png",
    # Merge-time reference material. These are snapshots of the OLD public repo
    # kept side-by-side during the merge so the two versions could be diffed.
    # They are not part of the product and must never reach a user's copy.
    "README.public-original.md",
)
NEVER_SHIP_GLOBS = (
    "STR-Agent-Updates-*.md",
    "_pub_*_reference.py",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
)


def tracked_files() -> list[Path]:
    """The exact set of files git publishes, as repo-relative paths."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        sys.exit(f"ERROR: could not read the git manifest ({exc}). Run this inside the repo.")

    files = [PROJECT_ROOT / p for p in out.split("\0") if p]
    if not files:
        sys.exit("ERROR: git reported no tracked files — refusing to build an empty zip.")
    return files


def is_safe(rel: Path) -> bool:
    if any(part in NEVER_SHIP for part in rel.parts):
        return False
    return not any(rel.match(g) or rel.name == g for g in NEVER_SHIP_GLOBS)


def build_zip(extra_out: Path | None = None) -> Path:
    DIST_DIR.mkdir(exist_ok=True)
    zip_path = DIST_DIR / ZIP_NAME
    zip_path.unlink(missing_ok=True)

    blocked, count = [], 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(tracked_files()):
            rel = path.relative_to(PROJECT_ROOT)
            if not is_safe(rel):
                blocked.append(rel)
                continue
            if not path.is_file():
                continue  # tracked but deleted locally
            zf.write(path, f"{TOP_LEVEL}/{rel}")
            count += 1

    size_kb = zip_path.stat().st_size / 1024
    print(f"\n{'=' * 56}")
    print("  Distribution zip created")
    print(f"  File:  {zip_path.resolve()}")
    print(f"  Size:  {size_kb:.0f} KB")
    print(f"  Files: {count}")
    if blocked:
        print(f"  Blocked by safety net: {', '.join(str(b) for b in blocked)}")
    print(f"{'=' * 56}")

    if extra_out:
        extra_out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(zip_path, extra_out)
        print(f"  Copied to: {extra_out}")

    return zip_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the distribution zip.")
    ap.add_argument("--out", type=Path, default=None,
                    help="Additional destination to copy the finished zip to")
    args = ap.parse_args()
    build_zip(args.out)
