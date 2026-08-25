"""The graph declares what it can answer semantically.

A client can see from `get_schema` that a vector index exists, but not which
field produced the vectors — and that changes what proximity means: over
`ontology_description` it is conceptual similarity, over `topic` it is thematic
co-occurrence. Declaring the composition is what lets a consumer know which one
it is interpreting.

The declaration also carries the warning that matters to a researcher: results
by proximity are approximate. A vector neighbour is a reading suggestion, not
something the researcher asserted.
"""

from __future__ import annotations

from synesis_graph.core import ProjectContextSpec, declare_semantic_capability


def _context(**overrides) -> ProjectContextSpec:
    base = dict(
        project_name="face85",
        description="",
        concept_label="Chain",
        template_name="t",
        template_doc="",
        project_summary="## Conteúdo deste grafo\n- 210 conceitos",
        compiler_version="1.1",
        synesis_graph_version="0.8.0",
        compiled_at="",
        generated_at="",
        source_count=20,
        item_count=174,
        concept_count=210,
    )
    base.update(overrides)
    return ProjectContextSpec(**base)


# ---------------------------------------------------------------------------
# Declaração
# ---------------------------------------------------------------------------


def test_default_is_no_semantic_search():
    """Um grafo sem vetores não anuncia capacidade nenhuma."""
    context = _context()

    assert context.embedding_fields == ""
    assert context.embedding_model == ""
    assert context.embedding_dimensions == 0


def test_declares_fields_model_and_dimensions():
    context = _context()
    declare_semantic_capability(context, ["ontology_description"], "all-MiniLM-L6-v2", 384)

    assert context.embedding_fields == "ontology_description"
    assert context.embedding_model == "all-MiniLM-L6-v2"
    assert context.embedding_dimensions == 384


def test_field_order_is_preserved():
    """A ordem muda o texto concatenado e portanto os vetores.

    `EmbeddingsSidecar.fields_hash` existe por essa razão; declarar a composição
    fora de ordem descreveria um índice que não é o que está no banco.
    """
    context = _context()
    declare_semantic_capability(context, ["ontology_description", "topic"], "m", 384)

    assert context.embedding_fields == "ontology_description,topic"


# ---------------------------------------------------------------------------
# Recusa de declaração parcial
# ---------------------------------------------------------------------------


def test_missing_parts_declare_nothing():
    """Meia declaração é pior que nenhuma: o consumidor consultaria por uma
    composição de campos que nunca existiu."""
    for fields, model, dims in [
        ([], "m", 384),  # sem campos
        (["f"], "", 384),  # sem modelo
        (["f"], "m", 0),  # sem dimensões
        (["f"], "m", -1),  # dimensão inválida
    ]:
        context = _context()
        declare_semantic_capability(context, fields, model, dims)
        assert context.embedding_fields == "", f"declarou com {fields}/{model}/{dims}"
        assert context.embedding_dimensions == 0


def test_tolerates_absent_context():
    """Payload montado à mão não tem contexto; não é erro."""
    declare_semantic_capability(None, ["f"], "m", 384)  # não deve levantar


def test_summary_untouched_when_nothing_is_declared():
    context = _context()
    before = context.project_summary
    declare_semantic_capability(context, [], "", 0)

    assert context.project_summary == before


# ---------------------------------------------------------------------------
# O que o cliente MCP lê de fato
# ---------------------------------------------------------------------------


def test_summary_announces_the_capability():
    """As propriedades escalares só são vistas por quem for procurá-las; o
    `project_summary` é o que chega ao modelo como texto."""
    context = _context()
    declare_semantic_capability(context, ["ontology_description"], "all-MiniLM-L6-v2", 384)

    assert "Busca semântica" in context.project_summary
    assert "ontology_description" in context.project_summary
    assert "384" in context.project_summary


def test_summary_warns_that_proximity_is_approximate():
    """A distinção que a pesquisa exige: proximidade é inferência do modelo de
    embeddings, não afirmação do pesquisador."""
    context = _context()
    declare_semantic_capability(context, ["ontology_description"], "m", 384)

    assert "aproximada" in context.project_summary
    assert "sugestão de leitura" in context.project_summary


def test_summary_keeps_the_previous_content():
    """A declaração é acrescentada, não substitui as contagens já renderizadas."""
    context = _context()
    declare_semantic_capability(context, ["ontology_description"], "m", 384)

    assert "210 conceitos" in context.project_summary


def test_summary_names_the_concept_label():
    """O rótulo varia por projeto; o cliente precisa saber sobre qual vértice
    o índice vetorial existe."""
    context = _context(concept_label="Code")
    declare_semantic_capability(context, ["ontology_description"], "m", 384)

    assert "`Code`" in context.project_summary
