"""Tests for writing embeddings into ArcadeDB.

The unit tests assert on the emitted statements. The integration tests matter
more than usual here: the study's Etapa 5 hid the project's worst defect behind
a `SET` that looked correct against a fake client and wrote to the wrong nodes
against a real one, so vector persistence is verified end to end whenever a live
server is available.
"""

from __future__ import annotations

import math
import os
from typing import Any

import pytest

from synesis_graph.arcadedb_client import ArcadeDBClient
from synesis_graph.backends.arcadedb import (
    VECTOR_PROPERTY_NAME,
    VECTOR_PROPERTY_TYPE,
    create_vector_schema,
    sync_to_arcadedb,
    sync_vectors,
)
from synesis_graph.core import SyncError
from synesis_graph.embeddings import ConceptText, EmbeddingsSidecar


class FakeClient:
    """Records statements; mirrors the fake in test_arcadedb_sync.py."""

    def __init__(self, query_results: dict[str, list[dict[str, Any]]] | None = None):
        self.statements: list[tuple[str, str, dict | None]] = []
        self.transactions: list[str] = []
        self._query_results = query_results or {}

    def command(self, statement, params=None, *, language="cypher", database=None):
        self.statements.append((language, statement, params))
        return []

    def query(self, statement, params=None, *, language="cypher", database=None):
        self.statements.append((language, statement, params))
        for needle, rows in self._query_results.items():
            if needle in statement:
                return rows
        return []

    def begin(self, database=None):
        self.transactions.append("begin")
        return "AS-fake"

    def commit(self, database=None):
        self.transactions.append("commit")

    def rollback(self, database=None):
        self.transactions.append("rollback")

    def sql(self) -> list[str]:
        return [s for lang, s, _ in self.statements if lang == "sql"]

    def cypher(self) -> list[str]:
        return [s for lang, s, _ in self.statements if lang == "cypher"]


def _sidecar(vectors: dict[str, list[float]], dimensions: int | None = 4):
    sc = EmbeddingsSidecar(fields=["description"])
    sc.model = "fake-model"
    sc.dimensions = dimensions
    for name in vectors:
        sc.concepts[name] = ConceptText(name=name, text=f"text of {name}")
    sc.vectors = dict(vectors)
    return sc


@pytest.fixture
def client() -> FakeClient:
    # count(*) is read back after writing, so the fake must answer it.
    return FakeClient({"count(*)": [{"n": 2}]})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_declares_the_property_before_indexing(client):
    """ArcadeDB refuses to index a property Cypher created implicitly."""
    create_vector_schema(client, "Concept", 384)
    sql = client.sql()
    declare = next(i for i, s in enumerate(sql) if "CREATE PROPERTY" in s)
    index = next(i for i, s in enumerate(sql) if "LSM_VECTOR" in s)
    assert declare < index


def test_property_uses_the_type_arcadedb_accepts(client):
    """Verified against 26.7.3: LIST OF FLOAT / FLOAT[] / LIST are all rejected."""
    create_vector_schema(client, "Concept", 384)
    assert any(f"Concept.{VECTOR_PROPERTY_NAME}" in s for s in client.sql())
    assert any(VECTOR_PROPERTY_TYPE in s for s in client.sql())
    assert VECTOR_PROPERTY_TYPE == "ARRAY_OF_FLOATS"


def test_index_carries_the_model_dimensions(client):
    create_vector_schema(client, "Concept", 384)
    assert any("dimensions: 384" in s for s in client.sql())


def test_index_defaults_to_cosine_and_int8(client):
    create_vector_schema(client, "Concept", 384)
    index = next(s for s in client.sql() if "LSM_VECTOR" in s)
    assert "similarity: 'COSINE'" in index
    assert "quantization: 'INT8'" in index


def test_index_is_idempotent_for_re_export(client):
    create_vector_schema(client, "Concept", 384)
    assert all("IF NOT EXISTS" in s for s in client.sql())


def test_unsafe_label_is_refused(client):
    create_vector_schema(client, "Concept; DROP DATABASE x", 384)
    assert client.sql() == []


def test_unsafe_metadata_is_refused_after_declaring(client):
    """The property is safe to declare; only the interpolated METADATA is not."""
    create_vector_schema(client, "Concept", 384, similarity="COSINE'; DROP")
    assert not any("LSM_VECTOR" in s for s in client.sql())


def test_zero_dimensions_creates_nothing(client):
    create_vector_schema(client, "Concept", 0)
    assert client.sql() == []


# ---------------------------------------------------------------------------
# Writing vectors
# ---------------------------------------------------------------------------


def test_matches_on_the_business_key_never_on_id(client):
    """`WHERE id(c) = id(n)` silently corrupted data in the metrics module."""
    sync_vectors(client, "Concept", {"a": [0.1] * 4})
    written = next(s for s in client.cypher() if "SET" in s)
    assert "{name: row.name}" in written
    assert "id(" not in written


