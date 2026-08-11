"""test_linkage.py - Etapa 4: reificacao de entidades (IDENTIFIES / REFERS TO).

Cobre:
  - extracao das declaracoes de ligacao do template (field_specs);
  - resolucao de nos reificados e arestas REFERS_TO sobre projetos reais;
  - o no nasce SO de IDENTIFIES (orfao nao cria stub);
  - namespacing de bibref por membro (dissolve colisao entre corpora);
  - sync Neo4j emite as queries de entidade/aresta e as constraints;
  - nao-regressao: projeto sem modificadores nao reifica nada.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from synesis_graph.backends.neo4j import _create_constraints, _sync_entities, _sync_refers_to
from synesis_graph.core import compile_project_to_json
from synesis_graph.linkage import (
    edges_as_rows,
    entities_as_rows,
    entity_label,
    extract_linkage_spec,
    resolve_linkage,
)

FIXTURES = Path(__file__).parent / "Link_Projects"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTx:
    """Records the Cypher queries and params it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(self, query: str, **params: Any) -> None:
        self.calls.append((query, params))

    def queries(self) -> str:
        return "\n".join(q for q, _p in self.calls)


@pytest.fixture(scope="module")
def members() -> list[dict[str, Any]]:
    out = []
    for alias in ("lattes", "abstracts"):
        data = compile_project_to_json(FIXTURES / alias / f"{alias}.synp")
        assert not hasattr(data, "message"), f"{alias} failed to compile: {data}"
        out.append({"alias": alias, "json_data": data})
    return out


def _member(alias: str, template: dict, bibliography: dict) -> dict:
    """Monta um membro sintetico com corpus derivado da bibliography.

    Os bibrefs da linkagem sao ancorados em corpus[].source_ref (como no export
    v3.0 real e como o Source node os grava), nao nas chaves da bibliography.
    Aqui source_ref = `@<chave>`, casando a chave para a resolucao de valores.
    """
    corpus = [{"source_ref": f"@{key}"} for key in bibliography]
    return {
        "alias": alias,
        "json_data": {"template": template, "bibliography": bibliography, "corpus": corpus},
    }


# ---------------------------------------------------------------------------
# entity_label
# ---------------------------------------------------------------------------


def test_entity_label_capitalizes():
    assert entity_label("researcher") == "Researcher"


def test_entity_label_handles_snake_case():
    assert entity_label("research_group") == "ResearchGroup"


def test_entity_label_sanitizes_unsafe_chars():
    # O rotulo vem do template do usuario e e interpolado em Cypher — os
    # caracteres perigosos precisam sumir antes de chegar na query.
    label = entity_label("re$search;er")
    assert ";" not in label and "$" not in label
    from synesis_graph.sanitize import validate_cypher_label

    assert validate_cypher_label(label)


def test_entity_label_never_empty():
    assert entity_label("$$$") == "Unknown"


# ---------------------------------------------------------------------------
# extract_linkage_spec
# ---------------------------------------------------------------------------


def test_extract_identifies_from_template(members):
    spec = extract_linkage_spec(members[0]["json_data"]["template"])
    assert [(d.field_name, d.entity) for d in spec.identifies] == [("lattes_id", "researcher")]
    assert spec.refers_to == []


def test_extract_refers_to_from_template(members):
    spec = extract_linkage_spec(members[1]["json_data"]["template"])
    assert [(d.field_name, d.entity) for d in spec.refers_to] == [("lattes_id", "researcher")]
    assert spec.identifies == []


def test_extract_on_project_without_modifiers_is_empty():
    spec = extract_linkage_spec({"field_specs": {"description": {"name": "description"}}})
    assert spec.is_empty()


# ---------------------------------------------------------------------------
# resolve_linkage
# ---------------------------------------------------------------------------


def test_resolve_creates_one_node_per_distinct_key(members):
    r = resolve_linkage(members)
    assert len(r.entities) == 1
    e = r.entities[0]
    assert e.label == "Researcher"
    assert e.entity_id == "3474555741700167"
    assert e.member == "lattes"


def test_resolve_creates_n_to_one_edges(members):
    r = resolve_linkage(members)
    # artigo_a e artigo_b apontam ao mesmo pesquisador
    assert len(r.edges) == 2
    assert {e.from_bibtex for e in r.edges} == {"abstracts:@artigo_a", "abstracts:@artigo_b"}
    assert all(e.entity_id == "3474555741700167" for e in r.edges)


