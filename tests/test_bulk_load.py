"""Etapa C: a rebuild writes its unique-keyed vertices through GraphBatch.

GraphBatch is ArcadeDB's bulk ingestion API (26.3.2+). It bypasses the
transactional layer, which is where the speed comes from and also every
constraint below: measured on this engine, 20,000 `Item` vertices take 1.03s
through Cypher `CREATE` (what Etapa A already gives) and 0.26s through
GraphBatch -- 3.9x on top of the earlier 12x.

Three properties make it safe, and each has tests here because violating any one
of them corrupts the graph rather than slowing it down:

1. **Rebuild only.** GraphBatch never deduplicates. Against a destination that
   may hold the keys already it would duplicate silently, or -- over a UNIQUE
   index -- abort the batch. `update` exists precisely for the case where the
   destination is not empty.
2. **Unique-keyed collections only.** `items`, `sources` and the ontology's
   `concepts` carry keys the compiler guarantees (0 duplicates across 272,154
   rows of the real corpus). Chains and taxonomies repeat keys on purpose, so
   their `MERGE` is load-bearing.
3. **Outside the transaction.** GraphBatch consumes an open transaction: the
   vertices land but the following `commit()` raises `Transaction not begun`.

And one property that makes it invisible: a graph built through the bulk path
must be indistinguishable from one built the ordinary way. The equivalence test
at the bottom is the real guarantee of that -- everything else only checks that
the right path was chosen.
"""

from __future__ import annotations

from typing import Any

import pytest

from synesis_graph.backends.arcadedb import (
    BULK_LOADABLE,
    _bulk_load_vertices,
    supports_bulk_load,
)


class _Recorder:
    """A transport that advertises bulk loading and records what it is asked."""

    def __init__(self, supported: bool = True) -> None:
        self._supported = supported
        self.bulk_calls: list[tuple[str, int]] = []
        self.statements: list[str] = []

    def supports_bulk_vertices(self) -> bool:
        return self._supported

    def bulk_create_vertices(self, type_name: str, rows: list[dict[str, Any]]) -> int:
        self.bulk_calls.append((type_name, len(rows)))
        return len(rows)

    def command(self, statement: str, params=None, **kw):
        self.statements.append(statement)
        return []

    query = command

    def begin(self, database=None):
        return "tx"

    def commit(self, database=None):
        return None

    def rollback(self, database=None):
        return None


def _payload(items: int = 5, sources: int = 2, concepts: int = 3, chains: int = 0):
    from tests.conftest import _make_payload

    return _make_payload(
        items=[{"item_id": f"i{i}", "citation": f"e{i}"} for i in range(items)],
        sources=[{"bibtex": f"s{i}", "title": f"t{i}"} for i in range(sources)],
        concepts=[
            {"props": {"name": f"c{i}"}, "relations": {"topic": ["T"]}}
            for i in range(concepts)
        ],
        chains=[
            {"source": f"c{i}", "target": "c0", "type": "APPLICATION", "description": "d"}
            for i in range(chains)
        ],
    )


# ---------------------------------------------------------------------------
# Which collections go through the bulk path
# ---------------------------------------------------------------------------


def test_rebuild_bulk_loads_the_three_unique_keyed_collections():
    client = _Recorder()

    loaded = _bulk_load_vertices(client, _payload(), mode="rebuild")

    assert loaded == frozenset(BULK_LOADABLE)
    assert {name for name, _ in client.bulk_calls} == {"Source", "Item", "Concept"}


def test_chains_and_taxonomies_are_never_bulk_loaded():
    """Their keys repeat on purpose; the MERGE that collapses them is the point."""
    client = _Recorder()

    _bulk_load_vertices(client, _payload(chains=50), mode="rebuild")

    types = {name for name, _ in client.bulk_calls}
    assert "Topic" not in types
    assert types == {"Source", "Item", "Concept"}, "no taxonomy or chain type"
    assert "chains" not in BULK_LOADABLE


