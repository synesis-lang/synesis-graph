"""HTML graph renderer and HTMLBackendAdapter."""

from __future__ import annotations

import json
import logging
import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from synesis_graph.backends.base import BackendAdapter
from synesis_graph.config import BACKEND_HTML, HTMLConfig
from synesis_graph.core import DependencyError, GraphPayload, PipelineError, SyncError
from synesis_graph.ui import TaskReporter

logger = logging.getLogger("synesis2graph")

# ============================================================================
# HTML BACKEND
# ============================================================================
_HTML_PALETTE = [
    "#1A3A5C",  # navy
    "#4A6741",  # sage
    "#8B4A3C",  # terracotta
    "#A8905A",  # gold
    "#3D5A7A",  # slate
    "#C8963A",  # amber
    "#6B6B70",  # ref-mid
    "#5c7088",  # code comment blue-grey
    "#7BB8E8",  # light blue (code ref token)
    "#7EC8A0",  # light green (code str token)
    "#E06C75",  # muted red
    "#AEAEB2",  # doc-light grey
]

_HTML_RELATION_COLORS: dict[str, str] = {
    "ENABLES":      "#4A6741",  # sage
    "INFLUENCES":   "#1A3A5C",  # navy
    "CONSTRAINS":   "#C8963A",  # amber
    "CONTESTED_BY": "#8B4A3C",  # terracotta
    "RELATES_TO":   "#3D5A7A",  # slate
    "ASSOCIATION":  "#A8905A",  # gold
    "APPLICATION":  "#8B4A3C",  # terracotta
    "METHODOLOGICAL": "#3D5A7A",  # slate
}


def _html_relation_color(relation: str) -> str:
    norm = relation.upper().replace("-", "_").replace(" ", "_")
    if norm in _HTML_RELATION_COLORS:
        return _HTML_RELATION_COLORS[norm]
    return _HTML_PALETTE[abs(hash(norm)) % len(_HTML_PALETTE)]


