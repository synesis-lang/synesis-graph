"""Tests for config.py — TOML loading, validation, path resolution."""

from __future__ import annotations

from synesis_graph.config import (
    BACKEND_ARCADEDB,
    BACKEND_HTML,
    BACKEND_NEO4J,
    SUPPORTED_BACKENDS,
    ArcadeDBConfig,
    HTMLConfig,
    Neo4jConfig,
    load_config,
    validate_backend_config,
)
from synesis_graph.core import (
    DEFAULT_ARCADEDB_ANALYZER,
    DEFAULT_ARCADEDB_URI,
    ConnectionError,
    resolve_arcadedb_analyzer,
)
from tests.conftest import (
    TOML_ARCADEDB_MINIMAL,
    TOML_ARCADEDB_MISSING_PASSWORD,
    TOML_ARCADEDB_VALID,
    TOML_HTML_CUSTOM,
    TOML_NEO4J_MISSING_PASSWORD,
    TOML_NEO4J_VALID,
)

# ---------------------------------------------------------------------------
# load_config — Neo4j
# ---------------------------------------------------------------------------


class TestLoadConfigNeo4j:
    def test_valid_neo4j_config(self, config_file):
        path = config_file(TOML_NEO4J_VALID)
        result = load_config(path, BACKEND_NEO4J)
        assert isinstance(result, Neo4jConfig)
        assert result.uri == "bolt://127.0.0.1:7687"
        assert result.user == "neo4j"
        assert result.password == "test"

    def test_missing_password_returns_error(self, config_file):
        path = config_file(TOML_NEO4J_MISSING_PASSWORD)
        result = load_config(path, BACKEND_NEO4J)
        assert isinstance(result, ConnectionError)
        assert result.stage == "config"

    def test_missing_file_returns_error(self, tmp_path):
        path = tmp_path / "nonexistent.toml"
        result = load_config(path, BACKEND_NEO4J)
        assert isinstance(result, ConnectionError)
        assert result.stage == "config"

    def test_uri_uppercase_key_accepted(self, config_file):
        toml = """\
[neo4j]
URI = "bolt://127.0.0.1:7687"
user = "neo4j"
password = "test"
"""
        path = config_file(toml)
        result = load_config(path, BACKEND_NEO4J)
        assert isinstance(result, Neo4jConfig)
        assert result.uri == "bolt://127.0.0.1:7687"

    def test_custom_database_field(self, config_file):
        toml = """\
[neo4j]
uri = "bolt://127.0.0.1:7687"
user = "neo4j"
password = "test"
database = "mydb"
"""
        path = config_file(toml)
        result = load_config(path, BACKEND_NEO4J)
        assert isinstance(result, Neo4jConfig)
        assert result.database == "mydb"

    def test_default_database_is_neo4j(self, config_file):
        path = config_file(TOML_NEO4J_VALID)
        result = load_config(path, BACKEND_NEO4J)
        assert isinstance(result, Neo4jConfig)
        assert result.database == "neo4j"


# ---------------------------------------------------------------------------
# load_config — ArcadeDB
# ---------------------------------------------------------------------------


