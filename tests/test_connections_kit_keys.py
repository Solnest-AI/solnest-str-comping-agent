"""Keys the STR Secrets connections kit already collected are used, never re-asked.

Summit attendees run the connections kit as pre-work, before they have this
folder. The kit registers the AirROI and Firecrawl MCP servers in the
top-level "mcpServers" of ~/.claude.json with the key in a header
(str-secrets-connections lib/mcp_register.py): airroi-official ->
X-API-KEY, firecrawl -> "Authorization: Bearer <key>". Its .env fan-out
only reaches skill folders that existed when it ran, so without this every
attendee would be asked again for keys they already gave (2026-09-26).
"""

from __future__ import annotations

import json

import config


def _write(tmp_path, servers) -> object:
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return p


KIT = {
    "airroi-official": {"type": "http", "url": "https://mcp.airroi.com",
                        "headers": {"X-API-KEY": "ar_kit_key_123"}},
    "firecrawl": {"type": "http", "url": "https://mcp.firecrawl.dev/v2/mcp",
                  "headers": {"Authorization": "Bearer fc-kit-key-456"}},
}


def test_reads_both_keys_the_kit_registered(tmp_path):
    p = _write(tmp_path, KIT)
    assert config.key_from_connections_kit("AIRROI_API_KEY", p) == "ar_kit_key_123"
    assert config.key_from_connections_kit("FIRECRAWL_API_KEY", p) == "fc-kit-key-456"


def test_reads_an_env_block_too(tmp_path):
    """The kit's stdio fallback for Firecrawl registers the key as env."""
    p = _write(tmp_path, {"firecrawl": {"command": "npx", "args": ["-y", "firecrawl-mcp"],
                                        "env": {"FIRECRAWL_API_KEY": "fc-env-789"}}})
    assert config.key_from_connections_kit("FIRECRAWL_API_KEY", p) == "fc-env-789"


def test_templates_and_junk_are_not_keys(tmp_path):
    p = _write(tmp_path, {
        "airroi-official": {"url": "https://mcp.airroi.com", "headers": {"X-API-KEY": "AIRROI_API_KEY"}},
        "firecrawl": {"url": "https://mcp.firecrawl.dev/v2/mcp", "headers": {"Authorization": "${FIRECRAWL_API_KEY}"}},
        "broken": "not a dict",
    })
    assert config.key_from_connections_kit("AIRROI_API_KEY", p) == ""
    assert config.key_from_connections_kit("FIRECRAWL_API_KEY", p) == ""
    assert config.key_from_connections_kit("SOME_OTHER_KEY", p) == ""


def test_missing_or_malformed_file_is_just_no_key(tmp_path):
    assert config.key_from_connections_kit("AIRROI_API_KEY", tmp_path / "nope.json") == ""
    bad = tmp_path / ".claude.json"
    bad.write_text("{not json", encoding="utf-8")
    assert config.key_from_connections_kit("AIRROI_API_KEY", bad) == ""


def test_key_precedence_kit_env_then_this_folder_then_claude_json(monkeypatch):
    """The kit's .env is the master copy (Ryan, 2026-09-28)."""
    monkeypatch.setattr(config, "key_from_connections_kit", lambda name, p=None: "from_claude_json")
    monkeypatch.setattr(config, "_kit_key", lambda name: "from_kit_env")
    monkeypatch.setenv("AIRROI_API_KEY", "from_env")
    assert config._key("AIRROI_API_KEY") == ("from_kit_env", "connections kit (.env)")
    monkeypatch.setattr(config, "_kit_key", lambda name: "")
    assert config._key("AIRROI_API_KEY") == ("from_env", ".env")
    monkeypatch.delenv("AIRROI_API_KEY")
    assert config._key("AIRROI_API_KEY") == ("from_claude_json", "connections kit (~/.claude.json)")


def _make_kit(d, env=None):
    d.mkdir(parents=True)
    (d / "CONNECTIONS.md").write_text("", encoding="utf-8")
    (d / "fan-out-env.sh").write_text("", encoding="utf-8")
    if env is not None:
        (d / ".env").write_text(env, encoding="utf-8")
    return d


def test_finds_the_kit_on_the_desktop_and_reads_its_env(tmp_path, monkeypatch):
    import kit
    monkeypatch.delenv("STR_SECRETS_KIT", raising=False)
    k = _make_kit(tmp_path / "Desktop" / "str-secrets-connections",
                  'AIRROI_API_KEY="ar_desk"\nFIRECRAWL_API_KEY=\n')
    found = kit.find_kit(home=tmp_path, near=tmp_path / "elsewhere" / "comping")
    assert found == k.resolve()
    assert kit.kit_value("AIRROI_API_KEY", found) == "ar_desk"
    assert kit.kit_value("FIRECRAWL_API_KEY", found) == ""


