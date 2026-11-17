"""Hierarquia adotada no grafo (etapa A4 do plano ROLE/EXPORT).

Arestas KIND_OF / PART_OF / INSTANCE_OF a partir de `indices.relations` do JSON
3.1 (visao canonica, o .syno), com o campo que declara a especie, gravadas
pelos dois backends e removidas na recarga em modo update.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from synesis_graph.backends.neo4j import _sync_hierarchy
from synesis_graph.core import build_hierarchy_edges

JSON = {
    "template": {
        "field_specs": {
            "tipo_de": {"role": "HIERARCHY KIND"},
            "parte_de": {"role": "HIERARCHY PART"},
            "definicao": {"role": "NOTE DEFINITION"},
        }
    },
    "indices": {
        "relations": {
            "KIND": [
                ["phishing", "fraude_online"],
                ["phishing", "engenharia_social"],
                ["smishing", "phishing"],
            ],
            "PART": [["link", "mensagem"]],
            "INSTANCE": [],
        }
    },
}


def test_edges_come_from_the_relation_index_with_their_field():
    edges = build_hierarchy_edges(JSON)
    assert {
        "child": "phishing",
        "parent": "engenharia_social",
        "type": "KIND_OF",
        "field": "tipo_de",
    } in edges
    assert {"child": "link", "parent": "mensagem", "type": "PART_OF", "field": "parte_de"} in edges
    # dois pais: duas arestas
    assert sum(1 for e in edges if e["child"] == "phishing") == 2


def test_names_are_reconciled_with_the_ontology_spelling():
    edges = build_hierarchy_edges(JSON, {"smishing": "Smishing", "phishing": "Phishing"})
    assert {
        "child": "Smishing",
        "parent": "Phishing",
        "type": "KIND_OF",
        "field": "tipo_de",
    } in edges


def test_template_without_hierarchy_has_no_edges():
    assert build_hierarchy_edges({"indices": {}, "template": {"field_specs": {}}}) == []


def _queries(mode):
    tx = MagicMock()
    _sync_hierarchy(tx, build_hierarchy_edges(JSON), "Concept", mode=mode)
    return [c.args[0] for c in tx.run.call_args_list]


def test_update_removes_old_hierarchy_edges_before_writing():
    queries = _queries("update")
    deletes = [q for q in queries if "DELETE r" in q]
    assert len(deletes) == 3
    assert queries.index(deletes[-1]) < next(i for i, q in enumerate(queries) if "MERGE" in q)


def test_rebuild_does_not_delete_and_writes_each_type():
    queries = _queries("rebuild")
    assert not any("DELETE" in q for q in queries)
    assert any("MERGE (c)-[r:KIND_OF]->(p)" in q for q in queries)
    assert any("MERGE (c)-[r:PART_OF]->(p)" in q for q in queries)
    assert not any("INSTANCE_OF]->(p)" in q for q in queries)  # sem linhas, sem consulta


SYNP = (
    Path(__file__).resolve().parents[2]
    / "case-studies"
    / "Exemplo_Tipos_Relacionais"
    / "fraudes.synp"
)


@pytest.mark.skipif(not SYNP.exists(), reason="case-studies ausente")
def test_test_project_payload_carries_the_hierarchy():
    from synesis_graph.core import compile_project

    payload = compile_project(SYNP, MagicMock())
    types = {e["type"] for e in payload.hierarchy_edges}
    assert types == {"KIND_OF", "PART_OF", "INSTANCE_OF"}
    parents = {e["parent"] for e in payload.hierarchy_edges if e["child"] == "phishing"}
    assert parents == {"fraude_online", "engenharia_social"}
    # a camada de Ontologia do HTML desenha as mesmas arestas
    assert any(e["type"] == "KIND_OF" for e in payload.taxonomy_edges)


@pytest.mark.skipif(not SYNP.exists(), reason="case-studies ausente")
def test_html_ontology_layer_draws_the_hierarchy():
    from synesis_graph.backends.html import HTMLConfig, _html_render_payload
    from synesis_graph.core import compile_project

    template = Path(__file__).resolve().parents[1] / "templates" / "graph.html.tmpl"
    payload = compile_project(SYNP, MagicMock())
    html = _html_render_payload(payload, HTMLConfig(), template)
    # painel Ontologia do conceito: pais e filhos, com a especie
    assert "fraude_online (KIND)" in html and "engenharia_social (KIND)" in html
    assert "mensagem_de_phishing (PART)" in html


@pytest.mark.skipif(not SYNP.exists(), reason="case-studies ausente")
def test_context_teaches_the_subtree_query():
    from synesis_graph.core import compile_project

    payload = compile_project(SYNP, MagicMock())
    doc = payload.project_context.template_doc
    assert "KIND_OF|PART_OF|INSTANCE_OF" in doc
    assert "count(DISTINCT i)" in doc


def test_context_without_hierarchy_does_not_mention_it():
    from synesis_graph.core import _render_navigation_doc

    assert not any("KIND_OF" in line for line in _render_navigation_doc("Concept", []))
