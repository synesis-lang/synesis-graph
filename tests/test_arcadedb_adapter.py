"""Tests for ArcadeDBBackendAdapter — the BackendAdapter contract.

The client is faked, so these cover the orchestration: what each contract method
does, in which order, and how failures are reported. The statements themselves are
covered by `test_arcadedb_sync.py`.
"""

from __future__ import annotations

import os

import pytest

from synesis_graph.arcadedb_client import ArcadeDBClient, ArcadeDBError
from synesis_graph.backends.base import ArcadeDBBackendAdapter
from synesis_graph.config import BACKEND_ARCADEDB, ArcadeDBConfig
from synesis_graph.core import ConnectionError, SourceFieldSpec, SyncError
from synesis_graph.sanitize import sanitize_arcadedb_database_name


class DummyReporter:
    """Reporter that records what the adapter told the user."""

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
                return self

            def fail(self, detail: str = "") -> None:
                # Mirrors the real `_StepContext`: callers that report failure by
                # returning an error must be able to say so, or the step would
                # claim success. A double without this hides that bug.
                reporter.messages.append(("step_failed", detail))

            def __exit__(self, *exc):
                return False

        return _Step()

    def texts(self) -> str:
        return " ".join(m for _, m in self.messages)


class FakeClient:
    """Stand-in for ArcadeDBClient, recording calls."""

    def __init__(self):
        self.database: str | None = None
        self.created: list[str] = []
        self.closed = False
        self.statements: list[str] = []
        self.existing: list[str] = []
        self.fail_on: str | None = None

    def is_ready(self):
        return True

    def list_databases(self):
        if self.fail_on == "list_databases":
            raise ArcadeDBError("User/Password not valid", status=403)
        return list(self.existing)

    def database_exists(self, database=None):
        return (database or self.database) in self.existing

    def create_database(self, database=None):
        if self.fail_on == "create_database":
            raise ArcadeDBError("cannot create")
        name = database or self.database
        if name not in self.existing:
            self.existing.append(name)
        self.created.append(name)

    def command(
        self, statement, params=None, *, language="cypher", database=None, limit=None
    ):
        if self.fail_on and self.fail_on in statement:
            raise ArcadeDBError(f"forced failure: {self.fail_on}")
        self.statements.append(statement)
        return []

    def query(
        self, statement, params=None, *, language="cypher", database=None, limit=None
    ):
        self.statements.append(statement)
        return []

    def begin(self, database=None):
        pass

    def commit(self, database=None):
        pass

    def rollback(self, database=None):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def config() -> ArcadeDBConfig:
    return ArcadeDBConfig(uri="http://localhost:2480", user="root", password="pw")


@pytest.fixture
def adapter(config) -> ArcadeDBBackendAdapter:
    return ArcadeDBBackendAdapter(config)


@pytest.fixture
def connected(adapter):
    """Adapter with a fake client already installed."""
    adapter.client = FakeClient()
    return adapter


# ---------------------------------------------------------------------------
# Contract basics
# ---------------------------------------------------------------------------
def test_backend_name(adapter):
    assert adapter.backend_name == BACKEND_ARCADEDB


def test_preflight_passes_when_server_answers(adapter, monkeypatch):
    monkeypatch.setattr(ArcadeDBClient, "is_ready", lambda self: True)
    assert adapter.preflight(DummyReporter()) is None


def test_preflight_fails_before_compilation_when_server_is_down(adapter, monkeypatch):
    """Failing here saves compiling the whole project first."""
    monkeypatch.setattr(ArcadeDBClient, "is_ready", lambda self: False)
    error = adapter.preflight(DummyReporter())
    assert isinstance(error, ConnectionError)
    assert error.stage == "preflight"


def test_preflight_message_mentions_the_http_uri_confusion(adapter, monkeypatch):
    """bolt:// in the config is the likely mistake; say so."""
    monkeypatch.setattr(ArcadeDBClient, "is_ready", lambda self: False)
    error = adapter.preflight(DummyReporter())
    assert "bolt://" in (error.details or "")


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------
def test_connect_creates_a_client(adapter, monkeypatch):
    monkeypatch.setattr(ArcadeDBClient, "list_databases", lambda self: [])
    assert adapter.connect(DummyReporter()) is None
    assert adapter.client is not None


def test_connect_verifies_credentials(adapter, monkeypatch):
    """is_ready is unauthenticated, so a bad password must surface here — not
    halfway through the sync."""

    def _fail(self):
        raise ArcadeDBError("User/Password not valid", status=403)

    monkeypatch.setattr(ArcadeDBClient, "list_databases", _fail)
    error = adapter.connect(DummyReporter())
    assert isinstance(error, ConnectionError)
    assert error.stage == "connection"
    assert adapter.client is None


# ---------------------------------------------------------------------------
# prepare_destination
# ---------------------------------------------------------------------------
def test_prepare_creates_the_database_named_after_the_project(connected, minimal_payload):
    minimal_payload.project_name = "face85"
    assert connected.prepare_destination(minimal_payload, DummyReporter()) is None
    assert connected.client.created == ["face85"]
    assert connected.client.database == "face85"


