"""Tests for embedding generation, caching and invalidation.

Almost everything here runs with a fake model, so the suite stays runnable
without the `[embeddings]` extra. The one test that loads a real model skips
when sentence-transformers is absent, matching how the ArcadeDB suites gate on
a live server.
"""

from __future__ import annotations

import pytest

from synesis_graph.core import DependencyError, PipelineError
from synesis_graph.embeddings import ConceptText, EmbeddingsSidecar
from synesis_graph.embeddings_provider import (
    DEFAULT_EMBEDDING_MODEL,
    EncodeResult,
    encode_concepts,
    generate,
    get_embedding_model_factory,
    reusable_vectors,
    stale_concepts,
)

MODEL = "fake-model"


class FakeModel:
    """Deterministic stand-in honouring the EmbeddingModel protocol."""

    def __init__(self, dimensions: int = 4):
        self.dimensions = dimensions
        self.encoded: list[list[str]] = []
        self.kwargs: dict = {}

    def get_sentence_embedding_dimension(self) -> int | None:
        return self.dimensions

    def encode(self, sentences, **kwargs):
        self.encoded.append(list(sentences))
        self.kwargs = kwargs
        # Width must follow the declared dimensions, or the fake would trip the
        # width check that exists to catch a model contradicting itself.
        return [[float(len(s) % 7)] + [1.0] * (self.dimensions - 1) for s in sentences]


def _sidecar(fields=("description",), **concepts) -> EmbeddingsSidecar:
    sc = EmbeddingsSidecar(fields=list(fields))
    for name, text in concepts.items():
        sc.concepts[name] = ConceptText(name=name, text=text)
    return sc


def _encoded(sidecar, model=MODEL, dims=4, **kw) -> EmbeddingsSidecar:
    """A sidecar already carrying vectors, as a previous run would leave it."""
    result = generate(sidecar, None, model, model=FakeModel(dims), **kw)
    assert not isinstance(result, PipelineError)
    return result


# ---------------------------------------------------------------------------
# Dependency contract
# ---------------------------------------------------------------------------