def _html_slug(name: str) -> str:
    """Creates a stable, HTML-safe ID from a concept name."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "node"


def _html_apply_filters(
    payload: GraphPayload,
    min_frequency: int,
    min_source_count: int,
    max_nodes: int,
    include_isolated: bool,
) -> tuple[set, list[dict[str, Any]]]:
    """
    Filters concepts by mention frequency and source coverage, then by degree.
    Returns (kept_concept_names_set, filtered_chains).
    """
    item_to_source: dict[str, str] = {r["item_id"]: r["ref"] for r in payload.from_source}

    freq: dict[str, int] = {}
    concept_sources: dict[str, set] = {}
    for m in payload.mentions:
        c = m["concept"]
        freq[c] = freq.get(c, 0) + 1
        src = item_to_source.get(m["item_id"])
        if src:
            concept_sources.setdefault(c, set()).add(src)

    degree: dict[str, int] = {}
    for ch in payload.chains:
        degree[ch["source"]] = degree.get(ch["source"], 0) + 1
        degree[ch["target"]] = degree.get(ch["target"], 0) + 1

    all_names = {c["props"]["name"] for c in payload.concepts}

    kept: set = set()
    for name in all_names:
        f = freq.get(name, 0)
        sc = len(concept_sources.get(name, set()))
        if f >= min_frequency and sc >= min_source_count:
            kept.add(name)

    if not include_isolated:
        has_chain = set()
        for ch in payload.chains:
            has_chain.add(ch["source"])
            has_chain.add(ch["target"])
        kept = {n for n in kept if n in has_chain}

    if max_nodes > 0 and len(kept) > max_nodes:
        sorted_kept = sorted(kept, key=lambda n: degree.get(n, 0), reverse=True)
        kept = set(sorted_kept[:max_nodes])

    filtered_chains = [ch for ch in payload.chains if ch["source"] in kept and ch["target"] in kept]

    return kept, filtered_chains


def _html_resolve_grouping(
    payload: GraphPayload,
    kept: set,
    group_by: str | None,
) -> tuple[dict[str, int], dict[str, str], list[dict[str, Any]], str]:
    """
    Assigns integer community IDs to concepts by grouping on a graph_field.
    Returns (cid_map, cname_map, legend_list, field_name).
    """
    field_name = group_by
    if not field_name and payload.graph_fields:
        for gf in payload.graph_fields:
            if re.match(r"^topic", gf, re.IGNORECASE):
                field_name = gf
                break
        if not field_name:
            field_name = payload.graph_fields[0]

    concept_to_group: dict[str, str] = {}
    if field_name:
        for c in payload.concepts:
            name = c["props"]["name"]
            if name not in kept:
                continue
            vals = c["relations"].get(field_name)
            if isinstance(vals, list) and vals and vals[0]:
                concept_to_group[name] = str(vals[0])
            elif vals and not isinstance(vals, list):
                concept_to_group[name] = str(vals)
            else:
                concept_to_group[name] = "Other"
    else:
        for name in kept:
            concept_to_group[name] = "All"

    group_counts: dict[str, int] = {}
    for name in kept:
        g = concept_to_group.get(name, "Other")
        group_counts[g] = group_counts.get(g, 0) + 1

    groups_ordered = sorted(group_counts.keys(), key=lambda g: (-group_counts[g], g))
    group_to_cid = {g: i for i, g in enumerate(groups_ordered)}

    cid_map = {name: group_to_cid[concept_to_group.get(name, "Other")] for name in kept}
    cname_map = {name: concept_to_group.get(name, "Other") for name in kept}

    legend = [
        {
            "cid": group_to_cid[g],
            "color": _HTML_PALETTE[group_to_cid[g] % len(_HTML_PALETTE)],
            "label": g,
            "count": group_counts[g],
        }
        for g in groups_ordered
    ]

    return cid_map, cname_map, legend, (field_name or "All")


def _html_build_hyperedges(
    payload: GraphPayload,
    kept: set,
    max_hyperedges: int,
    slug_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """
    Builds hyperedge dicts from corpus entries that link ≥ 3 distinct concepts.
    Groups chains sharing the same parent corpus entry (same item_id prefix).
    """
    _suffix_re = re.compile(r"^(.+)_[nc]\d{4}$")

    item_desc: dict[str, str] = {
        item["item_id"]: item.get("description", "") for item in payload.items
    }
    item_source: dict[str, str] = {r["item_id"]: r["ref"] for r in payload.from_source}

    corpus_concepts: dict[str, set] = {}
    corpus_first_item: dict[str, str] = {}
    for ch in payload.chains:
        item_id = ch["item_id"]
        m = _suffix_re.match(item_id)
        corpus_id = m.group(1) if m else item_id
        corpus_concepts.setdefault(corpus_id, set())
        corpus_concepts[corpus_id].add(ch["source"])
        corpus_concepts[corpus_id].add(ch["target"])
        if corpus_id not in corpus_first_item:
            corpus_first_item[corpus_id] = item_id

    candidates = []
    for corpus_id, concepts_set in corpus_concepts.items():
        filtered = concepts_set & kept
        if len(filtered) < 3:
            continue
        first_item_id = corpus_first_item.get(corpus_id, "")
        desc = item_desc.get(first_item_id, "")
        src = item_source.get(first_item_id, corpus_id)
        label = ((desc[:57] + "…") if len(desc) > 60 else desc) if desc else f"{src} · {corpus_id}"
        candidates.append(
            {
                "corpus_id": corpus_id,
                "concepts": filtered,
                "label": label,
                "source_ref": src,
                "size": len(filtered),
            }
        )

    candidates.sort(key=lambda c: -c["size"])
    candidates = candidates[: max(0, max_hyperedges)]

    _sm = slug_map or {}
    return [
        {
            "id": _html_slug(cand["corpus_id"]),
            "label": cand["label"],
            "nodes": [_sm.get(c, _html_slug(c)) for c in cand["concepts"]],
            "relation": "RELATES_TO",
            "confidence": "EXTRACTED",
            "source_item": cand["source_ref"],
        }
        for cand in candidates
    ]


_TAX_NODE_COLOR = "#6B6B70"  # neutral grey for taxonomy-value nodes (Topic/Aspect/…)


def _html_build_taxonomy_view(
    payload: GraphPayload,
    kept: set,
    slug_map: dict[str, str],
    slug_counts: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Ontology-mode edges + taxonomy-value nodes, from payload.taxonomy_edges.

    Mirrors the Neo4j taxonomy structure: Concept -GROUPED_BY-> Topic, Topic -IS_LINKED_TO-
    Topic, etc. Concept endpoints reuse the existing slug_map; taxonomy values get fresh
    ``tax::`` slugs (added to slug_map so other views can reference them). Only edges whose
    concept endpoint is in ``kept`` are rendered (keeps the ontology view aligned with filters).
    """
    concept_names = {c["props"]["name"] for c in payload.concepts}
    tax_nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    _tax_seen: set[str] = set()

    def _slug_for(name: str) -> str:
        if name in slug_map:
            return slug_map[name]
        base = "tax_" + _html_slug(name)
        count = slug_counts.get(base, 0)
        slug_counts[base] = count + 1
        slug = base if count == 0 else f"{base}_{count + 1}"
        slug_map[name] = slug
        return slug

    for te in payload.taxonomy_edges:
        src, tgt, rel = te.get("source", ""), te.get("target", ""), te.get("type", "")
        if not src or not tgt:
            continue
        # Concept→taxonomy edges require the concept endpoint to be visible.
        src_is_concept = src in concept_names
        if src_is_concept and src not in kept:
            continue

        src_id = _slug_for(src)
        tgt_id = _slug_for(tgt)

        # Register taxonomy-value endpoints as diamond nodes.
        for name, is_concept in ((src, src_is_concept), (tgt, tgt in concept_names)):
            if is_concept or name in _tax_seen:
                continue
            _tax_seen.add(name)
            tax_nodes.append(
                {
                    "id": slug_map[name],
                    "label": name,
                    "shape": "diamond",
                    "color": {
                        "background": _TAX_NODE_COLOR,
                        "border": _TAX_NODE_COLOR,
                        "highlight": {"background": "#ffffff", "border": _TAX_NODE_COLOR},
                    },
                    "size": 10.0,
                    "font": {"size": 11},
                    "title": name,
                    "_community": -1,
                    "_community_name": "Taxonomy",
                    "_file_type": "taxonomy",
                    "_degree": 0,
                    "_taxonomy": True,
                    "_extra": {},
                }
            )

        color = _html_relation_color(rel)
        edge: dict[str, Any] = {
            "from": src_id,
            "to": tgt_id,
            "label": "",
            "title": rel + (f" (strength {te['strength']})" if "strength" in te else ""),
            "relation": rel,
            "relations": [{"type": rel, "count": 1}],
            "total": 1,
            "dashes": False,
            "width": 1.2,
            "color": {"color": color, "opacity": 0.6},
            "confidence": "ONTOLOGY",
            "bidirectional": False,
        }
        edges.append(edge)

    return edges, tax_nodes



