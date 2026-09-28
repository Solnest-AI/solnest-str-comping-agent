"""scripts/check_setup.py: the kit and both keys, before any paid run. Hermetic."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_setup as cs  # noqa: E402


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake kit, fake probes and a private stamp file."""
    kit_dir = tmp_path / "str-secrets-connections"
    kit_dir.mkdir()
    (kit_dir / "CONNECTIONS.md").write_text("", encoding="utf-8")
    (kit_dir / "fan-out-env.sh").write_text("", encoding="utf-8")
    probes = {"AIRROI_API_KEY": "ok", "FIRECRAWL_API_KEY": "ok"}
    calls = []

    def probe(name):
        def run(key):
            calls.append(name)
            return probes[name]
        return run

    monkeypatch.setattr(cs.kit, "find_kit", lambda: kit_dir)
    monkeypatch.setattr(cs, "PROBES", {n: probe(n) for n in cs.REQUIRED})
    monkeypatch.setattr(cs, "STAMP", tmp_path / "stamp.json")
    # Hermetic: never adopt a key from this machine's real .env or ~/.claude.json.
    monkeypatch.setattr(cs, "other_copy", lambda name: ("", ""))
    return kit_dir, probes, calls


def test_ready_when_both_keys_work_then_skips_paid_probes_for_a_day(world, capsys):
    kit_dir, _, calls = world
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    assert cs.main(["--no-open"]) == 0
    assert calls == ["AIRROI_API_KEY", "FIRECRAWL_API_KEY"]
    assert cs.main(["--no-open"]) == 0
    assert len(calls) == 2, "a fresh pass must not pay for another probe"
    assert "READY" in capsys.readouterr().out


def test_a_changed_key_is_probed_again(world):
    kit_dir, _, calls = world
    env = kit_dir / ".env"
    env.write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    cs.main(["--no-open"])
    env.write_text("AIRROI_API_KEY=new\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    cs.main(["--no-open"])
    assert len(calls) == 4


def test_blank_firecrawl_is_required_and_gets_a_line_to_paste_into(world, capsys):
    kit_dir, _, _ = world
    (kit_dir / ".env").write_text("STACK_PMS=hospitable\nAIRROI_API_KEY=a", encoding="utf-8")
    assert cs.main(["--no-open"]) == 2
    text = (kit_dir / ".env").read_text(encoding="utf-8")
    assert "STACK_PMS=hospitable\nAIRROI_API_KEY=a\nFIRECRAWL_API_KEY=\n" == text
    out = capsys.readouterr().out
    assert "FIRECRAWL_API_KEY: blank" in out and "never in the chat" in out


def test_spaces_around_the_equals_sign_still_read_as_set(world):
    """The kit's env_load accepts `KEY = value`. Reading it as blank here once
    appended a second, empty KEY= line, which blanked the key for the kit too."""
    kit_dir, _, _ = world
    env = kit_dir / ".env"
    env.write_text('AIRROI_API_KEY = "a"\nFIRECRAWL_API_KEY =f\n', encoding="utf-8")
    assert cs.main(["--no-open"]) == 0
    assert env.read_text(encoding="utf-8").count("AIRROI_API_KEY") == 1


def test_a_rejected_key_is_not_ready(world):
    kit_dir, probes, _ = world
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    probes["AIRROI_API_KEY"] = "rejected"
    assert cs.main(["--no-open"]) == 2


def test_kit_never_run_or_missing_says_to_set_it_up(world, monkeypatch, capsys):
    assert cs.main(["--no-open"]) == 3            # kit folder, no .env
    monkeypatch.setattr(cs.kit, "find_kit", lambda: None)
    assert cs.main(["--no-open"]) == 3            # no kit at all
    assert "CONNECTIONS.md" in capsys.readouterr().out


def test_no_key_value_is_ever_printed(world, capsys):
    kit_dir, probes, _ = world
    (kit_dir / ".env").write_text("AIRROI_API_KEY=ar_SECRET_VALUE\nFIRECRAWL_API_KEY=fc-SECRET\n", encoding="utf-8")
    probes["FIRECRAWL_API_KEY"] = "rejected"
    cs.main(["--no-open"])
    out = capsys.readouterr().out
    assert "SECRET" not in out


def test_a_key_blank_in_the_kit_but_found_elsewhere_is_adopted_not_asked_for(world, monkeypatch, capsys):
    """A student who gave the key before (this folder's .env, ~/.claude.json)
    must not be asked to paste it again; the kit's .env stays the master copy."""
    kit_dir, _, calls = world
    env = kit_dir / ".env"
    env.write_text("STACK_PMS=x\nAIRROI_API_KEY=\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    monkeypatch.setattr(cs, "other_copy",
                        lambda n: ("found-secret-123", "~/.claude.json") if n == "AIRROI_API_KEY" else ("", ""))
    assert cs.main(["--no-open"]) == 0
    assert env.read_text(encoding="utf-8") == "STACK_PMS=x\nAIRROI_API_KEY=found-secret-123\nFIRECRAWL_API_KEY=f\n"
    assert "AIRROI_API_KEY" in calls, "an adopted key is still probed for real"
    out = capsys.readouterr().out
    assert "copied in from ~/.claude.json" in out and "found-secret-123" not in out


def test_an_adopted_key_that_is_rejected_still_opens_the_kit_env(world, monkeypatch):
    kit_dir, probes, _ = world
    (kit_dir / ".env").write_text("AIRROI_API_KEY=\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    monkeypatch.setattr(cs, "other_copy", lambda n: ("old", ".env") if n == "AIRROI_API_KEY" else ("", ""))
    probes["AIRROI_API_KEY"] = "rejected"
    assert cs.main(["--no-open"]) == 2


def test_other_copy_prefers_this_folders_env_then_the_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "ROOT", tmp_path)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "from-env")
    (tmp_path / ".env").write_text("AIRROI_API_KEY = local\n", encoding="utf-8")
    assert cs.other_copy("AIRROI_API_KEY") == ("local", "this folder's .env")
    assert cs.other_copy("FIRECRAWL_API_KEY") == ("from-env", "the environment")