def test_prepare_sanitizes_the_project_name(connected, minimal_payload):
    minimal_payload.project_name = "my project/name"
    connected.prepare_destination(minimal_payload, DummyReporter())
    assert connected.client.created == ["my_project_name"]


def test_prepare_keeps_underscores_and_case(connected, minimal_payload):
    """ArcadeDB accepts them, and the project name is what the user recognises."""
    minimal_payload.project_name = "Quinto_Andar"
    connected.prepare_destination(minimal_payload, DummyReporter())
    assert connected.client.created == ["Quinto_Andar"]


def test_explicit_database_in_config_wins(config, minimal_payload):
    config.database = "chosen"
    adapter = ArcadeDBBackendAdapter(config)
    adapter.client = FakeClient()
    minimal_payload.project_name = "ignored"
    adapter.prepare_destination(minimal_payload, DummyReporter())
    assert adapter.client.created == ["chosen"]


def test_prepare_is_idempotent_for_an_existing_database(connected, minimal_payload):
    connected.client.existing = ["face85"]
    minimal_payload.project_name = "face85"
    assert connected.prepare_destination(minimal_payload, DummyReporter()) is None


def test_prepare_without_connection_is_an_error(adapter, minimal_payload):
    error = adapter.prepare_destination(minimal_payload, DummyReporter())
    assert isinstance(error, ConnectionError)


def test_prepare_reports_a_creation_failure(connected, minimal_payload):
    connected.client.fail_on = "create_database"
    error = connected.prepare_destination(minimal_payload, DummyReporter())
    assert isinstance(error, SyncError)
    assert error.stage == "database_setup"


# ---------------------------------------------------------------------------
# clear_destination — real work here, unlike the Neo4j adapter
# ---------------------------------------------------------------------------
def test_clear_destination_actually_clears(connected, minimal_payload):
    assert connected.clear_destination(minimal_payload, DummyReporter()) is None
    assert any("DETACH DELETE" in s for s in connected.client.statements)


def test_clear_destination_drops_indexes(connected, minimal_payload):
    """Dropping indexes is what lets a changed analyzer take effect."""
    connected.clear_destination(minimal_payload, DummyReporter())
    assert any("schema:indexes" in s for s in connected.client.statements)


def test_clear_without_connection_is_an_error(adapter, minimal_payload):
    error = adapter.clear_destination(minimal_payload, DummyReporter())
    assert isinstance(error, ConnectionError)


# ---------------------------------------------------------------------------
# synchronize_payload
# ---------------------------------------------------------------------------
def test_synchronize_writes_the_payload(connected, minimal_payload):
    assert connected.synchronize_payload(minimal_payload, DummyReporter()) is None
    # CREATE, not MERGE: the adapter clears before syncing, so it runs in
    # rebuild mode. See test_sync_mode.py.
    assert any("CREATE (s:Source" in s for s in connected.client.statements)


def test_synchronize_passes_the_configured_analyzer(config, minimal_payload):
    config.fulltext_analyzer = "brazilian"
    adapter = ArcadeDBBackendAdapter(config)
    adapter.client = FakeClient()
    adapter.synchronize_payload(minimal_payload, DummyReporter())
    assert any(
        "org.apache.lucene.analysis.br.BrazilianAnalyzer" in s
        for s in adapter.client.statements
    )


def test_synchronize_reports_failure_as_sync_error(connected, minimal_payload):
    connected.client.fail_on = "(s:Source {bibtex"
    error = connected.synchronize_payload(minimal_payload, DummyReporter())
    assert isinstance(error, SyncError)


def test_synchronize_without_connection_is_an_error(adapter, minimal_payload):
    error = adapter.synchronize_payload(minimal_payload, DummyReporter())
    assert isinstance(error, ConnectionError)


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------
def test_metrics_run_and_report_the_scope_caveat(connected, minimal_payload):
    """The scores are not comparable to Neo4j's; saying so is part of the export.

    The terminal carries the plain-language wording — the `algo.*` phrasing is
    written into `ProjectContext` instead, where the reader is a program.
    """
    reporter = DummyReporter()
    assert connected.compute_backend_metrics(minimal_payload, reporter) is None
    assert any("not directly comparable" in message for _, message in reporter.messages)


def test_metrics_without_connection_is_an_error(adapter, minimal_payload):
    error = adapter.compute_backend_metrics(minimal_payload, DummyReporter())
    assert isinstance(error, ConnectionError)


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------
def test_close_releases_the_client(connected):
    client = connected.client
    connected.close()
    assert client.closed is True
    assert connected.client is None


def test_close_is_safe_without_a_connection(adapter):
    adapter.close()  # must not raise


def test_full_cycle_closes_resources(config, minimal_payload):
    """Mirrors the Neo4j adapter smoke test: the pipeline always calls close()."""
    from synesis_graph.pipeline import execute_backend_pipeline

    adapter = ArcadeDBBackendAdapter(config)
    fake = FakeClient()
    adapter.connect = lambda reporter: setattr(adapter, "client", fake)  # type: ignore

    error = execute_backend_pipeline(adapter, minimal_payload, DummyReporter())

    assert error is None
    assert fake.closed is True


