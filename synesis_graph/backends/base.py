"""BackendAdapter ABC and Neo4jBackendAdapter."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from synesis_graph.arcadedb_client import ArcadeDBClient, ArcadeDBError
from synesis_graph.backends.arcadedb import clear_database as arcadedb_clear_database
from synesis_graph.backends.arcadedb import sync_to_arcadedb
from synesis_graph.backends.neo4j import sync_to_neo4j
from synesis_graph.config import (
    BACKEND_ARCADEDB,
    BACKEND_NEO4J,
    ArcadeDBConfig,
    Neo4jConfig,
    ensure_database_exists,
)
from synesis_graph.core import (
    ConnectionError,
    DependencyError,
    GraphPayload,
    PipelineError,
    SyncError,
    get_neo4j_driver_factory,
)
from synesis_graph.embeddings import EmbeddingsSidecar
from synesis_graph.metrics import compute_metrics
from synesis_graph.metrics_arcadedb import compute_metrics as compute_arcadedb_metrics
from synesis_graph.sanitize import sanitize_arcadedb_database_name, sanitize_database_name
from synesis_graph.ui import TaskReporter

logger = logging.getLogger("synesis2graph")


def _notification_filter() -> dict[str, Any]:
    """Driver kwargs limiting server notifications to actual warnings.

    Filtering server-side means the INFORMATION notices are never sent at all,
    which is cheaper than logging them and discarding them. Under `-v` the
    filter is lifted so debugging still sees everything the server has to say.

    Returns an empty dict when the driver is absent or too old to know the
    option, so connecting never fails over a cosmetic setting.
    """
    try:
        from neo4j import NotificationMinimumSeverity
    except Exception:
        return {}

    verbose = logging.getLogger("synesis2graph").isEnabledFor(logging.DEBUG)
    severity = (
        NotificationMinimumSeverity.INFORMATION if verbose else NotificationMinimumSeverity.WARNING
    )
    return {"notifications_min_severity": severity}


# ============================================================================
# BACKEND ADAPTERS (Phase 3)
# ============================================================================
class BackendAdapter(ABC):
    """Contract for backend-specific persistence and metrics operations."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Human-readable backend name."""
        raise NotImplementedError

    @abstractmethod
    def preflight(self, reporter: TaskReporter) -> PipelineError | None:
        """Runs backend checks that should happen before compilation."""
        raise NotImplementedError

    @abstractmethod
    def connect(self, reporter: TaskReporter) -> PipelineError | None:
        """Opens backend connection resources."""
        raise NotImplementedError

    @abstractmethod
    def prepare_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        """Prepares destination structures before synchronization."""
        raise NotImplementedError

    @abstractmethod
    def clear_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        """Clears existing destination data when required."""
        raise NotImplementedError

    @abstractmethod
    def synchronize_payload(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        """Writes payload data to the destination backend."""
        raise NotImplementedError

    @abstractmethod
    def compute_backend_metrics(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        """Calculates backend-specific metrics."""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """Releases open backend resources."""
        raise NotImplementedError


class Neo4jBackendAdapter(BackendAdapter):
    """Neo4j backend implementation bound to the BackendAdapter contract."""

    def __init__(self, config: Neo4jConfig):
        self.config = config
        self.driver: Any = None
        self.session: Any = None
        self.db_name = "neo4j"

    @property
    def backend_name(self) -> str:
        return BACKEND_NEO4J

    def preflight(self, reporter: TaskReporter) -> PipelineError | None:
        return None

    def connect(self, reporter: TaskReporter) -> PipelineError | None:
        driver_factory = get_neo4j_driver_factory()
        if driver_factory is None:
            return DependencyError(
                message="Neo4j dependency is missing",
                stage="dependency",
                details="Install with: pip install neo4j",
            )

        try:
            reporter.info(f"Connecting to {self.config.uri}")
            self.driver = driver_factory.driver(
                self.config.uri,
                auth=(self.config.user, self.config.password),
                # Ask the server for warnings only. The audience is qualitative
                # researchers, and INFORMATION notifications are addressed to
                # database engineers: a re-export legitimately drops indexes that
                # the preceding wipe already removed, and Neo4j narrates each one
                # as `Neo.ClientNotification.Schema.IndexOrConstraintDoesNotExist`.
                # Nothing is wrong, so nothing should be printed. Real problems
                # arrive as WARNING and still surface.
                **_notification_filter(),
            )
            return None
        except Exception as e:
            return ConnectionError(
                message="Failed to connect to Neo4j",
                stage="connection",
                details=str(e),
            )

    def prepare_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        if self.driver is None:
            return ConnectionError(
                message="Neo4j connection not initialized",
                stage="connection",
            )

        requested_name = sanitize_database_name(payload.project_name)
        reporter.info(f"Target database: {requested_name}")

        with reporter.step("Checking/Creating Database"):
            self.db_name, db_error = ensure_database_exists(
                self.driver, requested_name, reporter, default_database=self.config.database
            )
            if db_error:
                return db_error

        try:
            self.session = self.driver.session(database=self.db_name)
            return None
        except Exception as e:
            return ConnectionError(
                message="Failed to open Neo4j session",
                stage="connection",
                details=str(e),
            )

    def clear_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        # Neo4j clearing is currently performed inside sync_to_neo4j.
        return None

    def synchronize_payload(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        if self.session is None:
            return ConnectionError(
                message="Neo4j session not initialized",
                stage="connection",
            )

        with reporter.step("Synchronizing Graph (Transactional)"):
            sync_error = sync_to_neo4j(self.session, payload, self.config.fulltext_analyzer)
            if sync_error:
                return sync_error
        return None

    def compute_backend_metrics(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        if self.session is None:
            return ConnectionError(
                message="Neo4j session not initialized",
                stage="connection",
            )
        try:
            compute_metrics(self.session, payload, reporter)
            return None
        except Exception as e:
            return SyncError(
                message="Metrics calculation failed",
                stage="metrics",
                details=str(e),
            )

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
        if self.driver is not None:
            self.driver.close()
            self.driver = None


class ArcadeDBBackendAdapter(BackendAdapter):
    """ArcadeDB backend implementation bound to the BackendAdapter contract.

    Three steps differ from the Neo4j adapter, all found while piloting face85:

    - `preflight` can actually check the server, because ArcadeDB answers a
      readiness probe without authentication. Failing here means failing before the
      project is compiled, rather than after.
    - `prepare_destination` creates the database with a server command; ArcadeDB has
      no `CREATE DATABASE` statement and no notion of a default database to fall
      back on.
    - `clear_destination` does real work. In the Neo4j adapter it is a no-op because
      `sync_to_neo4j` clears internally; keeping that here would hide the
      backend-specific index dropping that a changed analyzer depends on.
    """

    def __init__(self, config: ArcadeDBConfig):
        self.config = config
        self.client: ArcadeDBClient | None = None
        self.db_name = config.database or ""
        # Set by the pipeline when --vector-embeddings (or the config block) asks
        # for them. Kept off the BackendAdapter contract because no other backend
        # has anywhere to put a vector yet.
        self.embeddings: EmbeddingsSidecar | None = None

    @property
    def backend_name(self) -> str:
        return BACKEND_ARCADEDB

    def preflight(self, reporter: TaskReporter) -> PipelineError | None:
        """Checks that the server is reachable before anything expensive happens."""
        probe = ArcadeDBClient(
            uri=self.config.uri,
            user=self.config.user,
            password=self.config.password,
        )
        if not probe.is_ready():
            return ConnectionError(
                message="ArcadeDB server is not reachable",
                stage="preflight",
                details=(
                    f"No response from {self.config.uri}. Check that the server is "
                    f"running and that the URI is the HTTP endpoint (not bolt://)."
                ),
            )
        return None

    def connect(self, reporter: TaskReporter) -> PipelineError | None:
        """Opens the client and verifies the credentials.

        `is_ready` in preflight is unauthenticated, so a wrong password would only
        surface later, in the middle of the sync. Listing databases is the cheapest
        call that actually exercises the credentials.
        """
        try:
            reporter.info(f"Connecting to {self.config.uri}")
            self.client = ArcadeDBClient(
                uri=self.config.uri,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database or None,
            )
            self.client.list_databases()
            return None
        except ArcadeDBError as e:
            self.client = None
            return ConnectionError(
                message="Failed to connect to ArcadeDB",
                stage="connection",
                details=str(e),
            )

    def prepare_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        if self.client is None:
            return ConnectionError(
                message="ArcadeDB connection not initialized",
                stage="connection",
            )

        # An explicit `database` in the config wins; otherwise the project names it,
        # matching the Neo4j adapter's behaviour.
        requested = self.config.database or sanitize_arcadedb_database_name(payload.project_name)
        self.db_name = requested
        self.client.database = requested
        reporter.info(f"Target database: {requested}")

        with reporter.step("Checking/Creating Database"):
            try:
                self.client.create_database(requested)
            except ArcadeDBError as e:
                return SyncError(
                    message="Failed to create database",
                    stage="database_setup",
                    details=str(e),
                )
        return None

    def clear_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        """Drops indexes and data so the compiler stays the source of truth.

        Unlike Neo4j, this is not folded into the sync: dropping the indexes is what
        makes a changed `fulltext_analyzer` take effect, since ArcadeDB would
        otherwise keep the existing index and report success.
        """
        if self.client is None:
            return ConnectionError(
                message="ArcadeDB connection not initialized",
                stage="connection",
            )
        try:
            arcadedb_clear_database(self.client)
            return None
        except ArcadeDBError as e:
            return SyncError(
                message="Failed to clear database",
                stage="clear",
                details=str(e),
            )

    def synchronize_payload(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        if self.client is None:
            return ConnectionError(
                message="ArcadeDB connection not initialized",
                stage="connection",
            )

        label = "Synchronizing Graph (Transactional)"
        if self.embeddings is not None and self.embeddings.vectors:
            label = f"{label} + {len(self.embeddings.vectors)} vectors"

        with reporter.step(label):
            sync_error = sync_to_arcadedb(
                self.client, payload, self.config.fulltext_analyzer, self.embeddings
            )
            if sync_error:
                return sync_error
        return None

    def compute_backend_metrics(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        if self.client is None:
            return ConnectionError(
                message="ArcadeDB connection not initialized",
                stage="connection",
            )
        try:
            compute_arcadedb_metrics(self.client, payload, reporter)
            return None
        except Exception as e:
            return SyncError(
                message="Metrics calculation failed",
                stage="metrics",
                details=str(e),
            )

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


# ============================================================================
# HTML BACKEND
# ============================================================================