def test_volume_does_not_change_the_choice():
    """The trigger is the nature of the operation, not a row count (study 4.6).

    A count-based threshold would mean production ran one path and the test
    suite another, since every fixture is small.
    """
    small = _Recorder()
    large = _Recorder()

    _bulk_load_vertices(small, _payload(items=1, sources=1, concepts=1), "rebuild")
    _bulk_load_vertices(large, _payload(items=5000, sources=900, concepts=700), "rebuild")

    assert {n for n, _ in small.bulk_calls} == {n for n, _ in large.bulk_calls}


def test_update_never_bulk_loads():
    """GraphBatch does not deduplicate; update's destination is not empty."""
    client = _Recorder()

    assert _bulk_load_vertices(client, _payload(), mode="update") == frozenset()
    assert client.bulk_calls == []


def test_an_engine_without_graphbatch_falls_back_without_error():
    """"Nothing can break": an older engine takes the Etapa A path silently."""
    client = _Recorder(supported=False)

    assert _bulk_load_vertices(client, _payload(), mode="rebuild") == frozenset()
    assert client.bulk_calls == []


def test_a_transport_that_never_heard_of_bulk_loading_is_fine():
    """The HTTP client has no such method. Probing must not raise."""

    class _Plain:
        pass

    assert supports_bulk_load(_Plain()) is False


def test_an_empty_collection_is_left_to_the_ordinary_path():
    """Not reporting it as loaded is what keeps this a fallback, not a gap."""
    client = _Recorder()

    loaded = _bulk_load_vertices(client, _payload(items=0), mode="rebuild")

    assert "items" not in loaded
    assert "sources" in loaded


# ---------------------------------------------------------------------------
# Handing off to the transaction
# ---------------------------------------------------------------------------


def test_skipped_collections_are_not_written_again():
    """A second write of the same vertices would duplicate or abort."""
    from synesis_graph.backends.arcadedb import _execute_sync_transaction

    client = _Recorder()
    _execute_sync_transaction(
        client, _payload(), mode="rebuild", skip=frozenset(BULK_LOADABLE)
    )

    text = "\n".join(client.statements)
    assert "(i:Item {item_id: row.item_id})" not in text
    assert "(s:Source {bibtex: row.bibtex})" not in text


def test_skipping_vertices_still_writes_their_edges():
    """The endpoints resolve by MATCH on the UNIQUE index, as before.

    This is the property that removed the need for a 272,154-entry key-to-RID
    map: only vertices go through the bulk path, edges are untouched.
    """
    from synesis_graph.backends.arcadedb import _execute_sync_transaction
    from tests.conftest import _make_payload

    payload = _make_payload(
        items=[{"item_id": "i0"}],
        sources=[{"bibtex": "s0"}],
        from_source=[{"item_id": "i0", "ref": "s0"}],
        mentions=[{"item_id": "i0", "concept": "c0", "mention_order": 0}],
    )

    client = _Recorder()
    _execute_sync_transaction(
        client, payload, mode="rebuild", skip=frozenset(BULK_LOADABLE)
    )

    text = "\n".join(client.statements)
    assert "FROM_SOURCE" in text
    assert "MENTIONS" in text


def test_taxonomy_edges_survive_a_bulk_loaded_concept_set():
    """The concept vertices were bulk-loaded; their taxonomy edges were not.

    `_sync_taxonomies` must still receive the full concept list. Narrowing it to
    the emptied local would drop every GROUPED_BY edge while leaving the vertices
    in place -- a graph that looks populated and answers nothing.
    """
    from synesis_graph.backends.arcadedb import _execute_sync_transaction

    client = _Recorder()
    _execute_sync_transaction(
        client, _payload(concepts=4), mode="rebuild", skip=frozenset(BULK_LOADABLE)
    )

    assert "GROUPED_BY" in "\n".join(client.statements)


def test_nothing_is_skipped_by_default():
    """A caller that knows nothing about bulk loading behaves exactly as before."""
    import inspect

    from synesis_graph.backends.arcadedb import _execute_sync_transaction

    assert inspect.signature(_execute_sync_transaction).parameters["skip"].default == frozenset()

    client = _Recorder()
    _execute_sync_transaction(client, _payload(), mode="rebuild")

    text = "\n".join(client.statements)
    assert "(i:Item {item_id: row.item_id})" in text


