#!/usr/bin/env python3
"""Bring a student's copy of the comping agent to the latest release.

    PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/update.py          update, then run setup Step 1
    PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/update.py --check  only say whether a newer release exists
    PY="$(bash scripts/ensure_env.sh)" && "$PY" scripts/update.py --undo   put back the version from before the last update

A student's copy is a plain folder, not a git clone (setup Step 0 runs
detach_from_github.sh: at the 2026-09-29 summit "save" turned into a push to
Solnest-AI's GitHub). A plain folder has no `git pull`. The old path copied
main's tarball over the folder, which left behind every file a newer version
deleted (a stale test or script can break setup), shipped untagged work on main,
and never told anyone an update existed. Every fix now ships as a GitHub release
(a tag plus plain-English notes); this downloads that release's source and
replaces the tool's own files. The Listing Optimizer shipped the same updater in
v1.1.0 (Solnest-AI/listing-optimizer PR #8).

What it never touches: .env (and every .env.* but the shipped .env.example),
branding.json, proforma.json, partners/, output/, .cache/, .venv/. Only paths in
the release are written, and only paths the previous release shipped are removed
(the manifest, .comping-files.txt). Every file it replaces or removes is copied
to .update-backup/<time>/ first, so --undo is one step, and a failure halfway
puts everything back before it exits. A folder still linked to git is refused
(use `git pull --ff-only` there).

Standard library only: a broken .venv, or a release that needs a new package,
must not stop the update that fixes it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
REPO = "Solnest-AI/solnest-str-comping-agent"
# One "<git blob hash> <path>" line per file the tool shipped. Plain text, not
# JSON, because detach_from_github.sh writes the first one with git alone,
# before setup has installed Python.
MANIFEST = ".comping-files.txt"
BACKUP_DIR = ".update-backup"
KEEP_BACKUPS = 3
NOTICE_CACHE = Path(".cache") / "update_check.json"
OPT_OUT = "COMPING_NO_UPDATE_CHECK"
UA = {"User-Agent": "solnest-comping-agent-updater"}

# Never written, never removed, whatever a manifest or tarball says.
PROTECTED_FILES = {".env", "branding.json", "proforma.json", MANIFEST, ".claude/settings.local.json"}
PROTECTED_DIRS = (".venv/", "venv/", "output/", ".cache/", "partners/", ".git/", f"{BACKUP_DIR}/", "dist/")


class UpdateError(RuntimeError):
    """The update could not run; the folder is unchanged (or was put back)."""


def _protected(rel: str) -> bool:
    return (rel in PROTECTED_FILES or (rel.startswith(".env.") and rel != ".env.example")
            or rel.startswith(PROTECTED_DIRS))


def parse_version(text) -> tuple[int, ...] | None:
    m = re.fullmatch(r"\s*v?(\d+)\.(\d+)\.(\d+)\s*", str(text or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def current_version(root: Path) -> str | None:
    try:
        v = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return v if parse_version(v) else None


# ── GitHub ─────────────────────────────────────────────────────────────
def latest_tag(timeout: float = 20) -> str:
    """The latest release's tag. github.com redirects /releases/latest to
    /releases/tag/<tag>; unlike the API, that is not limited to 60 requests an
    hour per network, and a classroom shares one network."""
    req = urllib.request.Request(f"https://github.com/{REPO}/releases/latest", headers=UA, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            final = r.geturl()
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"could not reach GitHub ({type(e).__name__})") from e
    m = re.search(r"/releases/tag/([^/?#]+)$", final)
    if not m or not parse_version(m.group(1)):
        raise UpdateError("GitHub has no published release of the comping agent yet")
    return m.group(1)


def download(tag: str, timeout: float = 60) -> bytes:
    url = f"https://github.com/{REPO}/archive/refs/tags/{tag}.tar.gz"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"could not download {tag} from GitHub ({type(e).__name__})") from e


def read_tarball(blob: bytes) -> dict[str, bytes]:
    """{relative path: content} from a GitHub source tarball. Anything that is
    not a plain file or folder inside the one top folder (a link, an absolute
    path, a ..) refuses the whole release: nothing is written."""
    files: dict[str, bytes] = {}
    meta = (tarfile.XGLTYPE, tarfile.XHDTYPE)  # GitHub's pax_global_header is metadata, not a path
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            members = tar.getmembers()
            tops = {PurePosixPath(m.name).parts[0] for m in members
                    if m.name and not m.name.startswith("/") and m.type not in meta}
            for m in members:
                p = PurePosixPath(m.name)
                if m.name.startswith(("/", "\\")) or ".." in p.parts or len(tops) != 1:
                    raise UpdateError(f"the release contains an unsafe path ({m.name}); nothing was changed")
                if m.isdir() or m.type in meta:
                    continue
                if not m.isfile():
                    raise UpdateError(f"the release contains a link or device ({m.name}); nothing was changed")
                rel = "/".join(p.parts[1:])
                if rel:
                    files[rel] = tar.extractfile(m).read()
    except (tarfile.TarError, OSError, EOFError) as e:
        raise UpdateError(f"the downloaded release is not a valid archive ({type(e).__name__})") from e
    if "VERSION" not in files or not parse_version(files["VERSION"].decode("utf-8", "replace")):
        raise UpdateError("the downloaded release has no VERSION file; nothing was changed")
    return files


# ── Manifest ───────────────────────────────────────────────────────────
def file_hash(data: bytes) -> str:
    """Git's blob hash of the bytes as they are on disk: exactly what
    `git hash-object --no-filters` prints, which is how detach_from_github.sh
    writes the first manifest before Python exists."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def load_manifest(root: Path) -> dict[str, str]:
    """{path: hash} of the files the installed release shipped; {} when there
    is none or it is damaged (then nothing is removed on that evidence)."""
    try:
        text = (root / MANIFEST).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    files = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        digest, sep, rel = line.partition(" ")
        if not (sep and re.fullmatch(r"[0-9a-f]{40}", digest) and rel) or "\0" in rel:
            return {}
        if not _protected(rel):
            files[rel] = digest
    return files