def _html_render_payload(
    payload: GraphPayload,
    config: HTMLConfig,
    template_path: Path,
) -> str:
    """Builds the complete HTML string from a GraphPayload and HTMLConfig."""
    kept, filtered_chains = _html_apply_filters(
        payload,
        min_frequency=config.min_frequency,
        min_source_count=config.min_source_count,
        max_nodes=config.max_nodes,
        include_isolated=config.include_isolated,
    )

    cid_map, cname_map, legend, legend_title = _html_resolve_grouping(
        payload, kept, group_by=config.group_by
    )

    # Build ALL_GROUPINGS: one entry per graph_field (for sidebar tabs).
    all_groupings: dict[str, Any] = {}
    for gf in payload.graph_fields:
        _cid_map, _cname_map, _leg, _fname = _html_resolve_grouping(payload, kept, group_by=gf)
        _title = _fname.replace("_", " ").title()
        all_groupings[gf] = {
            "title": _title,
            "legend": _leg,
            "value_to_cid": {e["label"]: e["cid"] for e in _leg},
            "value_to_color": {e["label"]: e["color"] for e in _leg},
        }

    degree: dict[str, int] = {}
    for ch in filtered_chains:
        degree[ch["source"]] = degree.get(ch["source"], 0) + 1
        degree[ch["target"]] = degree.get(ch["target"], 0) + 1

    item_to_source: dict[str, str] = {r["item_id"]: r["ref"] for r in payload.from_source}

    # Build source props map: ref → dict of all SOURCE block properties.
    # payload.sources entries use "bibtex" as the key field (from _build_source_props);
    # test payloads may use "ref" instead — accept both.
    source_props_map: dict[str, dict[str, Any]] = {}
    for s in payload.sources:
        key = s.get("bibtex") or s.get("ref") or ""
        if key:
            source_props_map[key] = s
    source_label_map: dict[str, str] = {}
    for ref, props in source_props_map.items():
        label = props.get("nome") or props.get("title") or props.get("author") or ref
        source_label_map[ref] = str(label)[:60]

    concept_first_source: dict[str, str] = {}
    for m in payload.mentions:
        c = m["concept"]
        if c in kept and c not in concept_first_source:
            src = item_to_source.get(m["item_id"], "")
            if src:
                concept_first_source[c] = src

    concept_index: dict[str, dict[str, Any]] = {c["props"]["name"]: c for c in payload.concepts}

    # Build collision-free slug map: two different names must never share an ID.
    # Cover ALL concept names referenced anywhere (kept ontology nodes + concepts
    # mentioned via CODE/CHAIN + chain endpoints), so evidence keying by slug works
    # for CODE-only concepts even when they are hidden by ontology filters.
    _all_concept_names: list[str] = list(kept)
    _seen_names: set[str] = set(kept)
    for m in payload.mentions:
        c = m.get("concept")
        if c and c not in _seen_names:
            _seen_names.add(c)
            _all_concept_names.append(c)
    for ch in payload.chains:
        for role in (ch.get("source"), ch.get("target")):
            if role and role not in _seen_names:
                _seen_names.add(role)
                _all_concept_names.append(role)

    _slug_counts: dict[str, int] = {}
    slug_map: dict[str, str] = {}
    for name in _all_concept_names:
        base = _html_slug(name)
        count = _slug_counts.get(base, 0)
        _slug_counts[base] = count + 1
        slug_map[name] = base if count == 0 else f"{base}_{count + 1}"

    # Build evidence data: concept_slug → [{src, _src_ref, type, text, anchor?, analysis?, ...}]
    # Driven by payload.mentions, which covers BOTH CODE references and CHAIN nodes —
    # so a concept referenced only via CODE (no chain) still gathers all its evidence.
    # For each concept node, collect every item that mentions it, exposing all template
    # ITEM fields (from payload.item_fields) plus citation/description/anchor/analysis.
    item_index: dict[str, dict[str, Any]] = {item["item_id"]: item for item in payload.items}
    item_fields_map: dict[str, dict[str, str]] = payload.item_fields or {}
    _ev_seen: dict[str, set] = {}
    evidence_by_slug: dict[str, list[dict[str, str]]] = {}
    ev_item_fields_seen: list[str] = []   # ordered list of extra field names seen across all items

    def _parse_note_fields(raw_note: str) -> dict[str, str]:
        """Extract anchor and analysis from a ChainNode string representation."""
        out: dict[str, str] = {}
        m_anchor = re.search(r'anchor\s*=\s*"([^"]*)"', raw_note)
        if m_anchor:
            out["anchor"] = m_anchor.group(1).strip()
        m_analysis = re.search(r'analysis\s*=\s*"([^"]*)"', raw_note)
        if m_analysis:
            out["analysis"] = m_analysis.group(1).strip()
        return out

    # Map each (item_id, concept) to the chain relation types it participates in. A
    # concept may appear in several chains of the same item with different relation types;
    # each type yields its own evidence record. CODE-only mentions have no chain type.
    _item_concept_types: dict[tuple[str, str], list[str]] = {}
    for ch in payload.chains:
        iid = ch.get("item_id", "")
        ch_type = ch.get("type", "")
        for role in (ch.get("source"), ch.get("target")):
            if iid and role:
                lst_types = _item_concept_types.setdefault((iid, role), [])
                if ch_type not in lst_types:
                    lst_types.append(ch_type)

    for m in payload.mentions:
        concept = m.get("concept")
        iid = m.get("item_id", "")
        if not concept or concept not in slug_map:
            continue
        item = item_index.get(iid)
        if not item:
            continue
        text = (item.get("citation") or "").strip()
        raw_note = (item.get("description") or "").strip()
        extra_fields: dict[str, str] = item_fields_map.get(iid, {})
        if not text and not raw_note and not extra_fields:
            continue

        src_ref = item_to_source.get(iid, iid)
        slug = slug_map[concept]
        # One record per relation type the concept has in this item (or one untyped
        # record for CODE-only mentions).
        ev_types = _item_concept_types.get((iid, concept)) or [""]
        for ev_type in ev_types:
            dedup_key = (iid, ev_type)
            if dedup_key in _ev_seen.get(slug, set()):
                continue
            _ev_seen.setdefault(slug, set()).add(dedup_key)
            lst = evidence_by_slug.setdefault(slug, [])
            if len(lst) >= 60:
                continue

            src_label = source_label_map.get(src_ref, src_ref)
            entry: dict[str, str] = {
                "src": src_ref[:60],           # bibtex key shown in cell
                "_src_ref": src_ref,           # raw ref key for edge lookup
                "_src_tooltip": src_label[:120],  # human-readable name for tooltip
                "type": ev_type,
                "text": text[:300],
                "_concept": concept,           # canonical concept name for this evidence record
            }
            # Structured note fields (anchor + analysis parsed from ChainNode repr).
            # For plain-text MEMO fields, fall back to including the raw text as "note".
            note_fields = _parse_note_fields(raw_note)
            if note_fields.get("anchor"):
                entry["anchor"] = note_fields["anchor"][:200]
            if note_fields.get("analysis"):
                entry["analysis"] = note_fields["analysis"][:300]
            if raw_note and not note_fields:
                entry["note"] = raw_note[:300]
            # Dynamic extra scalar fields from the item (zona, score_sugerido, criterio, ...)
            for fk, fv in extra_fields.items():
                entry[fk] = str(fv)[:200]
                if fk not in ev_item_fields_seen:
                    ev_item_fields_seen.append(fk)

            lst.append(entry)

    # Build aggregated chain edges: one edge per (source, target) pair, accumulating
    # relation type counts. Width ∝ total occurrences; colour = dominant type.
    # Uses full payload.chains so evidence is available even when both endpoints are
    # hidden by frequency/source filters.
    _agg: dict[tuple[str, str], dict] = {}
    for ch in payload.chains:
        src_slug = slug_map.get(ch["source"], _html_slug(ch["source"]))
        tgt_slug = slug_map.get(ch["target"], _html_slug(ch["target"]))
        ch_type = ch.get("type", "")
        iid = ch.get("item_id", "")
        src_ref = item_to_source.get(iid, iid)
        key = (src_slug, tgt_slug)
        if key not in _agg:
            _agg[key] = {"type_counts": {}, "src_refs": set()}
        _agg[key]["type_counts"][ch_type] = _agg[key]["type_counts"].get(ch_type, 0) + 1
        _agg[key]["src_refs"].add(src_ref)

    agg_edges: list[dict[str, Any]] = []
    for (src_slug, tgt_slug), agg_data in _agg.items():
        total = sum(agg_data["type_counts"].values())
        dominant = max(agg_data["type_counts"], key=agg_data["type_counts"].get)
        color = _html_relation_color(dominant)
        width = 1.0 + min(total, 40) * 0.35
        relations = sorted(
            [{"type": t, "count": c} for t, c in agg_data["type_counts"].items()],
            key=lambda x: -x["count"],
        )
        title = "  |  ".join(f'{r["type"]} ×{r["count"]}' for r in relations)
        agg_edges.append(
            {
                "from": src_slug,
                "to": tgt_slug,
                "label": "",
                "title": title,
                "relations": relations,
                "total": total,
                "dashes": False,
                "width": width,
                "arrows": {"to": {"enabled": True, "scaleFactor": 0.5}},
                "color": {"color": color, "opacity": 0.65},
                "_dominant_type": dominant,
            }
        )

    def _build_concept_node(
        name: str, slug: str, cid: int, deg: int, filtered: bool
    ) -> dict[str, Any]:
        color = _HTML_PALETTE[cid % len(_HTML_PALETTE)]
        size = 8.0 if filtered else 8 + min(deg, 30) * 1.0
        c_data = concept_index.get(name, {})
        props = c_data.get("props", {})
        rels = c_data.get("relations", {})
        extra: dict[str, Any] = {}
        for sf in payload.scalar_fields:
            val = props.get(sf)
            if val is not None and val != "":
                extra[sf] = val
        for gf in payload.graph_fields:
            vals = rels.get(gf)
            if vals:
                first_val = vals[0] if isinstance(vals, list) else vals
                if first_val:
                    extra[gf] = first_val
        node: dict[str, Any] = {
            "id": slug,
            "label": name,
            "color": {
                "background": color,
                "border": color,
                "highlight": {"background": "#ffffff", "border": color},
            },
            "size": size,
            "font": {"size": 12},
            "title": name,
            "_community": cid,
            "_community_name": cname_map.get(name, "Other"),
            "_source_file": concept_first_source.get(name, ""),
            "_file_type": "concept",
            "_degree": deg,
            "_extra": extra,
        }
        if filtered:
            node["_filtered"] = True
        return node

    raw_nodes = []
    for name in kept:
        raw_nodes.append(
            _build_concept_node(
                name, slug_map[name], cid_map.get(name, 0), degree.get(name, 0), False
            )
        )

    # Concepts referenced in chains but outside kept (filtered by frequency/source).
    # Included in the unified graph with _filtered=True (JS renders them at reduced opacity).
    _chain_concept_names: set[str] = set()
    for ch in payload.chains:
        _chain_concept_names.add(ch.get("source", ""))
        _chain_concept_names.add(ch.get("target", ""))
    _chain_concept_names -= kept
    _chain_concept_names.discard("")
    _kept_slugs = {slug_map[n] for n in kept}
    _filtered_seen: set[str] = set()
    for name in _chain_concept_names:
        slug = slug_map.get(name)
        if slug is None or slug in _kept_slugs or slug in _filtered_seen:
            continue
        _filtered_seen.add(slug)
        raw_nodes.append(
            _build_concept_node(
                name, slug, cid_map.get(name, 0), degree.get(name, 0), True
            )
        )

    raw_edges = agg_edges

    hyperedges = _html_build_hyperedges(payload, kept, config.max_hyperedges, slug_map)

    communities_count = len(set(cid_map.values()))
    hidden_count = len(payload.concepts) - len(kept)
    stats_parts = [
        f"{len(kept)} nodes",
        f"{len(agg_edges)} edges",
        f"{communities_count} communities",
    ]
    if hidden_count > 0:
        stats_parts.append(f"{hidden_count} hidden by filter")

    try:
        _sg_ver = _pkg_version("synesis-graph")
    except PackageNotFoundError:
        _sg_ver = "dev"
    try:
        _syn_ver = _pkg_version("synesis")
    except PackageNotFoundError:
        _syn_ver = "?"

    stats_parts.append(f"synesis-graph v{_sg_ver}")
    stats_parts.append(f"synesis v{_syn_ver}")
    stats_text = " · ".join(stats_parts)

    tmpl = template_path.read_text("utf-8")
    return (
        tmpl.replace("{{TITLE}}", f"{payload.project_name} — Synesis Graph")
        .replace("{{RAW_NODES_JSON}}", json.dumps(raw_nodes, ensure_ascii=False))
        .replace("{{RAW_EDGES_JSON}}", json.dumps(raw_edges, ensure_ascii=False))
        .replace("{{ALL_GROUPINGS_JSON}}", json.dumps(all_groupings, ensure_ascii=False))
        .replace("{{ACTIVE_GROUPING}}", json.dumps(legend_title))
        .replace("{{HYPEREDGES_JSON}}", json.dumps(hyperedges, ensure_ascii=False))
        .replace("{{EVIDENCE_JSON}}", json.dumps(evidence_by_slug, ensure_ascii=False))
        .replace("{{EV_ITEM_FIELDS_JSON}}", json.dumps(ev_item_fields_seen, ensure_ascii=False))
        .replace("{{STATS_TEXT}}", stats_text)
    )