def test_writes_in_one_unwind_not_per_concept(client):
    sync_vectors(client, "Concept", {f"c{i}": [0.1] * 4 for i in range(50)})
    assert len([s for s in client.cypher() if "UNWIND" in s]) == 1


def test_large_corpora_are_batched(client):
    sync_vectors(client, "Concept", {f"c{i}": [0.1] * 4 for i in range(1200)}, batch_size=500)
    unwinds = [p for lang, s, p in client.statements if lang == "cypher" and "UNWIND" in s]
    assert [len(p["rows"]) for p in unwinds] == [500, 500, 200]


def test_reports_what_the_database_actually_holds():
    """Counting the sidecar would hide names that match no concept."""
    client = FakeClient({"count(*)": [{"n": 7}]})
    assert sync_vectors(client, "Concept", {"a": [0.1] * 4}) == 7


def test_empty_vectors_touch_nothing(client):
    assert sync_vectors(client, "Concept", {}) == 0
    assert client.statements == []


def test_unsafe_label_writes_nothing(client):
    assert sync_vectors(client, "bad label!", {"a": [0.1] * 4}) == 0
    assert client.statements == []


# ---------------------------------------------------------------------------
# Integration with the sync pipeline
# ---------------------------------------------------------------------------


def test_sync_without_embeddings_is_unchanged(client, minimal_payload):
    sync_to_arcadedb(client, minimal_payload, "brazilian")
    assert not any("LSM_VECTOR" in s for s in client.sql())


def test_sync_with_embeddings_creates_the_index(minimal_payload):
    client = FakeClient({"count(*)": [{"n": 3}]})
    names = [c["props"]["name"] for c in minimal_payload.concepts]
    sidecar = _sidecar({n: [0.1, 0.2, 0.3, 0.4] for n in names})

    assert sync_to_arcadedb(client, minimal_payload, "brazilian", sidecar) is None
    assert any("LSM_VECTOR" in s for s in client.sql())


def test_vectors_are_written_after_the_nodes_and_index_exist(minimal_payload):
    """MATCH finds nothing if the nodes are not committed, and the index must
    exist before the property it covers is populated."""
    client = FakeClient({"count(*)": [{"n": 3}]})
    names = [c["props"]["name"] for c in minimal_payload.concepts]
    sync_to_arcadedb(client, minimal_payload, "brazilian", _sidecar({n: [0.1] * 4 for n in names}))

    index_at = next(i for i, (_l, s, _p) in enumerate(client.statements) if "LSM_VECTOR" in s)
    set_at = next(
        i
        for i, (lang, s, _p) in enumerate(client.statements)
        if lang == "cypher" and f"SET c.{VECTOR_PROPERTY_NAME}" in s
    )
    readback_at = next(
        i
        for i, (_l, s, _p) in enumerate(client.statements)
        if f"WHERE {VECTOR_PROPERTY_NAME} IS NOT NULL" in s
    )

    assert index_at < set_at < readback_at
    # The sync transaction has closed before any of this runs.
    assert client.transactions == ["begin", "commit"]


def test_sidecar_without_dimensions_is_an_error(client, minimal_payload):
    sidecar = _sidecar({"Resilience": [0.1] * 4}, dimensions=None)
    result = sync_to_arcadedb(client, minimal_payload, "brazilian", sidecar)
    assert isinstance(result, SyncError)
    assert "dimension" in result.message.lower()


def test_width_disagreeing_with_dimensions_is_an_error(client, minimal_payload):
    """A mismatch is not rejected at insert time — it surfaces as wrong neighbours."""
    sidecar = _sidecar({"Resilience": [0.1] * 8}, dimensions=4)
    result = sync_to_arcadedb(client, minimal_payload, "brazilian", sidecar)
    assert isinstance(result, SyncError)
    assert "rebuild-embeddings" in (result.details or "")


def test_empty_sidecar_is_not_an_error(client, minimal_payload):
    assert sync_to_arcadedb(client, minimal_payload, "brazilian", _sidecar({})) is None
    assert not any("LSM_VECTOR" in s for s in client.sql())


# ---------------------------------------------------------------------------
# Integration — live server
# ---------------------------------------------------------------------------


def _live_client(database: str = "") -> ArcadeDBClient | None:
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
    name = "synesis_vector_it"
    if admin.database_exists(name):
        admin.drop_database(name)
    admin.create_database(name)
    client = _live_client(name)
    try:
        yield client
    finally:
        client.close()
        admin.drop_database(name)


def _unit(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values]


