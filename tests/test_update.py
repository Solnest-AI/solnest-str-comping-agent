"""Student copies update by tagged release, keep their own files, and can go back.

A student's copy is a plain folder (scripts/detach_from_github.sh removes .git),
so it has no `git pull`. The old "update the comping agent" path copied main's
tarball over the folder: files a newer version deleted stayed behind (a stale
test or script can break setup), untagged work on main shipped, and nothing told
a student an update existed. scripts/update.py is the release-based updater the
Listing Optimizer shipped in v1.1.0 (Solnest-AI/listing-optimizer PR #8).
"""

from __future__ import annotations

import ast
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import update  # noqa: E402

V1 = {"VERSION": "1.1.0\n", "agent.py": "print('v1')\n", "scripts/old.py": "print('old')\n",
      "tests/test_old.py": "def test_old(): pass\n", "scripts/legacy/helper.py": "x = 1\n",
      ".env.example": "AIRROI_API_KEY=\n"}
V2 = {"VERSION": "1.2.0\n", "agent.py": "print('v2')\n", "scripts/new.py": "print('new')\n",
      ".env.example": "AIRROI_API_KEY=\nFIRECRAWL_API_KEY=\n"}
# Everything the student owns. None of it is in a release, and no release may touch it.
USER = {".env": "AIRROI_API_KEY=keep-me\n", ".env.local": "SECRET=1\n", "branding.json": '{"name": "Mine"}',
        "proforma.json": '{"mgmt_pct": 20}', "partners/jane-doe.json": '{"name": "Jane"}',
        "output/123-main-st.html": "<html>report</html>", "output/123-main-st.report-data.json": "{}",
        ".cache/vendor/abc.json": "{}", ".cache/brand_logo.png": "png", ".cache/setup_ok.json": "{}",
        ".venv/bin/python": "py", ".claude/settings.local.json": "{}"}