def write_manifest(root: Path, paths) -> None:
    """Record the tool's own files (and their content) so the next update knows
    what it may replace or remove, and which ones the student edited."""
    lines = ["# Files the comping agent shipped (scripts/update.py). Do not edit."]
    for rel in sorted(set(paths)):
        p = root / rel
        if not _protected(rel) and p.is_file():
            lines.append(f"{file_hash(p.read_bytes())} {rel}")
    _write(root / MANIFEST, ("\n".join(lines) + "\n").encode("utf-8"))


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".update-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _remove(root: Path, rel: str) -> None:
    """Delete one file, then any folder that leaves empty (up to the root)."""
    (root / rel).unlink(missing_ok=True)
    parent = (root / rel).parent
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


# ── Update / undo ──────────────────────────────────────────────────────
def update(root: Path = ROOT) -> dict:
    if (root / ".git").exists():
        raise UpdateError("this folder is still linked to git: update it with `git pull --ff-only`, "
                          "or run setup Step 0, which unlinks an untouched copy")
    have = current_version(root)
    tag = latest_tag()
    want = tag.lstrip("v")
    if have and parse_version(have) >= parse_version(want):
        return {"status": "current", "from": have, "to": have}
    release = read_tarball(download(tag))
    labelled = release["VERSION"].decode("utf-8", "replace").strip()
    if parse_version(labelled) != parse_version(want):
        raise UpdateError(f"the {tag} release is labelled {labelled} inside, so installing it would never "
                          f"finish; nothing was changed. Tell Solnest AI")
    new = {rel: data for rel, data in release.items() if not _protected(rel)}

    old = load_manifest(root)
    if not old and have:
        # A tarball or zip install never had .git, so nothing wrote a manifest.
        # Its VERSION names the release it came from, which lists what it shipped.
        with contextlib.suppress(UpdateError):
            old = {rel: file_hash(data) for rel, data in read_tarball(download(f"v{have}")).items()
                   if not _protected(rel)}
    gone = sorted(rel for rel in set(old) - set(new) if (root / rel).is_file())
    edited = sorted(rel for rel, h in old.items()
                    if (root / rel).is_file() and file_hash((root / rel).read_bytes()) != h)

    backup = root / BACKUP_DIR / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    existed, added = [], []
    for rel in sorted(set(new) | set(gone)):
        src = root / rel
        if src.is_file():
            _write(backup / rel, src.read_bytes())
            existed.append(rel)
        else:
            added.append(rel)
    had_manifest = (root / MANIFEST).is_file()
    if had_manifest:
        _write(backup / MANIFEST, (root / MANIFEST).read_bytes())
    _write(backup / "undo.json", json.dumps({"from": have, "to": want, "existed": existed, "added": added,
                                             "had_manifest": had_manifest}, indent=1).encode())
    try:
        for rel, data in new.items():
            _write(root / rel, data)
        for rel in gone:
            _remove(root, rel)
        write_manifest(root, new)
    except OSError as e:
        undo(root, backup)
        raise UpdateError(f"the update stopped halfway ({type(e).__name__}: {e}) and the previous version "
                          f"was put back; close other programs using this folder and try again") from e
    _prune(root / BACKUP_DIR)
    return {"status": "updated", "from": have or "unknown", "to": want, "added": added, "removed": gone,
            "edited": [rel for rel in edited if rel in new or rel in gone], "backup": str(backup)}


