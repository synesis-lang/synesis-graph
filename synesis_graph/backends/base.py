"""BackendAdapter ABC and Neo4jBackendAdapter."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from synesis_graph.backends.neo4j import sync_to_neo4j
from synesis_graph.config import (
    BACKEND_NEO4J,
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
from synesis_graph.metrics import compute_metrics
from synesis_graph.sanitize import sanitize_database_name
from synesis_graph.ui import TaskReporter

logger = logging.getLogger("synesis2graph")


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
            sync_error = sync_to_neo4j(self.session, payload)
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




# ============================================================================
# HTML BACKEND
# ============================================================================