def test_orphan_creates_no_node(members):
    r = resolve_linkage(members)
    assert ("researcher", "9999999999999999", "abstracts") in r.orphans
    # o orfao NAO vira no reificado
    assert all(e.entity_id != "9999999999999999" for e in r.entities)


def test_owner_is_the_identifies_side(members):
    r = resolve_linkage(members)
    assert r.owners == {"researcher": "lattes"}


def test_node_source_bibtex_is_qualified(members):
    r = resolve_linkage(members)
    assert r.entities[0].source_bibtex == "lattes:@curriculo_pesq1"


def test_single_member_alone_resolves_no_edges(members):
    """Um projeto so com IDENTIFIES cria o no; sem REFERS TO nao ha aresta."""
    r = resolve_linkage([members[0]])
    assert len(r.entities) == 1
    assert r.edges == []


def test_refers_to_alone_creates_nothing(members):
    """Sem o dono, o REFERS TO fica orfao — nenhum no stub."""
    r = resolve_linkage([members[1]])
    assert r.entities == []
    assert r.edges == []
    assert len(r.orphans) == 3


def test_duplicate_entity_owner_is_reported():
    """Dois membros com IDENTIFIES do mesmo rotulo: o 2o e recusado."""
    tmpl = {"field_specs": {"k": {"identifies": "researcher"}}}
    m = [
        _member("a", tmpl, {"x": {"k": "1"}}),
        _member("b", tmpl, {"y": {"k": "2"}}),
    ]
    r = resolve_linkage(m)
    assert r.duplicate_owners == [("researcher", "a", "b")]
    assert r.owners == {"researcher": "a"}


def test_exact_match_no_case_folding():
    """Regra anti-fuzzy: difere so em caixa -> orfao, nunca aresta."""
    m = [
        _member(
            "owner",
            {"field_specs": {"h": {"identifies": "account"}}},
            {"x": {"h": "@thiagonogueira"}},
        ),
        _member(
            "ref", {"field_specs": {"h": {"refers_to": "account"}}}, {"y": {"h": "@ThiagoNogueira"}}
        ),
    ]
    r = resolve_linkage(m)
    assert r.edges == []
    assert ("account", "@ThiagoNogueira", "ref") in r.orphans


def test_multivalued_field_yields_one_edge_per_value():
    m = [
        _member(
            "owner",
            {"field_specs": {"k": {"identifies": "person"}}},
            {"a": {"k": "1"}, "b": {"k": "2"}},
        ),
        _member(
            "hub", {"field_specs": {"links": {"refers_to": "person"}}}, {"h": {"links": ["1", "2"]}}
        ),
    ]
    r = resolve_linkage(m)
    assert len(r.edges) == 2
    assert {e.entity_id for e in r.edges} == {"1", "2"}


def test_bibref_shared_between_members_does_not_collide():
    """linkedin.bib e posts.bib compartilham bibref no corpus real."""
    shared = "thiago-nogueira-60854758"
    m = [
        _member("owner", {"field_specs": {"k": {"identifies": "person"}}}, {shared: {"k": "1"}}),
        _member("posts", {"field_specs": {"k": {"refers_to": "person"}}}, {shared: {"k": "1"}}),
    ]
    r = resolve_linkage(m)
    assert r.entities[0].source_bibtex == f"owner:@{shared}"
    assert r.edges[0].from_bibtex == f"posts:@{shared}"


def test_entity_source_bibtex_uses_original_case_from_corpus():
    """Regressao: bibref com caixa mista deve casar com o Source node.

    O bib_loader normaliza as CHAVES da bibliography em minusculas, mas o Source
    node grava o bibtex na caixa do bloco SOURCE (corpus[].source_ref). Se o
    Researcher.source_bibtex viesse da chave normalizada, o MATCH case-sensitive
    do sync nunca criaria a aresta IDENTIFIED_AS (bug observado com
    `@Vitor_Mourao_Hanriot_...`). Ancorar em corpus[].source_ref alinha os dois.
    """
    template = {"field_specs": {"lattes_id": {"identifies": "researcher"}}}
    # Chave normalizada na bibliography (minuscula), mas corpus com caixa original.
    json_data = {
        "template": template,
        "bibliography": {
            "vitor_mourao_hanriot_3474555741700167": {"lattes_id": "3474555741700167"}
        },
        "corpus": [{"source_ref": "@Vitor_Mourao_Hanriot_3474555741700167"}],
    }
    r = resolve_linkage([{"alias": "lattes", "json_data": json_data}])
    assert len(r.entities) == 1
    # caixa ORIGINAL do corpus, nao a chave minuscula da bibliography
    assert r.entities[0].source_bibtex == "lattes:@Vitor_Mourao_Hanriot_3474555741700167"


