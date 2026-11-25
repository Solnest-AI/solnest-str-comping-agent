#!/usr/bin/env python3
"""Solnest auto-update: keeps every Solnest tool on its latest release, silently.

Each tool already has its own updater (it downloads the latest GitHub release and
never touches the user's keys, branding, settings or output). This file only runs
those updaters by itself, so nobody has to say "update the ..." again.

    python solnest_autoupdate.py register --name comping-agent --folder DIR \
        --update CMD --after CMD [--undo CMD]     add a tool (its setup runs this)
    python solnest_autoupdate.py run              what the Claude Code start hook calls
    python solnest_autoupdate.py run --now        update everything now, in the foreground
    python solnest_autoupdate.py status           what is installed, last check, last result
    python solnest_autoupdate.py remove --name X  stop updating one tool
    python solnest_autoupdate.py pause | resume   kill switch for all of it

Everything lives in ~/.solnest/ (outside every tool folder, so no update can wipe it):
    autoupdate.py   the newest copy of this file any tool has shipped
    tools.json      the registered tools and the commands that update them
    state.json      last check and last result per tool
    autoupdate.log  one line per update, failure and undo
    <tool>/my-instructions.md   the user's own changes to how a tool behaves

How one update runs (at most once a day per tool, in the background, never blocking
Claude Code's start): the tool's update command, then if VERSION changed its after
command (new packages, launcher refresh). If the after command fails, the undo command
puts the old version back and the after command runs again, so a bad release never
leaves a broken tool. Standard library only, Python 3.9+.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

RUNNER_VERSION = 1
HOME = Path(os.environ.get("SOLNEST_HOME") or (Path.home() / ".solnest"))
SETTINGS = Path(os.environ.get("SOLNEST_CLAUDE_SETTINGS") or (Path.home() / ".claude" / "settings.json"))
TOOLS = HOME / "tools.json"
STATE = HOME / "state.json"
LOG = HOME / "autoupdate.log"
LOCK = HOME / ".autoupdate.lock"
PAUSED = HOME / "paused"
INSTALLED = HOME / "autoupdate.py"
HOOK_TAG = "solnest-autoupdate"
EVERY_SECONDS = 20 * 3600          # once a day, with slack so a daily start always qualifies
UPDATE_TIMEOUT = 300
AFTER_TIMEOUT = 900
WIN = os.name == "nt"


# ---------- small helpers ----------

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def log(msg: str) -> None:
    with contextlib.suppress(OSError):
        HOME.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
        lines = LOG.read_text(encoding="utf-8").splitlines()
        if len(lines) > 2000:
            LOG.write_text("\n".join(lines[-1000:]) + "\n", encoding="utf-8")


def _version(folder: Path) -> str:
    with contextlib.suppress(OSError):
        return (folder / "VERSION").read_text(encoding="utf-8").strip()
    return ""


def _bash() -> str | None:
    if not WIN:
        return shutil.which("bash") or "/bin/bash"
    for c in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if c and Path(c).exists():
            return c
    return None


def _sh(cmd: str, folder: Path, timeout: int) -> tuple[int, str]:
    """Run one tool command in its folder with bash. {python} is this interpreter."""
    bash = _bash()
    if not bash:
        return 127, "no bash found"
    cmd = cmd.replace("{python}", '"' + sys.executable.replace("\\", "/") + '"')
    env = dict(os.environ, SOLNEST_AUTOUPDATE="1", NO_COLOR="1")
    try:
        p = subprocess.run([bash, "-c", cmd], cwd=str(folder), capture_output=True, text=True,
                           timeout=timeout, env=env, stdin=subprocess.DEVNULL,
                           encoding="utf-8", errors="replace")
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-1500:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _one_line(text: str) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return (lines[-1] if lines else "")[:300]


# ---------- the Claude Code start hook ----------

def _hook_command() -> str:
    py = sys.executable.replace("\\", "/")
    script = str(INSTALLED).replace("\\", "/")
    return f'"{py}" "{script}" run >/dev/null 2>&1 || true  # {HOOK_TAG}'


def ensure_hook() -> str:
    """Merge one SessionStart hook into ~/.claude/settings.json. Never drops a key."""
    try:
        data = json.loads(SETTINGS.read_text(encoding="utf-8")) if SETTINGS.exists() else {}
    except Exception:
        return "settings.json is not valid JSON, left it alone"
    if not isinstance(data, dict):
        return "settings.json is not an object, left it alone"
    hooks = data.setdefault("hooks", {})
    starts = hooks.setdefault("SessionStart", [])
    want = _hook_command()
    for group in starts:
        for h in group.get("hooks", []) if isinstance(group, dict) else []:
            if HOOK_TAG in str(h.get("command", "")):
                if h.get("command") == want:
                    return "start hook already in place"
                h["command"] = want
                break
        else:
            continue
        break
    else:
        starts.append({"hooks": [{"type": "command", "command": want, "timeout": 10}]})
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():
        shutil.copy2(SETTINGS, SETTINGS.with_name("settings.json.bak-solnest-autoupdate"))
    _write_json(SETTINGS, data)
    json.loads(SETTINGS.read_text(encoding="utf-8"))   # prove it still parses
    return "start hook added"


def remove_hook() -> None:
    data = _read_json(SETTINGS, None)
    if not isinstance(data, dict):
        return
    starts = data.get("hooks", {}).get("SessionStart", [])
    keep = []
    for group in starts:
        hs = [h for h in group.get("hooks", []) if HOOK_TAG not in str(h.get("command", ""))]
        if hs:
            group["hooks"] = hs
            keep.append(group)
    if starts != keep:
        data["hooks"]["SessionStart"] = keep
        if not keep:
            data["hooks"].pop("SessionStart")
        _write_json(SETTINGS, data)


# ---------- commands ----------

def _runner_version(path: Path) -> int:
    with contextlib.suppress(Exception):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("RUNNER_VERSION"):
                return int(line.split("=")[1].strip())
    return 0


def install_self(source: Path | None = None) -> None:
    """Copy a runner to ~/.solnest/autoupdate.py unless a newer one is already there."""
    src = Path(source or __file__).resolve()
    HOME.mkdir(parents=True, exist_ok=True)
    if src.exists() and src != INSTALLED.resolve() and _runner_version(src) >= _runner_version(INSTALLED):
        shutil.copy2(src, INSTALLED)


def cmd_register(a) -> int:
    folder = Path(a.folder).expanduser().resolve()
    if not folder.is_dir():
        print(f"[autoupdate] no such folder: {folder}")
        return 1
    install_self()
    tools = _read_json(TOOLS, {})
    tools[a.name] = {"folder": str(folder), "update": a.update, "after": a.after or "",
                     "undo": a.undo or "", "registered": datetime.now().isoformat(timespec="seconds")}
    _write_json(TOOLS, tools)
    (HOME / a.name).mkdir(parents=True, exist_ok=True)
    hook = ensure_hook()
    log(f"{a.name}: registered {folder} (v{_version(folder) or '?'})")
    print(f"[autoupdate] {a.name} will keep itself up to date ({hook}).")
    return 0


def cmd_remove(a) -> int:
    tools = _read_json(TOOLS, {})
    tools.pop(a.name, None)
    _write_json(TOOLS, tools)
    if not tools:
        remove_hook()
    print(f"[autoupdate] {a.name} removed." + ("" if tools else " No tools left, start hook removed."))
    return 0


def update_one(name: str, t: dict) -> str:
    folder = Path(t["folder"])
    if not folder.is_dir():
        return "folder gone, skipped"
    before = _version(folder)
    code, out = _sh(t["update"], folder, UPDATE_TIMEOUT)
    if code != 0:
        log(f"{name}: update failed ({code}): {_one_line(out)}")
        return f"update failed: {_one_line(out)}"
    after_v = _version(folder)
    if after_v == before:
        return f"up to date (v{before or '?'})"
    if t.get("after"):
        code, out = _sh(t["after"], folder, AFTER_TIMEOUT)
        if code != 0:
            log(f"{name}: v{before} -> v{after_v} setup failed ({code}): {_one_line(out)}")
            if t.get("undo"):
                ucode, uout = _sh(t["undo"], folder, UPDATE_TIMEOUT)
                rcode, _ = _sh(t["after"], folder, AFTER_TIMEOUT)
                log(f"{name}: undo -> v{_version(folder)} (undo {ucode}, setup {rcode}) {_one_line(uout)}")
                return f"v{after_v} failed setup, put back v{_version(folder)}"
            return f"v{after_v} installed but setup failed: {_one_line(out)}"
    for shipped in (folder / "scripts" / "solnest_autoupdate.py", folder / "solnest_autoupdate.py"):
        if shipped.exists():
            install_self(shipped)   # a newer runner rides along with any tool's release
            break
    log(f"{name}: updated v{before} -> v{after_v}")
    return f"updated v{before} -> v{after_v}"


def _locked() -> bool:
    try:
        if LOCK.exists() and time.time() - LOCK.stat().st_mtime < 3600:
            return True
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
        return False
    except OSError:
        return True


def run_all(force: bool) -> None:
    if PAUSED.exists() or os.environ.get("SOLNEST_NO_AUTOUPDATE"):
        return
    if _locked():
        return
    try:
        tools = _read_json(TOOLS, {})
        state = _read_json(STATE, {})
        now = time.time()
        for name, t in tools.items():
            s = state.get(name, {})
            if not force and now - s.get("checked", 0) < EVERY_SECONDS:
                continue
            try:
                result = update_one(name, t)
            except Exception as e:  # noqa: BLE001  one tool never stops the others
                result = f"error: {type(e).__name__}: {e}"
                log(f"{name}: {result}")
            state[name] = {"checked": time.time(), "result": result,
                           "when": datetime.now().isoformat(timespec="seconds")}
            _write_json(STATE, state)
    finally:
        with contextlib.suppress(OSError):
            LOCK.unlink()


def cmd_run(a) -> int:
    if a.now or a.child:
        run_all(force=a.now)
        if a.now:
            cmd_status(a)
        return 0
    # Called by the start hook: hand off to a detached child and return at once.
    if PAUSED.exists() or os.environ.get("SOLNEST_NO_AUTOUPDATE"):
        return 0
    args = [sys.executable, str(Path(__file__).resolve()), "run", "--child"]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if WIN:
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # detached, new group, no window
    else:
        kw["start_new_session"] = True
    with contextlib.suppress(Exception):
        subprocess.Popen(args, **kw)
    return 0


def cmd_status(a) -> int:
    tools = _read_json(TOOLS, {})
    state = _read_json(STATE, {})
    if PAUSED.exists():
        print("[autoupdate] PAUSED (resume with: autoupdate.py resume)")
    if not tools:
        print("[autoupdate] no tools registered")
    for name, t in tools.items():
        s = state.get(name, {})
        print(f"{name}: v{_version(Path(t['folder'])) or '?'} in {t['folder']}\n"
              f"    last check {s.get('when', 'never')}: {s.get('result', '-')}")
    return 0


def cmd_pause(a) -> int:
    HOME.mkdir(parents=True, exist_ok=True)
    PAUSED.write_text("paused\n", encoding="utf-8")
    print("[autoupdate] paused. Nothing updates until: autoupdate.py resume")
    return 0


def cmd_resume(a) -> int:
    with contextlib.suppress(OSError):
        PAUSED.unlink()
    print("[autoupdate] resumed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Keep Solnest tools on their latest release.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("--name", required=True)
    r.add_argument("--folder", required=True)
    r.add_argument("--update", required=True)
    r.add_argument("--after", default="")
    r.add_argument("--undo", default="")
    r.set_defaults(fn=cmd_register)
    rm = sub.add_parser("remove")
    rm.add_argument("--name", required=True)
    rm.set_defaults(fn=cmd_remove)
    ru = sub.add_parser("run")
    ru.add_argument("--now", action="store_true")
    ru.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ru.set_defaults(fn=cmd_run)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("pause").set_defaults(fn=cmd_pause)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