def _tarball(files: dict[str, str], top: str = "solnest-str-comping-agent-1.2.0", extra=None) -> bytes:
    """What github.com/<repo>/archive/refs/tags/<tag>.tar.gz serves: one top folder."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(f"{top}/{rel}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in extra or []:
            tar.addfile(info, io.BytesIO(b"x" * info.size) if info.isfile() else None)
    return buf.getvalue()


def _plain_copy(tmp_path: Path, files=V1, manifest: bool = True, user=USER) -> Path:
    root = tmp_path / "solnest-str-comping-agent"
    for rel, body in {**files, **user}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    if manifest:
        update.write_manifest(root, list(files))
    return root


def _fake_release(monkeypatch, releases=None, latest="v1.2.0", blob=None) -> list[str]:
    """GitHub, offline: `latest` is the newest tag, `releases` maps each tag to its files."""
    releases = releases or {"v1.2.0": V2}
    calls: list[str] = []
    monkeypatch.setattr(update, "latest_tag", lambda timeout=20: latest)

    def download(tag, timeout=60):
        calls.append(tag)
        if blob is not None:
            return blob
        if tag not in releases:
            raise update.UpdateError(f"could not download {tag} from GitHub (HTTPError)")
        return _tarball(releases[tag], top=f"solnest-str-comping-agent-{tag.lstrip('v')}")
    monkeypatch.setattr(update, "download", download)
    return calls


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)).replace(os.sep, "/"): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _without_backups(snap: dict[str, bytes]) -> dict[str, bytes]:
    return {k: v for k, v in snap.items() if not k.startswith(update.BACKUP_DIR + "/")}


# ── Updating ───────────────────────────────────────────────────────────
def test_an_update_replaces_the_tool_and_keeps_every_student_file(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    _fake_release(monkeypatch)
    report = update.update(root)
    assert report["status"] == "updated" and report["from"] == "1.1.0" and report["to"] == "1.2.0"
    assert (root / "agent.py").read_text(encoding="utf-8") == "print('v2')\n"
    assert (root / "scripts/new.py").exists()
    assert "FIRECRAWL_API_KEY" in (root / ".env.example").read_text(encoding="utf-8"), "the shipped example is the tool's"
    for gone in ("scripts/old.py", "tests/test_old.py", "scripts/legacy/helper.py"):
        assert not (root / gone).exists(), f"{gone}: a file the release dropped would linger and can break setup"
    assert not (root / "scripts/legacy").exists(), "a folder the release emptied goes too"
    for rel, body in USER.items():
        assert (root / rel).read_text(encoding="utf-8") == body, f"{rel} must survive an update"
    assert (root / "VERSION").read_text(encoding="utf-8").strip() == "1.2.0"
    assert set(update.load_manifest(root)) == set(V2)
    assert sorted(report["removed"]) == ["scripts/legacy/helper.py", "scripts/old.py", "tests/test_old.py"]
    assert report["added"] == ["scripts/new.py"]


def test_a_release_can_never_write_over_the_students_files(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    hostile = {**V2, **{rel: "from the release\n" for rel in USER}}
    _fake_release(monkeypatch, releases={"v1.2.0": hostile})
    update.update(root)
    for rel, body in USER.items():
        assert (root / rel).read_text(encoding="utf-8") == body, f"{rel} was overwritten by a release"
    assert not set(USER) & set(update.load_manifest(root)), "student files never enter the manifest"


def test_an_edited_file_is_backed_up_and_named(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    (root / "agent.py").write_text("print('my tweak')\n", encoding="utf-8")
    _fake_release(monkeypatch)
    report = update.update(root)
    assert report["edited"] == ["agent.py"]
    assert (Path(report["backup"]) / "agent.py").read_text(encoding="utf-8") == "print('my tweak')\n"


def test_undo_puts_back_exactly_the_previous_version(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch)
    update.update(root)
    report = update.undo(root)
    assert report["to"] == "1.1.0" and report["from"] == "1.2.0"
    assert _snapshot(root) == before, "one step back is exactly where the student was"
    assert not (root / update.BACKUP_DIR).exists()


def test_undo_with_nothing_to_undo_says_so(tmp_path):
    root = _plain_copy(tmp_path)
    with pytest.raises(update.UpdateError, match="no earlier version"):
        update.undo(root)


def test_already_current_downloads_nothing(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path, files={**V1, "VERSION": "1.2.0\n"})
    calls = _fake_release(monkeypatch)
    assert update.update(root)["status"] == "current" and calls == []


def test_a_folder_still_linked_to_git_is_not_overwritten(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    (root / ".git").mkdir()
    calls = _fake_release(monkeypatch)
    with pytest.raises(update.UpdateError, match="git pull"):
        update.update(root)
    assert calls == [] and (root / "agent.py").read_text(encoding="utf-8") == "print('v1')\n"


@pytest.mark.parametrize("evil", ["../escape.py", "/etc/passwd", "link"])
def test_a_tarball_that_reaches_outside_the_folder_is_refused(tmp_path, monkeypatch, evil):
    info = tarfile.TarInfo(evil if evil.startswith("/") else f"solnest-str-comping-agent-1.2.0/{evil}")
    if evil == "link":
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
    else:
        info.size = 1
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch, blob=_tarball(V2, extra=[info]))
    with pytest.raises(update.UpdateError):
        update.update(root)
    assert _snapshot(root) == before and not (tmp_path / "escape.py").exists()


def test_a_release_without_a_version_file_is_refused(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch, releases={"v1.2.0": {k: v for k, v in V2.items() if k != "VERSION"}})
    with pytest.raises(update.UpdateError, match="VERSION"):
        update.update(root)
    assert _snapshot(root) == before


def test_a_release_labelled_with_the_wrong_version_is_refused(tmp_path, monkeypatch):
    """Tagged v1.2.0 but VERSION still says 1.1.0 (a forgotten bump): installing it would
    leave the copy 'behind' forever, re-downloading on every update and nagging every run."""
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch, releases={"v1.2.0": {**V2, "VERSION": "1.1.0\n"}})
    with pytest.raises(update.UpdateError, match="1.1.0"):
        update.update(root)
    assert _snapshot(root) == before


def test_a_failure_halfway_rolls_back(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    before = _snapshot(root)
    _fake_release(monkeypatch)
    real = update._write

    def flaky(path, data):
        if path.name == "new.py":
            raise OSError("disk full")
        real(path, data)
    monkeypatch.setattr(update, "_write", flaky)
    with pytest.raises(update.UpdateError, match="put back"):
        update.update(root)
    assert _without_backups(_snapshot(root)) == before


def test_no_network_is_a_clear_message_and_changes_nothing(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    before = _snapshot(root)

    def offline(timeout=20):
        raise update.UpdateError("could not reach GitHub (URLError)")
    monkeypatch.setattr(update, "latest_tag", offline)
    with pytest.raises(update.UpdateError, match="could not reach GitHub"):
        update.update(root)
    assert _snapshot(root) == before


def test_a_copy_without_a_manifest_learns_its_files_from_its_own_release(tmp_path, monkeypatch):
    """A tarball or zip install never had .git, so detach never wrote a manifest. Its VERSION
    names the release it came from, and that release lists exactly what it shipped."""
    root = _plain_copy(tmp_path, manifest=False)
    (root / "my-notes.md").write_text("mine\n", encoding="utf-8")
    calls = _fake_release(monkeypatch, releases={"v1.2.0": V2, "v1.1.0": V1})
    (root / "agent.py").write_text("print('my tweak')\n", encoding="utf-8")
    report = update.update(root)
    assert "v1.1.0" in calls
    assert not (root / "scripts/old.py").exists() and not (root / "tests/test_old.py").exists()
    assert (root / "my-notes.md").read_text(encoding="utf-8") == "mine\n", "a file no release shipped is the student's"
    assert report["edited"] == ["agent.py"]


def test_a_copy_from_before_versions_removes_nothing_it_cannot_prove_is_ours(tmp_path, monkeypatch):
    """Copies from before VERSION existed (the 2026-09-29 summit) have no manifest and no
    version: the update replaces the tool's files and leaves everything else alone."""
    v0 = {k: v for k, v in V1.items() if k != "VERSION"}
    root = _plain_copy(tmp_path, files=v0, manifest=False)
    (root / "my-notes.md").write_text("mine\n", encoding="utf-8")
    calls = _fake_release(monkeypatch)
    report = update.update(root)
    assert calls == ["v1.2.0"] and report["from"] == "unknown" and report["removed"] == []
    assert (root / "my-notes.md").exists() and (root / "scripts/old.py").exists()
    assert (root / "agent.py").read_text(encoding="utf-8") == "print('v2')\n"
    update.undo(root)
    assert not (root / "VERSION").exists() and (root / "agent.py").read_text(encoding="utf-8") == "print('v1')\n"


