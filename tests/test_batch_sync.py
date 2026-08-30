"""`_run_in_batches` exists to keep a single `UNWIND $rows` from ever holding an
entire corpus. A real sync of 246,588 items against a 2-vCPU/8GB ArcadeDB
container drove the JVM to 205% CPU and 79.7% of its heap; a scaling test
against that same server pinned the failure between 2,000 and 5,000 rows in one
transaction. See arcadedb_batch_sync_study.md for the full investigation.

These tests pin the three properties the helper promises: one call when the
list already fits, no call at all when the list is empty, and — the one a
chunking bug would violate silently — that slicing loses nothing and
duplicates nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from synesis_graph.backends.neo4j import _run_in_batches


class _RecordingTx:
    """Stands in for the Neo4j/ArcadeDB `tx.run` interface, recording calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []

    def run(self, query: str, **params: Any) -> None:
        # `rows` is absent for statements that carry no list — the
        # IS_LINKED_TO aggregation in `_sync_taxonomies` is one. Recording
        # them as None keeps the double honest instead of raising KeyError
        # and hiding that such calls exist.
        self.calls.append((query, params.get("rows")))

    @property
    def row_calls(self) -> list[tuple[str, list[dict[str, Any]]]]:
        """Only the calls that carried a `rows` list — what batching applies to."""
        return [(q, r) for q, r in self.calls if r is not None]


QUERY = "UNWIND $rows AS row MERGE (i:Item {item_id: row.item_id})"


def test_a_list_within_batch_size_makes_exactly_one_call():
    """No fatter than calling `tx.run` directly — nothing extra for Neo4j."""
    tx = _RecordingTx()
    rows = [{"item_id": i} for i in range(500)]

    _run_in_batches(tx, QUERY, rows, batch_size=500)

    assert len(tx.calls) == 1
    assert tx.calls[0] == (QUERY, rows)


def test_a_shorter_list_also_makes_exactly_one_call():
    tx = _RecordingTx()
    rows = [{"item_id": i} for i in range(3)]

    _run_in_batches(tx, QUERY, rows, batch_size=500)

    assert len(tx.calls) == 1
    assert tx.calls[0] == (QUERY, rows)


def test_an_empty_list_makes_no_call_at_all():
    """Matches every `_sync_*` function's own "nothing to do" guard."""
    tx = _RecordingTx()

    _run_in_batches(tx, QUERY, [], batch_size=500)

    assert tx.calls == []


def test_a_longer_list_is_split_into_the_expected_number_of_batches():
    tx = _RecordingTx()
    rows = [{"item_id": i} for i in range(1250)]

    _run_in_batches(tx, QUERY, rows, batch_size=500)

    # ceil(1250 / 500) = 3
    assert len(tx.calls) == 3
    assert [len(batch) for _, batch in tx.calls] == [500, 500, 250]


def test_every_call_uses_the_same_query():
    """A chunked sync must not silently run a different statement per batch."""
    tx = _RecordingTx()
    rows = [{"item_id": i} for i in range(1200)]

    _run_in_batches(tx, QUERY, rows, batch_size=500)

    assert all(query == QUERY for query, _ in tx.calls)


def test_slicing_loses_nothing_and_duplicates_nothing():
    """The property a chunking bug would violate without any test noticing.

    Off-by-one errors in a range/slice loop either drop the last partial batch
    or repeat the boundary row — both leave the union of batches different from
    the original list, which is exactly what this checks, not just the count.
    """
    tx = _RecordingTx()
    rows = [{"item_id": i} for i in range(2001)]  # deliberately not a multiple

    _run_in_batches(tx, QUERY, rows, batch_size=500)

    reassembled = [row for _, batch in tx.calls for row in batch]
    assert reassembled == rows


def test_batches_are_contiguous_and_do_not_overlap():
    """Each row appears in exactly one batch, in original order."""
    tx = _RecordingTx()
    rows = [{"item_id": i} for i in range(777)]

    _run_in_batches(tx, QUERY, rows, batch_size=250)

    seen_ids: list[int] = []
    for _, batch in tx.calls:
        seen_ids.extend(row["item_id"] for row in batch)

    assert seen_ids == list(range(777)), "rows must appear exactly once, in order"


def test_a_batch_size_larger_than_the_list_behaves_like_no_batching():
    """The Neo4j default (a large batch_size) must be a no-op for real corpora."""
    tx = _RecordingTx()
    rows = [{"item_id": i} for i in range(200)]

    _run_in_batches(tx, QUERY, rows, batch_size=50_000)

    assert len(tx.calls) == 1
    assert tx.calls[0][1] == rows


# ---------------------------------------------------------------------------
# The `_sync_*` functions actually route through the helper
#
# The suite above pins `_run_in_batches` in isolation. These pin that each
# corpus-scaled sync function uses it — a function that kept calling `tx.run`
# directly would still pass every test above while sending the whole corpus in
# one statement, which is the failure this work exists to prevent.
# ---------------------------------------------------------------------------


