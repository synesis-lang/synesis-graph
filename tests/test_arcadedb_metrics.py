"""Tests for ArcadeDB graph metrics.

The centre of gravity here is the regression test for the silent-failure mode: two
plausible ways of persisting an `algo.*` result write nothing (or write to the wrong
nodes) while reporting success. A test that only asserts "no error was raised" passes
against both bugs, so these assert on what actually landed on the nodes.
"""

from __future__ import annotations

import os

import pytest

from synesis_graph.arcadedb_client import ArcadeDBClient, ArcadeDBError
from synesis_graph.backends.arcadedb import sync_to_arcadedb
from synesis_graph.metrics_arcadedb import (
    SCOPE_NOTE_SHORT,
    _persist_scores,
    compute_metrics,
)


class DummyReporter:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))

    def success(self, message):
        self.messages.append(("success", message))

    def step(self, _label):
        reporter = self

        class _Step:
            def __enter__(self):
                return reporter

            def __exit__(self, *exc):
                return False

        return _Step()

    def texts(self) -> str:
        return " ".join(m for _, m in self.messages)


class FakeClient:
    """Client that answers algo.* calls and records the writes."""

    def __init__(self, algo_rows=None, concept_rows=None):
        self.statements: list[tuple[str, dict | None]] = []
        self._algo_rows = algo_rows if algo_rows is not None else []
        self._concept_rows = concept_rows if concept_rows is not None else []
        self.fail_on: str | None = None

    def command(self, statement, params=None, *, language="cypher", database=None, limit=None):
        if self.fail_on and self.fail_on in statement:
            raise ArcadeDBError(f"forced failure: {self.fail_on}")
        self.statements.append((statement, params))
        if "CALL algo." in statement:
            return list(self._algo_rows)
        return []

    def query(self, statement, params=None, *, language="cypher", database=None, limit=None):
        self.statements.append((statement, params))
        if "@rid" in statement:
            return list(self._concept_rows)
        if "count(n)" in statement:
            # The vertex count that feeds the duration estimate.
            return [{"n": len(self._concept_rows)}]
        return []

    def writes(self) -> list[tuple[str, dict | None]]:
        return [(s, p) for s, p in self.statements if "SET c." in s]


# ---------------------------------------------------------------------------
# _persist_scores — the silent-failure surface
# ---------------------------------------------------------------------------
class TestPersistScores:
    def test_writes_one_row_per_concept(self):
        client = FakeClient(
            concept_rows=[{"@rid": "#1:0", "name": "a"}, {"@rid": "#1:1", "name": "b"}]
        )
        rows = [{"node": "#1:0", "value": 0.5}, {"node": "#1:1", "value": 0.3}]

        updated = _persist_scores(client, "Chain", rows, "pagerank")

        assert updated == 2
        statement, params = client.writes()[0]
        assert "SET c.pagerank = row.value" in statement
        assert params["rows"] == [
            {"name": "a", "value": 0.5},
            {"name": "b", "value": 0.3},
        ]

    def test_drops_rids_that_are_not_concepts(self):
        """This is the scope filter — algo.* has none of its own.

        On face85 the algorithms return 480 rows for 210 concepts; the rest are
        Item/Source/taxonomy nodes and must not receive concept scores.
        """
        client = FakeClient(concept_rows=[{"@rid": "#1:0", "name": "a"}])
        rows = [
            {"node": "#1:0", "value": 0.5},
            {"node": "#9:7", "value": 0.9},  # an Item
        ]

        updated = _persist_scores(client, "Chain", rows, "pagerank")

        assert updated == 1
        _, params = client.writes()[0]
        assert [r["name"] for r in params["rows"]] == ["a"]

    def test_never_matches_by_id(self):
        """`WHERE id(c) = id(node)` compares a vertex id to a string id and
        degenerates into a cartesian product — measured writing concept scores onto
        Items. The write must key on `name` instead."""
        client = FakeClient(concept_rows=[{"@rid": "#1:0", "name": "a"}])

        _persist_scores(client, "Chain", [{"node": "#1:0", "value": 1.0}], "pagerank")

        statement, _ = client.writes()[0]
        assert "id(" not in statement
        assert "{name: row.name}" in statement

    def test_no_rows_writes_nothing(self):
        client = FakeClient(concept_rows=[{"@rid": "#1:0", "name": "a"}])
        assert _persist_scores(client, "Chain", [], "pagerank") == 0
        assert client.writes() == []

    def test_no_matching_concept_writes_nothing(self):
        client = FakeClient(concept_rows=[])
        assert _persist_scores(client, "Chain", [{"node": "#1:0", "value": 1}], "x") == 0
        assert client.writes() == []

    def test_null_values_are_skipped(self):
        client = FakeClient(concept_rows=[{"@rid": "#1:0", "name": "a"}])
        assert _persist_scores(client, "Chain", [{"node": "#1:0", "value": None}], "x") == 0