def test_only_the_last_three_backups_are_kept(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path)
    for minor in (2, 3, 4, 5):
        tag = f"v1.{minor}.0"
        _fake_release(monkeypatch, latest=tag, releases={tag: {**V2, "VERSION": f"1.{minor}.0\n"}})
        update.update(root)
    assert len([p for p in (root / update.BACKUP_DIR).iterdir() if p.is_dir()]) == update.KEEP_BACKUPS
    assert update.undo(root)["to"] == "1.4.0"


def test_the_latest_release_comes_from_the_github_redirect_not_the_api(monkeypatch):
    """The API allows 60 requests an hour per network, and a classroom shares one."""
    seen = []

    class Answer:
        def __init__(self, url):
            self.url = url

        def geturl(self):
            return self.url

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return Answer("https://github.com/Solnest-AI/solnest-str-comping-agent/releases/tag/v1.4.2")
    monkeypatch.setattr(update.urllib.request, "urlopen", fake_urlopen)
    assert update.latest_tag() == "v1.4.2"
    assert seen == ["https://github.com/Solnest-AI/solnest-str-comping-agent/releases/latest"]
    monkeypatch.setattr(update.urllib.request, "urlopen",
                        lambda req, timeout=None: Answer("https://github.com/Solnest-AI/solnest-str-comping-agent/releases"))
    with pytest.raises(update.UpdateError, match="no published release"):
        update.latest_tag()


