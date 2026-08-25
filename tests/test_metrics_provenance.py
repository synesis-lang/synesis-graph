"""Network metrics carry their backend and scope.

This is not bookkeeping — it changes how the number must be read. ArcadeDB's
`algo.*` procedures accept no scope filter and run over the WHOLE graph, so a
concept's PageRank incorporates its edges to Items, Sources and taxonomy nodes;
Neo4j's GDS projects only the concept subgraph.

The two are not comparable, and a consumer ranking "most central concepts" has
no way to know that from the score alone.
"""

from __future__ import annotations

from synesis_graph.core import ProjectContextSpec, declare_metrics_provenance
from synesis_graph.metrics_arcadedb import SCOPE_NOTE


def _context() -> ProjectContextSpec:
    return ProjectContextSpec(
        project_name="face85",
        description="",
        concept_label="Chain",
        template_name="t",
        template_doc="",
        project_summary="resumo",
        compiler_version="",
        synesis_graph_version="",
        compiled_at="",
        generated_at="",
        source_count=0,
        item_count=0,
        concept_count=0,
    )


def test_arcadedb_declares_whole_graph_scope():
    context = _context()
    declare_metrics_provenance(context, "arcadedb", "whole_graph", SCOPE_NOTE)

    assert context.metrics_backend == "arcadedb"
    assert context.metrics_scope == "whole_graph"


def test_neo4j_declares_the_concept_subgraph():
    context = _context()
    declare_metrics_provenance(context, "neo4j", "concept_subgraph")

    assert context.metrics_scope == "concept_subgraph"


def test_the_scope_note_reaches_the_prose_block():
    """The scalar property alone would not tell a consumer why the score differs."""
    context = _context()
    declare_metrics_provenance(context, "arcadedb", "whole_graph", SCOPE_NOTE)

    assert "whole graph" in context.project_summary
    assert "not directly comparable" in context.project_summary


def test_centrality_is_presented_as_a_methodological_choice():
    """Degree, PageRank and betweenness answer different questions."""
    context = _context()
    declare_metrics_provenance(context, "neo4j", "concept_subgraph")

    assert "escolha metodológica" in context.project_summary
    assert "Diga qual usou" in context.project_summary


def test_nothing_declared_without_backend_or_scope():
    context = _context()
    declare_metrics_provenance(context, "", "whole_graph")
    declare_metrics_provenance(context, "arcadedb", "")

    assert context.metrics_backend == ""
    assert "Métricas de rede" not in context.project_summary


def test_tolerates_absent_context():
    declare_metrics_provenance(None, "arcadedb", "whole_graph")
