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


def test_this_folders_env_wins_over_the_kit(monkeypatch):
    monkeypatch.setattr(config, "key_from_connections_kit", lambda name, p=None: "from_kit")
    monkeypatch.setenv("AIRROI_API_KEY", "from_env")
    assert config._key("AIRROI_API_KEY") == ("from_env", ".env")
    monkeypatch.delenv("AIRROI_API_KEY")
    assert config._key("AIRROI_API_KEY") == ("from_kit", "connections kit (~/.claude.json)")


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