# ── The manifest ───────────────────────────────────────────────────────
def test_the_manifest_round_trips_paths_with_spaces(tmp_path):
    root = tmp_path / "copy"
    (root / "docs").mkdir(parents=True)
    (root / "docs/read me.md").write_text("hi\n", encoding="utf-8")
    (root / "agent.py").write_text("x\n", encoding="utf-8")
    update.write_manifest(root, ["docs/read me.md", "agent.py", ".env"])
    assert update.load_manifest(root) == {"docs/read me.md": update.file_hash(b"hi\n"),
                                          "agent.py": update.file_hash(b"x\n")}


@pytest.mark.skipif(shutil.which("git") is None, reason="git needed")
def test_the_manifest_hash_is_gits_own(tmp_path):
    """detach_from_github.sh runs before Python is installed, so it hashes with git
    (`git hash-object --no-filters`). The updater must compute the same value."""
    f = tmp_path / "a.py"
    f.write_bytes(b"print('hi')\r\nemoji \xf0\x9f\x8f\xa0\n")
    git = subprocess.run(["git", "hash-object", "--no-filters", str(f)], capture_output=True, text=True,
                         check=True).stdout.strip()
    assert update.file_hash(f.read_bytes()) == git


def test_a_damaged_manifest_counts_as_none(tmp_path):
    root = tmp_path / "copy"
    root.mkdir()
    (root / update.MANIFEST).write_text("not a manifest\n\x00\n", encoding="utf-8")
    assert update.load_manifest(root) == {}


