"""CLI and config surface for --vector-embeddings.

The pipeline is monkeypatched: what matters here is that the flag reaches
run_pipeline correctly parsed, that the config block is read, and that the
precedence between them matches --database's.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from synesis_graph.cli import _split_fields, main
from synesis_graph.config import BACKEND_ARCADEDB, ArcadeDBConfig, load_config
from synesis_graph.core import DEFAULT_EMBEDDING_MODEL


def _run(*args: str):
    return CliRunner().invoke(main, list(args))


# ---------------------------------------------------------------------------
# Parsing the flag
# ---------------------------------------------------------------------------


def test_single_field():
    assert _split_fields("ontology_description") == ["ontology_description"]


def test_several_fields():
    assert _split_fields("a,b,c") == ["a", "b", "c"]


def test_spaces_after_commas_are_tolerated():
    """`--vector-embeddings "a, b"` is what a user naturally types."""
    assert _split_fields("a, b, c") == ["a", "b", "c"]


def test_empty_and_none_yield_nothing():
    assert _split_fields(None) == []
    assert _split_fields("") == []
    assert _split_fields("  ") == []


def test_trailing_comma_does_not_create_an_empty_field():
    assert _split_fields("a,b,") == ["a", "b"]


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_help_documents_the_flag():
    out = _run("arcadedb", "--help").output
    assert "--vector-embeddings" in out
    assert "--rebuild-embeddings" in out


def test_help_names_the_extra_needed():
    """The flag is useless without the extra; the help has to say so."""
    out = _run("arcadedb", "--help").output
    assert "synesis-graph[embeddings]" in out


def test_help_shows_a_worked_example():
    out = _run("arcadedb", "--help").output
    assert "--vector-embeddings ontology_description,topic" in out


def test_help_shows_the_config_block():
    out = _run("arcadedb", "--help").output
    assert "[arcadedb.embeddings]" in out


def test_other_backends_do_not_offer_the_flag():
    """Neo4j has no vector persistence in this version (study §10)."""
    assert "--vector-embeddings" not in _run("neo4j", "--help").output
    assert "--vector-embeddings" not in _run("html", "--help").output


# ---------------------------------------------------------------------------
# Reaching the pipeline
# ---------------------------------------------------------------------------


@pytest.fixture
def captured(monkeypatch, tmp_path):
    """Captures run_pipeline's kwargs instead of running it."""
    calls: dict = {}

    class Result:
        success = True
        error = None
        stats: dict = {}

    def fake(**kwargs):
        calls.update(kwargs)
        return Result()

    import synesis_graph.pipeline as pipeline

    monkeypatch.setattr(pipeline, "run_pipeline", fake)
    project = tmp_path / "p.synp"
    project.write_text("", encoding="utf-8")
    config = tmp_path / "c.toml"
    config.write_text("[arcadedb]\npassword = 'x'\n", encoding="utf-8")
    return calls, project, config


def test_fields_reach_the_pipeline(captured):
    calls, project, config = captured
    result = _run(
        "arcadedb",
        "--project",
        str(project),
        "--config",
        str(config),
        "--vector-embeddings",
        "ontology_description,topic",
    )
    assert result.exit_code == 0
    assert calls["vector_embeddings"] == ["ontology_description", "topic"]


def test_absent_flag_passes_no_fields(captured):
    calls, project, config = captured
    _run("arcadedb", "--project", str(project), "--config", str(config))
    assert calls["vector_embeddings"] == []
    assert calls["rebuild_embeddings"] is False


def test_rebuild_flag_reaches_the_pipeline(captured):
    calls, project, config = captured
    _run(
        "arcadedb",
        "--project",
        str(project),
        "--config",
        str(config),
        "--vector-embeddings",
        "a",
        "--rebuild-embeddings",
    )
    assert calls["rebuild_embeddings"] is True


def test_rebuild_without_fields_is_a_usage_error(captured):
    """Doing nothing silently would read as "the rebuild happened"."""
    _calls, project, config = captured
    result = _run(
        "arcadedb",
        "--project",
        str(project),
        "--config",
        str(config),
        "--rebuild-embeddings",
    )
    assert result.exit_code != 0
    assert "--vector-embeddings" in result.output


# ---------------------------------------------------------------------------
# Config block
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_absent_block_leaves_the_feature_off(tmp_path):
    """No existing config may break."""
    cfg = load_config(
        _config(
            tmp_path,
            """
        [arcadedb]
        password = "secret"
    """,
        ),
        BACKEND_ARCADEDB,
    )
    assert isinstance(cfg, ArcadeDBConfig)
    assert cfg.embedding_fields == []
    assert cfg.embedding_model == DEFAULT_EMBEDDING_MODEL


def test_block_is_read(tmp_path):
    cfg = load_config(
        _config(
            tmp_path,
            """
        [arcadedb]
        password = "secret"

        [arcadedb.embeddings]
        model = "sentence-transformers/all-MiniLM-L6-v2"
        fields = ["ontology_description", "topic"]
    """,
        ),
        BACKEND_ARCADEDB,
    )
    assert cfg.embedding_model == "sentence-transformers/all-MiniLM-L6-v2"
    assert cfg.embedding_fields == ["ontology_description", "topic"]


def test_model_alone_keeps_the_feature_off(tmp_path):
    """A model without fields names a preference, not a request to embed."""
    cfg = load_config(
        _config(
            tmp_path,
            """
        [arcadedb]
        password = "secret"

        [arcadedb.embeddings]
        model = "some/model"
    """,
        ),
        BACKEND_ARCADEDB,
    )
    assert cfg.embedding_model == "some/model"
    assert cfg.embedding_fields == []


def test_a_bare_string_is_taken_as_one_field(tmp_path):
    """Iterating the characters would report `o` as an unknown field."""
    cfg = load_config(
        _config(
            tmp_path,
            """
        [arcadedb]
        password = "secret"

        [arcadedb.embeddings]
        fields = "ontology_description"
    """,
        ),
        BACKEND_ARCADEDB,
    )
    assert cfg.embedding_fields == ["ontology_description"]


def test_a_malformed_fields_value_is_an_error(tmp_path):
    result = load_config(
        _config(
            tmp_path,
            """
        [arcadedb]
        password = "secret"

        [arcadedb.embeddings]
        fields = 42
    """,
        ),
        BACKEND_ARCADEDB,
    )
    assert not isinstance(result, ArcadeDBConfig)
    assert "fields" in (result.details or "")


def test_the_neo4j_block_is_unaffected(tmp_path):
    """The new keys live under [arcadedb]; nothing else changes."""
    from synesis_graph.config import BACKEND_NEO4J, Neo4jConfig

    cfg = load_config(
        _config(
            tmp_path,
            """
        [neo4j]
        uri = "bolt://localhost:7687"
        user = "neo4j"
        password = "secret"
    """,
        ),
        BACKEND_NEO4J,
    )
    assert isinstance(cfg, Neo4jConfig)
