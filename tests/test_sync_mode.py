"""Etapa A: `CREATE` where the payload already guarantees uniqueness.

`MERGE` costs an index lookup per row against an index that grows as the load
proceeds. Measured against the real Hostinger server, that degrades
quadratically -- 10,000 nodes in 22.2s, 40,000 in 472.8s -- extrapolating to
roughly five hours for the 246,588-item Quinto Andar corpus. The same load with
`CREATE` measured 12x faster.

`CREATE` is only sound where two things hold at once: the destination is empty
(both backends call `clear_database` immediately before syncing) and the
payload has no repeated key. The second was measured on the real corpus:
`items` 246,588 rows / 0 duplicate keys, `sources` 2,981 / 0, ontology
`concepts` 22,585 / 0.

Chains and taxonomies fail the second condition by design -- 302,392 chain
endpoints collapse to 22,553 distinct concepts, one taxonomy value is shared by
many concepts -- so their `MERGE` is doing real work and must survive both
modes. Most of what follows pins exactly that: not that `CREATE` appears, but
that it does not appear where it would silently multiply nodes.
"""

from __future__ import annotations

from typing import Any

import pytest

from synesis_graph.backends.neo4j import (
    _sync_concepts,
    _sync_items,
    _sync_sources,
    _sync_taxonomies,
)


