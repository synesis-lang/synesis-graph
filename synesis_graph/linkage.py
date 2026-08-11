"""linkage.py — Etapa 4: reificacao de entidades a partir de IDENTIFIES / REFERS TO.

Le as declaracoes de ligacao dos JSONs exportados pelo compilador Synesis
(`template.field_specs[campo].identifies` / `.refers_to`) e materializa:

  - um **no de identidade reificado** por valor distinto de `IDENTIFIES <entidade>`
    (label do no = rotulo capitalizado/sanitizado, ex. `researcher` -> `:Researcher`);
  - uma **aresta** `(:Source)-[:REFERS_TO {entity}]->(:<Entidade>)` para cada
    `REFERS TO` casado por igualdade exata de valor.

Regras herdadas do design (synesis-planning/synesis/multiproject_key_ref.md, §4/§7):
  - O no nasce **so** de `IDENTIFIES` — o rotulo tem dono unico. `REFERS TO`
    orfao NAO cria no stub (vira aviso).
  - Casamento por **igualdade exata** pos-trim: sem case-folding, sem
    normalizacao silenciosa, sem fuzzy. Canonizar e trabalho da origem.
  - O valor de um campo SCOPE SOURCE vem de `bibliography[bibref][campo]`,
    independentemente da origem (texto do documento ou `.bib` via
    `ON BIBLIOGRAPHY`) — a v3.0 ja unifica as duas origens ali.
  - Campo multi-valorado gera **uma aresta por valor** (cardinalidade n:n).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from synesis_graph.sanitize import sanitize_cypher_label

# Tipo de relacao default para arestas de REFERS TO. O rotulo da entidade vai
# como propriedade `entity` em vez de virar parte do tipo (`REFERS_TO_RESEARCHER`
# seria pobre). O acucar `AS <relacao>` do §7 pode refinar isto no futuro.
REFERS_TO_RELATION = "REFERS_TO"


@dataclass
class EntityDecl:
    """Declaracao de ligacao de um campo, lida do template."""

    field_name: str
    entity: str


@dataclass
class LinkageSpec:
    """Declaracoes de ligacao de um membro (um JSON/projeto)."""

    identifies: list[EntityDecl] = field(default_factory=list)
    refers_to: list[EntityDecl] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.identifies and not self.refers_to


@dataclass
class EntityNode:
    """No de identidade reificado (nasce de IDENTIFIES)."""

    entity: str  # rotulo logico, ex. "researcher"
    label: str  # label Cypher sanitizado, ex. "Researcher"
    entity_id: str  # valor da chave primaria
    source_bibtex: str  # bibref do Source dono (ja qualificado por membro)
    member: str


@dataclass
class RefersToEdge:
    """Aresta (:Source)-[:REFERS_TO {entity}]->(:<Entidade>)."""

    entity: str
    label: str
    entity_id: str
    from_bibtex: str  # Source que referencia (ja qualificado)
    member: str


@dataclass
class LinkageResult:
    entities: list[EntityNode] = field(default_factory=list)
    edges: list[RefersToEdge] = field(default_factory=list)
    orphans: list[tuple[str, str, str]] = field(default_factory=list)  # (entity, value, member)
    owners: dict[str, str] = field(default_factory=dict)  # entity -> member alias
    duplicate_owners: list[tuple[str, str, str]] = field(default_factory=list)


def entity_label(entity: str) -> str:
    """Rotulo Cypher do no reificado: `researcher` -> `Researcher`.

    Sanitiza para uso direto em Cypher (o rotulo vem do template do usuario e
    e interpolado na query — nao pode ser parametrizado pelo driver).
    """
    parts = [p for p in entity.replace("-", "_").split("_") if p]
    camel = "".join(p[:1].upper() + p[1:] for p in parts) if parts else entity
    return sanitize_cypher_label(camel)


def extract_linkage_spec(template: dict[str, Any]) -> LinkageSpec:
    """Le `identifies` / `refers_to` de template.field_specs (JSON v3.0+).

    Campos sem os modificadores (o caso de todo projeto legado) produzem um
    LinkageSpec vazio — nenhuma reificacao acontece.
    """
    spec = LinkageSpec()
    field_specs = (template or {}).get("field_specs") or {}
    for name, fs in field_specs.items():
        if not isinstance(fs, dict):
            continue
        ident = fs.get("identifies")
        if ident:
            spec.identifies.append(EntityDecl(field_name=name, entity=str(ident)))
        ref = fs.get("refers_to")
        if ref:
            spec.refers_to.append(EntityDecl(field_name=name, entity=str(ref)))
    return spec


def _trim(value: Any) -> str:
    """Valor comparavel: str + trim de bordas. SEM case-folding (regra anti-fuzzy)."""
    return str(value).strip()


def _values_of(raw: Any) -> list[str]:
    """Campo multi-valorado gera uma aresta por valor."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [_trim(v) for v in raw if _trim(v)]
    trimmed = _trim(raw)
    return [trimmed] if trimmed else []


