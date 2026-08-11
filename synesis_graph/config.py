"""Backend configuration dataclasses and TOML loaders."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synesis_graph.ui import TaskReporter

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

from synesis_graph.core import ConnectionError, SyncError
from synesis_graph.sanitize import sanitize_database_name

BACKEND_NEO4J = "neo4j"
BACKEND_HTML = "html"
SUPPORTED_BACKENDS = (BACKEND_NEO4J, BACKEND_HTML)


# ============================================================================
# CONFIGURATION
# ============================================================================
@dataclass
class Neo4jConfig:
    """Neo4j connection configuration."""

    uri: str
    user: str
    password: str
    database: str = "neo4j"


@dataclass
class HTMLConfig:
    """HTML graph output configuration."""

    output_path: str = "./graph.html"
    group_by: str | None = None
    min_frequency: int = 0
    min_source_count: int = 0
    max_nodes: int = 0
    max_hyperedges: int = 50
    include_isolated: bool = True


PipelineConfig = Neo4jConfig | HTMLConfig


def _load_neo4j_config(parsed_cfg: dict[str, Any]) -> Neo4jConfig | ConnectionError:
    """Loads and validates Neo4j configuration block."""
    try:
        cfg = parsed_cfg["neo4j"]
        # Accept both 'uri' and 'URI'
        uri = cfg.get("uri") or cfg.get("URI")
        if not uri:
            raise KeyError("'uri'")
        return Neo4jConfig(
            uri=uri,
            user=cfg["user"],
            password=cfg["password"],
            database=cfg.get("database", "neo4j"),
        )
    except KeyError as e:
        return ConnectionError(
            message="Incomplete configuration",
            stage="config",
            details=f"Required field missing in [neo4j]: {e}",
        )
    except Exception as e:
        return ConnectionError(
            message="Error reading Neo4j configuration",
            stage="config",
            details=str(e),
        )



def _load_html_config(parsed_cfg: dict[str, Any]) -> HTMLConfig:
    """Loads HTML configuration block with defaults (all fields optional)."""
    _defaults = HTMLConfig()
    cfg = parsed_cfg.get("html", {})
    return HTMLConfig(
        output_path=str(cfg.get("output_path", _defaults.output_path)),
        group_by=cfg.get("group_by") or None,
        min_frequency=int(cfg.get("min_frequency", _defaults.min_frequency)),
        min_source_count=int(cfg.get("min_source_count", _defaults.min_source_count)),
        max_nodes=int(cfg.get("max_nodes", _defaults.max_nodes)),
        max_hyperedges=int(cfg.get("max_hyperedges", _defaults.max_hyperedges)),
        include_isolated=bool(cfg.get("include_isolated", _defaults.include_isolated)),
    )


def load_config(config_path: Path, backend: str) -> PipelineConfig | ConnectionError:
    """Loads backend-specific configuration from TOML file."""
    if backend == BACKEND_HTML:
        if not config_path.exists():
            return HTMLConfig()
        try:
            parsed_cfg = tomllib.loads(config_path.read_text("utf-8"))
            return _load_html_config(parsed_cfg)
        except Exception:
            return HTMLConfig()

    if not config_path.exists():
        return ConnectionError(
            message="Configuration file not found", stage="config", details=str(config_path)
        )

    try:
        parsed_cfg = tomllib.loads(config_path.read_text("utf-8"))
    except Exception as e:
        return ConnectionError(
            message="Error reading configuration",
            stage="config",
            details=str(e),
        )

    if backend == BACKEND_NEO4J:
        return _load_neo4j_config(parsed_cfg)

    return ConnectionError(
        message="Unsupported backend in configuration loader",
        stage="backend",
        details=f"Supported backends: {', '.join(SUPPORTED_BACKENDS)}",
    )


def validate_backend_config(config: PipelineConfig, backend: str) -> ConnectionError | None:
    """Validates configuration type against selected backend."""
    if backend == BACKEND_NEO4J and isinstance(config, Neo4jConfig):
        return None
    if backend == BACKEND_HTML and isinstance(config, HTMLConfig):
        return None

    return ConnectionError(
        message="Configuration/backend mismatch",
        stage="config",
        details=f"Backend '{backend}' does not match loaded configuration type.",
    )



# ============================================================================
# DATABASE CREATION
# ============================================================================
def ensure_database_exists(
    driver: Any, database_name: str, reporter: TaskReporter, default_database: str = "neo4j"
) -> tuple[str, SyncError | None]:
    """
    Creates the database if it doesn't exist.

    Neo4j Community Edition and single-database Aura instances don't support
    CREATE DATABASE, so this falls back to `default_database` when that happens.
    Neo4j Enterprise/Aura (multi-db tiers) support multiple databases.

    Returns the database name that should actually be used for the session,
    along with an error (if any).
    """
    safe_name = sanitize_database_name(database_name)

    try:
        with driver.session(database="system") as session:
            # Check if database exists
            result = session.run("SHOW DATABASES")
            existing = {record["name"] for record in result}

            if safe_name not in existing:
                reporter.info(f"Creating database: {safe_name}")
                session.run(f"CREATE DATABASE `{safe_name}` IF NOT EXISTS")
                # Wait for database to become available
                import time as _time

                _time.sleep(2)
            else:
                reporter.info(f"Database already exists: {safe_name}")
        return safe_name, None
    except Exception as e:
        # If fails (e.g.: Community Edition, single-db Aura), use default database
        error_msg = str(e)
        if "Unsupported" in error_msg or "not supported" in error_msg.lower():
            reporter.warning(
                f"Multi-database not supported. Using default database '{default_database}'."
            )
            return default_database, None
        return safe_name, SyncError(
            message="Failed to create database", stage="database_setup", details=error_msg
        )


def get_database_name_from_project(json_data: dict[str, Any]) -> str:
    """Extracts project name to use as database name."""
    project_name = json_data.get("project", {}).get("name", "synesis")
    # Sanitize to valid database name (Neo4j only accepts letters, numbers, dots and hyphens)
    return sanitize_database_name(project_name)
