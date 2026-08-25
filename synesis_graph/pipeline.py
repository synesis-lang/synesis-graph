"""Top-level pipeline orchestration for synesis-graph."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from synesis_graph.backends.base import (
    ArcadeDBBackendAdapter,
    BackendAdapter,
    Neo4jBackendAdapter,
)
from synesis_graph.backends.html import HTMLBackendAdapter
from synesis_graph.config import (
    BACKEND_ARCADEDB,
    BACKEND_HTML,
    BACKEND_NEO4J,
    SUPPORTED_BACKENDS,
    ArcadeDBConfig,
    HTMLConfig,
    Neo4jConfig,
    PipelineConfig,
    load_config,
    validate_backend_config,
)
from synesis_graph.core import (
    CompilationError,
    ConnectionError,
    GraphPayload,
    PipelineError,
    PipelineResult,
    SyncError,
    _build_graph_payload,
    analyze_template,
    compile_project,
    compile_project_to_json,
    declare_metrics_provenance,
    declare_semantic_capability,
    load_json_project,
    merge_payloads,
)
from synesis_graph.metrics_arcadedb import SCOPE_NOTE as ARCADEDB_SCOPE_NOTE
from synesis_graph.ui import TaskReporter

logger = logging.getLogger("synesis2graph")


def build_backend_adapter(
    backend: str,
    config: PipelineConfig,
    config_path: Path,
    project_path: Path,
) -> BackendAdapter | ConnectionError:
    """Creates the backend adapter for the selected backend."""
    if backend == BACKEND_NEO4J:
        if not isinstance(config, Neo4jConfig):
            return ConnectionError(
                message="Internal configuration type mismatch",
                stage="config",
                details="Expected Neo4jConfig for backend 'neo4j'.",
            )
        return Neo4jBackendAdapter(config)

    if backend == BACKEND_HTML:
        if not isinstance(config, HTMLConfig):
            return ConnectionError(
                message="Internal configuration type mismatch",
                stage="config",
                details="Expected HTMLConfig for backend 'html'.",
            )
        return HTMLBackendAdapter(config, config_path=config_path)

    if backend == BACKEND_ARCADEDB:
        if not isinstance(config, ArcadeDBConfig):
            return ConnectionError(
                message="Internal configuration type mismatch",
                stage="config",
                details="Expected ArcadeDBConfig for backend 'arcadedb'.",
            )
        return ArcadeDBBackendAdapter(config)

    return ConnectionError(
        message="Unsupported backend",
        stage="backend",
        details=f"Supported backends: {', '.join(SUPPORTED_BACKENDS)}",
    )


def execute_backend_pipeline(
    adapter: BackendAdapter,
    payload: GraphPayload,
    reporter: TaskReporter,
) -> PipelineError | None:
    """Executes backend pipeline operations using the adapter contract."""
    operation_error: PipelineError | None = None
    close_error: ConnectionError | None = None

    try:
        connect_error = adapter.connect(reporter)
        if connect_error:
            operation_error = connect_error
        else:
            prepare_error = adapter.prepare_destination(payload, reporter)
            if prepare_error:
                operation_error = prepare_error
            else:
                clear_error = adapter.clear_destination(payload, reporter)
                if clear_error:
                    operation_error = clear_error
                else:
                    sync_error = adapter.synchronize_payload(payload, reporter)
                    if sync_error:
                        operation_error = sync_error
                    else:
                        metrics_error = adapter.compute_backend_metrics(payload, reporter)
                        if metrics_error:
                            operation_error = metrics_error
    except Exception as e:
        operation_error = SyncError(
            message="Unhandled backend execution error",
            stage="sync",
            details=str(e),
        )
    finally:
        try:
            adapter.close()
        except Exception as e:
            close_error = ConnectionError(
                message="Failed to close backend resources",
                stage="shutdown",
                details=str(e),
            )

        if close_error:
            if operation_error is None:
                operation_error = close_error
            else:
                reporter.warning(
                    f"[{adapter.backend_name}] Shutdown warning after prior failure: "
                    f"{close_error.details}"
                )

    return operation_error


# ============================================================================
# MAIN PIPELINE
# ============================================================================
def _compile_and_link(
    project_paths: list[Path],
    reporter: TaskReporter,
) -> GraphPayload | CompilationError:
    """Compiles N projects in isolation and merges them, reifying identities.

    Each member must compile cleanly on its own — a broken member aborts the
    link, since an aggregate built from a partial member would silently miss
    edges.
    """
    members: list[tuple[str, GraphPayload, dict[str, Any]]] = []
    for path in project_paths:
        alias = path.stem
        reporter.info(f"Compiling member '{alias}'")
        json_data = compile_project_to_json(path)
        if isinstance(json_data, CompilationError):
            return json_data

        (
            scalar_fields,
            graph_fields,
            chain_fields,
            code_fields,
            value_maps,
            source_fields,
            memo_field_name,
            quotation_field_name,
        ) = analyze_template(json_data["template"])

        payload = _build_graph_payload(
            json_data=json_data,
            scalar_fields=scalar_fields,
            graph_fields=graph_fields,
            chain_fields=chain_fields,
            code_fields=code_fields,
            value_maps=value_maps,
            source_fields=source_fields,
            memo_field_name=memo_field_name,
            quotation_field_name=quotation_field_name,
            # Each member gets ITS OWN root, before `merge_payloads`: in a linked
            # study the projects live in different directories, and there is no
            # single root that is correct for all of them.
            project_root=str(path.parent.resolve()).replace("\\", "/"),
        )
        members.append((alias, payload, json_data))

    return merge_payloads(members, reporter)


def run_pipeline(
    project_path: Path | None,
    config_path: Path,
    reporter: TaskReporter,
    backend: str = BACKEND_NEO4J,
    html_options: dict[str, Any] | None = None,
    json_path: Path | None = None,
    database: str | None = None,
    extra_projects: list[Path] | None = None,
    vector_embeddings: list[str] | None = None,
    rebuild_embeddings: bool = False,
) -> PipelineResult:
    """
    Executes complete pipeline: compilation → connection → synchronization.

    Args:
        project_path: Path to .synp project (mutually exclusive with json_path)
        config_path: Path to config.toml
        reporter: Reporter for visual feedback
        html_options: Optional CLI overrides for the HTML backend (keys match HTMLConfig fields)
        json_path: Path to pre-compiled Synesis JSON export (alternative to project_path)
        database: when set, overrides payload.project_name — this names the Neo4j
            database (sanitized) and the HTML graph title. It is the intended way
            to name the unified graph of several linked projects.
        extra_projects: Additional .synp projects to link with project_path. When
            present, identities declared with IDENTIFIES/REFERS TO are reified
            across all members. Defaults to None (single-project path unchanged).
        vector_embeddings: ontology field names to embed (ArcadeDB only).
            Overrides [arcadedb.embeddings].fields, as --database overrides its
            config counterpart.
        rebuild_embeddings: recompute every vector, ignoring the cached sidecar.

    Returns:
        PipelineResult indicating success or typed error.
    """
    # 1. Input validation
    if backend not in SUPPORTED_BACKENDS:
        return PipelineResult(
            success=False,
            error=ConnectionError(
                message="Unsupported backend",
                stage="backend",
                details=f"Supported backends: {', '.join(SUPPORTED_BACKENDS)}",
            ),
        )

    if json_path is None and project_path is None:
        return PipelineResult(
            success=False,
            error=CompilationError(
                message="Either --project or --json must be specified",
                stage="validation",
            ),
        )

    source_path = json_path or project_path
    if not source_path.exists():
        return PipelineResult(
            success=False,
            error=CompilationError(
                message="Input file not found", stage="validation", details=str(source_path)
            ),
        )

    # 2. Configuration
    with reporter.step("Loading Configuration"):
        config_result = load_config(config_path, backend)
        if isinstance(config_result, ConnectionError):
            return PipelineResult(success=False, error=config_result)
        config = config_result

        if backend == BACKEND_HTML and html_options and isinstance(config, HTMLConfig):
            for k, v in html_options.items():
                if v is not None and hasattr(config, k):
                    setattr(config, k, v)

        if database and isinstance(config, (Neo4jConfig, ArcadeDBConfig)):
            config.database = database

        config_error = validate_backend_config(config, backend)
        if config_error:
            return PipelineResult(success=False, error=config_error)

    adapter_result = build_backend_adapter(
        backend=backend,
        config=config,
        config_path=config_path,
        project_path=project_path or Path("."),
    )
    if isinstance(adapter_result, ConnectionError):
        reporter.error(f"Backend error: {adapter_result.message} — {adapter_result.details}")
        return PipelineResult(
            success=False,
            error=adapter_result,
        )
    adapter = adapter_result

    preflight_error = adapter.preflight(reporter)
    if preflight_error:
        reporter.error(f"Preflight failed: {preflight_error.message} — {preflight_error.details}")
        return PipelineResult(success=False, error=preflight_error)

    # 3. Compilation or JSON load
    if extra_projects:
        # Multi-project: each member compiles in isolation and the identities
        # declared with IDENTIFIES/REFERS TO are reified across them.
        all_projects = [project_path, *extra_projects]
        step_label = f"Compiling and Linking {len(all_projects)} Projects"

        def load_fn():
            return _compile_and_link(all_projects, reporter)
    elif json_path is not None:
        step_label = "Loading JSON Export"

        def load_fn():
            return load_json_project(json_path, reporter)
    else:
        step_label = "Compiling Project (In-Memory)"

        def load_fn():
            return compile_project(project_path, reporter)

    with reporter.step(step_label):
        compile_result = load_fn()
        if isinstance(compile_result, CompilationError):
            reporter.print_diagnostics(compile_result.diagnostics)
            return PipelineResult(success=False, error=compile_result)
        payload = compile_result

    # An explicit --database names the aggregate: it becomes payload.project_name,
    # which drives the Neo4j database name (sanitized) and the HTML graph title.
    # This is the intended way to name the unified graph of several linked
    # projects, e.g. `--project lattes.synp --project abstracts.synp --database
    # Quinto_Andar`. Without it, the name is derived (the .synp project name for a
    # single project, or the members joined by "_" for a link).
    if database:
        payload.project_name = database

    # 3b. Embeddings, when asked for. Runs before the sync so a validation error
    # (an unknown field name) costs nothing: the database is untouched.
    if backend == BACKEND_ARCADEDB and isinstance(config, ArcadeDBConfig):
        fields = vector_embeddings or config.embedding_fields
        if fields:
            from synesis_graph.embeddings_provider import prepare_embeddings

            sidecar = prepare_embeddings(
                payload=payload,
                fields=list(fields),
                model_name=config.embedding_model,
                project_path=project_path,
                reporter=reporter,
                rebuild=rebuild_embeddings,
            )
            if isinstance(sidecar, PipelineError):
                reporter.error(f"{sidecar.message} — {sidecar.details}")
                return PipelineResult(success=False, error=sidecar)
            # Vectors are ArcadeDB-only, so the sidecar rides on that adapter
            # rather than on the BackendAdapter contract every backend shares.
            if isinstance(adapter, ArcadeDBBackendAdapter):
                adapter.embeddings = sidecar

                # Declare the capability on the context, so a client learns from
                # the graph itself which fields it can search by meaning — and
                # that those results are approximate. Here and not in
                # `build_project_context` because the sidecar only exists now.
                declare_semantic_capability(
                    payload.project_context,
                    sidecar.fields,
                    sidecar.model or "",
                    sidecar.dimensions or 0,
                )

    # Metrics provenance, declared BEFORE the sync writes the context vertex.
    #
    # The metrics themselves are computed after the sync — but which backend runs
    # them, and over what projection, is known now. It has to be declared here or
    # it never reaches the graph, and without it a consumer ranking concepts by
    # PageRank cannot tell that ArcadeDB's score includes edges to Items, Sources
    # and taxonomy nodes.
    if isinstance(adapter, ArcadeDBBackendAdapter):
        declare_metrics_provenance(
            payload.project_context, "arcadedb", "whole_graph", ARCADEDB_SCOPE_NOTE
        )
    else:
        declare_metrics_provenance(payload.project_context, "neo4j", "concept_subgraph")

    # 4. Backend synchronization via adapter contract
    backend_error = execute_backend_pipeline(adapter, payload, reporter)
    if backend_error:
        reporter.error(f"Backend sync failed: {backend_error.message} — {backend_error.details}")
        return PipelineResult(
            success=False,
            error=backend_error,
        )

    return PipelineResult(
        success=True,
        stats={
            "concepts": len(payload.concepts),
            "sources": len(payload.sources),
            "items": len(payload.items),
            "chains": len(payload.chains),
        },
    )
