"""Every text-mode file read/write names its encoding.

Without `encoding=`, Python uses the OS locale encoding: UTF-8 on macOS and
Linux, but cp1252 on most Windows installs until Python 3.15. Airbnb listing
names are full of emoji, which cp1252 cannot encode, so an unguarded
`Path.write_text()` of report data crashes the run on Windows, and an
unguarded `read_text()` of a UTF-8 fixture raises UnicodeDecodeError there.
Found 2026-09-26 before the STR Secrets summit, where attendees run this on
their own machines. Both CI and this test run on macOS/Linux too, so the
check is static: it cannot rely on the host's default encoding to fail.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent
SKIP_DIRS = {".venv", "venv", ".git", "output", ".cache", "dist", "__pycache__"}


def _python_files():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if any(part in SKIP_DIRS or part.startswith(".venv") or part.startswith("_pre-sync")
               for part in rel.parts):
            continue
        yield p


def _mode(call: ast.Call) -> str:
    if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
        return str(call.args[1].value)
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return "r"


def _unguarded_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        has_encoding = any(kw.arg == "encoding" for kw in node.keywords)
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
            # write_text(data, encoding, ...) may pass it positionally as arg 2
            positional = 2 if func.attr == "write_text" else 1
            if not has_encoding and len(node.args) < positional:
                yield node.lineno, f".{func.attr}()"
        elif (isinstance(func, ast.Name) and func.id == "open") or (
            isinstance(func, ast.Attribute) and func.attr == "open"
            and isinstance(func.value, ast.Name) and func.value.id in ("io", "builtins")
        ):
            if "b" not in _mode(node) and not has_encoding:
                yield node.lineno, "open()"


def test_no_text_io_relies_on_the_locale_encoding():
    offenders = [
        f"{p.relative_to(ROOT)}:{line} {what}"
        for p in _python_files()
        for line, what in _unguarded_calls(p)
    ]
    assert not offenders, (
        "Text file I/O without encoding= (breaks on Windows cp1252):\n  "
        + "\n  ".join(offenders)
    )


def test_the_checker_catches_the_windows_bug(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from pathlib import Path\n"
        "Path('x').write_text('🏔️')\n"
        "Path('x').read_text()\n"
        "open('x', 'w')\n"
        "open('x', 'rb')\n"
        "Path('x').write_text('ok', encoding='utf-8')\n",
        encoding="utf-8",
    )
    found = [what for _, what in _unguarded_calls(bad)]
    assert found == [".write_text()", ".read_text()", "open()"]
