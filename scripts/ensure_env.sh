#!/usr/bin/env bash
# Makes this folder runnable on a fresh Mac or Windows machine, with no typing.
#
#   PY="$(bash scripts/ensure_env.sh)" && "$PY" agent.py --input "..."
#
# Prints ONE line on stdout: the Python to run everything with. Progress goes to
# stderr. Run it before every command: when nothing changed it only checks, ~1s.
#
# Why not plain `python`: on a fresh Windows machine `python` is the Microsoft
# Store stub, which opens the Store instead of running (2026-09-28). The STR
# Secrets connections kit installs uv for everyone, and uv downloads its own
# Python, so this builds a private .venv with it and never touches the stub.
# Without uv it installs uv (per-user, no admin), and only if that is blocked
# does it fall back to a real Python 3.10+ already on the machine.
#
#   --dev   also install requirements-dev.txt (pytest, ruff)
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"
log() { printf '[setup] %s\n' "$*" >&2; }
die() { printf '[setup] FAILED: %s\n' "$*" >&2; exit 1; }

REQS=(requirements.txt)
[ "${1:-}" = "--dev" ] && REQS+=(requirements-dev.txt)

case "$(uname -s)" in MINGW* | MSYS* | CYGWIN*) WIN=1 ;; *) WIN=0 ;; esac
if [ "$WIN" = 1 ]; then VPY="$ROOT/.venv/Scripts/python.exe"; else VPY="$ROOT/.venv/bin/python"; fi
STAMP="$ROOT/.venv/.deps-stamp"

# Native Windows programs (uv, python) want C:/... paths, not /c/...
native() { if [ "$WIN" = 1 ]; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

# The Claude Code desktop app on Windows is a Microsoft Store (MSIX) app: what it writes
# under AppData is redirected into its sandbox, and uv's default Python home
# (AppData\Roaming\uv\python) then fails with "Failed to create Python minor version link
# directory" (os error 1921). Reproduced inside the app on 2026-09-28. Keep Python under the
# profile, exactly where the STR Secrets connections kit puts it (install-tools.sh), so both
# share one copy. A value the user already set wins.
if [ "$WIN" = 1 ]; then
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$(cygpath -w "${USERPROFILE:-$HOME}")\\.uv\\python}"
fi

venv_ok() { [ -f "$VPY" ] && "$VPY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; }
# find_spec locates each package without importing it: ~0.1s instead of ~3s.
deps_ok() { "$VPY" -c 'import importlib.util as u, sys; sys.exit(any(u.find_spec(m) is None for m in ("httpx", "jinja2", "dotenv", "pydantic", "bs4", "anthropic")))' >/dev/null 2>&1; }
# One "<checksum> <file>" line per requirements file, so a --dev install also
# satisfies a plain run instead of reinstalling.
want_stamp() { local r; for r in "${REQS[@]}"; do printf '%s %s
' "$(cksum < "$r" | cut -d' ' -f1)" "$r"; done; }
stamp_ok() {
    local line
    [ -f "$STAMP" ] || return 1
    while IFS= read -r line; do grep -qxF -- "$line" "$STAMP" || return 1; done < <(want_stamp)
}

done_ok() { native "$VPY"; echo; exit 0; }

# ── Fast path: nothing to do ──
if venv_ok && stamp_ok && deps_ok; then
    done_ok
fi

find_uv() {
    local c lad=""
    if c="$(command -v uv 2>/dev/null)"; then echo "$c"; return 0; fi
    [ "$WIN" = 1 ] && [ -n "${LOCALAPPDATA:-}" ] && lad="$(cygpath -u "$LOCALAPPDATA")"
    # The desktop app may have been open since before uv was installed, so its
    # PATH does not know yet: look where the installers put it.
    for c in "${UV_UNMANAGED_INSTALL:-/nonexistent}/uv" "${UV_INSTALL_DIR:-/nonexistent}/uv" \
             "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" "$lad/Microsoft/WinGet/Links/uv" \
             /opt/homebrew/bin/uv /usr/local/bin/uv; do
        [ -f "$c" ] && { echo "$c"; return 0; }
        [ -f "$c.exe" ] && { echo "$c.exe"; return 0; }
    done
    return 1
}

# A real Python 3.10+, never the Store stub (the stub fails this check).
find_python() {
    local c
    for c in "py -3" python3 python; do
        # shellcheck disable=SC2086
        if $c -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
            echo "$c"; return 0
        fi
    done
    return 1
}

UV=""
if ! UV="$(find_uv)"; then
    log "uv not found; installing it for this user (no admin needed)..."
    if [ "$WIN" = 1 ]; then
        powershell.exe -NoProfile -ExecutionPolicy ByPass -Command \
            "irm https://astral.sh/uv/install.ps1 | iex" >&2 || log "uv installer failed"
    else
        curl -LsSf https://astral.sh/uv/install.sh | sh >&2 || log "uv installer failed"
    fi
    UV="$(find_uv)" || UV=""
fi

# ── The private environment ──
if ! venv_ok; then
    rm -rf "$ROOT/.venv"
    if [ -n "$UV" ]; then
        log "creating .venv with Python 3.13 (uv downloads it if needed)..."
        "$UV" venv --quiet --python 3.13 "$(native "$ROOT/.venv")" >&2 \
            || die "uv could not create the environment. Check the internet connection and run this again."
    elif SYS_PY="$(find_python)"; then
        log "uv unavailable; creating .venv with $SYS_PY..."
        # shellcheck disable=SC2086
        $SYS_PY -m venv "$(native "$ROOT/.venv")" >&2 || die "could not create .venv with $SYS_PY"
    else
        die "no uv and no Python 3.10+ on this machine. Run the connections kit's 'Python via uv' row, then run this again."
    fi
    venv_ok || die ".venv was created but its Python does not run"
fi

# ── Dependencies ──
log "installing dependencies (${REQS[*]})..."
REQ_ARGS=()
for r in "${REQS[@]}"; do REQ_ARGS+=(-r "$r"); done
if [ -n "$UV" ]; then
    "$UV" pip install --quiet --python "$(native "$VPY")" "${REQ_ARGS[@]}" >&2 \
        || die "dependency install failed. Check the internet connection and run this again."
else
    "$VPY" -m pip install --quiet --disable-pip-version-check "${REQ_ARGS[@]}" >&2 \
        || die "dependency install failed. Check the internet connection and run this again."
fi
deps_ok || die "dependencies installed but do not import"
want_stamp > "$STAMP"
log "ready: $("$VPY" --version 2>&1)"
done_ok
