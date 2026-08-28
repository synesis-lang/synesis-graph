"""Tests for the [arcadedb-embedded] configuration block.

Two things separate this loader from the others, and both are deliberate:

- **An absent section is valid.** There is no credential to supply and no host to
  reach, so the defaults already describe a working setup.
- **`db_path` is the server root, not the database directory.** Getting that
  wrong is the silent failure this whole block exists to prevent: the server
  starts, reports success, and finds no databases at all.
"""

from __future__ import annotations

from pathlib import Path

from synesis_graph.config import (
    BACKEND_ARCADEDB,
    BACKEND_ARCADEDB_EMBEDDED,
    SUPPORTED_BACKENDS,
    ArcadeDBConfig,
    ArcadeDBEmbeddedConfig,
    load_config,
    validate_backend_config,
)
from synesis_graph.core import DEFAULT_ARCADEDB_ANALYZER, DEFAULT_EMBEDDING_MODEL, ConnectionError


class TestBackendRegistration:
    def test_backend_is_supported(self):
        assert BACKEND_ARCADEDB_EMBEDDED in SUPPORTED_BACKENDS

    def test_name_is_the_pypi_package_not_local(self):
        """`arcadedb-local` would be ambiguous with a TCP server on localhost."""
        assert BACKEND_ARCADEDB_EMBEDDED == "arcadedb-embedded"

    def test_config_type_matches_only_its_own_backend(self):
        assert (
            validate_backend_config(ArcadeDBEmbeddedConfig(), BACKEND_ARCADEDB_EMBEDDED) is None
        )
        mismatch = validate_backend_config(ArcadeDBEmbeddedConfig(), BACKEND_ARCADEDB)
        assert isinstance(mismatch, ConnectionError)

    def test_the_tcp_config_does_not_satisfy_the_embedded_backend(self):
        """Separate sections mean separate types; the pipeline must not swap them."""
        result = validate_backend_config(ArcadeDBConfig(password="x"), BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ConnectionError)


class TestDatabaseLayout:
    """The contract between the export phase and the serving phase."""

    def test_database_lives_under_a_databases_subdirectory(self):
        """`create_server(root_path=X)` scans `X/databases/`, not `X`.

        Writing to `<root>/<project>` makes the server start, register its MCP
        endpoint, and find nothing — with no error anywhere. Measured against
        arcadedb-embedded 26.8.1.
        """
        cfg = ArcadeDBEmbeddedConfig(db_path="/srv/graphs")
        assert cfg.database_dir("face85") == Path("/srv/graphs") / "databases" / "face85"

    def test_layout_is_derived_once(self):
        """Both phases call the same method, so they cannot disagree."""
        cfg = ArcadeDBEmbeddedConfig(db_path="/srv")
        assert cfg.database_dir("p").parent.name == "databases"

    def test_default_root_keeps_the_graph_beside_the_project(self):
        """A `.db` is a research artefact, not a cache: never under `~/.cache`."""
        assert ArcadeDBEmbeddedConfig().db_path == "."

    def test_default_layout_does_not_double_the_databases_segment(self):
        """`databases/databases/face85` reads like a bug and invites a bad "fix".

        Someone correcting it by pointing the root one level deeper lands exactly
        on the silent failure: server starts, finds nothing, reports success.
        """
        assert ArcadeDBEmbeddedConfig().database_dir("face85") == Path("databases/face85")