def _merge_source_origins(data: dict[str, Any]) -> dict[str, Any]:
    """Une as secoes `bibliography` e `dataset` do JSON por bibref (§12.2).

    A exportacao mantem as origens SEPARADAS no JSON (bibliografia BibTeX vs.
    registros TOML `ON DATASET`) para consumidores distinguirem. Para a
    linkagem/sync, porem, um campo SCOPE SOURCE e um campo SCOPE SOURCE
    independentemente da origem — aqui as duas secoes sao fundidas por chave de
    bibref (campos de `dataset` coexistem com os de `bibliography`; em colisao
    de mesmo campo, bibliography vence por ser a origem historica).
    """
    bibliography = data.get("bibliography") or {}
    dataset = data.get("dataset") or {}
    if not dataset:
        return bibliography
    merged: dict[str, Any] = {}
    for key in set(bibliography) | set(dataset):
        entry: dict[str, Any] = {}
        entry.update(dataset.get(key) or {})
        entry.update(bibliography.get(key) or {})  # bibliography prevalece
        merged[key] = entry
    return merged


def _field_value(bibliography: dict[str, Any], bibref: str, field_name: str) -> Any:
    """Valor de um campo SCOPE SOURCE na visao unificada por bibref (v3.0).

    Cobre as tres origens: extraido do documento, lido do `.bib`
    (`ON BIBLIOGRAPHY`) e lido do registro TOML (`ON DATASET`) — a exportacao
    v3.0 consolida documento+.bib em bibliography, e _merge_source_origins
    acrescenta a secao dataset antes de chamar esta funcao.

    Tolerante a caixa: as chaves de bibliography vem normalizadas (minusculas)
    pelo bib_loader, enquanto o bibref pode chegar com a caixa original do
    SOURCE block. Tenta a forma dada, sem `@`, e por fim a versao minuscula.
    """
    key = bibref.lstrip("@")
    entry = (
        bibliography.get(bibref)
        or bibliography.get(key)
        or bibliography.get(key.lower())
        or {}
    )
    return entry.get(field_name)


def _source_refs(data: dict[str, Any]) -> list[str]:
    """Bibrefs distintos do corpus, na CAIXA ORIGINAL (como o Source node grava).

    A resolucao da linkagem deve ancorar no mesmo bibref que o no Source usa
    (`corpus[].source_ref`, derivado do bloco SOURCE), NAO nas chaves de
    bibliography — que vem normalizadas em minusculas pelo bib_loader. Ancorar
    na chave normalizada gerava um `source_bibtex` com caixa divergente do
    `Source.bibtex`, e o MATCH case-sensitive do sync nunca criava a aresta
    IDENTIFIED_AS (bug observado com bibref de nome proprio, ex.
    `@Vitor_Mourao_Hanriot_...`).
    """
    seen: dict[str, None] = {}  # preserva ordem, deduplica
    for item in data.get("corpus", []) or []:
        ref = item.get("source_ref")
        if ref and ref not in seen:
            seen[ref] = None
    return list(seen.keys())