def test_entity_source_bibtex_matches_source_node_bibtex():
    """O source_bibtex do Researcher casa EXATAMENTE com o bibtex do Source node.

    Verificacao ponta-a-ponta sobre o corpus real Quinto_Andar: o defeito so se
    manifesta quando o bibref do dono tem letra maiuscula.
    """
    lattes = Path(
        r"d:\GitHub\case-studies\Quinto_Andar\Projetos_Unificados\Dados_Lattes\lattes.synp"
    )
    abstracts = Path(
        r"d:\GitHub\case-studies\Quinto_Andar\Projetos_Unificados\Dados_Abstracts\abstracts.synp"
    )
    if not lattes.exists() or not abstracts.exists():
        pytest.skip("corpus Quinto_Andar indisponivel")

    from synesis_graph.pipeline import _compile_and_link
    from synesis_graph.ui import TaskReporter

    payload = _compile_and_link([lattes, abstracts], TaskReporter("test"))
    source_bibtexs = {s["bibtex"] for s in payload.sources}
    for rows in payload.entities.values():
        for row in rows:
            assert row["source_bibtex"] in source_bibtexs, (
                f"{row['source_bibtex']!r} nao casa com nenhum Source.bibtex"
            )


# ---------------------------------------------------------------------------
# _build_source_props — campos SCOPE SOURCE nao podem sumir por caixa
# ---------------------------------------------------------------------------


def test_source_props_tolerate_bibref_case_mismatch():
    """Regressao: bibref com caixa mista nao pode perder os campos SOURCE.

    As chaves de bibliography vem minusculas (bib_loader); o source_ref preserva
    a caixa do bloco SOURCE. Um get() exato retornava {} e o Source node ficava
    sem lattes_id/nome/cargo (bug observado no lattes do Quinto_Andar).
    """
    from synesis_graph.core import _build_source_props

    bibliography = {
        "vitor_mourao_hanriot_3474555741700167": {
            "lattes_id": "3474555741700167",
            "nome": "Vitor Mourao Hanriot",
            "cargo_institucional": "NA",
        }
    }
    props = _build_source_props(
        "Vitor_Mourao_Hanriot_3474555741700167",  # caixa ORIGINAL
        {},
        bibliography,
        ["lattes_id", "nome", "cargo_institucional"],
    )
    assert props["lattes_id"] == "3474555741700167"
    assert props["nome"] == "Vitor Mourao Hanriot"
    assert props["cargo_institucional"] == "NA"


def test_source_props_exact_key_still_works():
    """Chave ja minuscula continua funcionando (nao-regressao)."""
    from synesis_graph.core import _build_source_props

    props = _build_source_props(
        "artigo2024", {}, {"artigo2024": {"knowledge_area": "AI"}}, ["knowledge_area"]
    )
    assert props["knowledge_area"] == "AI"


# ---------------------------------------------------------------------------
# ITEM hibrido (chain + code): o criterio conecta a cada Item node do bloco
# ---------------------------------------------------------------------------


def _extract_hybrid(corpus):
    from synesis_graph.core import _extract_corpus_data

    return _extract_corpus_data(
        corpus,
        bibliography={},
        relation_definitions={},
        code_field_names=["criterio"],
        chain_field_names=["chain"],
        source_fields=[],
        ontology_field_names=[],
        memo_field_name="memo",
        quotation_field_name="trecho",
    )


def test_hybrid_item_criterion_mentions_each_chain_item():
    """Regressao: criterio de ITEM com chain deve gerar MENTIONS (nao ficar orfao).

    ITEM com 2 chains + criterio -> 2 Item nodes, cada um menciona o criterio.
    Sem isso, conhecimento_ia_real (que so aparece junto de chains) flutuava
    desconectado no grafo.
    """
    corpus = [{
        "id": "src_i0001",
        "source_ref": "@artigo",
        "data": {
            "trecho": "texto",
            "criterio": "conhecimento_ia_real",
            "chain": [
                {"from": "neural_network", "relation": "USES", "to": "random_projection"},
                {"from": "online_learning", "relation": "APPLIES", "to": "industrial_process"},
            ],
        },
    }]
    _sources, items, mentions, chains, _from_source, _fields = _extract_hybrid(corpus)

    assert len(items) == 2  # um Item node por chain triple
    assert len(chains) == 2
    # cada Item node menciona o criterio
    crit_mentions = [m for m in mentions if m["concept"] == "conhecimento_ia_real"]
    assert len(crit_mentions) == 2
    assert {m["item_id"] for m in crit_mentions} == {"src_i0001_n0001", "src_i0001_n0002"}
    # os conceitos de chain continuam mencionados
    assert any(m["concept"] == "neural_network" for m in mentions)
    assert any(m["concept"] == "industrial_process" for m in mentions)