# ── The once-a-day notice at the end of a run ──────────────────────────
def test_the_notice_names_the_new_version_once_a_day(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPING_NO_UPDATE_CHECK", raising=False)
    root = _plain_copy(tmp_path)
    asked = []
    monkeypatch.setattr(update, "latest_tag", lambda timeout=3: asked.append(timeout) or "v1.2.0")
    msg = update.notice(root, today="2026-10-10")
    assert msg.startswith("Update available") and "1.2.0" in msg and "1.1.0" in msg
    assert '"update the comping agent"' in msg and "—" not in msg
    assert update.notice(root, today="2026-10-10") == msg
    assert asked == [3], "one quick check per day, not one per run"
    update.notice(root, today="2026-10-11")
    assert len(asked) == 2


def test_the_notice_is_silent_when_current_offline_or_switched_off(tmp_path, monkeypatch):
    root = _plain_copy(tmp_path, files={**V1, "VERSION": "1.2.0\n"})
    monkeypatch.delenv("COMPING_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "latest_tag", lambda timeout=3: "v1.2.0")
    assert update.notice(root, today="2026-10-10") is None

    def offline(timeout=3):
        raise update.UpdateError("offline")
    monkeypatch.setattr(update, "latest_tag", offline)
    assert update.notice(root, today="2026-10-11") is None
    monkeypatch.setenv("COMPING_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(update, "latest_tag", lambda timeout=3: "v9.0.0")
    assert update.notice(root, today="2026-10-12") is None


def test_the_notice_never_breaks_a_run(tmp_path, monkeypatch):
    monkeypatch.delenv("COMPING_NO_UPDATE_CHECK", raising=False)
    root = _plain_copy(tmp_path)
    (root / update.NOTICE_CACHE).parent.mkdir(parents=True, exist_ok=True)
    (root / update.NOTICE_CACHE).write_text("[not, json", encoding="utf-8")

    def boom(timeout=3):
        raise RuntimeError("anything at all")
    monkeypatch.setattr(update, "latest_tag", boom)
    assert update.notice(root, today="2026-10-10") is None


def test_a_finished_run_ends_with_the_notice(monkeypatch, capsys):
    import agent

    async def finished():
        print("Report Complete!")
    monkeypatch.setattr(agent, "main", finished)
    monkeypatch.setattr(agent, "_update_notice_line",
                        lambda root=None: "Update available: Comping Agent 9.9.9 (test)")
    agent.run()
    assert capsys.readouterr().out.strip().splitlines()[-1] == "Update available: Comping Agent 9.9.9 (test)"


def test_a_failed_run_prints_no_notice(monkeypatch, capsys):
    import agent

    async def failed():
        sys.exit(2)
    monkeypatch.setattr(agent, "main", failed)
    monkeypatch.setattr(agent, "_update_notice_line", lambda root=None: "Update available: should not show")
    with pytest.raises(SystemExit):
        agent.run()
    assert "Update available" not in capsys.readouterr().out


def test_the_agent_reads_the_notice_from_the_updater(tmp_path, monkeypatch):
    """agent.py loads scripts/update.py by path (scripts/ is not a package), and a missing
    or broken updater never stops a report."""
    import agent
    from datetime import date
    root = tmp_path / "copy"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "update.py", root / "scripts" / "update.py")
    (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    (root / ".cache").mkdir()
    (root / ".cache" / "update_check.json").write_text(
        json.dumps({"checked": date.today().isoformat(), "latest": "v9.9.9"}), encoding="utf-8")
    monkeypatch.delenv("COMPING_NO_UPDATE_CHECK", raising=False)
    assert "9.9.9" in agent._update_notice_line(root)
    monkeypatch.setenv("COMPING_NO_UPDATE_CHECK", "1")
    assert agent._update_notice_line(root) is None
    monkeypatch.delenv("COMPING_NO_UPDATE_CHECK")
    (root / "scripts" / "update.py").write_text("raise SystemExit('broken')\n", encoding="utf-8")
    assert agent._update_notice_line(root) is None
    (root / "scripts" / "update.py").unlink()
    assert agent._update_notice_line(root) is None


# ── Tests never reach the network ──────────────────────────────────────
def test_tests_run_with_the_update_check_off():
    assert os.environ.get("COMPING_NO_UPDATE_CHECK") == "1"


def test_tests_cannot_reach_the_internet():
    """Blocked at the socket, not at urlopen: patching urlopen misses build_opener() and httpx."""
    with pytest.raises(OSError, match="network"):
        socket.create_connection(("github.com", 443), timeout=2)
    with pytest.raises(OSError, match="network"):
        socket.getaddrinfo("github.com", 443)
    with pytest.raises(update.UpdateError, match="could not reach GitHub"):
        update.latest_tag(timeout=2)


def test_tests_can_still_use_this_computer():
    """asyncio and local test servers connect to 127.0.0.1; that is not the internet."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        with socket.create_connection(server.getsockname(), timeout=2):
            pass
    finally:
        server.close()


# ── Shipped where students and Claude find it ──────────────────────────
def test_the_repo_ships_a_version_and_ignores_local_update_files():
    assert update.parse_version((ROOT / "VERSION").read_text(encoding="utf-8")) is not None
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert update.MANIFEST in ignored and update.BACKUP_DIR + "/" in ignored


def test_the_updater_needs_nothing_but_python():
    """A broken .venv, or a release that needs a new package, must not stop the update
    that fixes it."""
    tree = ast.parse((ROOT / "scripts" / "update.py").read_text(encoding="utf-8"))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module and n.level == 0}
    assert mods <= set(sys.stdlib_module_names) | {"__future__"}, mods - set(sys.stdlib_module_names)


def _section(text: str, heading: str) -> str:
    return text.split(heading, 1)[1].split("\n## ", 1)[0]


def test_claude_updates_by_release_not_by_copying_main():
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    section = _section(claude, '## When the user says "update the comping agent"')
    assert "scripts/update.py" in section and "--undo" in section and "--check" in section
    assert "refs/heads/main" not in section and "cp -R" not in section
    assert "Update available" in section, "Claude must know what the end-of-run line means"
    assert "—" not in section


def test_the_skill_relays_the_notice_and_never_updates_on_its_own():
    """The skill runs reports from any window, where this folder's CLAUDE.md is not loaded,
    so it must say what the end-of-run line means by itself."""
    skill = (ROOT / ".claude/skills/str-comping-agent/SKILL.md").read_text(encoding="utf-8")
    section = _section(skill, "## Reading the output")
    assert "Update available" in section and '"update the comping agent"' in section
    assert "scripts/update.py" in section and "yes" in section


def test_the_readme_explains_updating_in_plain_english():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = _section(readme, "## Updating")
    for must in ('"update the comping agent"', "--undo", "COMPING_NO_UPDATE_CHECK", "/releases",
                 "branding.json", "partners/", "proforma.json"):
        assert must in section, must
    assert "—" not in section