def resolve_linkage(members: list[dict[str, Any]]) -> LinkageResult:
    """Resolve entidades e arestas sobre N membros.

    Args:
        members: lista de dicts {alias, json_data} — o JSON de cada projeto.

    Returns:
        LinkageResult com nos reificados, arestas, orfaos e donos de rotulo.
    """
    result = LinkageResult()

    specs: list[tuple[str, LinkageSpec, dict[str, Any]]] = []
    for m in members:
        alias = m["alias"]
        data = m["json_data"]
        spec = extract_linkage_spec(data.get("template") or {})
        specs.append((alias, spec, data))

    # --- 1. Nos reificados: nascem SO de IDENTIFIES; rotulo tem dono unico ---
    # pk_index: entity -> {valor: bibtex_do_dono}
    pk_index: dict[str, dict[str, str]] = {}
    for alias, spec, data in specs:
        bibliography = _merge_source_origins(data)
        for decl in spec.identifies:
            if decl.entity in result.owners:
                result.duplicate_owners.append(
                    (decl.entity, result.owners[decl.entity], alias)
                )
                continue
            result.owners[decl.entity] = alias
            bucket = pk_index.setdefault(decl.entity, {})
            label = entity_label(decl.entity)

            for bibref in _source_refs(data):
                for value in _values_of(_field_value(bibliography, bibref, decl.field_name)):
                    if value in bucket:
                        # Unicidade ja validada no compilador (E077); aqui o
                        # primeiro vence, sem duplicar o no.
                        continue
                    qualified = _qualify(alias, bibref)
                    bucket[value] = qualified
                    result.entities.append(EntityNode(
                        entity=decl.entity,
                        label=label,
                        entity_id=value,
                        source_bibtex=qualified,
                        member=alias,
                    ))

    # --- 2. Arestas: REFERS TO casado por igualdade exata; orfao nao cria no ---
    for alias, spec, data in specs:
        bibliography = _merge_source_origins(data)
        for decl in spec.refers_to:
            bucket = pk_index.get(decl.entity, {})
            label = entity_label(decl.entity)
            for bibref in _source_refs(data):
                for value in _values_of(_field_value(bibliography, bibref, decl.field_name)):
                    if value in bucket:
                        result.edges.append(RefersToEdge(
                            entity=decl.entity,
                            label=label,
                            entity_id=value,
                            from_bibtex=_qualify(alias, bibref),
                            member=alias,
                        ))
                    else:
                        result.orphans.append((decl.entity, value, alias))

    return result


def _qualify(alias: str, bibref: str) -> str:
    """Qualifica bibref por alias do membro — bibrefs sao locais ao membro.

    No corpus real, `linkedin.bib` e `posts.bib` compartilham o mesmo bibref;
    sem isto, os dois Sources colidiriam num unico no. A juncao de identidade
    passa exclusivamente por IDENTIFIES/REFERS TO, nunca por igualdade de bibref.
    """
    raw = bibref if bibref.startswith("@") else f"@{bibref}"
    return f"{alias}:{raw}"


def entities_as_rows(entities: list[EntityNode]) -> dict[str, list[dict[str, Any]]]:
    """Agrupa nos reificados por label Cypher (uma query por label)."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for e in entities:
        rows.setdefault(e.label, []).append({
            "entity_id": e.entity_id,
            "entity": e.entity,
            "member": e.member,
            "source_bibtex": e.source_bibtex,
        })
    return rows


def edges_as_rows(edges: list[RefersToEdge]) -> dict[str, list[dict[str, Any]]]:
    """Agrupa arestas por label Cypher do alvo."""
    rows: dict[str, list[dict[str, Any]]] = {}
    for e in edges:
        rows.setdefault(e.label, []).append({
            "entity_id": e.entity_id,
            "entity": e.entity,
            "from_bibtex": e.from_bibtex,
            "member": e.member,
        })
    return rows