class _RecordingTx:
    """Records the Cypher issued, so the mode can be read off the statements."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, query: str, **params: Any) -> None:
        self.queries.append(query)

    @property
    def text(self) -> str:
        return "\n".join(self.queries)


def _concepts(n: int = 3) -> list[dict[str, Any]]:
    return [
        {"props": {"name": f"c{i}"}, "relations": {"topic": ["T"], "aspect": ["A"]}}
        for i in range(n)
    ]


def _chains(n: int = 3) -> list[dict[str, Any]]:
    return [
        {"source": f"c{i}", "target": "c0", "type": "APPLICATION", "description": "d"}
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# The three collections the payload measures as unique
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn, args, pattern",
    [
        (_sync_items, ([{"item_id": "i1"}],), "(i:Item {item_id: row.item_id})"),
        (_sync_sources, ([{"bibtex": "s1"}],), "(s:Source {bibtex: row.bibtex})"),
    ],
)
def test_rebuild_creates_and_update_merges(fn, args, pattern):
    rebuild, update = _RecordingTx(), _RecordingTx()

    fn(rebuild, *args, mode="rebuild")
    fn(update, *args, mode="update")

    assert f"CREATE {pattern}" in rebuild.text
    assert "MERGE" not in rebuild.text
    assert f"MERGE {pattern}" in update.text
    assert "CREATE" not in update.text


def test_ontology_concepts_create_in_rebuild_only():
    """The first of the three statements in `_sync_concepts`, with no chains."""
    rebuild, update = _RecordingTx(), _RecordingTx()

    _sync_concepts(rebuild, [], _concepts(), "Concept", mode="rebuild")
    _sync_concepts(update, [], _concepts(), "Concept", mode="update")

    assert "CREATE (c:Concept {name: row.name})" in rebuild.text
    assert "MERGE (c:Concept {name: row.name})" in update.text


def test_the_default_mode_is_the_old_behaviour():
    """Anything that calls these without saying `mode` must be unaffected.

    Both backends share these functions (`arcadedb.py` reaches them through
    `_CypherRunner`), so a default of `rebuild` would change Neo4j's behaviour
    for every caller that predates this parameter.
    """
    tx = _RecordingTx()

    _sync_items(tx, [{"item_id": "i1"}])
    _sync_sources(tx, [{"bibtex": "s1"}])
    _sync_concepts(tx, [], _concepts(), "Concept")

    assert "CREATE" not in tx.text
    assert tx.text.count("MERGE") == 3


# ---------------------------------------------------------------------------
# Where `MERGE` is load-bearing and must survive rebuild
# ---------------------------------------------------------------------------


def test_chain_statements_keep_merge_in_both_modes():
    """302,392 endpoints -> 22,553 concepts. `CREATE` here multiplies by ~13."""
    for mode in ("rebuild", "update"):
        tx = _RecordingTx()

        _sync_concepts(tx, _chains(), [], "Concept", mode=mode)

        assert "MERGE (s:Concept {name: row.source})" in tx.text, mode
        assert "MERGE (t:Concept {name: row.target})" in tx.text, mode
        assert "CREATE" not in tx.text, mode


def test_taxonomies_keep_merge_in_both_modes():
    """A taxonomy value is shared by many concepts -- the dedup is the point.

    `_sync_taxonomies` takes no `mode` at all, which is the strongest form this
    guarantee can take. The test drives it in a rebuild-shaped call anyway, to
    fail if someone later threads a mode through it.
    """
    tx = _RecordingTx()

    _sync_taxonomies(tx, _concepts(), ["topic", "aspect"], "Concept")

    assert "CREATE" not in tx.text
    assert "MERGE (t:Topic {name: val})" in tx.text


def test_edge_statements_never_create():
    """Edges resolve their endpoints by MATCH; `CREATE` would duplicate them.

    Covers every statement a full rebuild issues, in one sweep, so a future
    `_write_clause` applied to the wrong line fails here even if no one thought
    to add a test for that particular edge.
    """
    tx = _RecordingTx()

    _sync_concepts(tx, _chains(), _concepts(), "Concept", mode="rebuild")

    creates = [q for q in tx.queries if "CREATE" in q]
    assert len(creates) == 1, "only the ontology-concept statement may CREATE"
    assert "(c:Concept {name: row.name})" in creates[0]


def test_relates_to_keeps_merge_in_rebuild():
    tx = _RecordingTx()

    _sync_concepts(tx, _chains(), [], "Concept", mode="rebuild")

    assert "MERGE (s)-[r:RELATES_TO {type: row.type}]->(t)" in tx.text


# ---------------------------------------------------------------------------
# The invariant that makes `CREATE` sound: both backends clear first
# ---------------------------------------------------------------------------


def test_execute_sync_transaction_asks_for_rebuild_on_both_backends():
    """`clear_database` runs immediately before, in both `sync_to_*` functions.

    If that ordering is ever broken, `rebuild` stops being safe -- so this pins
    that the two facts stay together rather than only pinning the mode.
    """
    import inspect

    from synesis_graph.backends import arcadedb, neo4j

    for mod, sync_fn in (
        (neo4j, "sync_to_neo4j"),
        (arcadedb, "sync_to_arcadedb"),
    ):
        signature = inspect.signature(mod._execute_sync_transaction)
        assert signature.parameters["mode"].default == "rebuild", mod.__name__

        src = inspect.getsource(getattr(mod, sync_fn))
        clear_at = src.index("clear_database")
        sync_at = src.index("_execute_sync_transaction")
        assert clear_at < sync_at, f"{sync_fn} must clear before it syncs"


# ---------------------------------------------------------------------------
# Against the real engine
# ---------------------------------------------------------------------------

pytest.importorskip(
    "arcadedb_embedded", reason="arcadedb-embedded unavailable on this platform"
)


def _count(client, label: str) -> int:
    return client.query(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]


def test_a_rebuild_creates_no_duplicates_in_a_real_database(tmp_path):
    """3,000 items through `CREATE`, counted back from the engine.

    Counting from the engine rather than the payload is the point: a mode bug
    leaves the payload untouched and only the graph wrong.
    """
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import _CypherRunner, sync_to_arcadedb
    from synesis_graph.backends.neo4j import _sync_from_source
    from tests.conftest import _make_payload

    n = 3_000
    items = [{"item_id": f"i{i}", "citation": f"excerpt {i}"} for i in range(n)]
    sources = [{"bibtex": "src1", "title": "Only source"}]
    from_source = [{"item_id": f"i{i}", "ref": "src1"} for i in range(n)]

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert sync_to_arcadedb(client, _make_payload()) is None

        tx = _CypherRunner(client)
        client.begin()
        _sync_sources(tx, sources, batch_size=500, mode="rebuild")
        _sync_items(tx, items, batch_size=500, mode="rebuild")
        _sync_from_source(tx, from_source, batch_size=500)
        client.commit()

        assert _count(client, "Item") == n
        assert _count(client, "Source") == 1
        assert (
            client.query("MATCH ()-[r:FROM_SOURCE]->() RETURN count(r) AS n")[0]["n"] == n
        ), "edges still resolve their endpoints by MATCH after a CREATE load"


def test_two_consecutive_rebuilds_produce_the_same_graph(tmp_path):
    """Idempotence comes from clear + create, not from `MERGE`.

    This is the property `MERGE` used to provide and `CREATE` does not. It now
    rests entirely on `clear_database`, so it needs its own test: without the
    clear, the second run would double every count.
    """
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import sync_to_arcadedb
    from tests.conftest import _make_payload

    payload = _make_payload(
        sources=[{"bibtex": "src1", "title": "Only source"}],
        items=[{"item_id": f"i{i}", "citation": f"excerpt {i}"} for i in range(50)],
        from_source=[{"item_id": f"i{i}", "ref": "src1"} for i in range(50)],
        concepts=_concepts(10),
        chains=_chains(10),
        mentions=[
            {"item_id": f"i{i}", "concept": f"c{i % 10}", "mention_order": 0}
            for i in range(50)
        ],
    )
    labels = ("Item", "Source", payload.concept_label, "Topic")

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        def snapshot() -> dict[str, int]:
            counts = {label: _count(client, label) for label in labels}
            for rel in ("FROM_SOURCE", "MENTIONS", "RELATES_TO", "GROUPED_BY"):
                counts[rel] = client.query(
                    f"MATCH ()-[r:{rel}]->() RETURN count(r) AS n"
                )[0]["n"]
            return counts

        assert sync_to_arcadedb(client, payload) is None
        first = snapshot()

        assert sync_to_arcadedb(client, payload) is None
        second = snapshot()

    assert first == second, "a second rebuild must not duplicate anything"
    assert first["Item"] == 50, "the fixture must actually write items"
    assert first["Concept"] == 10, "chains must not multiply the ontology concepts"
    assert first["FROM_SOURCE"] == 50