# ---------------------------------------------------------------------------
# Against the real engine
# ---------------------------------------------------------------------------

pytest.importorskip(
    "arcadedb_embedded", reason="arcadedb-embedded unavailable on this platform"
)


def _counts(client, payload) -> dict[str, int]:
    """Every vertex label and edge type the fixture can produce."""
    out = {}
    for label in ("Item", "Source", payload.concept_label, "Topic", "ProjectContext"):
        out[f"v:{label}"] = client.query(
            f"MATCH (n:{label}) RETURN count(n) AS n"
        )[0]["n"]
    for rel in ("FROM_SOURCE", "MENTIONS", "RELATES_TO", "GROUPED_BY"):
        out[f"e:{rel}"] = client.query(
            f"MATCH ()-[r:{rel}]->() RETURN count(r) AS n"
        )[0]["n"]
    return out


def _real_payload(n: int = 400):
    from tests.conftest import _make_payload

    return _make_payload(
        sources=[{"bibtex": f"s{i}", "title": f"Source {i}"} for i in range(10)],
        items=[{"item_id": f"i{i}", "citation": f"excerpt {i}"} for i in range(n)],
        from_source=[{"item_id": f"i{i}", "ref": f"s{i % 10}"} for i in range(n)],
        concepts=[
            {"props": {"name": f"c{i}"}, "relations": {"topic": [f"T{i % 4}"]}}
            for i in range(30)
        ],
        chains=[
            {
                "source": f"c{i % 30}",
                "target": f"c{(i + 1) % 30}",
                "type": "APPLICATION",
                "description": "d",
            }
            for i in range(60)
        ],
        mentions=[
            {"item_id": f"i{i}", "concept": f"c{i % 30}", "mention_order": 0}
            for i in range(n)
        ],
    )


def test_the_bulk_path_actually_runs_against_the_real_engine(tmp_path):
    """Without this the suite would only ever exercise the fallback."""
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import supports_bulk_load, sync_to_arcadedb

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert supports_bulk_load(client), "this engine must offer GraphBatch"
        assert sync_to_arcadedb(client, _real_payload(), mode="rebuild") is None
        assert client.query("MATCH (n:Item) RETURN count(n) AS n")[0]["n"] == 400


def test_the_bulk_path_and_the_cypher_path_produce_the_same_graph(tmp_path):
    """The guarantee the whole two-path design rests on.

    Study 3.5 verified indistinguishability on two hand-made vertices. This runs
    a whole payload both ways and compares every vertex label and every edge
    type, which is what "the researcher's queries return the same answers" means
    in practice.
    """
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends import arcadedb as mod

    payload = _real_payload()

    with ArcadeDBEmbeddedClient(tmp_path / "bulk") as client:
        assert mod.sync_to_arcadedb(client, payload, mode="rebuild") is None
        via_bulk = _counts(client, payload)

    # Same code, same payload, with the capability denied -- so the difference
    # under test is the write path and nothing else.
    with ArcadeDBEmbeddedClient(tmp_path / "cypher") as client:
        original = mod.supports_bulk_load
        mod.supports_bulk_load = lambda c: False
        try:
            assert mod.sync_to_arcadedb(client, payload, mode="rebuild") is None
            via_cypher = _counts(client, payload)
        finally:
            mod.supports_bulk_load = original

    assert via_bulk == via_cypher
    assert via_bulk["v:Item"] == 400, "the comparison must cover a populated graph"
    assert via_bulk["e:MENTIONS"] == 400
    assert via_bulk["e:GROUPED_BY"] > 0


def test_bulk_loaded_vertices_carry_the_same_properties(tmp_path):
    """Equal counts would hide a bulk path that dropped a property."""
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends import arcadedb as mod

    payload = _real_payload(50)

    def props(root):
        with ArcadeDBEmbeddedClient(root) as client:
            assert mod.sync_to_arcadedb(client, payload, mode="rebuild") is None
            return client.query(
                "MATCH (i:Item) WHERE i.item_id = 'i7' RETURN i.citation AS c"
            )

    bulk = props(tmp_path / "bulk")

    original = mod.supports_bulk_load
    mod.supports_bulk_load = lambda c: False
    try:
        cypher = props(tmp_path / "cypher")
    finally:
        mod.supports_bulk_load = original

    assert bulk == cypher
    assert bulk[0]["c"] == "excerpt 7"


