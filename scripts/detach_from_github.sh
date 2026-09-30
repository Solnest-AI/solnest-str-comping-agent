#!/usr/bin/env bash
# Makes a student's copy a plain folder with no link back to GitHub.
#
#   bash scripts/detach_from_github.sh
#
# The first step of "set this up". Students paste the GitHub link into the
# Claude Code desktop app, and Claude usually `git clone`s it before reading a
# word. A clone stays attached to our repo: at the 2026-09-29 summit, students
# said "save" and their Claude tried to commit and push to Solnest-AI's GitHub.
# The desktop app also treats a clone as a GitHub project (branch/PR bar,
# per-session worktree copies). The tool never needs git to run, so the
# student's copy should not have it.
#
# It removes .git only from an UNTOUCHED copy of the public repo. It keeps it,
# and says why, when the copy is someone's working copy: another origin, local
# edits to tracked files, commits, branches or stashes not on GitHub. A
# developer keeps git on a clean checkout with `touch .git/str-keep-attached`
# (permanent) or STR_KEEP_GIT=1 (one run).
#
# Always exits 0: a copy it keeps attached must not stop setup.
set -uo pipefail

cd "$(dirname "$0")/.."
say() { printf '[setup] %s\n' "$*"; }
REPO="solnest-ai/solnest-str-comping-agent"   # compared lower-case: GitHub ignores case

if [ ! -e .git ]; then
    say "GitHub: not attached (plain folder)."
    exit 0
fi
if [ -n "${STR_KEEP_GIT:-}" ] || [ -e .git/str-keep-attached ]; then
    say "GitHub: still attached (a developer's copy: STR_KEEP_GIT or .git/str-keep-attached)."
    exit 0
fi

# A Mac without developer tools has a /usr/bin/git stub that opens an install
# dialog instead of answering (2026-09-28). Only trust git when it answers.
git_works() { git --version >/dev/null 2>&1 && git rev-parse --git-dir >/dev/null 2>&1; }

if ! git_works; then
    # Nothing can use this .git without git, and it cannot be checked for
    # someone's work, so it is left alone rather than deleted blind.
    say "GitHub: left as is. Git does not answer on this computer, so nothing can use the link."
    exit 0
fi
origin="$(git remote get-url origin 2>/dev/null || true)"
case "$(printf '%s' "$origin" | tr '[:upper:]' '[:lower:]')" in
    *"$REPO"|*"$REPO".git|*"$REPO"/) ;;
    *)  say "GitHub: kept attached. This folder's git is not a copy of $REPO (origin: ${origin:-none})."
        exit 0 ;;
esac
if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    say "GitHub: kept attached. The code here has local edits (a working copy, not a fresh download)."
    exit 0
fi
if [ "$(git branch --format='%(refname:short)' 2>/dev/null | grep -vxc main)" != "0" ] \
   || [ -n "$(git log --branches --not --remotes --oneline 2>/dev/null | head -1)" ] \
   || [ -n "$(git stash list 2>/dev/null | head -1)" ]; then
    say "GitHub: kept attached. This copy has branches, commits or stashed work that are not on GitHub."
    exit 0
fi
git remote remove origin >/dev/null 2>&1 || true

# Git makes its object files read-only; on Windows plain rm refuses them.
chmod -R u+w .git 2>/dev/null || true
rm -rf .git 2>/dev/null
if [ -e .git ]; then
    say "GitHub: could not remove the .git folder (another program has it open). The GitHub link is removed;"
    say "        close other windows on this folder and run: bash scripts/detach_from_github.sh"
    exit 0
fi
say "GitHub: detached. This is now a plain folder, not linked to GitHub. To update later, paste the"
say "        GitHub link into Claude Code and say \"update the comping agent\"."
exit 0