def undo(root: Path = ROOT, backup: Path | None = None) -> dict:
    """Put back the files from before the last update (or the given backup)."""
    if backup is None:
        found = sorted(p for p in (root / BACKUP_DIR).glob("*") if (p / "undo.json").is_file()) \
            if (root / BACKUP_DIR).is_dir() else []
        if not found:
            raise UpdateError("there is no earlier version to go back to")
        backup = found[-1]
    meta = json.loads((backup / "undo.json").read_text(encoding="utf-8"))
    for rel in meta.get("added") or []:
        if not _protected(rel):
            _remove(root, rel)
    for rel in meta.get("existed") or []:
        if not _protected(rel):
            _write(root / rel, (backup / rel).read_bytes())
    if meta.get("had_manifest") and (backup / MANIFEST).is_file():
        _write(root / MANIFEST, (backup / MANIFEST).read_bytes())
    else:
        (root / MANIFEST).unlink(missing_ok=True)
    shutil.rmtree(backup, ignore_errors=True)
    if (root / BACKUP_DIR).is_dir() and not any((root / BACKUP_DIR).iterdir()):
        (root / BACKUP_DIR).rmdir()
    return {"status": "undone", "to": meta.get("from") or "unknown", "from": meta.get("to")}


def _prune(folder: Path) -> None:
    for old in sorted(p for p in folder.glob("*") if p.is_dir())[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)


# ── The once-a-day notice at the end of a run ──────────────────────────
def notice(root: Path = ROOT, today: str | None = None) -> str | None:
    """'Update available' text when a newer release exists, else None. Checked
    at most once a day (cached in .cache/), silent when offline, and off with
    COMPING_NO_UPDATE_CHECK=1. Never raises: a report must never fail on this."""
    try:
        if (os.environ.get(OPT_OUT) or "").strip().lower() in ("1", "true", "yes"):
            return None
        today = today or date.today().isoformat()
        cache = root / NOTICE_CACHE
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = {}
        latest = cached.get("latest") if isinstance(cached, dict) and cached.get("checked") == today else None
        if latest is None:
            try:
                latest = latest_tag(timeout=3)
            except Exception:  # offline, GitHub down, anything: say nothing
                latest = ""
            with contextlib.suppress(OSError):
                _write(cache, json.dumps({"checked": today, "latest": latest}).encode())
        have = current_version(root)
        if not parse_version(latest) or (have and parse_version(have) >= parse_version(latest)):
            return None
        return (f"Update available: Comping Agent {latest.lstrip('v')} (this copy: {have or 'an older version'}). "
                f"Say \"update the comping agent\" to get it; your keys, branding and reports are kept.")
    except Exception:
        return None


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Update this copy to the latest comping agent release.")
    ap.add_argument("--check", action="store_true", help="only say whether a newer release exists")
    ap.add_argument("--undo", action="store_true", help="put back the version from before the last update")
    args = ap.parse_args()
    try:
        if args.undo:
            r = undo()
            print(f"[update] Put back version {r['to']} (was {r['from']}). Run setup Step 1 next so packages match.")
        elif args.check:
            tag, have = latest_tag(), current_version(ROOT)
            newer = not have or parse_version(have) < parse_version(tag)
            print(f"[update] Latest release: {tag.lstrip('v')}; this copy: {have or 'unknown'}"
                  + (". An update is available." if newer else ". Up to date."))
        else:
            r = update()
            if r["status"] == "current":
                print(f"[update] Already on the latest release ({r['to']}). Nothing to do.")
            else:
                print(f"[update] Updated {r['from']} -> {r['to']}: {len(r['added'])} new file(s), "
                      f"{len(r['removed'])} removed. Kept: .env, branding.json, proforma.json, partners/, "
                      f"output/ and .cache/.")
                if r["edited"]:
                    print(f"[update] You had edited {len(r['edited'])} file(s); your versions are saved in "
                          f"{r['backup']}: {', '.join(r['edited'][:10])}")
                print("[update] Next: run setup Step 1 (it installs any new packages), then "
                      "scripts/check_setup.py. To go back: scripts/update.py --undo")
    except UpdateError as e:
        print(f"[update] {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