def test_a_bulk_rebuild_is_still_idempotent(tmp_path):
    """Twice in a row must not duplicate -- the clear is what guarantees it.

    If a future change ever let the bulk path run without clearing first, the
    UNIQUE index would raise `DuplicatedKeyException` here rather than quietly
    doubling the graph.
    """
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import sync_to_arcadedb

    payload = _real_payload(100)

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert sync_to_arcadedb(client, payload, mode="rebuild") is None
        first = _counts(client, payload)
        assert sync_to_arcadedb(client, payload, mode="rebuild") is None
        second = _counts(client, payload)

    assert first == second


def test_update_after_a_bulk_rebuild_adds_without_duplicating(tmp_path):
    """The mixed case the user asked about: small bulk graph, then a big update.

    The bulk-written vertices and the MERGE-written ones must be the same
    vertices to the update -- if GraphBatch produced anything the later `MERGE`
    could not recognise, this is where it would show as a doubled count.
    """
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import sync_to_arcadedb

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert sync_to_arcadedb(client, _real_payload(100), mode="rebuild") is None
        assert sync_to_arcadedb(client, _real_payload(150), mode="update") is None

        assert client.query("MATCH (n:Item) RETURN count(n) AS n")[0]["n"] == 150
        assert client.query("MATCH (n:Source) RETURN count(n) AS n")[0]["n"] == 10
        assert (
            client.query("MATCH ()-[r:FROM_SOURCE]->() RETURN count(r) AS n")[0]["n"]
            == 150
        )


def test_bulk_loading_a_repeated_key_fails_loudly(tmp_path):
    """The engine enforces "rebuild only" so the rule cannot rot into a comment.

    A repeated key over the UNIQUE index aborts the batch rather than silently
    creating a second vertex -- which is why a bulk load into a populated graph
    is a caught error, not a corrupted corpus.
    """
    from synesis_graph.arcadedb_client import ArcadeDBError
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import sync_to_arcadedb

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert sync_to_arcadedb(client, _real_payload(20), mode="rebuild") is None

        with pytest.raises(ArcadeDBError):
            client.bulk_create_vertices("Item", [{"item_id": "i0"}])


def test_the_bulk_load_runs_with_no_transaction_open(tmp_path):
    """Pins the ordering constraint discovered against the real engine.

    GraphBatch consumes the open transaction: the vertices land, then `commit()`
    raises `Transaction not begun`. So the bulk load must happen before
    `_execute_sync_transaction` opens one.

    Checked by watching the transaction state rather than by reading the source,
    because what matters is that no `begin()` is in effect at the moment the bulk
    write happens -- an equivalent reordering elsewhere would break it just the
    same.
    """
    from synesis_graph.backends import arcadedb as mod

    class _Watcher(_Recorder):
        def __init__(self) -> None:
            super().__init__()
            self.open = False
            self.bulk_while_open: list[str] = []

        def begin(self, database=None):
            self.open = True
            return "tx"

        def commit(self, database=None):
            self.open = False

        def rollback(self, database=None):
            self.open = False

        def bulk_create_vertices(self, type_name, rows):
            if self.open:
                self.bulk_while_open.append(type_name)
            return super().bulk_create_vertices(type_name, rows)

    client = _Watcher()
    mod._bulk_load_vertices(client, _payload(), mode="rebuild")
    mod._execute_sync_transaction(client, _payload(), mode="rebuild")
    assert client.bulk_calls, "the bulk path must have run for this to mean anything"
    assert client.bulk_while_open == [], "GraphBatch must not run inside a transaction"

    # And the real thing, end to end: a commit that succeeds proves the
    # transaction was never consumed by the batch.
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as real:
        assert mod.sync_to_arcadedb(real, _real_payload(50), mode="rebuild") is None
        assert real.query("MATCH (n:Item) RETURN count(n) AS n")[0]["n"] == 50