class TestLoadConfigArcadeDB:
    def test_valid_arcadedb_config(self, config_file):
        path = config_file(TOML_ARCADEDB_VALID)
        result = load_config(path, BACKEND_ARCADEDB)
        assert isinstance(result, ArcadeDBConfig)
        assert result.uri == "http://localhost:2480"
        assert result.user == "root"
        assert result.password == "test"
        assert result.database == "mycorpus"

    def test_only_password_is_required(self, config_file):
        """uri/user have working defaults; database is derived from the project."""
        path = config_file(TOML_ARCADEDB_MINIMAL)
        result = load_config(path, BACKEND_ARCADEDB)
        assert isinstance(result, ArcadeDBConfig)
        assert result.uri == DEFAULT_ARCADEDB_URI
        assert result.user == "root"
        assert result.database == ""

    def test_missing_password_returns_error(self, config_file):
        path = config_file(TOML_ARCADEDB_MISSING_PASSWORD)
        result = load_config(path, BACKEND_ARCADEDB)
        assert isinstance(result, ConnectionError)
        assert result.stage == "config"
        assert "password" in (result.details or "")

    def test_missing_section_returns_error(self, config_file):
        path = config_file(TOML_NEO4J_VALID)
        result = load_config(path, BACKEND_ARCADEDB)
        assert isinstance(result, ConnectionError)
        assert "[arcadedb]" in (result.details or "")

    def test_missing_file_returns_error(self, tmp_path):
        result = load_config(tmp_path / "nonexistent.toml", BACKEND_ARCADEDB)
        assert isinstance(result, ConnectionError)
        assert result.stage == "config"

    def test_uri_uppercase_key_accepted(self, config_file):
        path = config_file('[arcadedb]\nURI = "http://host:2480"\npassword = "t"\n')
        result = load_config(path, BACKEND_ARCADEDB)
        assert isinstance(result, ArcadeDBConfig)
        assert result.uri == "http://host:2480"

    def test_default_analyzer(self, config_file):
        path = config_file(TOML_ARCADEDB_MINIMAL)
        result = load_config(path, BACKEND_ARCADEDB)
        assert isinstance(result, ArcadeDBConfig)
        assert result.fulltext_analyzer == DEFAULT_ARCADEDB_ANALYZER

    def test_custom_analyzer(self, config_file):
        path = config_file('[arcadedb]\npassword = "t"\nfulltext_analyzer = "brazilian"\n')
        result = load_config(path, BACKEND_ARCADEDB)
        assert isinstance(result, ArcadeDBConfig)
        assert result.fulltext_analyzer == "brazilian"

    def test_section_that_is_not_a_table_is_rejected(self, config_file):
        path = config_file('arcadedb = "oops"\n')
        result = load_config(path, BACKEND_ARCADEDB)
        assert isinstance(result, ConnectionError)

    def test_invalid_toml_returns_error_not_defaults(self, config_file):
        """Unlike HTML, a database backend must never silently use defaults."""
        path = config_file("this is not valid toml !!!###")
        result = load_config(path, BACKEND_ARCADEDB)
        assert isinstance(result, ConnectionError)


# ---------------------------------------------------------------------------
# Analyzer name resolution
# ---------------------------------------------------------------------------


class TestResolveArcadeDBAnalyzer:
    def test_short_name_expands_to_lucene_class(self):
        assert (
            resolve_arcadedb_analyzer("brazilian")
            == "org.apache.lucene.analysis.br.BrazilianAnalyzer"
        )

    def test_resolution_is_case_insensitive(self):
        assert resolve_arcadedb_analyzer("Brazilian") == resolve_arcadedb_analyzer("brazilian")

    def test_neo4j_default_maps_to_an_arcadedb_equivalent(self):
        """A config written for Neo4j must not fail against ArcadeDB."""
        resolved = resolve_arcadedb_analyzer("standard-no-stop-words")
        assert resolved.startswith("org.apache.lucene.analysis.")

    def test_fully_qualified_class_passes_through(self):
        cls = "org.apache.lucene.analysis.custom.MyAnalyzer"
        assert resolve_arcadedb_analyzer(cls) == cls

    def test_unknown_name_passes_through(self):
        """The server stays the authority on which analyzers exist."""
        assert resolve_arcadedb_analyzer("klingon") == "klingon"

    def test_empty_name_falls_back_to_default(self):
        assert resolve_arcadedb_analyzer("") == DEFAULT_ARCADEDB_ANALYZER


# ---------------------------------------------------------------------------
# load_config — HTML
# ---------------------------------------------------------------------------


