"""The transport contract, and the guarantee that the HTTP client still meets it.

`ArcadeDBTransport` exists so the sync layer can run over a second transport (the
in-process embedded engine) without touching a single Cypher statement. That only
holds while the contract and `ArcadeDBClient` agree, and nothing in the language
enforces the agreement: Protocol conformance is structural, so a signature drifting
on either side would break the embedded backend silently, at a call site far from
the change.

These tests are that alarm. They compare the two surfaces mechanically rather than
restating the signatures by hand — a hand-copied expectation would drift too.
"""

from __future__ import annotations

import inspect
from typing import Any, get_type_hints

import pytest

from synesis_graph.arcadedb_client import ArcadeDBClient
from synesis_graph.arcadedb_transport import ArcadeDBTransport

# The five verbs the sync layer calls. Counted from the source, not guessed:
# `command` 13x, `query` 3x, and one each of begin/commit/rollback across
# backends/arcadedb.py and metrics_arcadedb.py.
CONTRACT_METHODS = ("command", "query", "begin", "commit", "rollback")


def test_contract_names_exactly_the_five_verbs():
    """Nothing crept in, and nothing fell out.

    The contract is deliberately smaller than the client: preflight and setup
    (`is_ready`, `list_databases`, `create_database`, `close`) live in the adapter,
    where the two transports genuinely differ. Widening it here would push
    server-shaped concepts onto a transport that has no server.
    """
    declared = {
        name
        for name in vars(ArcadeDBTransport)
        if not name.startswith("_") and callable(vars(ArcadeDBTransport)[name])
    }
    assert declared == set(CONTRACT_METHODS)


@pytest.mark.parametrize("method_name", CONTRACT_METHODS)
def test_client_signature_matches_the_contract(method_name):
    """`ArcadeDBClient` satisfies the contract call-for-call.

    Compares the full `inspect.Signature`, so a changed default, a parameter
    turned positional, or a dropped keyword all fail here — at the contract —
    rather than at the first call site that happens to rely on the difference.
    """
    contract = inspect.signature(getattr(ArcadeDBTransport, method_name))
    client = inspect.signature(getattr(ArcadeDBClient, method_name))
    assert client == contract, (
        f"ArcadeDBClient.{method_name}{client} no longer matches "
        f"ArcadeDBTransport.{method_name}{contract}"
    )


def test_language_stays_a_keyword_defaulting_to_cypher():
    """The default is load-bearing, not cosmetic.

    Sync statements are Cypher reused verbatim from the Neo4j backend; schema and
    index work passes `language="sql"` explicitly. The embedded engine takes
    `language` as its *first positional* argument, so a wrapper is free to get
    this backwards — and then every unqualified call would run as the wrong
    language. Pinning it here is what makes that a test failure instead of a
    puzzling runtime error.
    """
    for method_name in ("command", "query"):
        param = inspect.signature(getattr(ArcadeDBTransport, method_name)).parameters[
            "language"
        ]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default == "cypher"


def test_reads_and_writes_return_a_list_never_none():
    """Both verbs promise `list[dict]`.

    The sync layer iterates results directly. The embedded engine returns `None`
    from a write and a single-pass cursor from a read; the annotation is what
    obliges that transport to materialise both at its own boundary instead of
    leaking either upward.
    """
    for method_name in ("command", "query"):
        hints = get_type_hints(getattr(ArcadeDBTransport, method_name))
        assert hints["return"] == list[dict[str, Any]]


def test_database_is_declared_and_mutable():
    """`prepare_destination` assigns `client.database` once the project names it.

    Declaring it on the contract is what keeps that assignment type-checked for
    any transport, and `str | None` (not `str`) matches the client, which starts
    out with no database selected.
    """
    hints = get_type_hints(ArcadeDBTransport)
    assert hints["database"] == (str | None)


def test_live_client_instance_satisfies_the_protocol():
    """Structural conformance, checked at runtime on a real instance.

    The signature tests above compare the class; this one confirms an actual
    object passes `isinstance`, which is what a caller relying on the Protocol
    would see. No connection is made — constructing the client performs no I/O.
    """
    client = ArcadeDBClient(uri="http://localhost:2480", user="root", password="")
    assert isinstance(client, ArcadeDBTransport)