class HTMLBackendAdapter(BackendAdapter):
    """HTML graph backend — renders a self-contained vis-network HTML file."""

    def __init__(self, config: HTMLConfig, config_path: Path):
        self.config = config
        # Resolve template relative to the package root, not the user's config_path.
        # Works for both editable installs (templates/ at repo root) and installed
        # packages (templates/ copied alongside synesis_graph/).
        _pkg_root = Path(__file__).parent.parent.parent
        self._template_path = _pkg_root / "templates" / "graph.html.tmpl"
        self._output_path: Path | None = None

    @property
    def backend_name(self) -> str:
        return BACKEND_HTML

    def preflight(self, reporter: TaskReporter) -> PipelineError | None:
        if not self._template_path.exists():
            return DependencyError(
                message="HTML template not found",
                stage="preflight",
                details=str(self._template_path),
            )
        return None

    def connect(self, reporter: TaskReporter) -> PipelineError | None:
        return None

    def prepare_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        output = Path(self.config.output_path)
        if not output.is_absolute():
            output = Path.cwd() / output
        self._output_path = output
        try:
            self._output_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return ConnectionError(
                message="Cannot create output directory",
                stage="prepare",
                details=str(e),
            )
        return None

    def clear_destination(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        return None

    def synchronize_payload(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        if self._output_path is None:
            return ConnectionError(message="Output path not initialized", stage="sync")
        with reporter.step("Rendering HTML graph") as step:
            try:
                html = _html_render_payload(payload, self.config, self._template_path)
                self._output_path.write_text(html, encoding="utf-8")
            except Exception as e:
                step.fail()
                return SyncError(
                    message="Failed to render HTML graph",
                    stage="sync",
                    details=str(e),
                )
        reporter.dest(f"{self._output_path}  ({len(html):,} bytes)")
        return None

    def compute_backend_metrics(
        self, payload: GraphPayload, reporter: TaskReporter
    ) -> PipelineError | None:
        return None

    def close(self) -> None:
        pass
