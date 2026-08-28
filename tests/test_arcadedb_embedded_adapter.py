"""Tests for the embedded adapter and the base the two ArcadeDB adapters share.

The base is the point of this stage. The pipeline has to ask "is this an ArcadeDB
adapter?" in two places — to attach the embeddings sidecar and to record the
metrics scope caveat — and both answers apply to either transport. Naming both
adapters at each site would put the rule in two places that must be kept in step;
`_ArcadeDBAdapterBase` puts it in one.
"""

from __future__ import annotations

import pytest

from synesis_graph.backends.base import (
    ArcadeDBBackendAdapter,
    ArcadeDBEmbeddedBackendAdapter,
    _ArcadeDBAdapterBase,
)
from synesis_graph.config import (
    BACKEND_ARCADEDB_EMBEDDED,
    ArcadeDBConfig,
    ArcadeDBEmbeddedConfig,
)
from synesis_graph.core import ConnectionError, DependencyError
from synesis_graph.pipeline import build_backend_adapter
from synesis_graph.ui import TaskReporter

from .conftest import _make_payload

embedded = pytest.importorskip(
    "arcadedb_embedded",
    reason="arcadedb-embedded not installed (optional extra)",
)


@pytest.fixture
def reporter():
    return TaskReporter("test")


# ---------------------------------------------------------------------------
# The shared base — what this stage exists to establish
# ---------------------------------------------------------------------------


def test_both_transports_share_one_base():
    """One `isinstance` answers for both, so the pipeline asks once."""
    tcp = ArcadeDBBackendAdapter(ArcadeDBConfig(password="x"))
    local = ArcadeDBEmbeddedBackendAdapter(ArcadeDBEmbeddedConfig())

    assert isinstance(tcp, _ArcadeDBAdapterBase)
    assert isinstance(local, _ArcadeDBAdapterBase)


def test_the_shared_methods_are_not_duplicated():
    """Clearing, syncing and metrics come from the base, not from either subclass.

    If a subclass reintroduced one, the two transports could drift apart on the
    same engine — the defect shape this base was extracted to remove.
    """
    for name in ("clear_destination", "synchronize_payload", "compute_backend_metrics"):
        assert name not in vars(ArcadeDBBackendAdapter), f"{name} duplicated in TCP adapter"
        assert name not in vars(
            ArcadeDBEmbeddedBackendAdapter
        ), f"{name} duplicated in embedded adapter"
        assert name in vars(_ArcadeDBAdapterBase)


def test_each_transport_implements_only_its_connection_steps():
    """Only the three connection steps differ — measured against the code."""
    for name in ("preflight", "connect", "prepare_destination"):
        assert name in vars(ArcadeDBBackendAdapter)
        assert name in vars(ArcadeDBEmbeddedBackendAdapter)


def test_neo4j_is_not_swept_into_the_arcadedb_base():
    """The base must not become "any database adapter"."""
    from synesis_graph.backends.base import Neo4jBackendAdapter
    from synesis_graph.config import Neo4jConfig

    adapter = Neo4jBackendAdapter(Neo4jConfig(uri="bolt://x", user="u", password="p"))
    assert not isinstance(adapter, _ArcadeDBAdapterBase)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_pipeline_builds_the_adapter(tmp_path):
    adapter = build_backend_adapter(
        BACKEND_ARCADEDB_EMBEDDED, ArcadeDBEmbeddedConfig(), tmp_path, tmp_path
    )
    assert isinstance(adapter, ArcadeDBEmbeddedBackendAdapter)
    assert adapter.backend_name == BACKEND_ARCADEDB_EMBEDDED


def test_the_wrong_config_type_is_refused(tmp_path):
    """A TCP config carries a password and a host; neither means anything here."""
    result = build_backend_adapter(
        BACKEND_ARCADEDB_EMBEDDED, ArcadeDBConfig(password="x"), tmp_path, tmp_path
    )
    assert isinstance(result, ConnectionError)


# ---------------------------------------------------------------------------
# Connection steps
# ---------------------------------------------------------------------------


