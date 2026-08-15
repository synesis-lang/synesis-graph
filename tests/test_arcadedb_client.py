"""Tests for the ArcadeDB HTTP client.

The transport is faked at `urllib.request.urlopen`, the same monkeypatch approach
`test_phase7_multidb.py` uses for the Neo4j driver: no server is required, and the
tests assert on the request the client actually builds.

The integration tests at the bottom run against a live server when one is present
and skip otherwise, so CI never depends on it.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from synesis_graph.arcadedb_client import (
    SESSION_HEADER,
    ArcadeDBClient,
    ArcadeDBError,
)

ARCADEDB_URI = "http://localhost:2480"


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------
class FakeResponse:
    """Minimal stand-in for the object `urlopen` yields as a context manager."""

    def __init__(self, body: Any = None, headers: dict[str, str] | None = None):
        if body is None:
            self._raw = b""
        elif isinstance(body, (str, bytes)):
            self._raw = body.encode("utf-8") if isinstance(body, str) else body
        else:
            self._raw = json.dumps(body).encode("utf-8")
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Recorder:
    """Captures each request and replays a scripted sequence of responses."""

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request, timeout=None):
        self.calls.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "headers": {k.lower(): v for k, v in request.header_items()},
                "body": json.loads(request.data.decode("utf-8")) if request.data else None,
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError(f"Unexpected extra request: {request.full_url}")
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake_http(monkeypatch):
    """Returns a factory that installs a scripted transport."""

    def _install(responses: list[Any]) -> Recorder:
        recorder = Recorder(responses)
        monkeypatch.setattr(urllib.request, "urlopen", recorder)
        return recorder

    return _install


def make_client(**kwargs) -> ArcadeDBClient:
    params: dict[str, Any] = {
        "uri": ARCADEDB_URI,
        "user": "root",
        "password": "secret",
        "database": "testdb",
    }
    params.update(kwargs)
    return ArcadeDBClient(**params)


def http_error(status: int, body: Any) -> urllib.error.HTTPError:
    import io

    raw = json.dumps(body).encode("utf-8") if not isinstance(body, bytes) else body
    return urllib.error.HTTPError(
        url=ARCADEDB_URI, code=status, msg="error", hdrs=None, fp=io.BytesIO(raw)
    )


# ---------------------------------------------------------------------------
# Requests the client builds
# ---------------------------------------------------------------------------
def test_command_posts_language_and_statement(fake_http):
    recorder = fake_http([FakeResponse({"result": [{"n": 2}]})])
    client = make_client()

    result = client.command("MATCH (n) RETURN count(n) AS n")

    assert result == [{"n": 2}]
    call = recorder.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{ARCADEDB_URI}/api/v1/command/testdb"
    assert call["body"] == {"language": "cypher", "command": "MATCH (n) RETURN count(n) AS n"}


def test_command_sends_parameters_when_given(fake_http):
    recorder = fake_http([FakeResponse({"result": []})])
    rows = [{"name": "a"}, {"name": "b"}]

    make_client().command("UNWIND $rows AS row RETURN row", {"rows": rows})

    assert recorder.calls[0]["body"]["params"] == {"rows": rows}


def test_language_defaults_to_cypher_and_can_be_overridden(fake_http):
    recorder = fake_http([FakeResponse({"result": []}), FakeResponse({"result": []})])
    client = make_client()

    client.command("MATCH (n) RETURN n")
    client.command("CREATE VERTEX TYPE Concept", language="sql")

    assert recorder.calls[0]["body"]["language"] == "cypher"
    assert recorder.calls[1]["body"]["language"] == "sql"


def test_query_uses_the_query_endpoint(fake_http):
    recorder = fake_http([FakeResponse({"result": [{"v": 1}]})])

    make_client().query("MATCH (n) RETURN 1 AS v")

    assert recorder.calls[0]["url"] == f"{ARCADEDB_URI}/api/v1/query/testdb"


def test_database_argument_overrides_the_bound_one(fake_http):
    recorder = fake_http([FakeResponse({"result": []})])

    make_client().command("MATCH (n) RETURN n", database="other")

    assert recorder.calls[0]["url"].endswith("/command/other")


def test_command_without_any_database_fails_before_the_request(fake_http):
    recorder = fake_http([])

    with pytest.raises(ArcadeDBError, match="No database selected"):
        make_client(database=None).command("MATCH (n) RETURN n")

    assert recorder.calls == []


def test_basic_auth_header_is_sent(fake_http):
    recorder = fake_http([FakeResponse({"result": []})])

    make_client(user="root", password="pw").command("MATCH (n) RETURN n")

    # base64("root:pw")
    assert recorder.calls[0]["headers"]["authorization"] == "Basic cm9vdDpwdw=="


def test_trailing_slash_in_uri_does_not_double_up(fake_http):
    recorder = fake_http([FakeResponse({"result": []})])

    make_client(uri=f"{ARCADEDB_URI}/").command("MATCH (n) RETURN n")

    assert recorder.calls[0]["url"] == f"{ARCADEDB_URI}/api/v1/command/testdb"


def test_scalar_result_is_wrapped_in_a_list(fake_http):
    fake_http([FakeResponse({"result": {"n": 1}})])

    assert make_client().command("RETURN 1") == [{"n": 1}]


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------
def test_auth_failure_reports_the_server_message(fake_http):
    fake_http(
        [
            http_error(
                403,
                {
                    "error": "Security error",
                    "detail": "User/Password not valid",
                    "exception": "com.arcadedb.server.security.ServerSecurityException",
                },
            )
        ]
    )

    with pytest.raises(ArcadeDBError) as excinfo:
        make_client().command("MATCH (n) RETURN n")

    assert excinfo.value.status == 403
    assert "User/Password not valid" in str(excinfo.value)


def test_command_error_carries_the_server_detail(fake_http):
    """A schema mistake must arrive readable, not as a bare HTTP 500."""
    fake_http(
        [
            http_error(
                500,
                {
                    "error": "Error on transaction commit",
                    "detail": (
                        "Cannot create the index on type 'Chain.search_name' "
                        "because the property does not exist"
                    ),
                },
            )
        ]
    )

    with pytest.raises(ArcadeDBError) as excinfo:
        make_client().command("CREATE INDEX ON Chain (search_name) FULL_TEXT", language="sql")

    assert "because the property does not exist" in str(excinfo.value)


def test_error_field_in_a_200_body_is_still_an_error(fake_http):
    """ArcadeDB answers some failures with 200 and an `error` key."""
    fake_http([FakeResponse({"error": "Command text is null"})])

    with pytest.raises(ArcadeDBError, match="Command text is null"):
        make_client().command("MATCH (n) RETURN n")


def test_unreachable_server_names_the_uri(fake_http):
    fake_http([urllib.error.URLError("connection refused")])

    with pytest.raises(ArcadeDBError) as excinfo:
        make_client().command("MATCH (n) RETURN n")

    assert ARCADEDB_URI in str(excinfo.value)


def test_timeout_is_reported_as_such(fake_http):
    fake_http([TimeoutError("timed out")])

    with pytest.raises(ArcadeDBError, match="timed out"):
        make_client(timeout=1.0).command("MATCH (n) RETURN n")


def test_configured_timeout_reaches_urlopen(fake_http):
    recorder = fake_http([FakeResponse({"result": []})])

    make_client(timeout=12.5).command("MATCH (n) RETURN n")

    assert recorder.calls[0]["timeout"] == 12.5


def test_non_json_response_is_reported_with_a_snippet(fake_http):
    fake_http([FakeResponse("<html>502 Bad Gateway</html>")])

    with pytest.raises(ArcadeDBError) as excinfo:
        make_client().command("MATCH (n) RETURN n")

    assert "non-JSON" in str(excinfo.value)
    assert "502" in str(excinfo.value.detail)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------
def test_begin_captures_the_session_id(fake_http):
    fake_http([FakeResponse(None, {SESSION_HEADER: "AS-1234"})])
    client = make_client()

    assert client.begin() == "AS-1234"
    assert client.session_id == "AS-1234"


def test_statements_inside_a_transaction_carry_the_session_header(fake_http):
    recorder = fake_http(
        [
            FakeResponse(None, {SESSION_HEADER: "AS-1234"}),
            FakeResponse({"result": []}),
            FakeResponse({}),
        ]
    )
    client = make_client()

    client.begin()
    client.command("CREATE (n:Concept {name: 'x'})")
    client.commit()

    assert recorder.calls[1]["headers"][SESSION_HEADER] == "AS-1234"
    assert recorder.calls[2]["url"] == f"{ARCADEDB_URI}/api/v1/commit/testdb"


def test_no_session_header_outside_a_transaction(fake_http):
    recorder = fake_http([FakeResponse({"result": []})])

    make_client().command("MATCH (n) RETURN n")

    assert SESSION_HEADER not in recorder.calls[0]["headers"]


def test_commit_clears_the_session(fake_http):
    fake_http([FakeResponse(None, {SESSION_HEADER: "AS-1"}), FakeResponse({})])
    client = make_client()

    client.begin()
    client.commit()

    assert client.session_id is None


def test_rollback_clears_the_session(fake_http):
    fake_http([FakeResponse(None, {SESSION_HEADER: "AS-1"}), FakeResponse({})])
    client = make_client()

    client.begin()
    client.rollback()

    assert client.session_id is None


def test_failed_commit_still_clears_the_session(fake_http):
    """A stale session id would attach every later statement to a dead transaction."""
    fake_http(
        [
            FakeResponse(None, {SESSION_HEADER: "AS-1"}),
            http_error(500, {"error": "Transaction expired"}),
        ]
    )
    client = make_client()
    client.begin()

    with pytest.raises(ArcadeDBError):
        client.commit()

    assert client.session_id is None


def test_begin_rejects_a_second_transaction(fake_http):
    fake_http([FakeResponse(None, {SESSION_HEADER: "AS-1"})])
    client = make_client()
    client.begin()

    with pytest.raises(ArcadeDBError, match="already open"):
        client.begin()


def test_begin_without_session_header_is_an_error(fake_http):
    """Silently proceeding would run the whole sync outside any transaction."""
    fake_http([FakeResponse(None, {})])

    with pytest.raises(ArcadeDBError, match="session id"):
        make_client().begin()


def test_commit_without_a_transaction_is_an_error(fake_http):
    fake_http([])

    with pytest.raises(ArcadeDBError, match="No open transaction"):
        make_client().commit()


def test_close_rolls_back_an_open_transaction(fake_http):
    recorder = fake_http([FakeResponse(None, {SESSION_HEADER: "AS-1"}), FakeResponse({})])
    client = make_client()
    client.begin()

    client.close()

    assert recorder.calls[-1]["url"].endswith("/rollback/testdb")
    assert client.session_id is None


def test_close_without_a_transaction_does_nothing(fake_http):
    recorder = fake_http([])

    make_client().close()

    assert recorder.calls == []


def test_close_swallows_a_failing_rollback(fake_http):
    """close() runs in a `finally`; raising there would mask the original error."""
    fake_http(
        [
            FakeResponse(None, {SESSION_HEADER: "AS-1"}),
            http_error(500, {"error": "already gone"}),
        ]
    )
    client = make_client()
    client.begin()

    client.close()

    assert client.session_id is None


# ---------------------------------------------------------------------------
# Server and database management
# ---------------------------------------------------------------------------
def test_list_databases(fake_http):
    fake_http([FakeResponse({"result": ["face85", "factors"]})])

    assert make_client().list_databases() == ["face85", "factors"]


def test_database_exists(fake_http):
    fake_http([FakeResponse({"result": ["face85"]}), FakeResponse({"result": ["face85"]})])
    client = make_client()

    assert client.database_exists("face85") is True
    assert client.database_exists("absent") is False


def test_create_database_issues_the_server_command(fake_http):
    recorder = fake_http([FakeResponse({"result": []}), FakeResponse({"result": "ok"})])

    make_client().create_database("face85")

    assert recorder.calls[1]["url"] == f"{ARCADEDB_URI}/api/v1/server"
    assert recorder.calls[1]["body"] == {"command": "create database face85"}


def test_create_database_is_idempotent(fake_http):
    """`create database` fails on an existing name; a re-export must not error."""
    recorder = fake_http([FakeResponse({"result": ["face85"]})])

    make_client().create_database("face85")

    assert len(recorder.calls) == 1  # only the existence check


def test_drop_database(fake_http):
    recorder = fake_http([FakeResponse({"result": "ok"})])

    make_client().drop_database("face85")

    assert recorder.calls[0]["body"] == {"command": "drop database face85"}


def test_is_ready_true_on_probe_success(fake_http):
    fake_http([FakeResponse(None)])

    assert make_client().is_ready() is True


def test_is_ready_false_when_unreachable(fake_http):
    fake_http([urllib.error.URLError("refused")])

    assert make_client().is_ready() is False


# ---------------------------------------------------------------------------
# Integration — skipped unless a live server answers
# ---------------------------------------------------------------------------
def _live_client() -> ArcadeDBClient | None:
    import os

    password = os.environ.get("ARCADEDB_PASSWORD")
    if not password:
        return None
    client = ArcadeDBClient(
        uri=os.environ.get("ARCADEDB_HTTP_URI", ARCADEDB_URI),
        user=os.environ.get("ARCADEDB_USER", "root"),
        password=password,
        timeout=30.0,
    )
    return client if client.is_ready() else None


live = pytest.mark.skipif(
    _live_client() is None,
    reason="no live ArcadeDB (set ARCADEDB_PASSWORD and start the server)",
)


@live
def test_integration_full_cycle():
    """Exercises the real server: database, schema, transaction, rollback."""
    client = _live_client()
    db = "synesis_client_it"
    client.drop_database(db) if client.database_exists(db) else None
    client.create_database(db)
    client.database = db
    try:
        assert client.database_exists(db)

        client.command("CREATE VERTEX TYPE Concept IF NOT EXISTS", language="sql")
        client.command(
            "UNWIND $rows AS row MERGE (c:Concept {name: row.name}) SET c = row",
            {"rows": [{"name": "alpha"}, {"name": "beta"}]},
        )
        rows = client.query("MATCH (c:Concept) RETURN count(c) AS n")
        assert rows[0]["n"] == 2

        # Rollback must really discard the write.
        client.begin()
        client.command("MERGE (c:Concept {name: 'gamma'})")
        client.rollback()
        rows = client.query("MATCH (c:Concept) RETURN count(c) AS n")
        assert rows[0]["n"] == 2

        # Commit must really persist it.
        client.begin()
        client.command("MERGE (c:Concept {name: 'gamma'})")
        client.commit()
        rows = client.query("MATCH (c:Concept) RETURN count(c) AS n")
        assert rows[0]["n"] == 3
    finally:
        client.close()
        client.drop_database(db)
