"""Tests for config.py — TOML loading, validation, path resolution."""

from __future__ import annotations

from synesis_graph.config import (
    BACKEND_HTML,
    BACKEND_NEO4J,
    HTMLConfig,
    Neo4jConfig,
    load_config,
    validate_backend_config,
)
from synesis_graph.core import ConnectionError
from tests.conftest import (
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