def test_chain_only_item_unchanged():
    """Nao-regressao: ITEM so com chain (sem code) gera so os mentions da chain."""
    corpus = [{
        "id": "src_i0001",
        "source_ref": "@x",
        "data": {
            "trecho": "t",
            "chain": [{"from": "a", "relation": "USES", "to": "b"}],
        },
    }]
    _s, items, mentions, _c, _fs, _f = _extract_hybrid(corpus)
    assert len(items) == 1
    assert {m["concept"] for m in mentions} == {"a", "b"}


def test_code_only_item_unchanged():
    """Nao-regressao: ITEM so com code segue o ramo has_code (item_id _c)."""
    corpus = [{
        "id": "src_i0001",
        "source_ref": "@x",
        "data": {"trecho": "t", "criterio": "graduacao_instituicao"},
    }]
    _s, items, mentions, _c, _fs, _f = _extract_hybrid(corpus)
    assert len(items) == 1
    assert items[0]["item_id"] == "src_i0001_c0001"
    assert [m["concept"] for m in mentions] == ["graduacao_instituicao"]


# ---------------------------------------------------------------------------
# Neo4j sync
# ---------------------------------------------------------------------------


def test_sync_entities_emits_labeled_merge(members):
    r = resolve_linkage(members)
    tx = _FakeTx()
    _sync_entities(tx, entities_as_rows(r.entities))
    q = tx.queries()
    assert "MERGE (e:Researcher {entity_id: row.entity_id})" in q
    assert "IDENTIFIED_AS" in q
    assert tx.calls[0][1]["rows"][0]["entity_id"] == "3474555741700167"


def test_sync_refers_to_emits_edge_with_entity_property(members):
    r = resolve_linkage(members)
    tx = _FakeTx()
    _sync_refers_to(tx, edges_as_rows(r.edges))
    q = tx.queries()
    assert "MATCH (s:Source {bibtex: row.from_bibtex})" in q
    assert "MERGE (s)-[r:REFERS_TO]->(e)" in q
    assert "SET r.entity = row.entity" in q
    assert len(tx.calls[0][1]["rows"]) == 2


def test_sync_entities_noop_when_empty():
    tx = _FakeTx()
    _sync_entities(tx, {})
    _sync_refers_to(tx, {})
    assert tx.calls == []


def test_sync_skips_unsafe_label():
    """Label invalido nunca chega ao Cypher (e interpolado, nao parametrizavel)."""
    tx = _FakeTx()
    _sync_entities(
        tx,
        {"Bad;Label": [{"entity_id": "1", "entity": "x", "member": "m", "source_bibtex": "m:@a"}]},
    )
    assert tx.calls == []


def test_constraints_include_entity_labels():
    class _FakeSession(_FakeTx):
        pass

    s = _FakeSession()
    _create_constraints(s, [], "Concept", ["Researcher"])
    q = s.queries()
    assert "FOR (e:Researcher) REQUIRE e.entity_id IS UNIQUE" in q


def test_constraints_without_entities_unchanged():
    """Default None: chamadas legadas seguem identicas."""

    class _FakeSession(_FakeTx):
        pass

    s = _FakeSession()
    _create_constraints(s, [], "Concept")
    assert "entity_id" not in s.queries()


# ---------------------------------------------------------------------------
# merge_payloads — o pior caso de colisao: o MESMO projeto em dois membros
# ---------------------------------------------------------------------------

DAVI = Path(__file__).parent / "Davi_Projeto_Completo" / "Davi.synp"


def _build_davi():
    from synesis_graph.core import _build_graph_payload, analyze_template

    data = compile_project_to_json(DAVI)
    (sf, gf, cf, cdf, vm, srcf, memo, quot) = analyze_template(data["template"])
    payload = _build_graph_payload(
        json_data=data,
        scalar_fields=sf,
        graph_fields=gf,
        chain_fields=cf,
        code_fields=cdf,
        value_maps=vm,
        source_fields=srcf,
        memo_field_name=memo,
        quotation_field_name=quot,
    )
    return payload, data