def test_set_value_replaces_the_last_line_and_touches_nothing_else(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# AIRROI_API_KEY=example\nAIRROI_API_KEY=\nX=1\nAIRROI_API_KEY =\n", encoding="utf-8")
    cs.kit.set_value(env, "AIRROI_API_KEY", "k")
    assert env.read_text(encoding="utf-8") == "# AIRROI_API_KEY=example\nAIRROI_API_KEY=\nX=1\nAIRROI_API_KEY=k\n"
    assert cs.kit.read_env(env)["AIRROI_API_KEY"] == "k"


def test_a_key_or_credit_failure_mid_run_forgets_the_24h_pass(tmp_path, monkeypatch):
    """A pass is trusted for a day; a key that runs out of credit inside that
    day must not keep reporting READY."""
    stamp = tmp_path / "setup_ok.json"
    monkeypatch.setattr(cs.kit, "SETUP_STAMP", stamp)
    stamp.write_text("{}", encoding="utf-8")
    assert cs.kit.key_failure("AirROI", 500) == "" and stamp.exists()
    hint = cs.kit.key_failure("AirROI", 402)
    assert "out of credit" in hint and "check_setup.py" in hint and not stamp.exists()


def test_airroi_402_says_key_not_property_once(tmp_path, monkeypatch, capsys):
    from scrapers import airroi
    monkeypatch.setattr(cs.kit, "SETUP_STAMP", tmp_path / "setup_ok.json")
    monkeypatch.setattr(airroi, "_KEY_FAILURE_SHOWN", False)
    for _ in range(3):   # a comp fetch runs 6-wide: one line, not six
        airroi.AirROIError(402, "Payment Required")
    airroi.AirROIError(404, "not found")
    err = capsys.readouterr().err
    assert err.count("out of credit") == 1 and "Not a problem with the property" in err


def test_a_key_failure_stops_the_run_instead_of_falling_back():
    """A bad AirROI key once fell through to 'Subject photo refused'."""
    import agent
    from scrapers.airroi import AirROIError
    for status in (401, 402, 403):
        with pytest.raises(SystemExit):
            agent._stop_on_key_failure(AirROIError(status, "x"))
    agent._stop_on_key_failure(AirROIError(404, "x"))    # ordinary errors still fall back
    agent._stop_on_key_failure(TimeoutError())