def test_sync_items_batches_and_loses_nothing():
    from synesis_graph.backends.neo4j import _sync_items

    tx = _RecordingTx()
    items = [{"item_id": f"i{i}"} for i in range(1200)]

    _sync_items(tx, items, batch_size=500)

    assert len(tx.calls) == 3
    reassembled = [row for _, batch in tx.calls for row in batch]
    assert reassembled == items


def test_sync_sources_batches_and_loses_nothing():
    from synesis_graph.backends.neo4j import _sync_sources

    tx = _RecordingTx()
    sources = [{"bibtex": f"s{i}"} for i in range(1001)]

    _sync_sources(tx, sources, batch_size=500)

    assert len(tx.calls) == 3
    reassembled = [row for _, batch in tx.calls for row in batch]
    assert reassembled == sources


def test_sync_from_source_batches_and_loses_nothing():
    from synesis_graph.backends.neo4j import _sync_from_source

    tx = _RecordingTx()
    edges = [{"item_id": f"i{i}", "ref": "s0"} for i in range(750)]

    _sync_from_source(tx, edges, batch_size=250)

    assert len(tx.calls) == 3
    reassembled = [row for _, batch in tx.calls for row in batch]
    assert reassembled == edges


def test_sync_mentions_batches_and_loses_nothing():
    from synesis_graph.backends.neo4j import _sync_mentions

    tx = _RecordingTx()
    mentions = [{"item_id": f"i{i}", "concept": "c", "mention_order": 0} for i in range(600)]

    _sync_mentions(tx, mentions, "Chain", batch_size=200)

    assert len(tx.calls) == 3
    reassembled = [row for _, batch in tx.calls for row in batch]
    assert reassembled == mentions


def test_sync_concepts_batches_every_one_of_its_three_statements():
    """Concepts, chain endpoints and RELATES_TO all scale with the corpus."""
    from synesis_graph.backends.neo4j import _sync_concepts

    tx = _RecordingTx()
    concepts = [{"props": {"name": f"c{i}"}} for i in range(300)]
    chains = [
        {"source": f"c{i}", "target": f"c{i+1}", "type": "T", "description": "", "item_id": "i"}
        for i in range(300)
    ]

    _sync_concepts(tx, chains, concepts, "Chain", batch_size=100)

    # 3 statements x 3 batches each
    assert len(tx.calls) == 9
    for start in (0, 3, 6):
        batch_lengths = [len(batch) for _, batch in tx.calls[start : start + 3]]
        assert batch_lengths == [100, 100, 100]


def test_the_default_batch_size_sends_a_real_corpus_in_one_call():
    """Neo4j's behaviour must be unchanged: the default must not slice."""
    from synesis_graph.backends.neo4j import DEFAULT_SYNC_BATCH_SIZE, _sync_items

    tx = _RecordingTx()
    # Larger than the biggest Synesis corpus measured to date (246,588 items)
    # would exceed this, so the constant is checked rather than assumed.
    assert DEFAULT_SYNC_BATCH_SIZE >= 50_000

    _sync_items(tx, [{"item_id": f"i{i}"} for i in range(10_000)])

    assert len(tx.calls) == 1


# ---------------------------------------------------------------------------
# Integration: a batched sync against a real engine must land every row
#
# The unit tests above prove the slicing arithmetic. Only a real database can
# prove that splitting one statement into many still produces the same graph —
# `MERGE` semantics, edge matching across batch boundaries, and the enclosing
# transaction all have to hold.
# ---------------------------------------------------------------------------

pytest.importorskip(
    "arcadedb_embedded", reason="arcadedb-embedded unavailable on this platform"
)