def test_factory_returns_none_when_the_extra_is_absent(monkeypatch):
    """Mirrors get_neo4j_driver_factory: absence is None, never an exception."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("sentence_transformers"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    assert get_embedding_model_factory() is None


def test_missing_extra_yields_an_actionable_error(monkeypatch):
    monkeypatch.setattr(
        "synesis_graph.embeddings_provider.get_embedding_model_factory", lambda: None
    )
    sc = _sidecar(a="text a")
    result = encode_concepts(sc, ["a"], MODEL)

    assert isinstance(result, DependencyError)
    assert result.stage == "dependency"
    assert "pip install" in (result.details or "")
    assert "embeddings" in (result.details or "")


def test_model_load_failure_is_reported_not_raised(monkeypatch):
    def exploding_factory(name):
        raise OSError("no such model")

    monkeypatch.setattr(
        "synesis_graph.embeddings_provider.get_embedding_model_factory",
        lambda: exploding_factory,
    )
    result = encode_concepts(_sidecar(a="x"), ["a"], "nonexistent/model")

    assert isinstance(result, DependencyError)
    assert "nonexistent/model" in result.message
    assert "OSError" in (result.details or "")


def test_encode_failure_is_reported_not_raised():
    class Exploding(FakeModel):
        def encode(self, sentences, **kwargs):
            raise RuntimeError("out of memory")

    result = encode_concepts(_sidecar(a="x"), ["a"], MODEL, model=Exploding())
    assert isinstance(result, DependencyError)
    assert "RuntimeError" in (result.details or "")


def test_model_without_dimensions_is_rejected():
    class Dimensionless(FakeModel):
        def get_sentence_embedding_dimension(self):
            return None

    result = encode_concepts(_sidecar(a="x"), ["a"], MODEL, model=Dimensionless())
    assert isinstance(result, DependencyError)
    assert "dimensions" in result.message


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def test_vectors_are_plain_floats_not_numpy():
    """float32 is not JSON-serializable; the sidecar has to be writable."""
    result = encode_concepts(_sidecar(a="x"), ["a"], MODEL, model=FakeModel())
    assert all(type(v) is float for v in result.vectors["a"])


def test_dimensions_come_from_the_model_never_hardcoded():
    result = encode_concepts(_sidecar(a="x"), ["a"], MODEL, model=FakeModel(dimensions=4))
    assert result.dimensions == 4


def test_encoding_requests_normalized_vectors():
    """The index is created with COSINE, which assumes unit vectors."""
    model = FakeModel()
    encode_concepts(_sidecar(a="x"), ["a"], MODEL, model=model)
    assert model.kwargs.get("normalize_embeddings") is True


def test_progress_bar_is_suppressed():
    model = FakeModel()
    encode_concepts(_sidecar(a="x"), ["a"], MODEL, model=model)
    assert model.kwargs.get("show_progress_bar") is False


def test_empty_name_list_does_not_load_or_encode():
    model = FakeModel()
    result = encode_concepts(_sidecar(a="x"), [], MODEL, model=model)
    assert isinstance(result, EncodeResult)
    assert result.computed == 0
    assert model.encoded == []


def test_width_mismatch_is_caught():
    """A model whose output width contradicts its reported dimensions."""

    class Inconsistent(FakeModel):
        def encode(self, sentences, **kwargs):
            return [[0.1, 0.2] for _ in sentences]  # 2 wide, reports 4

    result = encode_concepts(_sidecar(a="x"), ["a"], MODEL, model=Inconsistent())
    assert isinstance(result, DependencyError)
    assert "unexpected width" in result.message


# ---------------------------------------------------------------------------
# Staleness — what must be recomputed
# ---------------------------------------------------------------------------


def test_everything_is_stale_without_a_previous_run():
    sc = _sidecar(a="x", b="y")
    assert stale_concepts(sc, None, MODEL) == ["a", "b"]


def test_nothing_is_stale_when_nothing_changed():
    sc = _sidecar(a="x", b="y")
    previous = _encoded(_sidecar(a="x", b="y"))
    assert stale_concepts(sc, previous, MODEL) == []


def test_only_the_changed_concept_is_stale():
    previous = _encoded(_sidecar(a="x", b="y"))
    sc = _sidecar(a="x", b="CHANGED")
    assert stale_concepts(sc, previous, MODEL) == ["b"]


def test_a_new_concept_is_stale():
    previous = _encoded(_sidecar(a="x"))
    sc = _sidecar(a="x", b="new")
    assert stale_concepts(sc, previous, MODEL) == ["b"]


def test_changing_the_model_invalidates_everything():
    """Vectors from two models are individually valid and mutually meaningless."""
    previous = _encoded(_sidecar(a="x", b="y"), model="model-one")
    sc = _sidecar(a="x", b="y")
    assert stale_concepts(sc, previous, "model-two") == ["a", "b"]


def test_changing_the_field_composition_invalidates_everything():
    """Distance between compositions measures the composition, not the meaning."""
    previous = _encoded(_sidecar(("description",), a="x", b="y"))
    sc = _sidecar(("description", "topic"), a="x", b="y")
    assert stale_concepts(sc, previous, MODEL) == ["a", "b"]


def test_reordering_fields_invalidates_everything():
    previous = _encoded(_sidecar(("topic", "description"), a="x"))
    sc = _sidecar(("description", "topic"), a="x")
    assert stale_concepts(sc, previous, MODEL) == ["a"]


def test_matching_hash_without_a_vector_is_still_stale():
    """A sidecar written by stage 1 has hashes but no vectors."""
    previous = _sidecar(a="x")  # no vectors
    assert stale_concepts(_sidecar(a="x"), previous, MODEL) == ["a"]


# ---------------------------------------------------------------------------
# Reuse
# ---------------------------------------------------------------------------


def test_unchanged_vectors_are_reused():
    previous = _encoded(_sidecar(a="x", b="y"))
    sc = _sidecar(a="x", b="CHANGED")
    reused = reusable_vectors(sc, previous, ["b"])
    assert list(reused) == ["a"]


def test_removed_concepts_are_not_carried_over():
    previous = _encoded(_sidecar(a="x", b="y"))
    sc = _sidecar(a="x")  # b deleted from the ontology
    assert list(reusable_vectors(sc, previous, [])) == ["a"]


# ---------------------------------------------------------------------------
# generate — the whole cycle
# ---------------------------------------------------------------------------


def test_generate_fills_every_concept():
    sc = _sidecar(a="x", b="y")
    result = generate(sc, None, MODEL, model=FakeModel())
    assert set(result.vectors) == {"a", "b"}
    assert result.model == MODEL
    assert result.dimensions == 4


def test_generate_recomputes_only_what_changed():
    previous = _encoded(_sidecar(a="x", b="y"))
    model = FakeModel()
    generate(_sidecar(a="x", b="CHANGED"), previous, MODEL, model=model)
    assert model.encoded == [["CHANGED"]]


def test_generate_encodes_nothing_when_the_corpus_is_unchanged():
    previous = _encoded(_sidecar(a="x", b="y"))
    model = FakeModel()
    result = generate(_sidecar(a="x", b="y"), previous, MODEL, model=model)
    assert model.encoded == []
    assert set(result.vectors) == {"a", "b"}


class RefusingModel(FakeModel):
    """Fails if loaded at all — proves a code path avoided the model entirely."""

    def get_sentence_embedding_dimension(self):
        raise AssertionError("model should not have been loaded")

    def encode(self, sentences, **kwargs):
        raise AssertionError("model should not have been loaded")


def test_a_fully_cached_run_does_not_load_the_model():
    """Loading costs ~2.4s on face85; paying it to encode nothing is waste."""
    previous = _encoded(_sidecar(a="x", b="y"))
    result = generate(_sidecar(a="x", b="y"), previous, MODEL, model=RefusingModel())
    assert not isinstance(result, PipelineError)
    assert result.dimensions == 4
    assert set(result.vectors) == {"a", "b"}


def test_the_cached_dimension_is_not_trusted_across_models():
    """A previous model's width says nothing about this one's."""
    previous = _encoded(_sidecar(a="x"), model="model-one", dims=8)
    model = FakeModel(dimensions=4)
    result = generate(_sidecar(a="x"), previous, "model-two", model=model)
    assert result.dimensions == 4
    assert model.encoded == [["x"]]


def test_rebuild_still_loads_the_model_despite_a_cache():
    previous = _encoded(_sidecar(a="x"))
    model = FakeModel()
    generate(_sidecar(a="x"), previous, MODEL, model=model, rebuild=True)
    assert model.encoded == [["x"]]


def test_rebuild_forces_a_full_recompute():
    previous = _encoded(_sidecar(a="x", b="y"))
    model = FakeModel()
    generate(_sidecar(a="x", b="y"), previous, MODEL, model=model, rebuild=True)
    assert model.encoded == [["x", "y"]]


def test_generate_records_the_model_it_used():
    result = generate(_sidecar(a="x"), None, "some/model", model=FakeModel())
    assert result.model == "some/model"


def test_reused_vectors_of_a_different_width_trigger_a_recompute():
    """Same model name, different width — only a full recompute is safe."""
    previous = _encoded(_sidecar(a="x", b="y"), dims=8)
    model = FakeModel(dimensions=4)
    result = generate(_sidecar(a="x", b="CHANGED"), previous, MODEL, model=model)
    assert all(len(v) == 4 for v in result.vectors.values())
    assert set(result.vectors) == {"a", "b"}


def test_generate_propagates_errors():
    class Exploding(FakeModel):
        def encode(self, sentences, **kwargs):
            raise RuntimeError("boom")

    assert isinstance(generate(_sidecar(a="x"), None, MODEL, model=Exploding()), PipelineError)


def test_generated_sidecar_round_trips_through_the_file(tmp_path):
    from synesis_graph.embeddings import load_sidecar

    path = tmp_path / "p.embeddings.json"
    generate(_sidecar(a="x", b="y"), None, MODEL, model=FakeModel()).write(path)

    loaded = load_sidecar(path)
    assert loaded.model == MODEL
    assert loaded.dimensions == 4
    assert set(loaded.vectors) == {"a", "b"}
    assert all(type(v) is float for v in loaded.vectors["a"])


def test_a_reloaded_sidecar_needs_no_recompute(tmp_path):
    """The cache has to survive a round trip, or it never helps in practice."""
    from synesis_graph.embeddings import load_sidecar

    path = tmp_path / "p.embeddings.json"
    generate(_sidecar(a="x", b="y"), None, MODEL, model=FakeModel()).write(path)

    model = FakeModel()
    generate(_sidecar(a="x", b="y"), load_sidecar(path), MODEL, model=model)
    assert model.encoded == []


# ---------------------------------------------------------------------------
# Real model
# ---------------------------------------------------------------------------

# Same shape as the ArcadeDB suites' `live` marker: a bare skipif, so the test
# is silently absent where the extra is not installed (CI without the extra).
needs_extra = pytest.mark.skipif(
    get_embedding_model_factory() is None,
    reason="no [embeddings] extra (pip install 'synesis-graph[embeddings]')",
)


@needs_extra
def test_integration_real_model_produces_normalized_vectors():
    """The English model is used here: smallest download, and language is irrelevant
    to what this asserts (width and normalization)."""
    import math

    sc = _sidecar(
        governance="corporate governance. Power structures within the firm.",
        bias="cognitive biases. Systematic deviations of judgement.",
    )
    result = generate(sc, None, "sentence-transformers/all-MiniLM-L6-v2")
    assert not isinstance(result, PipelineError)
    assert result.dimensions == 384

    for vector in result.vectors.values():
        assert len(vector) == 384
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, abs_tol=1e-4)


def test_default_model_is_multilingual():
    """English-only models reproduce BM25's error on Portuguese corpora."""
    assert "multilingual" in DEFAULT_EMBEDDING_MODEL
