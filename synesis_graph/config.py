"""Backend configuration dataclasses and TOML loaders."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synesis_graph.ui import TaskReporter

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

from synesis_graph.core import (
    DEFAULT_ARCADEDB_ANALYZER,
    DEFAULT_ARCADEDB_URI,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_FULLTEXT_ANALYZER,
    ConnectionError,
    SyncError,
)
from synesis_graph.sanitize import sanitize_database_name

BACKEND_NEO4J = "neo4j"
BACKEND_ARCADEDB = "arcadedb"
BACKEND_HTML = "html"
SUPPORTED_BACKENDS = (BACKEND_NEO4J, BACKEND_ARCADEDB, BACKEND_HTML)


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
    # Lucene analyzer for the full-text indexes. A corpus-language analyzer
    # (`brazilian`, `portuguese`, `english`, ...) adds stemming and accent folding,
    # measurably improving recall: on face85, `governanca` (no cedilla) and
    # `governancas` (plural) only reach `governança_corporativa` under `brazilian`.
    # It lives in the config, not the code, because the right value follows the
    # corpus: `brazilian` suits face85 and would degrade the English factors corpus.
    # Not validated here — `CALL db.index.fulltext.listAvailableAnalyzers()` varies
    # by Neo4j version, so only the server is authoritative.
    fulltext_analyzer: str = DEFAULT_FULLTEXT_ANALYZER


@dataclass
class ArcadeDBConfig:
    """ArcadeDB connection configuration.

    `uri` is the HTTP endpoint, not a BOLT one: the backend talks to ArcadeDB over
    its HTTP/JSON API, which works on a stock installation. (ArcadeDB also speaks
    BOLT, but that plugin has to be enabled on every server start.)
    """

    uri: str = DEFAULT_ARCADEDB_URI
    user: str = "root"
    password: str = ""
    database: str = ""
    # Lucene analyzer for the full-text indexes. Same purpose as Neo4jConfig's field,
    # different vocabulary: Neo4j takes a short name (`brazilian`), ArcadeDB takes the
    # Lucene class (`org.apache.lucene.analysis.br.BrazilianAnalyzer`). Short names are
    # accepted here too and expanded by the sync layer, so a config can be moved between
    # the two backends unchanged.
    # Not validated here — the set of available analyzers depends on the server build.
    fulltext_analyzer: str = DEFAULT_ARCADEDB_ANALYZER
    # [arcadedb.embeddings]. The model is per project on purpose: a Portuguese
    # corpus and an English one have different requirements, and that is a
    # research decision rather than a code one. `embedding_fields` empty means
    # the feature is off unless --vector-embeddings names fields.
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_fields: list[str] = field(default_factory=list)


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


PipelineConfig = Neo4jConfig | ArcadeDBConfig | HTMLConfig


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
            fulltext_analyzer=str(cfg.get("fulltext_analyzer") or DEFAULT_FULLTEXT_ANALYZER),
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


def _load_arcadedb_config(parsed_cfg: dict[str, Any]) -> ArcadeDBConfig | ConnectionError:
    """Loads and validates the [arcadedb] configuration block.

    Only `password` is mandatory. `uri` and `user` have working defaults for a local
    server (`http://localhost:2480`, `root`), and `database` is normally derived from
    the project name — the same rule the Neo4j backend follows.
    """
    try:
        cfg = parsed_cfg["arcadedb"]
    except KeyError:
        return ConnectionError(
            message="Incomplete configuration",
            stage="config",
            details="Missing [arcadedb] section in the configuration file.",
        )

    if not isinstance(cfg, dict):
        return ConnectionError(
            message="Error reading ArcadeDB configuration",
            stage="config",
            details="[arcadedb] must be a table.",
        )

    # Accept 'URI' as well, matching the Neo4j loader's tolerance.
    uri = cfg.get("uri") or cfg.get("URI") or DEFAULT_ARCADEDB_URI

    password = cfg.get("password")
    if password is None:
        return ConnectionError(
            message="Incomplete configuration",
            stage="config",
            details="Required field missing in [arcadedb]: 'password'",
        )

    # [arcadedb.embeddings] is optional in full: absent, the feature is simply
    # off, and no existing config breaks.
    emb = cfg.get("embeddings") or {}
    if not isinstance(emb, dict):
        return ConnectionError(
            message="Error reading ArcadeDB configuration",
            stage="config",
            details="[arcadedb.embeddings] must be a table.",
        )

    raw_fields = emb.get("fields") or []
    if isinstance(raw_fields, str):
        # A bare string is almost certainly meant as one field name; accepting it
        # avoids a confusing "no such field: o" from iterating the characters.
        raw_fields = [raw_fields]
    if not isinstance(raw_fields, list):
        return ConnectionError(
            message="Error reading ArcadeDB configuration",
            stage="config",
            details="[arcadedb.embeddings].fields must be a list of field names.",
        )

    try:
        return ArcadeDBConfig(
            uri=str(uri),
            user=str(cfg.get("user", "root")),
            password=str(password),
            database=str(cfg.get("database") or ""),
            fulltext_analyzer=str(cfg.get("fulltext_analyzer") or DEFAULT_ARCADEDB_ANALYZER),
            embedding_model=str(emb.get("model") or DEFAULT_EMBEDDING_MODEL),
            embedding_fields=[str(f) for f in raw_fields],
        )
    except Exception as e:
        return ConnectionError(
            message="Error reading ArcadeDB configuration",
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

    if backend == BACKEND_ARCADEDB:
        return _load_arcadedb_config(parsed_cfg)

    return ConnectionError(
        message="Unsupported backend in configuration loader",
        stage="backend",
        details=f"Supported backends: {', '.join(SUPPORTED_BACKENDS)}",
    )


def validate_backend_config(config: PipelineConfig, backend: str) -> ConnectionError | None:
    """Validates configuration type against selected backend."""
    if backend == BACKEND_NEO4J and isinstance(config, Neo4jConfig):
        return None
    if backend == BACKEND_ARCADEDB and isinstance(config, ArcadeDBConfig):
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
