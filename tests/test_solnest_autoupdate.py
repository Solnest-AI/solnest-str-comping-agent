"""The Solnest auto-updater: merges its hook without losing settings, checks once a day,
and puts the old version back when a new one fails setup."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
SCRIPT = next(p for p in (HERE / "scripts" / "solnest_autoupdate.py", HERE / "solnest_autoupdate.py") if p.exists())


@pytest.fixture
def au(tmp_path, monkeypatch):
    monkeypatch.setenv("SOLNEST_HOME", str(tmp_path / "home" / ".solnest"))
    monkeypatch.setenv("SOLNEST_CLAUDE_SETTINGS", str(tmp_path / "home" / ".claude" / "settings.json"))
    spec = importlib.util.spec_from_file_location("solnest_autoupdate_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tool(tmp_path, update, after="true", undo=""):
    folder = tmp_path / "tool"
    folder.mkdir(exist_ok=True)
    (folder / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    return {"folder": str(folder), "update": update, "after": after, "undo": undo}


def test_hook_merge_keeps_existing_settings(au):
    au.SETTINGS.parent.mkdir(parents=True)
    au.SETTINGS.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}), encoding="utf-8")
    au.ensure_hook()
    au.ensure_hook()
    d = json.loads(au.SETTINGS.read_text(encoding="utf-8"))
    assert d["model"] == "opus" and d["hooks"]["Stop"]
    tagged = [h for g in d["hooks"]["SessionStart"] for h in g["hooks"] if au.HOOK_TAG in h["command"]]
    assert len(tagged) == 1


def test_invalid_settings_left_alone(au):
    au.SETTINGS.parent.mkdir(parents=True)
    au.SETTINGS.write_text("{not json", encoding="utf-8")
    assert "left it alone" in au.ensure_hook()
    assert au.SETTINGS.read_text(encoding="utf-8") == "{not json"


def test_update_then_once_a_day(au, tmp_path):
    t = _tool(tmp_path, "echo 1.1.0 > VERSION")
    au._write_json(au.TOOLS, {"x": t})
    au.run_all(force=False)
    assert (Path(t["folder"]) / "VERSION").read_text(encoding="utf-8").strip() == "1.1.0"
    (Path(t["folder"]) / "VERSION").write_text("1.0.0\n", encoding="utf-8")
    au.run_all(force=False)                      # same day: skipped
    assert (Path(t["folder"]) / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_failed_setup_puts_old_version_back(au, tmp_path):
    t = _tool(tmp_path, "cp VERSION VERSION.old && echo 2.0.0 > VERSION",
              after='grep -q 2.0.0 VERSION && exit 3 || exit 0', undo="mv VERSION.old VERSION")
    au._write_json(au.TOOLS, {"x": t})
    au.run_all(force=True)
    assert (Path(t["folder"]) / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"
    assert "put back" in json.loads(au.STATE.read_text(encoding="utf-8"))["x"]["result"]


def test_paused_does_nothing(au, tmp_path):
    t = _tool(tmp_path, "echo 9.9.9 > VERSION")
    au._write_json(au.TOOLS, {"x": t})
    au.PAUSED.write_text("paused", encoding="utf-8")
    au.run_all(force=True)
    assert (Path(t["folder"]) / "VERSION").read_text(encoding="utf-8").strip() == "1.0.0"


def test_starter_survives_a_deleted_tool_python(au, tmp_path):
    gone = tmp_path / "gone" / "python"
    au.write_starter({"a": {"python": str(gone)}, "b": {"python": sys.executable}})
    text = au.STARTER.read_text(encoding="utf-8")
    assert str(gone).replace("\\", "/") in text and sys.executable.replace("\\", "/") in text
    assert "bash" in au._hook_command() and str(au.STARTER).replace("\\", "/") in au._hook_command()