@pytest.fixture(scope="module")
def merged_twins():
    """Mescla o mesmo projeto sob dois aliases — todo id colidiria sem namespacing."""
    from synesis_graph.core import merge_payloads
    from synesis_graph.ui import TaskReporter

    p1, d1 = _build_davi()
    p2, d2 = _build_davi()
    counts = {"items": len(p1.items), "sources": len(p1.sources), "concepts": len(p1.concepts)}
    merged = merge_payloads([("alpha", p1, d1), ("beta", p2, d2)], TaskReporter("test"))
    return merged, counts


def test_merge_preserves_all_items_and_sources(merged_twins):
    merged, counts = merged_twins
    assert len(merged.items) == 2 * counts["items"]
    assert len(merged.sources) == 2 * counts["sources"]


def test_merge_produces_no_id_collisions(merged_twins):
    merged, _counts = merged_twins
    item_ids = [i["item_id"] for i in merged.items]
    bibtexs = [s["bibtex"] for s in merged.sources]
    assert len(set(item_ids)) == len(item_ids)
    assert len(set(bibtexs)) == len(bibtexs)


def test_merge_qualifies_by_alias(merged_twins):
    merged, _counts = merged_twins
    bibtexs = {s["bibtex"] for s in merged.sources}
    assert "alpha:@entrevista01" in bibtexs
    assert "beta:@entrevista01" in bibtexs


def test_merge_unifies_concepts_by_name(merged_twins):
    """Membros de um estudo compartilham o vocabulario — conceito nao duplica."""
    merged, counts = merged_twins
    assert len(merged.concepts) == counts["concepts"]


def test_merge_keeps_from_source_consistent(merged_twins):
    """Arestas Item->Source continuam apontando para ids que existem."""
    merged, _counts = merged_twins
    item_ids = {i["item_id"] for i in merged.items}
    bibtexs = {s["bibtex"] for s in merged.sources}
    for fs in merged.from_source[:500]:
        assert fs["item_id"] in item_ids
        assert fs["ref"] in bibtexs


def test_merge_without_modifiers_reifies_nothing(merged_twins):
    """NAO-REGRESSAO: projeto sem IDENTIFIES/REFERS TO nao produz entidade."""
    merged, _counts = merged_twins
    assert merged.entities == {}
    assert merged.refers_to_edges == {}


# --------------------------------------- ON DATASET: união bibliography+dataset (§12)

def test_merge_source_origins_linkage_une_dataset():
    """Linkagem enxerga campos ON DATASET via união das seções (§12.2)."""
    from synesis_graph.linkage import _field_value, _merge_source_origins

    data = {
        "bibliography": {"rec-1": {"title": "X"}},
        "dataset": {"rec-1": {"lattes_id": "rec-1", "nome": "Fulano"}},
    }
    merged = _merge_source_origins(data)
    assert _field_value(merged, "@rec-1", "lattes_id") == "rec-1"
    assert _field_value(merged, "rec-1", "title") == "X"  # bibliography preservada


def test_merge_source_origins_linkage_noop_sem_dataset():
    from synesis_graph.linkage import _merge_source_origins

    data = {"bibliography": {"a": {"x": 1}}}
    assert _merge_source_origins(data) == {"a": {"x": 1}}


def test_merge_source_origins_payload_vira_prop_de_no():
    """Campo ON DATASET vira propriedade de nó Source no sync (§12.2/§12.3)."""
    from synesis_graph.core import _build_source_props, _merge_source_origins_payload

    data = {
        "bibliography": {"rec-1": {"title": "X"}},
        "dataset": {"rec-1": {"lattes_id": "rec-1", "bolsa": "1B"}},
    }
    merged = _merge_source_origins_payload(data)
    props = _build_source_props("rec-1", {}, merged, ["lattes_id", "bolsa"])
    assert props["lattes_id"] == "rec-1"
    assert props["bolsa"] == "1B"


def test_merge_source_origins_payload_bibliography_vence_colisao():
    """Em colisão de mesmo campo, bibliography prevalece sobre dataset."""
    from synesis_graph.core import _merge_source_origins_payload

    data = {
        "bibliography": {"r": {"nome": "do-bib"}},
        "dataset": {"r": {"nome": "do-dataset"}},
    }
    assert _merge_source_origins_payload(data)["r"]["nome"] == "do-bib"
