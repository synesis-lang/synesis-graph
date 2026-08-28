"""BackendAdapter ABC and Neo4jBackendAdapter."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from synesis_graph.arcadedb_client import ArcadeDBClient, ArcadeDBError
from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
from synesis_graph.arcadedb_transport import ArcadeDBTransport
from synesis_graph.backends.arcadedb import clear_database as arcadedb_clear_database
from synesis_graph.backends.arcadedb import sync_to_arcadedb
from synesis_graph.backends.neo4j import sync_to_neo4j
from synesis_graph.config import (
    BACKEND_ARCADEDB,
    BACKEND_ARCADEDB_EMBEDDED,
    BACKEND_NEO4J,
    ArcadeDBConfig,
    ArcadeDBEmbeddedConfig,
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

        with reporter.step("Building the graph"):
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


class _ArcadeDBAdapterBase(BackendAdapter):
    """What the two ArcadeDB adapters share: everything past the connection.

    The engine is the same whether it is reached over HTTP or run in-process, so
    clearing, syncing and metrics are identical once a transport exists. Only
    getting to that transport differs — `preflight`, `connect` and
    `prepare_destination` are left abstract here and implemented by each subclass.

    This class also answers a question the pipeline has to ask twice: *is this
    adapter an ArcadeDB one?* It asks in order to attach the embeddings sidecar
    and to record the metrics scope caveat, and both apply to either transport —
    same engine, same whole-graph `algo.*` limitation. Without a common base each
    site would name both adapters explicitly, and the rule "I am ArcadeDB" would
    live in two places that must be kept in step. One base is one place.
    """

    #: Set by the pipeline when --vector-embeddings (or the config block) asks for
    #: them. Kept off the BackendAdapter contract because no other backend has
    #: anywhere to put a vector yet.
    embeddings: EmbeddingsSidecar | None

    def __init__(self) -> None:
        self.client: ArcadeDBTransport | None = None
        self.db_name = ""
        self.embeddings = None

    @property
    def _fulltext_analyzer(self) -> str:
        """The analyzer to build full-text indexes with.

        Declared here because `synchronize_payload` needs it and the two configs
        are unrelated types — there is no shared base on the config side, and
        inventing one would be a bigger change than this property.
        """
        raise NotImplementedError

    def _not_connected(self) -> ConnectionError:
        return ConnectionError(
            message="ArcadeDB connection not initialized",
            stage="connection",
        )

    def clear_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        """Drops indexes and data so the compiler stays the source of truth.

        Unlike Neo4j, this is not folded into the sync: dropping the indexes is what
        makes a changed `fulltext_analyzer` take effect, since ArcadeDB would
        otherwise keep the existing index and report success.
        """
        if self.client is None:
            return self._not_connected()
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
            return self._not_connected()

        label = "Building the graph"
        if self.embeddings is not None and self.embeddings.vectors:
            label = f"{label} (with {len(self.embeddings.vectors)} concept vectors)"

        with reporter.step(label):
            sync_error = sync_to_arcadedb(
                self.client, payload, self._fulltext_analyzer, self.embeddings
            )
            if sync_error:
                return sync_error
        return None

    def compute_backend_metrics(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        if self.client is None:
            return self._not_connected()
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
            self.client.close()  # type: ignore[attr-defined]
            self.client = None


class ArcadeDBBackendAdapter(_ArcadeDBAdapterBase):
    """ArcadeDB over HTTP: a server someone else is running.

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
      (It now lives on `_ArcadeDBAdapterBase`, shared with the embedded adapter.)
    """

    #: Narrower than the base's `ArcadeDBTransport`, deliberately.
    #: `prepare_destination` calls `create_database`, which is a server operation
    #: and therefore absent from the transport contract — an in-process database
    #: has nothing to send it to. Declaring the concrete type here keeps that call
    #: type-checked without widening the contract for everyone.
    client: ArcadeDBClient | None

    def __init__(self, config: ArcadeDBConfig):
        super().__init__()
        self.config = config
        self.db_name = config.database or ""

    @property
    def backend_name(self) -> str:
        return BACKEND_ARCADEDB

    @property
    def _fulltext_analyzer(self) -> str:
        return self.config.fulltext_analyzer

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



class ArcadeDBEmbeddedBackendAdapter(_ArcadeDBAdapterBase):
    """ArcadeDB in-process: no server, no port, no Java to install.

    Same engine as the HTTP adapter, reached differently — which is why only the
    three connection steps are implemented here and everything downstream comes
    from `_ArcadeDBAdapterBase`.

    Each of the three is simpler than its HTTP counterpart, and for the same
    reason: there is no server in the picture.

    - `preflight` cannot probe a host, so it checks what can actually fail here —
      that the optional package is installed, and that the root directory can be
      written to. Both are reported before the project is compiled, which for a
      41k-item corpus is the difference between a wasted minute and a wasted hour.
    - `connect` is deferred: the database directory depends on the project name,
      which only `prepare_destination` receives. Opening happens there.
    - `prepare_destination` creates or opens a directory. There is no
      `CREATE DATABASE`, no credential to exercise, and no server to ask.
    """

    def __init__(self, config: ArcadeDBEmbeddedConfig):
        super().__init__()
        self.config = config

    @property
    def backend_name(self) -> str:
        return BACKEND_ARCADEDB_EMBEDDED

    @property
    def _fulltext_analyzer(self) -> str:
        return self.config.fulltext_analyzer

    def preflight(self, reporter: TaskReporter) -> PipelineError | None:
        """Fails early on the two things that can be known before compiling."""
        try:
            import arcadedb_embedded  # noqa: F401
        except ImportError:
            return DependencyError(
                message="The local graph engine is not available",
                stage="dependency",
                details=(
                    "The 'arcadedb-embedded' package ships with synesis-graph. "
                    "Reinstall with: pip install --force-reinstall synesis-graph"
                ),
            )

        root = Path(self.config.db_path)
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return ConnectionError(
                message="The database directory is not writable",
                stage="preflight",
                details=f"{root}: {e}",
            )
        return None

    def connect(self, reporter: TaskReporter) -> PipelineError | None:
        """Nothing to connect to yet.

        The HTTP adapter opens a client here because its target is a server that
        exists independently of the project. An embedded database *is* a
        directory named after the project, and the project name arrives with the
        payload — so opening waits for `prepare_destination`, one step later.
        """
        return None

    def prepare_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        """Opens the project's database, creating it on first export."""
        name = sanitize_arcadedb_database_name(payload.project_name)
        self.db_name = name
        # `database_dir` is the single derivation of the layout the serving side
        # also relies on: <root>/databases/<name>. Deriving it here instead would
        # let the two drift, and the failure mode is silent — a server that finds
        # no databases and still reports success.
        target = self.config.database_dir(name)
        reporter.info(f"Graph location: {target}")

        with reporter.step("Opening the local graph"):
            try:
                self.client = ArcadeDBEmbeddedClient(target, database=name)
            except ArcadeDBError as e:
                return ConnectionError(
                    message="Failed to open the local database",
                    stage="database_setup",
                    details=str(e),
                )
        return None


# ============================================================================
# HTML BACKEND
# ============================================================================