class TestLoadConfigHTML:
    def test_missing_file_returns_default_html_config(self, tmp_path):
        path = tmp_path / "nonexistent.toml"
        result = load_config(path, BACKEND_HTML)
        assert isinstance(result, HTMLConfig)
        assert result.output_path == "./graph.html"

    def test_custom_html_config(self, config_file):
        path = config_file(TOML_HTML_CUSTOM)
        result = load_config(path, BACKEND_HTML)
        assert isinstance(result, HTMLConfig)
        assert result.output_path == "./custom.html"
        assert result.min_frequency == 5
        assert result.min_source_count == 3
        assert result.max_nodes == 100

    def test_html_defaults_when_section_absent(self, config_file):
        path = config_file(TOML_NEO4J_VALID)
        result = load_config(path, BACKEND_HTML)
        assert isinstance(result, HTMLConfig)
        assert result.min_frequency == 0
        assert result.min_source_count == 0
        assert result.max_nodes == 0
        assert result.include_isolated is True

    def test_invalid_toml_falls_back_to_defaults(self, config_file):
        path = config_file("this is not valid toml !!!###")
        result = load_config(path, BACKEND_HTML)
        assert isinstance(result, HTMLConfig)


# ---------------------------------------------------------------------------
# validate_backend_config
# ---------------------------------------------------------------------------


class TestValidateBackendConfig:
    def test_neo4j_config_matches_neo4j_backend(self):
        cfg = Neo4jConfig(uri="bolt://x", user="u", password="p")
        assert validate_backend_config(cfg, BACKEND_NEO4J) is None

    def test_html_config_matches_html_backend(self):
        cfg = HTMLConfig()
        assert validate_backend_config(cfg, BACKEND_HTML) is None

    def test_neo4j_config_mismatches_html_backend(self):
        cfg = Neo4jConfig(uri="bolt://x", user="u", password="p")
        err = validate_backend_config(cfg, BACKEND_HTML)
        assert isinstance(err, ConnectionError)
        assert err.stage == "config"

    def test_html_config_mismatches_neo4j_backend(self):
        cfg = HTMLConfig()
        err = validate_backend_config(cfg, BACKEND_NEO4J)
        assert isinstance(err, ConnectionError)

    def test_arcadedb_config_matches_arcadedb_backend(self):
        cfg = ArcadeDBConfig(password="p")
        assert validate_backend_config(cfg, BACKEND_ARCADEDB) is None

    def test_arcadedb_config_mismatches_neo4j_backend(self):
        err = validate_backend_config(ArcadeDBConfig(password="p"), BACKEND_NEO4J)
        assert isinstance(err, ConnectionError)
        assert err.stage == "config"

    def test_neo4j_config_mismatches_arcadedb_backend(self):
        cfg = Neo4jConfig(uri="bolt://x", user="u", password="p")
        err = validate_backend_config(cfg, BACKEND_ARCADEDB)
        assert isinstance(err, ConnectionError)


# ---------------------------------------------------------------------------
# Backend registry
# ---------------------------------------------------------------------------


class TestSupportedBackends:
    def test_arcadedb_is_registered(self):
        assert BACKEND_ARCADEDB in SUPPORTED_BACKENDS

    def test_existing_backends_are_preserved(self):
        assert BACKEND_NEO4J in SUPPORTED_BACKENDS
        assert BACKEND_HTML in SUPPORTED_BACKENDS

    def test_arcadedb_builds_its_adapter(self, tmp_path):
        from pathlib import Path

        from synesis_graph.backends.base import ArcadeDBBackendAdapter
        from synesis_graph.pipeline import build_backend_adapter

        adapter = build_backend_adapter(
            BACKEND_ARCADEDB, ArcadeDBConfig(password="p"), Path("config.toml"), tmp_path
        )
        assert isinstance(adapter, ArcadeDBBackendAdapter)
        assert adapter.backend_name == BACKEND_ARCADEDB

    def test_arcadedb_backend_rejects_a_foreign_config(self, tmp_path):
        from pathlib import Path

        from synesis_graph.pipeline import build_backend_adapter

        result = build_backend_adapter(
            BACKEND_ARCADEDB,
            Neo4jConfig(uri="bolt://x", user="u", password="p"),
            Path("config.toml"),
            tmp_path,
        )
        assert isinstance(result, ConnectionError)