# ---------------------------------------------------------------------------
# Database name sanitization
# ---------------------------------------------------------------------------
class TestSanitizeArcadeDBDatabaseName:
    def test_preserves_underscores_and_case(self):
        """Neo4j's rules would degrade this to `quinto-andar`."""
        assert sanitize_arcadedb_database_name("Quinto_Andar") == "Quinto_Andar"

    def test_replaces_path_separators(self):
        """A slash makes the server create a stray directory under databases/."""
        assert "/" not in sanitize_arcadedb_database_name("a/b")

    def test_replaces_spaces(self):
        assert " " not in sanitize_arcadedb_database_name("my project")

    def test_keeps_dots_and_hyphens(self):
        assert sanitize_arcadedb_database_name("face-85.v2") == "face-85.v2"

    def test_prefixes_a_leading_digit(self):
        assert sanitize_arcadedb_database_name("2024corpus").startswith("db_")

    def test_empty_name_falls_back(self):
        assert sanitize_arcadedb_database_name("") == "synesis"

    def test_strips_surrounding_whitespace(self):
        assert sanitize_arcadedb_database_name("  face85  ") == "face85"


# ---------------------------------------------------------------------------
# Integration — skipped unless a live server answers
# ---------------------------------------------------------------------------
def _live_config() -> ArcadeDBConfig | None:
    password = os.environ.get("ARCADEDB_PASSWORD")
    if not password:
        return None
    config = ArcadeDBConfig(
        uri=os.environ.get("ARCADEDB_HTTP_URI", "http://localhost:2480"),
        user=os.environ.get("ARCADEDB_USER", "root"),
        password=password,
        database="synesis_adapter_it",
        fulltext_analyzer="brazilian",
    )
    probe = ArcadeDBClient(uri=config.uri, user=config.user, password=config.password)
    return config if probe.is_ready() else None


live = pytest.mark.skipif(
    _live_config() is None,
    reason="no live ArcadeDB (set ARCADEDB_PASSWORD and start the server)",
)


@live
def test_integration_adapter_runs_the_whole_pipeline(minimal_payload):
    from synesis_graph.pipeline import execute_backend_pipeline

    config = _live_config()
    adapter = ArcadeDBBackendAdapter(config)
    reporter = DummyReporter()

    assert adapter.preflight(reporter) is None
    error = execute_backend_pipeline(adapter, minimal_payload, reporter)

    admin = ArcadeDBClient(
        uri=config.uri, user=config.user, password=config.password
    )
    try:
        assert error is None
        verify = ArcadeDBClient(
            uri=config.uri,
            user=config.user,
            password=config.password,
            database=config.database,
        )
        n = verify.query("MATCH (c:Concept) RETURN count(c) AS v")[0]["v"]
        assert n == len(minimal_payload.concepts)
    finally:
        if admin.database_exists(config.database):
            admin.drop_database(config.database)


@live
def test_integration_rerun_is_idempotent(minimal_payload):
    """A second export over the same database must not trip over its own schema."""
    from synesis_graph.pipeline import execute_backend_pipeline

    config = _live_config()
    admin = ArcadeDBClient(uri=config.uri, user=config.user, password=config.password)
    try:
        for _ in range(2):
            adapter = ArcadeDBBackendAdapter(config)
            assert (
                execute_backend_pipeline(adapter, minimal_payload, DummyReporter())
                is None
            )
        verify = ArcadeDBClient(
            uri=config.uri,
            user=config.user,
            password=config.password,
            database=config.database,
        )
        n = verify.query("MATCH (c:Concept) RETURN count(c) AS v")[0]["v"]
        assert n == len(minimal_payload.concepts)
    finally:
        if admin.database_exists(config.database):
            admin.drop_database(config.database)


# ---------------------------------------------------------------------------
# A template may declare a field named like a structural bibliographic prop
# ---------------------------------------------------------------------------


def test_source_index_props_list_a_repeated_property_once(payload_factory):
    """`title` is prepended structurally AND declarable by the template.

    ArcadeDB does not raise on the repetition the way Neo4j does — it accepts the
    composite and indexes the same column twice — so the defect is silent here.
    The declared capability is derived from this same list, so a duplicate would
    also reach `ProjectContext.fulltext_source_fields` and teach the consumer a
    `SEARCH_INDEX` name that does not match the index actually created.
    """
    from synesis_graph.backends.arcadedb import _source_index_props

    payload = payload_factory(
        source_fields=[
            SourceFieldSpec("title", "TEXT"),
            SourceFieldSpec("headline", "TEXT"),
        ]
    )

    props = _source_index_props(payload)

    assert props.count("title") == 1
    assert len(props) == len(set(props))
    assert "headline" in props


def test_concept_index_props_list_a_repeated_property_once(payload_factory):
    from synesis_graph.backends.arcadedb import _concept_index_props

    payload = payload_factory(scalar_fields=["search_name", "ontology_description"])

    props = _concept_index_props(payload)

    assert props.count("search_name") == 1
    assert len(props) == len(set(props))
