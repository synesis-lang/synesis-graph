"""Local embedding generation, behind the optional `[embeddings]` extra.

Separated from `embeddings.py` on purpose: that module decides *which* text
becomes a vector and has no machine-learning dependency, so field selection can
be inspected without installing torch. This module is the only place that needs
the extra.

Nothing here imports `sentence_transformers` at module level — the import is
deferred to `get_embedding_model_factory()`, mirroring
`core.get_neo4j_driver_factory()`, so importing `synesis_graph` never pulls in
torch and never fails because the extra is absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# DEFAULT_EMBEDDING_MODEL is defined in core, not here, so config.py can name it
# without importing anything that pulls in torch. Re-exported because this is
# where callers look for it.
from synesis_graph.core import DEFAULT_EMBEDDING_MODEL as DEFAULT_EMBEDDING_MODEL
from synesis_graph.core import (
    DependencyError,
    GraphPayload,
    PipelineError,
)
from synesis_graph.embeddings import (
    EmbeddingsSidecar,
    build_sidecar,
    load_sidecar,
    resolve_fields,
    sidecar_path,
)

INSTALL_HINT = 'Install with: pip install "synesis-graph[embeddings]"'


class EmbeddingModel(Protocol):
    """The slice of SentenceTransformer this module relies on.

    Declared as a Protocol so tests can substitute a fake without importing
    torch — the same reason the suite must stay runnable without the extra.
    """

    def encode(self, sentences: list[str], **kwargs: Any) -> Any: ...

    def get_sentence_embedding_dimension(self) -> int | None: ...


def get_embedding_model_factory() -> Any:
    """Loads the SentenceTransformer class lazily, or None when absent.

    Returns None instead of raising, matching `get_neo4j_driver_factory()`: the
    caller turns absence into a `DependencyError` carrying an install hint, so a
    missing extra reads like every other pipeline error rather than a traceback.
    """
    try:
        from sentence_transformers import SentenceTransformer

        return SentenceTransformer
    except ImportError:
        return None


@dataclass
class EncodeResult:
    """Vectors for the concepts that needed them, plus what was reused."""

    vectors: dict[str, list[float]]
    dimensions: int
    computed: int
    reused: int


def stale_concepts(
    sidecar: EmbeddingsSidecar,
    previous: EmbeddingsSidecar | None,
    model: str,
) -> list[str]:
    """Names whose vectors must be recomputed.

    Everything is stale when the model or the field composition changed: vectors
    from a different model, or from a different field composition, are
    individually valid and mutually meaningless, and mixing them degrades search
    with no error to show for it. `fields_hash` covers composition because the
    per-concept text hash cannot — two compositions can yield the same text for
    some concepts and not others.
    """
    if previous is None:
        return sorted(sidecar.concepts)
    if previous.model != model:
        return sorted(sidecar.concepts)
    if previous.fields_hash != sidecar.fields_hash:
        return sorted(sidecar.concepts)

    stale = []
    for name, current in sidecar.concepts.items():
        old = previous.concepts.get(name)
        if old is None or old.hash != current.hash or name not in previous.vectors:
            stale.append(name)
    return sorted(stale)


def reusable_vectors(
    sidecar: EmbeddingsSidecar,
    previous: EmbeddingsSidecar | None,
    stale: list[str],
) -> dict[str, list[float]]:
    """Vectors carried over from a previous run, for concepts that did not change."""
    if previous is None:
        return {}
    stale_set = set(stale)
    return {
        name: vector
        for name, vector in previous.vectors.items()
        if name in sidecar.concepts and name not in stale_set
    }


def encode_concepts(
    sidecar: EmbeddingsSidecar,
    names: list[str],
    model_name: str,
    *,
    model: EmbeddingModel | None = None,
    batch_size: int = 32,
    known_dimensions: int | None = None,
) -> EncodeResult | PipelineError:
    """Computes vectors for `names`, loading the model only if there is work.

    `model` is injectable so the bulk of the suite runs without the extra; in
    normal use it is None and the real model is loaded here.

    `known_dimensions` short-circuits the load when there is nothing to encode:
    loading the model costs ~2.4 s (measured on face85), and a fully cached run
    would otherwise pay it to learn a number the previous run already wrote to
    the sidecar.

    Vectors are L2-normalized at encode time because the index is created with
    `similarity: 'COSINE'`. Normalizing later, or not at all, produces rankings
    that look plausible and are wrong — the kind of failure this codebase has
    already been bitten by twice.
    """
    if not names and known_dimensions:
        return EncodeResult(vectors={}, dimensions=int(known_dimensions), computed=0, reused=0)

    if model is None:
        factory = get_embedding_model_factory()
        if factory is None:
            return DependencyError(
                message="Embedding dependency is missing",
                stage="dependency",
                details=INSTALL_HINT,
            )
        try:
            model = factory(model_name)
        except Exception as exc:  # noqa: BLE001 — surfaced to the user verbatim
            return DependencyError(
                message=f"Could not load embedding model '{model_name}'",
                stage="embeddings",
                details=f"{type(exc).__name__}: {exc}",
            )

    dimensions = model.get_sentence_embedding_dimension()
    if not dimensions:
        return DependencyError(
            message=f"Model '{model_name}' did not report its dimensions",
            stage="embeddings",
            details=(
                "The vector index needs the dimension count; a model that cannot "
                "report it cannot be used."
            ),
        )

    if not names:
        return EncodeResult(vectors={}, dimensions=int(dimensions), computed=0, reused=0)

    texts = [sidecar.concepts[name].text for name in names]
    try:
        raw = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the user verbatim
        return DependencyError(
            message="Embedding generation failed",
            stage="embeddings",
            details=f"{type(exc).__name__}: {exc}",
        )

    vectors = {name: _as_float_list(row) for name, row in zip(names, raw, strict=True)}

    mismatched = [n for n, v in vectors.items() if len(v) != dimensions]
    if mismatched:
        return DependencyError(
            message="Model returned vectors of unexpected width",
            stage="embeddings",
            details=(
                f"Expected {dimensions} dimensions; "
                f"{len(mismatched)} vector(s) differ (e.g. '{mismatched[0]}')."
            ),
        )

    return EncodeResult(
        vectors=vectors,
        dimensions=int(dimensions),
        computed=len(vectors),
        reused=0,
    )


def _as_float_list(row: Any) -> list[float]:
    """Converts one encoded row to plain floats.

    numpy stays inside this module: it arrives with sentence-transformers, and
    `float32` is not JSON-serializable, so the sidecar would fail to write with
    an error pointing at json rather than at the cause.
    """
    if hasattr(row, "tolist"):
        row = row.tolist()
    return [float(v) for v in row]


def generate(
    sidecar: EmbeddingsSidecar,
    previous: EmbeddingsSidecar | None,
    model_name: str,
    *,
    model: EmbeddingModel | None = None,
    rebuild: bool = False,
) -> EmbeddingsSidecar | PipelineError:
    """Fills a sidecar with vectors, reusing whatever is still valid.

    `rebuild=True` forces a full recompute — the escape hatch for a corpus whose
    vectors are suspect for a reason the hashes cannot see.
    """
    baseline = None if rebuild else previous
    stale = stale_concepts(sidecar, baseline, model_name)
    reused = reusable_vectors(sidecar, baseline, stale)

    # Only trust the recorded width when the previous run used this same model:
    # a different model's dimensions say nothing about this one's.
    known = baseline.dimensions if baseline is not None and baseline.model == model_name else None

    result = encode_concepts(sidecar, stale, model_name, model=model, known_dimensions=known)
    if isinstance(result, PipelineError):
        return result

    if reused:
        widths = {len(v) for v in reused.values()}
        if widths != {result.dimensions}:
            # Reaching here means a previous run wrote vectors of a different
            # width under the same model name. Recomputing is the only safe move.
            stale = sorted(sidecar.concepts)
            reused = {}
            result = encode_concepts(sidecar, stale, model_name, model=model)
            if isinstance(result, PipelineError):
                return result

    sidecar.model = model_name
    sidecar.dimensions = result.dimensions
    sidecar.vectors = {**reused, **result.vectors}
    return sidecar


def prepare_embeddings(
    payload: GraphPayload,
    fields: list[str],
    model_name: str,
    project_path: Path | None,
    reporter: Any,
    *,
    rebuild: bool = False,
) -> EmbeddingsSidecar | PipelineError:
    """Validates fields, generates vectors and persists the sidecar.

    The single entry point the CLI needs: everything between "the user named
    some fields" and "there is a sidecar ready to sync".

    Progress goes through `reporter` because the first run downloads the model —
    measured at 282 s for `portuguese-bge-m3` — and silence over that interval is
    indistinguishable from a hang.
    """
    resolved = resolve_fields(fields, payload.field_specs, payload)
    if isinstance(resolved, PipelineError):
        return resolved
    selected, warnings = resolved

    for warning in warnings:
        reporter.warning(warning)

    sidecar = build_sidecar(payload, selected)
    if not sidecar.concepts:
        return DependencyError(
            message="No concept produced any text to embed",
            stage="embeddings",
            details=f"Fields {selected} are empty across all {len(payload.concepts)} concepts.",
        )

    path = sidecar_path(project_path) if project_path else None
    previous = load_sidecar(path) if path else None

    pending = len(stale_concepts(sidecar, None if rebuild else previous, model_name))
    if pending:
        reporter.info(f"Embedding {pending} of {len(sidecar.concepts)} concepts with {model_name}")
        reporter.info("First run downloads the model; this can take several minutes.")
    else:
        reporter.info(f"Reusing {len(sidecar.concepts)} cached embeddings")

    with reporter.step("Generating embeddings"):
        result = generate(sidecar, previous, model_name, rebuild=rebuild)
        if isinstance(result, PipelineError):
            return result

    if path:
        try:
            result.write(path)
            reporter.dest(str(path))
        except OSError as exc:
            # The vectors are in memory and the sync can still proceed; losing the
            # cache costs time on the next run, not correctness.
            reporter.warning(f"Could not write {path.name}: {exc}")

    return result
