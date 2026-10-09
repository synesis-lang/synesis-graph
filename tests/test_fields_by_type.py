"""Campos identificados pelo TIPO do template, não pelo nome (auditoria de 2026-10-09).

- CHAIN: a extração lia `data["chain"]` e ignorava `chain_field_names`, que já
  é montado pelo tipo — um CHAIN com outro nome não gerava cadeia nenhuma.
- CODE: a descrição de cada código vinha de `justificativa_interna`/`descricao`
  (nomes de um template); o template declara a ligação por BUNDLE.
- TOPIC: os recursos de tópico procuravam o nome `topic`; 11 templates dos
  estudos de caso usam `group`, `grupo`, `topico` ou `category`.
"""

from __future__ import annotations

from typing import Any

from synesis_graph.core import (
    _build_taxonomy_edges,
    _extract_corpus_data,
    code_description_fields,
    topic_field_of,
)


def _extract(corpus: list[dict[str, Any]], **kw: Any):
    args: dict[str, Any] = {
        "bibliography": {},
        "relation_definitions": {},
        "code_field_names": [],
        "chain_field_names": [],
        "source_fields": [],
        "ontology_field_names": [],
        "memo_field_name": "note",
        "quotation_field_name": "citation",
    }
    args.update(kw)
    return _extract_corpus_data(corpus, **args)


class TestChainPeloTipo:
    CORPUS = [{
        "id": "x_item0001", "source_ref": "@x",
        "data": {
            "trecho": "A confiança aumenta a aceitação.",
            "relacoes": [{"from": "confianca", "relation": "AUMENTA", "to": "aceitacao"}],
            "causas": [{"from": "custo", "relation": "REDUZ", "to": "aceitacao"}],
        },
    }]

    def test_nome_arbitrario_gera_cadeias(self):
        _s, items, mentions, chains, _f, _i = _extract(
            self.CORPUS, chain_field_names=["relacoes", "causas"], quotation_field_name="trecho")
        assert {(c["source"], c["target"]) for c in chains} == {
            ("confianca", "aceitacao"), ("custo", "aceitacao")}
        assert len(items) == 2
        assert items[0]["citation"] == "A confiança aumenta a aceitação."

    def test_sem_template_cai_no_nome_chain(self):
        corpus = [{"id": "y_item0001", "source_ref": "@y",
                   "data": {"chain": [{"from": "a", "relation": "R", "to": "b"}]}}]
        _s, _i, _m, chains, _f, _x = _extract(corpus)
        assert [(c["source"], c["target"]) for c in chains] == [("a", "b")]


class TestCodePeloBundle:
    TEMPLATE = {"bundled_fields": {"ITEM": [["ordem_2a", "justificativa_interna"]]}}

    def test_parceiro_de_bundle(self):
        esperado = {"ordem_2a": "justificativa_interna"}
        assert code_description_fields(self.TEMPLATE, ["ordem_2a"]) == esperado
        assert code_description_fields({}, ["ordem_2a"]) == {}

    def test_descricao_e_trecho_pelo_template(self):
        corpus = [{"id": "d_item0001", "source_ref": "@d", "data": {
            "ordem_1a": "Relato de primeira ordem.",
            "ordem_2a": ["fe_madura"],
            "criterio": ["Critério aplicado."],
        }}]
        _s, items, _m, _c, _f, _x = _extract(
            corpus, code_field_names=["ordem_2a"], memo_field_name="ordem_1a",
            quotation_field_name="sem_quotation",
            code_descriptions={"ordem_2a": "criterio"})
        assert items[0]["citation"] == "Relato de primeira ordem."   # MEMO do template
        assert items[0]["description"] == "Critério aplicado."          # parceiro de BUNDLE


class TestTopicoPeloTipo:
    SPECS = {"grupo": {"type": "TOPIC", "scope": "ONTOLOGY"},
             "aspect": {"type": "ORDERED", "scope": "ONTOLOGY"}}

    def test_campo_de_topico_pelo_tipo(self):
        assert topic_field_of(["aspect", "grupo"], self.SPECS) == "grupo"
        assert topic_field_of(["topic", "aspect"], None) == "topic"      # sem tipo: o nome
        assert topic_field_of(["aspect"], None) is None

    def test_arestas_de_topico_com_outro_nome(self):
        concepts = [
            {"props": {"name": "confianca"}, "relations": {"grupo": "Social", "aspect": "Etico"}},
            {"props": {"name": "custo"}, "relations": {"grupo": "Economico", "aspect": "Etico"}},
        ]
        chains = [{"source": "custo", "target": "confianca"}]
        edges = _build_taxonomy_edges(concepts, ["grupo", "aspect"], chains, self.SPECS)
        tipos = {(e["source"], e["target"], e["type"]) for e in edges}
        assert ("Social", "Etico", "MAPPED_TO_ASPECT") in tipos
        assert ("Economico", "Social", "IS_LINKED_TO") in tipos

    def test_sem_field_specs_mantem_o_comportamento_antigo(self):
        concepts = [{"props": {"name": "c"}, "relations": {"grupo": "G", "aspect": "A"}}]
        edges = _build_taxonomy_edges(concepts, ["grupo", "aspect"], [])
        assert not any(e["type"] == "MAPPED_TO_ASPECT" for e in edges)


class TestNeo4jTopicoPeloTipo:
    def test_consultas_usam_rotulo_e_relacao_do_campo_topic(self):
        from unittest.mock import MagicMock

        from synesis_graph.backends.neo4j import _sync_taxonomies

        tx = MagicMock()
        concepts = [{"props": {"name": "c"}, "relations": {"grupo": "G", "aspect": "A"}}]
        _sync_taxonomies(tx, concepts, ["grupo", "aspect"], "Conceito",
                         field_specs=TestTopicoPeloTipo.SPECS)
        cypher = " ".join(str(c.args[0]) for c in tx.run.call_args_list)
        assert "MATCH (topic:Grupo" in cypher                       # mapeamento Topic -> Aspect
        assert "(t1:Grupo)<-[:HAS_GRUPO]-" in cypher                # Topic -> Topic
        assert ":Topic" not in cypher