@live
def test_integration_vectors_land_on_the_right_concepts(live_db, payload_factory):
    """The check a fake client cannot make: that the data went where it should."""
    concepts = [
        {"props": {"name": "Alpha", "search_name": "Alpha"}, "relations": {}},
        {"props": {"name": "Beta", "search_name": "Beta"}, "relations": {}},
        {"props": {"name": "Gamma", "search_name": "Gamma"}, "relations": {}},
    ]
    payload = payload_factory(concepts=concepts, chains=[], mentions=[])
    vectors = {
        "Alpha": _unit([1.0, 0.0, 0.0, 0.0]),
        "Beta": _unit([0.0, 1.0, 0.0, 0.0]),
        "Gamma": _unit([0.0, 0.0, 1.0, 0.0]),
    }

    assert sync_to_arcadedb(live_db, payload, "brazilian", _sidecar(vectors)) is None

    for name, expected in vectors.items():
        rows = live_db.query(
            f"SELECT {VECTOR_PROPERTY_NAME} AS v FROM Concept WHERE name = :n",
            {"n": name},
            language="sql",
        )
        stored = rows[0]["v"]
        assert len(stored) == 4
        # float32 storage, so exact equality would be wrong to assert.
        assert all(abs(a - b) < 1e-6 for a, b in zip(stored, expected, strict=True))


@live
def test_integration_scores_do_not_leak_onto_other_labels(live_db, payload_factory):
    """Guards the failure mode the metrics module documents: a cartesian product
    writing concept data onto Item nodes."""
    concepts = [{"props": {"name": "Alpha", "search_name": "Alpha"}, "relations": {}}]
    items = [
        {"item_id": "i1", "citation": "quote one", "description": "memo one"},
        {"item_id": "i2", "citation": "quote two", "description": "memo two"},
    ]
    payload = payload_factory(concepts=concepts, items=items, chains=[], mentions=[])

    sync_to_arcadedb(
        live_db, payload, "brazilian", _sidecar({"Alpha": _unit([1.0, 2.0, 3.0, 4.0])})
    )

    leaked = live_db.query(
        f"SELECT count(*) AS n FROM Item WHERE {VECTOR_PROPERTY_NAME} IS NOT NULL",
        language="sql",
    )[0]["n"]
    assert leaked == 0


@live
def test_integration_nearest_neighbour_is_the_query_vector_itself(live_db, payload_factory):
    """Proves the index is queryable and ranks correctly, not merely that it exists."""
    concepts = [
        {"props": {"name": f"C{i}", "search_name": f"C{i}"}, "relations": {}} for i in range(6)
    ]
    payload = payload_factory(concepts=concepts, chains=[], mentions=[])

    vectors = {
        "C0": _unit([1.0, 0.0, 0.0, 0.0]),
        "C1": _unit([0.99, 0.1, 0.0, 0.0]),
        "C2": _unit([0.0, 1.0, 0.0, 0.0]),
        "C3": _unit([0.0, 0.0, 1.0, 0.0]),
        "C4": _unit([0.0, 0.0, 0.0, 1.0]),
        "C5": _unit([-1.0, 0.0, 0.0, 0.0]),
    }
    assert sync_to_arcadedb(live_db, payload, "brazilian", _sidecar(vectors)) is None

    rows = live_db.query(
        f"SELECT expand(vector.neighbors('Concept[{VECTOR_PROPERTY_NAME}]', :v, 3))",
        {"v": vectors["C0"]},
        language="sql",
    )
    ranked = [r["name"] for r in rows]
    assert ranked[0] == "C0"
    assert "C1" in ranked
    assert "C5" not in ranked


@live
def test_integration_reexport_replaces_the_index(live_db, payload_factory):
    """A changed model means changed dimensions; the old index must not survive."""
    concepts = [{"props": {"name": "Alpha", "search_name": "Alpha"}, "relations": {}}]
    payload = payload_factory(concepts=concepts, chains=[], mentions=[])

    sync_to_arcadedb(live_db, payload, "brazilian", _sidecar({"Alpha": [0.5, 0.5, 0.5, 0.5]}))
    wide = _sidecar({"Alpha": [0.25] * 8}, dimensions=8)
    assert sync_to_arcadedb(live_db, payload, "brazilian", wide) is None

    stored = live_db.query(
        f"SELECT {VECTOR_PROPERTY_NAME} AS v FROM Concept WHERE name = 'Alpha'",
        language="sql",
    )[0]["v"]
    assert len(stored) == 8


@live
def test_integration_stale_sidecar_name_does_not_abort_the_sync(live_db, payload_factory):
    """A concept deleted from the ontology but still in the sidecar."""
    concepts = [{"props": {"name": "Alpha", "search_name": "Alpha"}, "relations": {}}]
    payload = payload_factory(concepts=concepts, chains=[], mentions=[])
    sidecar = _sidecar({"Alpha": [0.5] * 4, "Ghost": [0.1] * 4})

    assert sync_to_arcadedb(live_db, payload, "brazilian", sidecar) is None
    total = live_db.query("SELECT count(*) AS n FROM Concept", language="sql")[0]["n"]
    assert total == 1