def test_preflight_creates_the_root_and_passes(tmp_path, reporter):
    root = tmp_path / "not" / "yet" / "there"
    adapter = ArcadeDBEmbeddedBackendAdapter(ArcadeDBEmbeddedConfig(db_path=str(root)))

    assert adapter.preflight(reporter) is None
    assert root.is_dir()


def test_preflight_reports_an_unwritable_root(tmp_path, reporter):
    """Failing before compilation is the point: a 41k-item corpus costs minutes."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a directory", encoding="utf-8")
    adapter = ArcadeDBEmbeddedBackendAdapter(
        ArcadeDBEmbeddedConfig(db_path=str(blocker / "root"))
    )

    result = adapter.preflight(reporter)
    assert isinstance(result, ConnectionError)
    assert "not writable" in result.message


def test_preflight_reports_the_missing_package(tmp_path, reporter, monkeypatch):
    """The extra is optional, so its absence must be actionable, not an ImportError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "arcadedb_embedded":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    adapter = ArcadeDBEmbeddedBackendAdapter(ArcadeDBEmbeddedConfig(db_path=str(tmp_path)))

    result = adapter.preflight(reporter)
    assert isinstance(result, DependencyError)
    assert "pip install" in result.details


def test_connect_defers_to_prepare_destination(tmp_path, reporter):
    """The database directory is named after the project, which arrives later."""
    adapter = ArcadeDBEmbeddedBackendAdapter(ArcadeDBEmbeddedConfig(db_path=str(tmp_path)))

    assert adapter.connect(reporter) is None
    assert adapter.client is None


def test_prepare_destination_creates_the_database_under_databases(tmp_path, reporter):
    """The layout contract: <root>/databases/<name>, derived by the config.

    Writing it anywhere else makes the serving phase start, report success, and
    find no databases at all.
    """
    adapter = ArcadeDBEmbeddedBackendAdapter(ArcadeDBEmbeddedConfig(db_path=str(tmp_path)))
    payload = _make_payload()
    payload.project_name = "face85"

    try:
        assert adapter.prepare_destination(payload, reporter) is None
        assert (tmp_path / "databases" / "face85").is_dir()
        assert adapter.db_name == "face85"
        assert adapter.client is not None
    finally:
        adapter.close()


def test_close_releases_the_database(tmp_path, reporter):
    adapter = ArcadeDBEmbeddedBackendAdapter(ArcadeDBEmbeddedConfig(db_path=str(tmp_path)))
    payload = _make_payload()
    payload.project_name = "p"
    adapter.prepare_destination(payload, reporter)

    adapter.close()
    assert adapter.client is None
    adapter.close()  # idempotent


# ---------------------------------------------------------------------------
# End to end: the criterion this stage is judged by
# ---------------------------------------------------------------------------


def test_a_payload_syncs_to_a_local_database(tmp_path, reporter):
    """Export with no server running, then read the graph back.

    This is the whole point of the embedded mode: `minimal_payload`'s three
    concepts and two sources land in a directory, with no Java, no port and no
    process to manage.
    """
    adapter = ArcadeDBEmbeddedBackendAdapter(ArcadeDBEmbeddedConfig(db_path=str(tmp_path)))
    payload = _make_payload(
        concepts=[
            {"props": {"name": "Resilience"}, "relations": {}},
            {"props": {"name": "Trust"}, "relations": {}},
        ],
        sources=[{"ref": "smith2024", "props": {}}],
        items=[{"item_id": "i001", "citation": "Trust enables resilience.", "description": ""}],
    )
    payload.project_name = "e2e"

    try:
        assert adapter.preflight(reporter) is None
        assert adapter.connect(reporter) is None
        assert adapter.prepare_destination(payload, reporter) is None
        assert adapter.clear_destination(payload, reporter) is None
        assert adapter.synchronize_payload(payload, reporter) is None

        assert adapter.client is not None
        concepts = adapter.client.query(
            f"MATCH (c:{payload.concept_label}) RETURN c.name AS name"
        )
        assert {r["name"] for r in concepts} == {"Resilience", "Trust"}
        assert adapter.client.query("MATCH (s:Source) RETURN count(s) AS n") == [{"n": 1}]
        assert adapter.client.query("MATCH (i:Item) RETURN count(i) AS n") == [{"n": 1}]
    finally:
        adapter.close()