def test_a_batched_sync_lands_every_row_in_a_real_database(tmp_path):
    """3,000 items at batch_size=500 — six batches per statement, one graph.

    The sync functions are driven directly with a small `batch_size` rather than
    through `sync_to_arcadedb`, which still passes the 50,000 default until
    Etapa 2 wires the small value through. Calling them directly is what makes
    this test exercise real slicing today instead of silently sending one batch.

    The counts are read back from the engine rather than from the payload: a
    boundary bug that dropped a batch would leave the payload untouched and
    only the database short.
    """
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import _CypherRunner, sync_to_arcadedb
    from synesis_graph.backends.neo4j import (
        _sync_concepts,
        _sync_from_source,
        _sync_items,
        _sync_mentions,
        _sync_sources,
    )
    from tests.conftest import _make_payload

    n = 3_000
    sources = [{"bibtex": "src1", "title": "Only source"}]
    items = [{"item_id": f"i{i}", "citation": f"excerpt {i}"} for i in range(n)]
    from_source = [{"item_id": f"i{i}", "ref": "src1"} for i in range(n)]
    concepts = [
        {"props": {"name": f"Concept_{i}"}, "relations": {"topic": ["T"]}} for i in range(600)
    ]
    mentions = [
        {"item_id": f"i{i}", "concept": f"Concept_{i % 600}", "mention_order": 0}
        for i in range(n)
    ]

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        # Schema, constraints and indexes come from the real pipeline; an empty
        # payload keeps it from writing the rows this test wants to batch.
        error = sync_to_arcadedb(client, _make_payload())
        assert error is None, error

        tx = _CypherRunner(client)
        client.begin()
        _sync_sources(tx, sources, batch_size=500)
        _sync_items(tx, items, batch_size=500)
        _sync_from_source(tx, from_source, batch_size=500)
        _sync_concepts(tx, [], concepts, "Concept", batch_size=500)
        _sync_mentions(tx, mentions, "Concept", batch_size=500)
        client.commit()

        counts = {
            label: client.query(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]
            for label in ("Item", "Source", "Concept")
        }
        edges = {
            rel: client.query(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS n")[0]["n"]
            for rel in ("FROM_SOURCE", "MENTIONS")
        }

    assert counts["Item"] == n, "every item must survive the batching"
    assert counts["Source"] == 1
    assert counts["Concept"] == 600
    assert edges["FROM_SOURCE"] == n, "edges must match across batch boundaries"
    assert edges["MENTIONS"] == n


# ---------------------------------------------------------------------------
# Etapa A-0: as funções que ficaram de fora da primeira passagem
#
# Encontradas ao confrontar o estudo com o código: `_sync_taxonomies` roda
# sobre 22.585 conceitos x 4 campos de taxonomia sem lote, muito acima do
# ponto de ruptura medido (2.000-5.000 linhas por statement).
# ---------------------------------------------------------------------------


def test_sync_taxonomies_batches_the_concept_to_taxonomy_relations():
    """The 22,585-concept path that the first pass missed."""
    from synesis_graph.backends.neo4j import _sync_taxonomies

    tx = _RecordingTx()
    concepts = [
        {"props": {"name": f"c{i}"}, "relations": {"topic": ["T"]}} for i in range(1200)
    ]

    _sync_taxonomies(tx, concepts, ["topic"], "Concept", batch_size=500)

    assert len(tx.row_calls) == 3
    reassembled = [row["concept"] for _, batch in tx.row_calls for row in batch]
    assert reassembled == [f"c{i}" for i in range(1200)]


def test_sync_taxonomies_batches_each_graph_field_separately():
    """One statement per field, each sliced on its own."""
    from synesis_graph.backends.neo4j import _sync_taxonomies

    tx = _RecordingTx()
    concepts = [
        {"props": {"name": f"c{i}"}, "relations": {"topic": ["T"], "aspect": ["A"]}}
        for i in range(600)
    ]

    _sync_taxonomies(tx, concepts, ["topic", "aspect"], "Concept", batch_size=200)

    # 2 fields x 3 batches, plus the Topic->Aspect mapping (600 rows / 200 = 3)
    assert len(tx.row_calls) == 9
    assert all(len(batch) == 200 for _, batch in tx.row_calls)


def test_sync_entities_batches_per_label():
    from synesis_graph.backends.neo4j import _sync_entities

    tx = _RecordingTx()
    entities = {
        "Researcher": [
            {"entity_id": f"e{i}", "entity": "researcher", "member": "m", "source_bibtex": "s"}
            for i in range(750)
        ]
    }

    _sync_entities(tx, entities, batch_size=250)

    assert len(tx.calls) == 3
    reassembled = [row["entity_id"] for _, batch in tx.calls for row in batch]
    assert reassembled == [f"e{i}" for i in range(750)]


def test_sync_refers_to_batches_per_label():
    from synesis_graph.backends.neo4j import _sync_refers_to

    tx = _RecordingTx()
    edges = {
        "Researcher": [
            {"from_bibtex": "s", "entity_id": f"e{i}", "entity": "researcher", "member": "m"}
            for i in range(500)
        ]
    }

    _sync_refers_to(tx, edges, batch_size=200)

    assert len(tx.calls) == 3
    assert [len(b) for _, b in tx.calls] == [200, 200, 100]


def test_the_two_unbatched_calls_are_deliberate():
    """`_sync_project_context` writes one vertex; IS_LINKED_TO is an aggregation.

    Neither takes a `rows` list, so there is nothing to slice. Pinning this
    keeps a future reader from "fixing" them into a batched form that would be
    wrong — splitting the IS_LINKED_TO aggregation would change the `count(*)`
    of each group.
    """
    import inspect

    from synesis_graph.backends import neo4j as mod

    for fn_name in ("_sync_project_context", "_sync_taxonomies"):
        src = inspect.getsource(getattr(mod, fn_name))
        assert "Sem lote de propósito" in src, f"{fn_name} lost its exception comment"