def test_a_kit_that_was_run_beats_an_unused_download(tmp_path, monkeypatch):
    import kit
    monkeypatch.delenv("STR_SECRETS_KIT", raising=False)
    _make_kit(tmp_path / "Downloads" / "str-secrets-connections")
    used = _make_kit(tmp_path / "Documents" / "SECRETS" / "str-secrets-connections", "AIRROI_API_KEY=x\n")
    assert kit.find_kit(home=tmp_path, near=tmp_path / "x" / "y") == used.resolve()


def test_no_kit_is_none(tmp_path, monkeypatch):
    import kit
    monkeypatch.delenv("STR_SECRETS_KIT", raising=False)
    assert kit.find_kit(home=tmp_path, near=tmp_path / "x" / "y") is None


def test_reads_the_env_beside_the_bundled_stdio_server(tmp_path):
    """Kit "Path A" only: the key lives in mcp-servers/airroi/.env, not in ~/.claude.json.
    Windows registers native paths with forward or back slashes; both resolve."""
    server_dir = tmp_path / "mcp-servers" / "airroi"
    server_dir.mkdir(parents=True)
    (server_dir / "server.py").write_text("", encoding="utf-8")
    (server_dir / ".env").write_text("# comment\nAIRROI_API_KEY=ar_path_a_key\nOTHER=x\n", encoding="utf-8")
    for script in (str(server_dir / "server.py"), (server_dir / "server.py").as_posix()):
        p = _write(tmp_path, {"airroi": {"type": "stdio", "command": "python.exe", "args": [script], "env": {}}})
        assert config.key_from_connections_kit("AIRROI_API_KEY", p) == "ar_path_a_key"
    assert config.key_from_connections_kit("FIRECRAWL_API_KEY", p) == ""


def test_the_header_still_wins_over_the_stdio_env(tmp_path):
    server_dir = tmp_path / "airroi"
    server_dir.mkdir()
    (server_dir / ".env").write_text("AIRROI_API_KEY=ar_stdio\n", encoding="utf-8")
    p = _write(tmp_path, {**KIT, "airroi": {"command": "python", "args": [str(server_dir / "server.py")]}})
    assert config.key_from_connections_kit("AIRROI_API_KEY", p) == "ar_kit_key_123"


def test_blank_or_missing_stdio_env_is_no_key(tmp_path):
    server_dir = tmp_path / "airroi"
    server_dir.mkdir()
    (server_dir / ".env").write_text("AIRROI_API_KEY=\n", encoding="utf-8")
    p = _write(tmp_path, {"airroi": {"command": "python", "args": [str(server_dir / "server.py")]},
                          "gone": {"command": "python", "args": [str(tmp_path / "nope" / "server.py")]}})
    assert config.key_from_connections_kit("AIRROI_API_KEY", p) == ""


def test_a_newer_blank_download_does_not_displace_the_working_kit(tmp_path, monkeypatch):
    """A second clone in Downloads, newer but empty, must not win over the
    sibling kit the student filled in (the old sort was newest .env first)."""
    import os
    import time

    import kit
    monkeypatch.delenv("STR_SECRETS_KIT", raising=False)
    kit._FOUND.clear()
    near = tmp_path / "Documents" / "SECRETS" / "comping"
    working = _make_kit(near.parent / "str-secrets-connections",
                        "AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n")
    old = time.time() - 86400
    os.utime(working / ".env", (old, old))
    _make_kit(tmp_path / "Downloads" / "str-secrets-connections", "AIRROI_API_KEY=\nFIRECRAWL_API_KEY=\n")
    assert kit.find_kit(home=tmp_path, near=near) == working.resolve()


def test_the_explicit_override_wins_then_the_remembered_kit(tmp_path, monkeypatch):
    import kit
    kit._FOUND.clear()
    near = tmp_path / "Documents" / "comping"
    full = _make_kit(tmp_path / "Desktop" / "str-secrets-connections", "AIRROI_API_KEY=a\nFIRECRAWL_API_KEY=f\n")
    other = _make_kit(tmp_path / "Downloads" / "kit-copy", "AIRROI_API_KEY=a\n")
    monkeypatch.delenv("STR_SECRETS_KIT", raising=False)
    assert kit.find_kit(home=tmp_path, near=near) == full.resolve()
    kit.remember_kit(other)
    assert kit.find_kit(home=tmp_path, near=near) == other.resolve()
    monkeypatch.setenv("STR_SECRETS_KIT", str(full))
    assert kit.find_kit(home=tmp_path, near=near) == full.resolve()


def test_a_remembered_kit_that_was_deleted_falls_back_to_the_search(tmp_path, monkeypatch):
    import kit
    monkeypatch.delenv("STR_SECRETS_KIT", raising=False)
    kit._FOUND.clear()
    kit.KIT_CHOICE.write_text(str(tmp_path / "gone"), encoding="utf-8")
    k = _make_kit(tmp_path / "Desktop" / "str-secrets-connections", "AIRROI_API_KEY=a\n")
    assert kit.find_kit(home=tmp_path, near=tmp_path / "x" / "y") == k.resolve()
