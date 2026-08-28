"""Tests for the in-process ArcadeDB transport.

Unlike the HTTP client's tests, these need no server. The engine ships with
`synesis-graph`, so they normally run everywhere; the skip guard remains for the
platforms the wheel does not cover.

They are written against the real engine rather than a fake because the whole
point of this class is the five places where the binding's behaviour differs from
the HTTP client's. A fake would encode what we *believe* the binding does, which
is exactly the belief that was wrong in the 2026-08-16 design sketch.

Expect a "Windows fatal exception: access violation" traceback naming
`jpype._core.startJVM` on the first test that opens a database. It is not a
crash: pytest's faulthandler reports the signal handlers the JVM installs while
starting, the run continues, and every test passes (confirmed by re-running with
`-p no:faulthandler`, where the trace disappears and the results are identical).
Faulthandler is deliberately left enabled for the suite as a whole — it would
hide a genuine crash in the other 649 tests.
"""

from __future__ import annotations

import pytest

from synesis_graph.arcadedb_client import ArcadeDBError
from synesis_graph.arcadedb_transport import ArcadeDBTransport

embedded = pytest.importorskip(
    "arcadedb_embedded",
    reason="arcadedb-embedded unavailable on this platform",
)

from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient  # noqa: E402


@pytest.fixture
def client(tmp_path):
    """A fresh database per test, discarded with the tmp_path."""
    with ArcadeDBEmbeddedClient(tmp_path / "graph") as c:
        c.command("CREATE VERTEX TYPE Chain IF NOT EXISTS", language="sql")
        yield c


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_satisfies_the_transport_protocol(client):
    """The sync layer is typed against the Protocol, not against this class."""
    assert isinstance(client, ArcadeDBTransport)


def test_signatures_match_the_http_client(client):
    """`language` must stay keyword-with-default, not the binding's positional.

    The engine takes `language` first and positionally. Following that here would
    make every unqualified call in the sync layer run as the wrong language —
    which is not a crash, just wrong results.
    """
    import inspect

    from synesis_graph.arcadedb_client import ArcadeDBClient

    for name in ("command", "query", "begin", "commit", "rollback"):
        assert inspect.signature(getattr(ArcadeDBEmbeddedClient, name)) == inspect.signature(
            getattr(ArcadeDBClient, name)
        ), f"{name} diverged from the HTTP client"


# ---------------------------------------------------------------------------
# The five behavioural differences this class exists to absorb
# ---------------------------------------------------------------------------


def test_write_returns_an_empty_list_not_none(client):
    """The binding returns `None` from a write; the contract promises a list.

    The sync layer iterates the result of `command` directly, so `None` here
    would surface as `TypeError: 'NoneType' object is not iterable`.
    """
    result = client.command("CREATE (c:Chain {name: 'Resilience'})")
    assert result == []


def test_a_statement_without_parameters_does_not_raise(client):
    """Passing `None` to the binding raises `Ambiguous overloads` from JPype.

    This is the regression that the obvious `*(params,)` implementation would
    introduce — and it would only fire on the calls that pass no parameters,
    making it look intermittent.
    """
    client.command("CREATE (c:Chain {name: 'Trust'})")  # no params
    assert client.query("MATCH (c:Chain) RETURN count(c) AS n") == [{"n": 1}]


def test_results_survive_a_second_iteration(client):
    """The binding's ResultSet is single-pass; a materialised list is not.

    Re-reading the cursor yields `[]` with no error, so a caller that checked
    `if rows:` and then iterated would silently process nothing.
    """
    client.command("CREATE (c:Chain {name: 'Cooperation'})")
    rows = client.query("MATCH (c:Chain) RETURN c.name AS name")

    assert rows == [{"name": "Cooperation"}]
    assert rows == [{"name": "Cooperation"}], "second read differed — not materialised"


def test_engine_errors_arrive_as_this_packages_error(client):
    """The binding defines its own same-named `ArcadeDBError` in another module.

    Nine `except ArcadeDBError` sites in the sync layer and adapter expect this
    package's class. Untranslated, each would let the failure escape unhandled.
    """
    with pytest.raises(ArcadeDBError) as excinfo:
        client.query("MATCH (c:Chain RETURN broken syntax")

    assert excinfo.value.detail, "the offending statement should ride along"


def test_parameters_are_passed_through(client):
    """`UNWIND $rows` is the shape of all eight `_sync_*` functions."""
    client.command(
        "UNWIND $rows AS row MERGE (c:Chain {name: row.name})",
        {"rows": [{"name": "A"}, {"name": "B"}]},
    )
    rows = client.query(
        "MATCH (c:Chain) WHERE c.name = $n RETURN c.name AS name", {"n": "B"}
    )
    assert rows == [{"name": "B"}]


