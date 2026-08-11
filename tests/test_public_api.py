"""Public-API contract for synesis-graph.

This protects the Phase 6 refactor: whatever happens to the internal module
layout, these names must remain importable from both `synesis_graph` (the
package) and `synesis2graph` (the shim / original script).
"""

from __future__ import annotations

import subprocess
import sys

# The names the package and CLI depend on. The CLI imports run_pipeline and
# TaskReporter directly from synesis2graph in each subcommand body, so the shim
# must keep exposing them too.
_PUBLIC_NAMES = [
    "run_pipeline",
    "compile_project",
    "load_json_project",
    "GraphPayload",
    "PipelineResult",
    "BACKEND_NEO4J",
    "BACKEND_HTML",
    "SUPPORTED_BACKENDS",
]


def test_package_exposes_public_api():
    import synesis_graph

    for name in _PUBLIC_NAMES:
        assert hasattr(synesis_graph, name), f"synesis_graph missing public name: {name}"


def test_shim_exposes_public_api_and_taskreporter():
    import synesis2graph

    for name in [*_PUBLIC_NAMES, "TaskReporter"]:
        assert hasattr(synesis2graph, name), f"synesis2graph missing: {name}"


def test_graphpayload_new_fields_are_additive_with_defaults():
    """item_fields / taxonomy_edges must be optional (default empty) so existing
    GraphPayload construction sites keep working and Neo4j sync is unaffected."""
    from synesis_graph import GraphPayload

    payload = GraphPayload(
        project_name="p",
        concept_label="Concept",
        scalar_fields=[],
        graph_fields=[],
        chain_fields=[],
        code_fields=[],
        source_fields=[],
        value_maps={},
        concepts=[],
        sources=[],
        items=[],
        chains=[],
        mentions=[],
        from_source=[],
    )
    assert payload.item_fields == {}
    assert payload.taxonomy_edges == []


def test_direct_script_version_runs():
    """`python synesis2graph.py --version` must keep working post-refactor."""
    import synesis2graph

    script = synesis2graph.__file__
    result = subprocess.run(
        [sys.executable, script, "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0
    assert "synesis-graph" in result.stdout