# ---------------------------------------------------------------------------
# compute_metrics orchestration
# ---------------------------------------------------------------------------
class TestComputeMetrics:
    def _client(self):
        return FakeClient(
            algo_rows=[{"node": "#1:0", "value": 0.5}],
            concept_rows=[{"@rid": "#1:0", "name": "Resilience"}],
        )

    def test_runs_the_three_algorithms(self, minimal_payload):
        client = self._client()
        compute_metrics(client, minimal_payload, DummyReporter())
        called = " ".join(s for s, _ in client.statements)
        assert "algo.pagerank" in called
        assert "algo.betweenness" in called
        assert "algo.louvain" in called

    def test_runs_native_metrics_too(self, minimal_payload):
        client = self._client()
        compute_metrics(client, minimal_payload, DummyReporter())
        called = " ".join(s for s, _ in client.statements)
        assert "c.degree" in called
        assert "c.mention_count" in called

    def test_never_calls_gds(self, minimal_payload):
        """`gds.*` does not exist in ArcadeDB; algo.* needs no plugin."""
        client = self._client()
        compute_metrics(client, minimal_payload, DummyReporter())
        assert "gds." not in " ".join(s for s, _ in client.statements)

    def test_reports_the_scope_caveat(self, minimal_payload):
        """The terminal gets the plain-language version, not the graph's.

        Two audiences, two texts: `SCOPE_NOTE` goes into `ProjectContext` for a
        program about to rank concepts by these scores, and needs to name
        `algo.*` precisely. The researcher reading the terminal needs to know
        only that these numbers are not comparable with a Neo4j export's.
        """
        reporter = DummyReporter()
        compute_metrics(self._client(), minimal_payload, reporter)

        texts = reporter.texts()
        assert SCOPE_NOTE_SHORT in texts
        assert "algo.*" not in texts, "jargon belongs in the graph, not the terminal"

    def test_one_failing_algorithm_does_not_stop_the_others(self, minimal_payload):
        client = self._client()
        client.fail_on = "algo.betweenness"
        reporter = DummyReporter()

        compute_metrics(client, minimal_payload, reporter)

        assert any("Betweenness failed" in m for _, m in reporter.messages)
        assert "algo.louvain" in " ".join(s for s, _ in client.statements)

    def test_unsafe_concept_label_is_skipped(self, minimal_payload):
        client = self._client()
        minimal_payload.concept_label = "Bad-Label"
        compute_metrics(client, minimal_payload, DummyReporter())
        assert client.statements == []


# ---------------------------------------------------------------------------
# Integration — skipped unless a live server answers
# ---------------------------------------------------------------------------
def _live_client(database=None) -> ArcadeDBClient | None:
    password = os.environ.get("ARCADEDB_PASSWORD")
    if not password:
        return None
    client = ArcadeDBClient(
        uri=os.environ.get("ARCADEDB_HTTP_URI", "http://localhost:2480"),
        user=os.environ.get("ARCADEDB_USER", "root"),
        password=password,
        database=database,
        timeout=120.0,
    )
    return client if client.is_ready() else None


live = pytest.mark.skipif(
    _live_client() is None,
    reason="no live ArcadeDB (set ARCADEDB_PASSWORD and start the server)",
)


@pytest.fixture
def live_db():
    admin = _live_client()
    name = "synesis_metrics_it"
    if admin.database_exists(name):
        admin.drop_database(name)
    admin.create_database(name)
    client = _live_client(name)
    try:
        yield client
    finally:
        client.close()
        admin.drop_database(name)


@live
def test_integration_metrics_actually_land_on_the_nodes(live_db, minimal_payload):
    """The regression test for the silent failure.

    Both discarded approaches return without error while writing nothing, so this
    asserts on the stored values, not on the absence of an exception.
    """
    sync_to_arcadedb(live_db, minimal_payload)
    compute_metrics(live_db, minimal_payload, DummyReporter())

    total = len(minimal_payload.concepts)
    for prop in ("degree", "mention_count", "pagerank", "betweenness", "community"):
        got = live_db.query(
            f"MATCH (c:Concept) WHERE c.{prop} IS NOT NULL RETURN count(c) AS v"
        )[0]["v"]
        assert got == total, f"{prop} landed on {got}/{total} concepts"


@live
def test_integration_scores_do_not_leak_onto_other_labels(live_db, minimal_payload):
    """The cartesian-product bug wrote concept scores onto Item nodes."""
    sync_to_arcadedb(live_db, minimal_payload)
    compute_metrics(live_db, minimal_payload, DummyReporter())

    leaked = live_db.query(
        "MATCH (i:Item) WHERE i.pagerank IS NOT NULL RETURN count(i) AS v"
    )[0]["v"]
    assert leaked == 0


@live
def test_integration_pagerank_values_are_plausible(live_db, minimal_payload):
    """A uniform score would mean the algorithm ran on a graph with no structure."""
    sync_to_arcadedb(live_db, minimal_payload)
    compute_metrics(live_db, minimal_payload, DummyReporter())

    scores = [
        r["v"]
        for r in live_db.query(
            "MATCH (c:Concept) WHERE c.pagerank IS NOT NULL RETURN c.pagerank AS v"
        )
    ]
    assert scores
    assert all(0 < s < 1 for s in scores)
