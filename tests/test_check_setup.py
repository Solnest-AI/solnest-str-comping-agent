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
    branding = tmp_path / "branding.json"
    branding.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cs, "BRANDING", branding)
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
    (kit_dir / ".env").write_text("AIRROI_API_KEY=ar_SECRET_VALUE\nFIRECRAWL_API_KEY=fc-kv-SECRET\n", encoding="utf-8")
    probes["FIRECRAWL_API_KEY"] = "rejected"
    cs.main(["--no-open"])
    out = capsys.readouterr().out
    # The full values, not "SECRET": a kit path like ".../SECRETS TEST/..." is fine to print.
    assert "ar_SECRET_VALUE" not in out and "fc-kv-SECRET" not in out


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
def test_set_value_keeps_crlf_and_bom_and_leaves_no_temp_file(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfSTACK_PMS=x\r\nAIRROI_API_KEY=\r\n")
    cs.kit.set_value(env, "AIRROI_API_KEY", "k")
    assert env.read_bytes() == b"\xef\xbb\xbfSTACK_PMS=x\r\nAIRROI_API_KEY=k\r\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


@pytest.mark.parametrize("bad", ["", "a\nFIRECRAWL_API_KEY=evil", "a\rb"])
def test_set_value_refuses_empty_or_multiline_values(tmp_path, bad):
    env = tmp_path / ".env"
    env.write_text("AIRROI_API_KEY=keep\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cs.kit.set_value(env, "AIRROI_API_KEY", bad)
    assert env.read_text(encoding="utf-8") == "AIRROI_API_KEY=keep\n"


def test_a_failed_forced_probe_clears_an_earlier_pass(world):
    """--force failing must not leave yesterday's READY for the next plain check."""
    kit_dir, probes, calls = world
    (kit_dir / ".env").write_text("AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n", encoding="utf-8")
    assert cs.main(["--no-open"]) == 0
    probes["AIRROI_API_KEY"] = "no credit"
    assert cs.main(["--no-open", "--force"]) == 2
    assert not cs.STAMP.exists()
    assert cs.main(["--no-open"]) == 2, "must probe again, not trust the old stamp"


@pytest.mark.parametrize("status", [401, 402, 403])
def test_key_failure_statuses_raise(status):
    with pytest.raises(cs.kit.KeyFailure) as e:
        cs.kit.check_key_status("AirROI", status)
    assert "check_setup.py" in str(e.value) and "Not a problem with the property" in str(e.value)


@pytest.mark.parametrize("status", [200, 404, 422, 429, 500])
def test_other_statuses_do_not(status):
    cs.kit.check_key_status("AirROI", status)


def test_a_key_failure_skips_every_fallback_even_six_wide():
    """The fallbacks in agent.py catch Exception / AirROIError and carry on with
    empty data. A key failure must get past all of them, including out of the
    six-wide asyncio.gather the comp fetch uses."""
    import asyncio

    async def one(i):
        try:
            if i == 3:
                cs.kit.check_key_status("AirROI", 402)
            await asyncio.sleep(0.05)
            return i
        except Exception:
            return None

    async def run():
        try:
            return await asyncio.gather(*[one(i) for i in range(6)])
        except Exception:
            return "swallowed"

    with pytest.raises(cs.kit.KeyFailure):
        asyncio.run(run())


def test_the_run_stops_cleanly_and_forgets_the_pass(tmp_path, monkeypatch, capsys):
    import agent
    stamp = tmp_path / "setup_ok.json"
    stamp.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cs.kit, "SETUP_STAMP", stamp)

    async def main():
        cs.kit.check_key_status("Firecrawl", 401)

    monkeypatch.setattr(agent, "main", main)
    with pytest.raises(SystemExit) as e:
        agent.run()
    assert e.value.code == 2 and not stamp.exists()
    err = capsys.readouterr().err
    assert "Firecrawl rejected the key" in err and "Traceback" not in err


def test_firecrawl_key_failure_raises_before_printing_the_body(monkeypatch, capsys):
    """The body could echo the token; an auth failure must never print it."""
    import asyncio

    import httpx

    from scrapers import property_search as ps

    monkeypatch.setattr(ps.config, "FIRECRAWL_API_KEY", "fc-token-xyz")
    transport = httpx.MockTransport(lambda req: httpx.Response(401, text="bad token fc-token-xyz"))
    real = httpx.AsyncClient
    monkeypatch.setattr(ps.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    with pytest.raises(cs.kit.KeyFailure):
        asyncio.run(ps._firecrawl_post("/scrape", {}))
    assert "fc-token-xyz" not in capsys.readouterr().err


def test_airroi_key_failure_skips_the_non_json_path():
    """A 401 with an HTML body must still be a key failure, not 'non-JSON response'."""
    import asyncio

    import httpx

    from scrapers import airroi

    transport = httpx.MockTransport(lambda req: httpx.Response(401, text="<html>nope</html>"))

    async def go():
        async with httpx.AsyncClient(transport=transport) as c:
            await airroi._request_with_retries(c, "https://x/test", {}, {})

    with pytest.raises(cs.kit.KeyFailure):
        asyncio.run(go())


def test_blank_lines_are_added_atomically_keeping_crlf_and_bom(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes(b"\xef\xbb\xbfSTACK_PMS=x\r\nAIRROI_API_KEY=a")
    cs.kit.add_blank_lines(env, ["AIRROI_API_KEY", "FIRECRAWL_API_KEY"])
    assert env.read_bytes() == b"\xef\xbb\xbfSTACK_PMS=x\r\nAIRROI_API_KEY=a\r\nFIRECRAWL_API_KEY=\r\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_a_failed_swap_leaves_the_master_intact_and_no_temp_file(tmp_path, monkeypatch):
    """Windows can refuse os.replace while another program holds the file."""
    env = tmp_path / ".env"
    env.write_text("AIRROI_API_KEY=old\n", encoding="utf-8")

    def refuse(*_a):
        raise PermissionError("in use")

    monkeypatch.setattr(cs.kit.os, "replace", refuse)
    with pytest.raises(PermissionError):
        cs.kit.set_value(env, "AIRROI_API_KEY", "new-secret")
    assert env.read_text(encoding="utf-8") == "AIRROI_API_KEY=old\n"
    assert [p.name for p in tmp_path.iterdir()] == [".env"], "a temp copy holding the key was left behind"


def test_vendor_replies_never_echo_the_key(monkeypatch, capsys):
    import asyncio

    import httpx

    from scrapers import airroi
    from scrapers import property_search as ps

    monkeypatch.setattr(airroi.config, "AIRROI_API_KEY", "ar-live-123")
    assert "ar-live-123" not in str(airroi.AirROIError(400, "bad request for key ar-live-123"))

    monkeypatch.setattr(ps.config, "FIRECRAWL_API_KEY", "fc-live-456")
    transport = httpx.MockTransport(lambda req: httpx.Response(429, text="slow down fc-live-456"))
    real = httpx.AsyncClient
    monkeypatch.setattr(ps.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    assert asyncio.run(ps._firecrawl_post("/scrape", {})) == {}
    err = capsys.readouterr().err
    assert "429" in err and "fc-live-456" not in err


def test_redaction_happens_before_truncation(monkeypatch, capsys):
    """A key straddling the 300-character cut must not leak its first half."""
    import asyncio

    import httpx

    from scrapers import airroi
    from scrapers import property_search as ps

    key = "fc-" + "k" * 40
    monkeypatch.setattr(ps.config, "FIRECRAWL_API_KEY", key)
    body = "x" * 280 + key
    transport = httpx.MockTransport(lambda req: httpx.Response(500, text=body))
    real = httpx.AsyncClient
    monkeypatch.setattr(ps.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))
    asyncio.run(ps._firecrawl_post("/scrape", {}))
    assert "fc-kkkk" not in capsys.readouterr().err

    akey = "ar-" + "q" * 40
    monkeypatch.setattr(airroi.config, "AIRROI_API_KEY", akey)
    msg, _ = airroi._extract_error(["y" * 280 + akey], 400)
    assert "ar-qqqq" not in msg