class TestLoadConfig:
    def test_absent_section_yields_working_defaults(self, config_file):
        """No credential, no host — nothing to demand from the researcher."""
        path = config_file('[neo4j]\nuri = "bolt://x"\nuser = "u"\npassword = "p"\n')
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ArcadeDBEmbeddedConfig)
        assert result.db_path == "."

    def test_absent_file_yields_working_defaults(self, tmp_path):
        """Same reasoning: a file whose every value repeats a default is friction."""
        result = load_config(tmp_path / "nonexistent.toml", BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ArcadeDBEmbeddedConfig)

    def test_values_are_read(self, config_file):
        path = config_file(
            "[arcadedb-embedded]\n"
            'db_path = "/data/graphs"\n'
            'fulltext_analyzer = "brazilian"\n'
        )
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ArcadeDBEmbeddedConfig)
        assert result.db_path == "/data/graphs"
        assert result.fulltext_analyzer == "brazilian"

    def test_underscore_spelling_is_accepted(self, config_file):
        """A reader who knows `[tool.ruff]` will reasonably try the underscore."""
        path = config_file('[arcadedb_embedded]\ndb_path = "/data/g"\n')
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ArcadeDBEmbeddedConfig)
        assert result.db_path == "/data/g"

    def test_embeddings_subtable_is_read(self, config_file):
        path = config_file(
            "[arcadedb-embedded]\n"
            "[arcadedb-embedded.embeddings]\n"
            'model = "custom/model"\n'
            'fields = ["ontology_description", "topic"]\n'
        )
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ArcadeDBEmbeddedConfig)
        assert result.embedding_model == "custom/model"
        assert result.embedding_fields == ["ontology_description", "topic"]

    def test_a_bare_string_of_fields_is_read_as_one_field(self, config_file):
        """Iterating the characters would report "no such field: o"."""
        path = config_file(
            "[arcadedb-embedded]\n[arcadedb-embedded.embeddings]\nfields = \"topic\"\n"
        )
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ArcadeDBEmbeddedConfig)
        assert result.embedding_fields == ["topic"]

    def test_defaults_apply_when_the_section_is_empty(self, config_file):
        path = config_file("[arcadedb-embedded]\n")
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ArcadeDBEmbeddedConfig)
        assert result.fulltext_analyzer == DEFAULT_ARCADEDB_ANALYZER
        assert result.embedding_model == DEFAULT_EMBEDDING_MODEL
        assert result.embedding_fields == []


class TestMalformedSectionsAreReported:
    """Typos are reported, never silently ignored — see the docstring rationale."""

    def test_section_of_the_wrong_type(self, config_file):
        path = config_file('arcadedb-embedded = "oops"\n')
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ConnectionError)
        assert "must be a table" in result.details

    def test_embeddings_of_the_wrong_type(self, config_file):
        path = config_file("[arcadedb-embedded]\nembeddings = 42\n")
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ConnectionError)
        assert "embeddings" in result.details

    def test_fields_of_the_wrong_type(self, config_file):
        path = config_file(
            "[arcadedb-embedded]\n[arcadedb-embedded.embeddings]\nfields = 42\n"
        )
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ConnectionError)
        assert "list of field names" in result.details


class TestSeparateSectionFromTcp:
    """Why not reuse [arcadedb]: a field that looks honoured but is not."""

    def test_a_tcp_section_alone_does_not_configure_the_embedded_backend(self, config_file):
        """`uri`/`user`/`password` are meaningless in-process.

        Reusing [arcadedb] would leave them sitting in the file, read by nobody —
        the same defect shape as the `database` key currently ignored in a real
        project config.
        """
        path = config_file('[arcadedb]\npassword = "x"\ndb_path = "/ignored"\n')
        result = load_config(path, BACKEND_ARCADEDB_EMBEDDED)
        assert isinstance(result, ArcadeDBEmbeddedConfig)
        assert result.db_path == ".", "must not read db_path out of [arcadedb]"

    def test_both_sections_can_coexist(self, config_file):
        """One project may be exported to a server and to a local file."""
        path = config_file(
            '[arcadedb]\npassword = "x"\nuri = "http://remote:2480"\n\n'
            '[arcadedb-embedded]\ndb_path = "/local/graphs"\n'
        )
        tcp = load_config(path, BACKEND_ARCADEDB)
        embedded = load_config(path, BACKEND_ARCADEDB_EMBEDDED)

        assert isinstance(tcp, ArcadeDBConfig)
        assert isinstance(embedded, ArcadeDBEmbeddedConfig)
        assert tcp.uri == "http://remote:2480"
        assert embedded.db_path == "/local/graphs"