# ---------------------------------------------------------------------------
# Transactions and lifecycle
# ---------------------------------------------------------------------------


def test_commit_persists_and_rollback_discards(client):
    """`_execute_sync_transaction` relies on both halves of this."""
    client.begin()
    client.command("CREATE (c:Chain {name: 'Committed'})")
    client.commit()

    client.begin()
    client.command("CREATE (c:Chain {name: 'Discarded'})")
    client.rollback()

    names = {r["name"] for r in client.query("MATCH (c:Chain) RETURN c.name AS name")}
    assert names == {"Committed"}


def test_begin_returns_a_string_no_caller_consumes(client):
    """The HTTP client returns a session id; the embedded engine has none.

    Returning `""` keeps the signature honest without inventing an identifier.
    """
    assert client.begin() == ""
    client.rollback()


def test_reopening_an_existing_database_keeps_the_data(tmp_path):
    """`prepare_destination` opens when the database exists, creates when not.

    This is also what makes the two-phase flow work: the export writes and
    closes, then a server opens the same directory to serve it.
    """
    path = tmp_path / "graph"
    with ArcadeDBEmbeddedClient(path) as first:
        first.command("CREATE VERTEX TYPE Chain IF NOT EXISTS", language="sql")
        first.command("CREATE (c:Chain {name: 'Persisted'})")

    with ArcadeDBEmbeddedClient(path) as second:
        assert second.query("MATCH (c:Chain) RETURN c.name AS name") == [
            {"name": "Persisted"}
        ]


def test_using_a_closed_client_is_an_error_not_a_crash(tmp_path):
    """Closing releases the database; later use must say so clearly."""
    c = ArcadeDBEmbeddedClient(tmp_path / "graph")
    c.close()
    c.close()  # idempotent

    with pytest.raises(ArcadeDBError, match="closed"):
        c.query("MATCH (n) RETURN n")


def test_database_name_defaults_to_the_directory_name(tmp_path):
    """The Protocol declares `database`; the adapter reads it back."""
    with ArcadeDBEmbeddedClient(tmp_path / "face85") as c:
        assert c.database == "face85"

    with ArcadeDBEmbeddedClient(tmp_path / "other", database="explicit") as c:
        assert c.database == "explicit"


# ---------------------------------------------------------------------------
# Terminal output: what the researcher actually sees
# ---------------------------------------------------------------------------


def test_engine_chatter_is_configured_away():
    """ArcadeDB narrates every index build at INFO; that is not for this audience."""
    from synesis_graph.arcadedb_embedded_client import _LOG_CONFIG, _quiet_jvm_args

    assert ".level=WARNING" in _LOG_CONFIG
    assert "com.arcadedb.level=WARNING" in _LOG_CONFIG
    args = _quiet_jvm_args()
    assert args and args[0].startswith("-Djava.util.logging.config.file=")


def test_the_config_file_is_rewritten_when_it_changes(tmp_path, monkeypatch):
    """A stale file from an older version would silence the wrong things forever."""
    import tempfile

    from synesis_graph.arcadedb_embedded_client import _LOG_CONFIG, _quiet_jvm_args

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    target = tmp_path / "synesis-graph-jvm-logging.properties"
    target.write_text("outdated", encoding="utf-8")

    _quiet_jvm_args()

    assert target.read_text(encoding="utf-8") == _LOG_CONFIG


def test_only_the_two_known_jvm_lines_are_dropped(capfd):
    """The filter must never swallow a real Java failure.

    Both noise lines are written to file descriptor 2 by the JVM itself, so they
    cannot be caught by replacing `sys.stderr` — the descriptor is redirected and
    the capture inspected afterwards. Everything unrecognised is re-emitted.
    """
    import os

    from synesis_graph.arcadedb_embedded_client import _without_jvm_startup_noise

    with _without_jvm_startup_noise():
        os.write(2, b"WARNING: Using incubator modules: jdk.incubator.vector\n")
        os.write(2, b"java.lang.OutOfMemoryError: Java heap space\n")
        os.write(2, b"WARNI [GraalPolyglotEngine] GraalVM Polyglot Engine: no languages found\n")

    err = capfd.readouterr().err
    assert "OutOfMemoryError" in err, "a real failure must still reach the terminal"
    assert "incubator modules" not in err
    assert "no languages found" not in err
