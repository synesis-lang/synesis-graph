"""Tests for `synesis-graph serve` — Phase B.

Most of this module is about what the engine does *not* do on its own: MCP starts
disabled, writes would be allowed if nobody said otherwise, and the layout
mismatch that makes a server run happily over nothing is invisible from inside.

The tests that need a live server are marked and skip without the optional
extra; the rest exercise the decisions, which are the part worth pinning.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from synesis_graph.core import ConnectionError, DependencyError
from synesis_graph.serve import (
    DEFAULT_PORT,
    ServeHandle,
    ServeOptions,
    _mcp_config_payload,
    client_config_snippet,
    find_databases,
    generate_password,
    install_into_claude_desktop,
    install_into_vscode,
    start_server,
)

# ---------------------------------------------------------------------------
# Permissions: the decision the engine will not make for us
# ---------------------------------------------------------------------------


def test_mcp_is_enabled_explicitly():
    """The plugin registers and then refuses every call until this is sent."""
    assert _mcp_config_payload(allow_writes=False)["enabled"] is True


def test_reads_are_on_and_writes_are_off_by_default():
    """A corpus is months of coding work; reading it is the use case."""
    payload = _mcp_config_payload(allow_writes=False)

    assert payload["allowReads"] is True
    for key in ("allowInsert", "allowUpdate", "allowDelete", "allowSchemaChange"):
        assert payload[key] is False, f"{key} must be off unless asked for"


def test_allow_writes_opens_data_operations():
    payload = _mcp_config_payload(allow_writes=True)

    for key in ("allowInsert", "allowUpdate", "allowDelete", "allowSchemaChange"):
        assert payload[key] is True


def test_admin_stays_blocked_even_with_writes():
    """Administrative calls reach past the corpus to the server itself.

    "Let the assistant edit my data" does not mean "let it manage the server",
    so this one flag is not reachable from the CLI at all.
    """
    assert _mcp_config_payload(allow_writes=True)["allowAdmin"] is False


def test_every_flag_is_stated_rather_than_defaulted():
    """A future engine default must not silently widen what a client may do."""
    payload = _mcp_config_payload(allow_writes=False)
    expected = {
        "enabled",
        "allowReads",
        "allowInsert",
        "allowUpdate",
        "allowDelete",
        "allowSchemaChange",
        "allowAdmin",
    }
    assert set(payload) == expected


# ---------------------------------------------------------------------------
# Finding the databases — the layout contract, from the serving side
# ---------------------------------------------------------------------------


def test_databases_are_found_under_the_databases_subdirectory(tmp_path):
    (tmp_path / "databases" / "face85").mkdir(parents=True)
    (tmp_path / "databases" / "quinto_andar").mkdir(parents=True)

    assert find_databases(tmp_path) == ["face85", "quinto_andar"]


def test_a_database_beside_the_root_is_not_found(tmp_path):
    """This is the silent failure, made loud.

    An export written to `<root>/<project>` leaves the server running over
    nothing: it starts, registers MCP, and answers every query with no rows.
    Reporting it here is the difference between a message and a mystery.
    """
    (tmp_path / "face85").mkdir()

    assert find_databases(tmp_path) == []


def test_serving_nothing_is_refused_with_the_command_to_fix_it(tmp_path):
    result = start_server(ServeOptions(db_path=tmp_path))

    assert isinstance(result, (ConnectionError, DependencyError))
    if isinstance(result, ConnectionError):
        assert "arcadedb-embedded --project" in result.details, "must say how to fix it"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_generated_passwords_are_unique_and_long():
    """Printed, never stored — so its strength is the whole protection."""
    first, second = generate_password(), generate_password()

    assert first != second
    assert len(first) >= 24


def test_a_supplied_password_is_reused(tmp_path):
    """A stable password keeps a client config valid across restarts."""
    options = ServeOptions(db_path=tmp_path, password="kept")

    assert options.password == "kept"


# ---------------------------------------------------------------------------
# The client snippet
# ---------------------------------------------------------------------------


def _handle(**kwargs) -> ServeHandle:
    defaults = {
        "server": None,
        "port": DEFAULT_PORT,
        "user": "root",
        "password": "pw",
        "databases": ["face85"],
    }
    defaults.update(kwargs)
    return ServeHandle(**defaults)


def test_snippet_is_valid_json_naming_the_endpoint():
    snippet = json.loads(client_config_snippet(_handle(), windows=False))
    args = snippet["synesis-local"]["args"]

    assert f"http://localhost:{DEFAULT_PORT}/api/v1/mcp" in args


def test_snippet_uses_the_right_launcher_per_platform():
    """`npx` alone routinely fails to launch on Windows."""
    windows = json.loads(client_config_snippet(_handle(), windows=True))
    unix = json.loads(client_config_snippet(_handle(), windows=False))

    assert windows["synesis-local"]["command"] == "npx.cmd"
    assert unix["synesis-local"]["command"] == "npx"


def test_snippet_carries_a_decodable_basic_header():
    import base64

    handle = _handle(password="s3cret")
    snippet = json.loads(client_config_snippet(handle, windows=False))
    args = snippet["synesis-local"]["args"]
    header = args[args.index("--header") + 1]

    token = header.removeprefix("Authorization: Basic ")
    assert base64.b64decode(token).decode() == "root:s3cret"


# ---------------------------------------------------------------------------
# Live server — the criterion this stage is judged by
# ---------------------------------------------------------------------------

pytest.importorskip(
    "arcadedb_embedded", reason="arcadedb-embedded not installed (optional extra)"
)


@pytest.mark.skipif(
    not os.environ.get("SYNESIS_TEST_LIVE_SERVER"),
    reason=(
        "starts a full ArcadeDB server; the JVM segfaults on Linux while the "
        "interpreter shuts down. Set SYNESIS_TEST_LIVE_SERVER=1 to run it."
    ),
)
def test_a_served_database_answers_over_mcp(tmp_path):
    """Export, serve, query — the whole of Phase B, with no server installed.

    Opt-in rather than deleted: this is the only end-to-end proof that MCP
    answers over a real socket, and it passes. What fails is the teardown —
    every test in the run succeeds, then the JVM crashes as Python exits,
    turning a green suite into exit code 139 (run 33134063688, Python 3.11 on
    ubuntu). The crash is in the engine's shutdown, not in anything this
    package does, and it takes the whole job down with it.

    Everything `serve` itself decides — permissions, layout, credentials, the
    client entries — is covered by the tests above, which need no server.

    Uses a non-default port so a real ArcadeDB on 2480 does not collide.
    """
    import urllib.request

    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient

    db_dir = tmp_path / "databases" / "corpus"
    with ArcadeDBEmbeddedClient(db_dir) as client:
        client.command("CREATE VERTEX TYPE Chain IF NOT EXISTS", language="sql")
        client.command("CREATE (c:Chain {name: 'Resilience'})")

    handle = start_server(
        ServeOptions(db_path=tmp_path, port=2479, password="test_pw_serve")
    )
    assert not isinstance(handle, (ConnectionError, DependencyError)), handle
    assert handle.databases == ["corpus"]

    try:
        request = urllib.request.Request(
            handle.endpoint,
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "query",
                        "arguments": {
                            "database": "corpus",
                            "language": "cypher",
                            "query": "MATCH (c:Chain) RETURN c.name AS name",
                        },
                    },
                }
            ).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": handle.basic_auth,
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read())

        # MCP wraps the result as text; the payload inside is the query answer.
        assert body["result"]["isError"] is False
        assert "Resilience" in body["result"]["content"][0]["text"]
    finally:
        handle.stop()


# ---------------------------------------------------------------------------
# Installing into the chat client's configuration
# ---------------------------------------------------------------------------


def test_the_snippet_is_one_entry_not_a_whole_file():
    """Printing `{"mcpServers": {...}}` invites destroying existing entries.

    Most researchers already have servers configured. Pasting a full-file
    wrapper over that section replaces them, and nothing warns: Claude Desktop
    simply stops seeing the databases that used to work.
    """
    snippet = json.loads(client_config_snippet(_handle(), windows=False))

    assert "mcpServers" not in snippet
    assert list(snippet) == ["synesis-local"]


def test_install_preserves_every_other_server_and_setting(tmp_path):
    """Hand-editing JSON is the step this exists to remove.

    A missing comma disables every MCP server at once, and the symptom appears
    later and elsewhere, as "Claude cannot see my data".
    """
    config = tmp_path / "claude_desktop_config.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "aura": {"command": "npx", "args": ["-y", "mcp-remote", "https://x"]},
                    "other": {"command": "uvx", "args": ["thing"]},
                },
                "preferences": {"theme": "dark"},
                "coworkUserFilesPath": "/home/researcher/files",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = install_into_claude_desktop(_handle(), path=config)
    assert not isinstance(result, (ConnectionError, DependencyError))
    path, replaced = result
    assert replaced is False

    written = json.loads(config.read_text(encoding="utf-8"))
    assert set(written["mcpServers"]) == {"aura", "other", "synesis-local"}
    assert written["mcpServers"]["aura"]["args"] == ["-y", "mcp-remote", "https://x"]
    assert written["preferences"] == {"theme": "dark"}
    assert written["coworkUserFilesPath"] == "/home/researcher/files"


def test_reinstalling_updates_in_place(tmp_path):
    """Every `serve` run has a new password; entries must not accumulate."""
    config = tmp_path / "claude_desktop_config.json"
    install_into_claude_desktop(_handle(port=2480), path=config)
    result = install_into_claude_desktop(_handle(port=2499), path=config)

    assert result[1] is True, "should report that it replaced an entry"
    written = json.loads(config.read_text(encoding="utf-8"))
    assert list(written["mcpServers"]) == ["synesis-local"]
    assert "2499" in " ".join(written["mcpServers"]["synesis-local"]["args"])


def test_install_creates_the_file_in_an_existing_config_directory(tmp_path):
    """A first-ever entry is normal; the *directory* is what proves the app exists."""
    config = tmp_path / "claude_desktop_config.json"

    result = install_into_claude_desktop(_handle(), path=config)

    assert not isinstance(result, (ConnectionError, DependencyError))
    assert json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["synesis-local"]


def test_a_previous_version_is_kept_as_a_backup(tmp_path):
    config = tmp_path / "claude_desktop_config.json"
    config.write_text('{"mcpServers": {"keep": {"command": "x"}}}', encoding="utf-8")

    install_into_claude_desktop(_handle(), path=config)

    backup = tmp_path / "claude_desktop_config.json.synesis-backup"
    assert "keep" in backup.read_text(encoding="utf-8")


def test_unreadable_config_is_refused_never_overwritten(tmp_path):
    """Broken to us may still be valuable to them; rewriting it discards work."""
    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{ this is not json", encoding="utf-8")

    result = install_into_claude_desktop(_handle(), path=config)

    assert isinstance(result, ConnectionError)
    assert config.read_text(encoding="utf-8") == "{ this is not json"


def test_the_config_location_is_not_guessed_where_there_is_no_official_build(monkeypatch):
    """Linux has no official Claude Desktop; a guessed path writes nowhere useful."""
    from synesis_graph.serve import claude_desktop_config_path

    monkeypatch.delenv("SYNESIS_MCP_CONFIG", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")

    assert claude_desktop_config_path() is None


def test_an_override_wins_on_every_platform(monkeypatch, tmp_path):
    """An unusual install should be a setting, not a dead end."""
    from synesis_graph.serve import claude_desktop_config_path

    target = tmp_path / "elsewhere.json"
    monkeypatch.setenv("SYNESIS_MCP_CONFIG", str(target))
    monkeypatch.setattr(sys, "platform", "linux")

    assert claude_desktop_config_path() == target


def test_install_refuses_rather_than_inventing_a_config_tree(tmp_path):
    """Creating the directory would configure an app that is not installed."""
    missing = tmp_path / "no-such-app" / "claude_desktop_config.json"

    result = install_into_claude_desktop(_handle(), path=missing)

    assert isinstance(result, ConnectionError)
    assert not missing.parent.exists(), "must not create the tree"
    assert "SYNESIS_MCP_CONFIG" in result.details, "must say how to point it elsewhere"


def test_a_write_failure_names_the_likely_causes(tmp_path, monkeypatch):
    """Read-only or root-owned files are the norm this has to fail gracefully on."""
    config = tmp_path / "claude_desktop_config.json"
    config.write_text("{}", encoding="utf-8")

    def _deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "write_text", _deny)
    result = install_into_claude_desktop(_handle(), path=config)

    assert isinstance(result, ConnectionError)
    assert "read-only" in result.details or "another user" in result.details


# ---------------------------------------------------------------------------
# The password the engine keeps for itself
# ---------------------------------------------------------------------------


def test_an_unknown_stored_credential_is_reset_not_reported(tmp_path):
    """`root_password` is honoured only while the users file is being created.

    On a later start the engine keeps the stored hash and ignores what it is
    given — silently — so a generated password yields HTTP 403 after an
    apparently successful startup. The credential guards nothing (a local
    server, reachable only from this machine), so resetting it is better than
    handing the researcher a chore whose only answer is yes.
    """
    from synesis_graph.serve import _reset_server_credentials, server_root_is_initialized

    users = tmp_path / "config" / "server-users.jsonl"
    users.parent.mkdir(parents=True)
    users.write_text('{"name": "root"}', encoding="utf-8")
    assert server_root_is_initialized(tmp_path) is True

    assert _reset_server_credentials(tmp_path) is None

    assert not users.exists(), "the stale credential must be out of the way"
    assert (
        users.with_suffix(".jsonl.superseded").read_text(encoding="utf-8")
        == '{"name": "root"}'
    ), "the old file is set aside, never destroyed"


def test_the_databases_are_untouched_by_a_credential_reset(tmp_path):
    """Resetting a password must never risk the corpus it protects."""
    from synesis_graph.serve import _reset_server_credentials

    corpus = tmp_path / "databases" / "corpus"
    corpus.mkdir(parents=True)
    (corpus / "data.bucket").write_text("graph bytes", encoding="utf-8")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "server-users.jsonl").write_text("{}", encoding="utf-8")

    _reset_server_credentials(tmp_path)

    assert (corpus / "data.bucket").read_text(encoding="utf-8") == "graph bytes"


def test_a_remembered_password_is_reused(tmp_path):
    """Without this, a second start generates a password the engine rejects."""
    from synesis_graph.serve import _remember_password, _remembered_password

    _remember_password(tmp_path, "kept_across_restarts")

    assert _remembered_password(tmp_path) == "kept_across_restarts"


def test_no_remembered_password_is_not_an_error(tmp_path):
    from synesis_graph.serve import _remembered_password

    assert _remembered_password(tmp_path) is None


# ---------------------------------------------------------------------------
# VS Code speaks a different dialect
# ---------------------------------------------------------------------------


def test_vscode_uses_http_directly_without_the_npx_bridge():
    """VS Code connects to an MCP server over HTTP itself.

    Claude Desktop's shape — `mcpServers`, `command: npx`, `mcp-remote` — is
    silently ignored by VS Code: no error, the server simply never appears. The
    two formats are kept apart rather than approximated.
    """
    from synesis_graph.serve import vscode_server_entry

    entry = vscode_server_entry(_handle())

    assert entry["type"] == "http"
    assert entry["url"] == f"http://localhost:{DEFAULT_PORT}/api/v1/mcp"
    assert entry["headers"]["Authorization"].startswith("Basic ")
    assert "command" not in entry and "args" not in entry


def test_vscode_install_uses_servers_not_mcpservers(tmp_path):
    """The top-level key differs too — `servers`, not `mcpServers`."""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"existing": {"type": "http", "url": "http://x"}}}),
        encoding="utf-8",
    )

    result = install_into_vscode(_handle(), path=config)
    assert not isinstance(result, (ConnectionError, DependencyError))

    written = json.loads(config.read_text(encoding="utf-8"))
    assert set(written["servers"]) == {"existing", "synesis-local"}
    assert written["servers"]["existing"]["url"] == "http://x"
    assert "mcpServers" not in written


def test_vscode_reinstall_updates_in_place(tmp_path):
    config = tmp_path / "mcp.json"
    install_into_vscode(_handle(port=2480), path=config)
    result = install_into_vscode(_handle(port=2499), path=config)

    assert result[1] is True
    written = json.loads(config.read_text(encoding="utf-8"))
    assert list(written["servers"]) == ["synesis-local"]
    assert written["servers"]["synesis-local"]["url"].endswith("2499/api/v1/mcp")


def test_vscode_unreadable_config_is_refused_never_overwritten(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text("{ broken", encoding="utf-8")

    result = install_into_vscode(_handle(), path=config)

    assert isinstance(result, ConnectionError)
    assert config.read_text(encoding="utf-8") == "{ broken"
