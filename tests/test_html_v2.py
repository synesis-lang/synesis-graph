"""
Tests for html.py and graph.html.tmpl covering changes introduced in v0.2.x+:

  - Unified node schema (underscore-prefixed fields)
  - Light mode as default (_isDark / body.dark)
  - Cheatsheet palette (new _HTML_PALETTE and _HTML_RELATION_COLORS)
  - HTMLConfig open defaults (min_frequency=0, min_source_count=0, max_nodes=0)
  - Unified graph: single DataSet, no mode-switching, panel tabs instead
  - RAW_NODES=[] guard (loading overlay removed immediately)
  - Aggregated chain edges (one edge per pair, tooltip lists all types)
  - Relation color accuracy for Synesis-specific types
  - evidence_by_slug dedup (same item_id+type counted once per concept)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from synesis_graph.backends.html import _html_render_payload, _html_relation_color
from synesis_graph.config import HTMLConfig
from synesis_graph.core import GraphPayload
from tests.conftest import _make_payload

_TEMPLATE = Path(__file__).parent.parent / "templates" / "graph.html.tmpl"
pytestmark = pytest.mark.skipif(
    not _TEMPLATE.exists(), reason="graph.html.tmpl not found"
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _render(payload: GraphPayload, **kw) -> str:
    return _html_render_payload(payload, HTMLConfig(**kw), _TEMPLATE)


def _extract_json(html: str, var: str) -> list | dict:
    """Extract a JS variable assigned as `const VAR = <json>;`."""
    m = re.search(rf"(?:const\s+)?{re.escape(var)}\s*=\s*(\[.*?\]|\{{.*?\}})\s*;",
                  html, re.DOTALL)
    assert m, f"Variable '{var}' not found in HTML"
    return json.loads(m.group(1))


def _make_chain_payload(n_items: int = 3, n_sources: int = 2) -> GraphPayload:
    """Three concepts with chain edges, all passing filters when n_items>=3, n_sources>=2."""
    concepts = [
        {"props": {"name": "A"}, "relations": {"topic": ["Method"]}},
        {"props": {"name": "B"}, "relations": {"topic": ["Method"]}},
        {"props": {"name": "C"}, "relations": {"topic": ["Theme"]}},
    ]
    sources = [{"ref": f"s{i}", "props": {}} for i in range(n_sources)]
    items = [
        {"item_id": f"i{j}_n0001", "citation": f"text {j}", "description": f"note {j}"}
        for j in range(n_items)
    ]
    from_source = [
        {"item_id": f"i{j}_n0001", "ref": f"s{j % n_sources}"} for j in range(n_items)
    ]
    chains = [
        {"item_id": f"i{j}_n0001", "source": "A", "target": "B", "type": "METHODOLOGICAL"}
        for j in range(n_items)
    ] + [
        {"item_id": f"i{j}_n0001", "source": "B", "target": "C", "type": "APPLICATION"}
        for j in range(n_items)
    ]
    mentions = [
        {"item_id": f"i{j}_n0001", "concept": c}
        for j in range(n_items)
        for c in ("A", "B", "C")
    ]
    return _make_payload(
        concepts=concepts, sources=sources, items=items,
        chains=chains, mentions=mentions, from_source=from_source,
        graph_fields=["topic"],
    )


# ── 1. Unified node schema ────────────────────────────────────────────────────

class TestUnifiedNodeSchema:
    """RAW_NODES must use underscore-prefixed field convention."""

    def test_raw_nodes_have_underscore_prefixed_fields(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0, include_isolated=True)
        nodes = _extract_json(html, "RAW_NODES")
        assert nodes, "Expected at least one RAW_NODE"
        n = nodes[0]
        for field in ("_community", "_community_name", "_source_file", "_file_type", "_degree", "_extra"):
            assert field in n, f"Missing field '{field}' in RAW_NODE"

    def test_raw_nodes_have_no_unscoped_legacy_fields(self):
        """Old schema used 'community', 'degree', 'extra' without underscore prefix."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0, include_isolated=True)
        nodes = _extract_json(html, "RAW_NODES")
        assert nodes
        n = nodes[0]
        for legacy in ("community", "degree", "extra", "community_name", "source_file", "file_type"):
            assert legacy not in n, f"Legacy field '{legacy}' should not appear in RAW_NODE"

    def test_raw_node_id_equals_concept_slug(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        nodes = _extract_json(html, "RAW_NODES")
        for n in nodes:
            assert re.match(r"^[a-z0-9_]+$", n["id"]), f"Non-slug id: {n['id']}"

    def test_filtered_concepts_included_in_raw_nodes(self):
        """Concepts filtered by frequency must still appear in RAW_NODES with _filtered=True."""
        payload = _make_chain_payload(n_items=1, n_sources=1)
        # min_frequency=5 filters out all 3 concepts — they must still be in RAW_NODES
        html = _render(payload, min_frequency=5, min_source_count=5, include_isolated=False)
        nodes = _extract_json(html, "RAW_NODES")
        filtered = [n for n in nodes if n.get("_filtered")]
        assert filtered, "Filtered concepts must appear in RAW_NODES with _filtered=True"

    def test_non_filtered_nodes_lack_filtered_flag(self):
        """Concepts that pass the filter must not carry _filtered."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0, include_isolated=True)
        nodes = _extract_json(html, "RAW_NODES")
        non_filtered = [n for n in nodes if not n.get("_filtered")]
        assert non_filtered, "Expected non-filtered nodes"
        for n in non_filtered:
            assert "_filtered" not in n or n["_filtered"] is False


# ── 2. Panel tabs (Ontologia / Evidência) ────────────────────────────────────

class TestPanelTabs:
    """Ontologia/Evidência are panel tabs, not mode-switching buttons for the graph."""

    def test_panel_tabs_present_in_html(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "tab-onto" in html
        assert "tab-evid" in html
        assert "setPanelTab" in html

    def test_no_setmode_function(self):
        """setMode must not exist — replaced by setPanelTab."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "function setMode(" not in html

    def test_panel_tabs_hidden_by_default(self):
        """node-panel-tabs must start hidden (display:none) before any node is selected."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert 'id="node-panel-tabs" style="display:none"' in html

    def test_no_ev_source_nodes_constant(self):
        """EV_SOURCE_NODES no longer exists — nodes are unified in RAW_NODES."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "EV_SOURCE_NODES" not in html

    def test_no_ev_mention_edges_constant(self):
        """EV_MENTION_EDGES no longer exists — edges are unified as aggregated RAW_EDGES."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "EV_MENTION_EDGES" not in html


# ── 3. Light mode default ─────────────────────────────────────────────────────

class TestLightModeDefault:

    def test_root_vars_use_paper_background(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        # :root should declare the paper background, not dark
        root_m = re.search(r":root\s*\{([^}]+)\}", html)
        assert root_m, ":root block not found"
        root_block = root_m.group(1)
        assert "#F7F4EF" in root_block, "--bg should be paper (#F7F4EF) in :root (light default)"

    def test_dark_mode_uses_body_dark_class(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "body.dark" in html, "Dark mode must be body.dark class"
        assert "body.light" not in html, "body.light class must not exist (light is default)"

    def test_toggle_function_uses_is_dark(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "_isDark" in html, "Theme flag must be _isDark (not _isLight)"
        assert "_isLight" not in html, "_isLight must not exist"
        assert "body.dark" in html

    def test_theme_button_initial_icon_is_moon(self):
        """Moon icon (🌙) means 'switch to dark' — correct when starting in light mode."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        moon = "\U0001f319"  # 🌙
        sun = "☀"        # ☀
        assert moon in html, "Initial theme button must show 🌙 (offer dark mode)"

    def test_node_labels_default_to_dark_ink_color(self):
        """vis-network font color must start as ink (#1C1C1E) for light backgrounds."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "color: '#1C1C1E'" in html or "'#1C1C1E'" in html

    def test_export_png_uses_conditional_background(self):
        """exportPNG must pick background based on _isDark, not hardcode dark."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        export_start = html.find("function exportPNG")
        export_end   = html.find("\nfunction ", export_start + 1)
        export_body  = html[export_start:export_end]
        assert "_isDark" in export_body, "exportPNG must check _isDark for background color"
        assert "#F7F4EF" in export_body, "exportPNG must use paper color for light mode"


# ── 4. Cheatsheet palette ─────────────────────────────────────────────────────

class TestCheatsheetPalette:
    """New palette tokens must be present; old Tableau tokens must be gone from CSS."""

    CHEATSHEET_COLORS = {
        "#1A3A5C",  # navy
        "#4A6741",  # sage
        "#8B4A3C",  # terracotta
        "#A8905A",  # gold
        "#3D5A7A",  # slate
        "#C8963A",  # amber
    }
    OLD_TABLEAU = {"#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC948"}

    def test_cheatsheet_colors_present_in_html(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        for color in self.CHEATSHEET_COLORS:
            assert color in html, f"Cheatsheet color {color} not found in rendered HTML"

    def test_old_tableau_colors_absent_from_css_and_js_palette(self):
        """Old Tableau palette must not appear in _HTML_PALETTE or RELATION_COLORS."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        # Extract the JS RELATION_COLORS block
        rc_start = html.find("const RELATION_COLORS")
        rc_end   = html.find("};", rc_start) + 2
        rc_block = html[rc_start:rc_end]
        for old_color in ("#4E79A7", "#F28E2B", "#E15759", "#59A14F"):
            assert old_color not in rc_block, \
                f"Old Tableau color {old_color} found in RELATION_COLORS"

    def test_relation_colors_cover_synesis_types(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        rc_start = html.find("const RELATION_COLORS")
        rc_end   = html.find("};", rc_start) + 2
        rc_block = html[rc_start:rc_end]
        for rel_type in ("ASSOCIATION", "APPLICATION", "METHODOLOGICAL"):
            assert f'"{rel_type}"' in rc_block, \
                f"Synesis relation type '{rel_type}' missing from RELATION_COLORS"

    def test_application_uses_terracotta(self):
        assert _html_relation_color("APPLICATION") == "#8B4A3C"

    def test_methodological_uses_slate(self):
        assert _html_relation_color("METHODOLOGICAL") == "#3D5A7A"

    def test_association_uses_gold(self):
        assert _html_relation_color("ASSOCIATION") == "#A8905A"

    def test_enables_uses_sage(self):
        assert _html_relation_color("ENABLES") == "#4A6741"

    def test_influences_uses_navy(self):
        assert _html_relation_color("INFLUENCES") == "#1A3A5C"

    def test_relates_to_uses_slate(self):
        assert _html_relation_color("RELATES_TO") == "#3D5A7A"

    def test_node_colors_come_from_cheatsheet_palette(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0, include_isolated=True)
        nodes = _extract_json(html, "RAW_NODES")
        for n in nodes:
            bg = n["color"]["background"]
            assert bg in self.CHEATSHEET_COLORS, \
                f"Node color {bg} not in cheatsheet palette"


# ── 5. HTMLConfig open defaults ───────────────────────────────────────────────

class TestHTMLConfigDefaults:

    def test_default_min_frequency_is_zero(self):
        from synesis_graph.config import HTMLConfig
        cfg = HTMLConfig()
        assert cfg.min_frequency == 0

    def test_default_min_source_count_is_zero(self):
        from synesis_graph.config import HTMLConfig
        cfg = HTMLConfig()
        assert cfg.min_source_count == 0

    def test_default_max_nodes_is_zero_meaning_unlimited(self):
        from synesis_graph.config import HTMLConfig
        cfg = HTMLConfig()
        assert cfg.max_nodes == 0

    def test_default_include_isolated_is_true(self):
        from synesis_graph.config import HTMLConfig
        cfg = HTMLConfig()
        assert cfg.include_isolated is True

    def test_open_defaults_show_all_concepts(self):
        """With open defaults, single-source corpus must show all concepts."""
        payload = _make_chain_payload(n_items=1, n_sources=1)
        html = _render(payload)  # uses HTMLConfig() defaults
        nodes = _extract_json(html, "RAW_NODES")
        assert len(nodes) == 3, "All 3 concepts must appear with open defaults"

    def test_open_defaults_no_hidden_by_filter_in_stats(self):
        payload = _make_chain_payload()
        html = _render(payload)
        assert "hidden by filter" not in html

    def test_strict_filters_move_concepts_to_filtered(self):
        """With strict filters, all concepts land in RAW_NODES with _filtered=True."""
        payload = _make_chain_payload(n_items=1, n_sources=1)
        html = _render(payload, min_frequency=10, min_source_count=10)
        nodes = _extract_json(html, "RAW_NODES")
        # In unified graph, filtered concepts stay in RAW_NODES with _filtered flag
        for n in nodes:
            assert n.get("_filtered") is True, \
                f"Node '{n['id']}' should be _filtered=True under strict filters"


# ── 6. RAW_NODES empty guard ─────────────────────────────────────────────────

class TestEmptyKeptGuard:
    """When no concepts pass frequency/source filters, the graph still renders
    with all concepts present as _filtered nodes."""

    def test_stabilization_event_always_present(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        nodes = _extract_json(html, "RAW_NODES")
        assert len(nodes) > 0
        assert "stabilizationIterationsDone" in html

    def test_strict_filter_still_produces_aggregated_edges(self):
        """RAW_EDGES must still contain chain edges even when all concepts are filtered."""
        payload = _make_chain_payload(n_items=1, n_sources=1)
        html = _render(payload, min_frequency=50, min_source_count=50)
        raw_edges = _extract_json(html, "RAW_EDGES")
        assert len(raw_edges) > 0, \
            "RAW_EDGES must be populated even when all concepts are below frequency threshold"

    def test_filtered_nodes_present_in_raw_nodes(self):
        payload = _make_chain_payload(n_items=1, n_sources=1)
        html = _render(payload, min_frequency=50, min_source_count=50)
        nodes = _extract_json(html, "RAW_NODES")
        assert nodes, "RAW_NODES must contain filtered concepts"
        assert all(n.get("_filtered") for n in nodes)


# ── 7. Evidence data deduplication ───────────────────────────────────────────

class TestEvidenceDedup:

    def test_same_item_and_type_counted_once_per_concept(self):
        """Duplicate chains with same item_id and type must not create duplicate evidence."""
        concepts = [{"props": {"name": "X"}, "relations": {}},
                    {"props": {"name": "Y"}, "relations": {}}]
        items = [{"item_id": "dup_n0001", "citation": "some text", "description": "note"}]
        from_source = [{"item_id": "dup_n0001", "ref": "src0"}]
        # Same source→target, same item_id → duplicate chain (e.g. after MERGE fix)
        chains = [
            {"item_id": "dup_n0001", "source": "X", "target": "Y", "type": "APPLICATION"},
            {"item_id": "dup_n0001", "source": "X", "target": "Y", "type": "APPLICATION"},
        ]
        mentions = [{"item_id": "dup_n0001", "concept": "X"},
                    {"item_id": "dup_n0001", "concept": "Y"}]
        sources = [{"ref": "src0", "props": {}}]
        payload = _make_payload(
            concepts=concepts, items=items, chains=chains, mentions=mentions,
            from_source=from_source, sources=sources, graph_fields=[],
        )
        html = _render(payload, min_frequency=0, min_source_count=0)
        ev_data = _extract_json(html, "EVIDENCE_DATA")
        # Both X and Y should each have exactly 1 evidence record for this item+type
        for slug in ev_data:
            records = ev_data[slug]
            pairs = [(r["src"], r["type"]) for r in records]
            assert len(pairs) == len(set(pairs)), \
                f"Duplicate evidence records for slug '{slug}': {pairs}"

    def test_different_types_same_item_produce_separate_records(self):
        """Two chains with same pair but different types must each produce an evidence record."""
        concepts = [{"props": {"name": "P"}, "relations": {}},
                    {"props": {"name": "Q"}, "relations": {}}]
        items = [{"item_id": "multi_n0001", "citation": "text", "description": ""}]
        from_source = [{"item_id": "multi_n0001", "ref": "srcA"}]
        chains = [
            {"item_id": "multi_n0001", "source": "P", "target": "Q", "type": "APPLICATION"},
            {"item_id": "multi_n0001", "source": "P", "target": "Q", "type": "METHODOLOGICAL"},
        ]
        mentions = [{"item_id": "multi_n0001", "concept": c} for c in ("P", "Q")]
        sources = [{"ref": "srcA", "props": {}}]
        payload = _make_payload(
            concepts=concepts, items=items, chains=chains, mentions=mentions,
            from_source=from_source, sources=sources, graph_fields=[],
        )
        html = _render(payload, min_frequency=0, min_source_count=0)
        ev_data = _extract_json(html, "EVIDENCE_DATA")
        # slug for "p" should have 2 distinct type entries
        p_slug = next((k for k in ev_data if k.startswith("p")), None)
        assert p_slug is not None
        types = {r["type"] for r in ev_data[p_slug]}
        assert "APPLICATION" in types
        assert "METHODOLOGICAL" in types


# ── 8. Aggregated chain edges ─────────────────────────────────────────────────

class TestAggregatedEdges:
    """RAW_EDGES are aggregated by (source, target) pair — one edge per pair."""

    def test_edges_aggregated_by_pair(self):
        """Multiple chains between same pair collapse into one edge with multiple relations."""
        payload = _make_chain_payload(n_items=3, n_sources=2)
        html = _render(payload, min_frequency=0, min_source_count=0)
        raw_edges = _extract_json(html, "RAW_EDGES")
        # 3 METHODOLOGICAL A→B + 3 APPLICATION B→C = 2 unique pairs → 2 aggregated edges
        assert len(raw_edges) == 2, \
            f"Expected 2 aggregated edges, got {len(raw_edges)}"

    def test_edge_has_required_fields(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        edges = _extract_json(html, "RAW_EDGES")
        assert edges
        for e in edges:
            assert "from" in e
            assert "to" in e
            assert "relations" in e
            assert "total" in e
            assert "_dominant_type" in e
            assert "title" in e

    def test_edge_tooltip_lists_all_types(self):
        """tooltip (title) must include all relation types with counts."""
        concepts = [{"props": {"name": "X"}, "relations": {}},
                    {"props": {"name": "Y"}, "relations": {}}]
        items = [{"item_id": f"i{j}_n0001", "citation": "t", "description": ""}
                 for j in range(4)]
        from_source = [{"item_id": f"i{j}_n0001", "ref": "s0"} for j in range(4)]
        chains = (
            [{"item_id": f"i{j}_n0001", "source": "X", "target": "Y", "type": "ENABLES"}
             for j in range(3)]
            + [{"item_id": "i3_n0001", "source": "X", "target": "Y", "type": "INFLUENCES"}]
        )
        mentions = [{"item_id": f"i{j}_n0001", "concept": c}
                    for j in range(4) for c in ("X", "Y")]
        sources = [{"ref": "s0", "props": {}}]
        from tests.conftest import _make_payload
        payload = _make_payload(
            concepts=concepts, sources=sources, items=items,
            chains=chains, mentions=mentions, from_source=from_source, graph_fields=[],
        )
        html = _render(payload, min_frequency=0, min_source_count=0)
        edges = _extract_json(html, "RAW_EDGES")
        assert len(edges) == 1
        e = edges[0]
        assert "ENABLES" in e["title"] and "×3" in e["title"]
        assert "INFLUENCES" in e["title"] and "×1" in e["title"]
        assert e["total"] == 4

    def test_edge_width_proportional_to_total(self):
        """Edge with more chain occurrences must have greater width."""
        concepts = [{"props": {"name": "A"}, "relations": {}},
                    {"props": {"name": "B"}, "relations": {}},
                    {"props": {"name": "C"}, "relations": {}}]
        n_heavy, n_light = 10, 1
        items = [{"item_id": f"i{j}_n0001", "citation": "t", "description": ""}
                 for j in range(n_heavy + n_light)]
        from_source = [{"item_id": f"i{j}_n0001", "ref": "s0"}
                       for j in range(n_heavy + n_light)]
        chains = (
            [{"item_id": f"i{j}_n0001", "source": "A", "target": "B", "type": "ENABLES"}
             for j in range(n_heavy)]
            + [{"item_id": f"i{n_heavy}_n0001", "source": "B", "target": "C", "type": "ENABLES"}]
        )
        mentions = [{"item_id": f"i{j}_n0001", "concept": c}
                    for j in range(n_heavy + n_light) for c in ("A", "B", "C")]
        sources = [{"ref": "s0", "props": {}}]
        from tests.conftest import _make_payload
        payload = _make_payload(
            concepts=concepts, sources=sources, items=items,
            chains=chains, mentions=mentions, from_source=from_source, graph_fields=[],
        )
        html = _render(payload, min_frequency=0, min_source_count=0)
        edges = _extract_json(html, "RAW_EDGES")
        heavy = next(e for e in edges if e["total"] == n_heavy)
        light = next(e for e in edges if e["total"] == n_light)
        assert heavy["width"] > light["width"], \
            "Edge with more occurrences must have greater width"

    def test_edge_color_matches_dominant_type(self):
        """Edge color must match the most frequent relation type."""
        from synesis_graph.backends.html import _html_relation_color
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        edges = _extract_json(html, "RAW_EDGES")
        for e in edges:
            expected = _html_relation_color(e["_dominant_type"])
            actual = e["color"]["color"]
            assert actual == expected, \
                f"Dominant type {e['_dominant_type']}: expected {expected}, got {actual}"

    def test_no_taxonomy_edges_in_raw_edges(self):
        """RAW_EDGES must not contain taxonomy edges (GROUPED_BY, IS_LINKED_TO, etc.)."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        edges = _extract_json(html, "RAW_EDGES")
        taxonomy_types = {"GROUPED_BY", "QUALIFIED_BY", "BELONGS_TO", "IS_LINKED_TO",
                          "MAPPED_TO_ASPECT", "MAPPED_TO_DIMENSION"}
        for e in edges:
            dominant = e.get("_dominant_type", "")
            assert dominant not in taxonomy_types, \
                f"Taxonomy relation '{dominant}' found in RAW_EDGES"


# ── 9. ALL_GROUPINGS structure ────────────────────────────────────────────────

class TestAllGroupings:

    def test_all_groupings_has_required_keys(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        groupings = _extract_json(html, "ALL_GROUPINGS")
        assert "topic" in groupings
        for gk, gv in groupings.items():
            assert "title" in gv
            assert "legend" in gv
            assert "value_to_cid" in gv
            assert "value_to_color" in gv

    def test_legend_colors_come_from_cheatsheet_palette(self):
        cheatsheet = {
            "#1A3A5C", "#4A6741", "#8B4A3C", "#A8905A",
            "#3D5A7A", "#C8963A", "#6B6B70", "#5c7088",
            "#7BB8E8", "#7EC8A0", "#E06C75", "#AEAEB2",
        }
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        groupings = _extract_json(html, "ALL_GROUPINGS")
        for gk, gv in groupings.items():
            for entry in gv["legend"]:
                assert entry["color"] in cheatsheet, \
                    f"Legend color {entry['color']} not in cheatsheet palette"

    def test_empty_payload_groupings_well_formed(self):
        html = _render(_make_payload(), min_frequency=0, min_source_count=0)
        groupings = _extract_json(html, "ALL_GROUPINGS")
        assert isinstance(groupings, dict)


# ── 10. Stats text ────────────────────────────────────────────────────────────

class TestStatsText:

    def test_stats_text_present_in_html(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "STATS_TEXT" not in html
        m = re.search(r'<div id="stats">(.*?)</div>', html)
        assert m, "Stats div not found"
        assert "nodes" in m.group(1)

    def test_stats_text_includes_hidden_count_when_filtered(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=100, min_source_count=1)
        m = re.search(r'<div id="stats">(.*?)</div>', html)
        assert m
        stats = m.group(1)
        assert "hidden by filter" in stats

    def test_stats_text_no_hidden_when_open_defaults(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0, include_isolated=True)
        m = re.search(r'<div id="stats">(.*?)</div>', html)
        assert m
        assert "hidden by filter" not in m.group(1)


# ── 11. Placeholder completeness ──────────────────────────────────────────────

class TestPlaceholderCompleteness:

    PLACEHOLDERS = (
        "{{TITLE}}", "{{RAW_NODES_JSON}}", "{{RAW_EDGES_JSON}}",
        "{{ALL_GROUPINGS_JSON}}", "{{ACTIVE_GROUPING}}", "{{HYPEREDGES_JSON}}",
        "{{EVIDENCE_JSON}}", "{{EV_ITEM_FIELDS_JSON}}", "{{STATS_TEXT}}",
    )

    def test_all_placeholders_replaced(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        for ph in self.PLACEHOLDERS:
            assert ph not in html, f"Unreplaced placeholder: {ph}"

    def test_all_placeholders_replaced_on_empty_payload(self):
        html = _render(_make_payload(), min_frequency=0, min_source_count=0)
        for ph in self.PLACEHOLDERS:
            assert ph not in html, f"Unreplaced placeholder on empty payload: {ph}"


# ── 12. Dynamic evidence fields ──────────────────────────────────────────────

class TestDynamicEvidenceFields:
    """EV_ITEM_FIELDS carries extra item fields; EVIDENCE_DATA records include them."""

    def _make_payload_with_fields(self) -> GraphPayload:
        """Payload where extra item fields live in payload.item_fields (zona, area_tematica)."""
        concepts = [
            {"props": {"name": "X"}, "relations": {}},
            {"props": {"name": "Y"}, "relations": {}},
        ]
        sources = [{"ref": "s0", "props": {}}]
        items = [{"item_id": "xf_n0001", "citation": "some text", "description": "note"}]
        item_fields = {"xf_n0001": {"zona": "Norte", "area_tematica": "Saúde"}}
        from_source = [{"item_id": "xf_n0001", "ref": "s0"}]
        chains = [{"item_id": "xf_n0001", "source": "X", "target": "Y", "type": "APPLICATION"}]
        mentions = [{"item_id": "xf_n0001", "concept": c} for c in ("X", "Y")]
        from tests.conftest import _make_payload
        return _make_payload(
            concepts=concepts, sources=sources, items=items, item_fields=item_fields,
            chains=chains, mentions=mentions, from_source=from_source,
            graph_fields=[],
        )

    def test_ev_item_fields_json_is_list(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        fields = _extract_json(html, "EV_ITEM_FIELDS")
        assert isinstance(fields, list)

    def test_ev_item_fields_empty_when_no_extra_fields(self):
        """Standard _make_chain_payload has no _fields → EV_ITEM_FIELDS must be []."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        fields = _extract_json(html, "EV_ITEM_FIELDS")
        assert fields == []

    def test_ev_item_fields_contains_extra_field_names(self):
        payload = self._make_payload_with_fields()
        html = _render(payload, min_frequency=0, min_source_count=0)
        fields = _extract_json(html, "EV_ITEM_FIELDS")
        assert "zona" in fields
        assert "area_tematica" in fields

    def test_evidence_record_contains_extra_fields(self):
        payload = self._make_payload_with_fields()
        html = _render(payload, min_frequency=0, min_source_count=0)
        ev_data = _extract_json(html, "EVIDENCE_DATA")
        all_records = [r for records in ev_data.values() for r in records]
        assert any("zona" in r for r in all_records), "Extra field 'zona' not in any evidence record"

    def test_note_anchor_parsed_from_chainnode_repr(self):
        """If description contains ChainNode repr with anchor=..., it appears in evidence record."""
        concepts = [
            {"props": {"name": "P"}, "relations": {}},
            {"props": {"name": "Q"}, "relations": {}},
        ]
        sources = [{"ref": "s0", "props": {}}]
        chain_repr = 'ChainNode(nodes=[\'relation = A; basis = deduction; anchor = "literal quote"; analysis = "inferred"\'])'
        items = [{"item_id": "an_n0001", "citation": "text", "description": chain_repr}]
        from_source = [{"item_id": "an_n0001", "ref": "s0"}]
        chains = [{"item_id": "an_n0001", "source": "P", "target": "Q", "type": "APPLICATION"}]
        mentions = [{"item_id": "an_n0001", "concept": c} for c in ("P", "Q")]
        from tests.conftest import _make_payload
        payload = _make_payload(
            concepts=concepts, sources=sources, items=items,
            chains=chains, mentions=mentions, from_source=from_source, graph_fields=[],
        )
        html = _render(payload, min_frequency=0, min_source_count=0)
        ev_data = _extract_json(html, "EVIDENCE_DATA")
        all_records = [r for records in ev_data.values() for r in records]
        assert any(r.get("anchor") == "literal quote" for r in all_records), \
            "anchor not parsed from ChainNode repr"
        assert any(r.get("analysis") == "inferred" for r in all_records), \
            "analysis not parsed from ChainNode repr"

    def test_ev_item_fields_are_declared_in_template(self):
        """Template must declare const EV_ITEM_FIELDS = ...; for dynamic column rendering."""
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "const EV_ITEM_FIELDS" in html


# ── 13. Degree slider CSS uses CSS variables ──────────────────────────────────

class TestDegreeSliderCSS:
    """Degree slider must use CSS variables, not hardcoded dark hex values."""

    def test_slider_track_uses_var_track(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "background: var(--track)" in html

    def test_slider_progress_uses_var_accent(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "background: var(--accent)" in html

    def test_degree_row_color_uses_var_muted(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "color: var(--muted)" in html

    def test_degree_row_button_uses_var_accent(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        assert "color: var(--accent)" in html

    def test_no_hardcoded_dark_hex_in_slider(self):
        payload = _make_chain_payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        # Extract the degree-slider CSS rule block
        deg_m = re.search(r"#degree-slider[^{]*\{[^}]*\}", html)
        if deg_m:
            block = deg_m.group(0)
            assert "#3a3a5e" not in block, "Hardcoded dark track color in slider CSS"
            assert "#4E79A7" not in block, "Hardcoded Tableau blue in slider CSS"


# ── 14. Layer separation + Neo4j-mode fidelity (Ontologia/Evidência/Grafo Neo4j) ─

def _make_code_only_payload() -> GraphPayload:
    """Concept reached ONLY via a CODE mention (no chain) — must still get evidence."""
    concepts = [{"props": {"name": "empresa_atual"}, "relations": {"topic": ["Economics"]}}]
    sources = [{"bibtex": "s0", "nome": "Fulano"}]
    items = [{"item_id": "c0_c0001", "citation": "trecho literal", "description": ""}]
    item_fields = {"c0_c0001": {"zona": "Atuacao", "score_sugerido": "1",
                                "criterio_5a": "empresa_atual"}}
    from_source = [{"item_id": "c0_c0001", "ref": "s0"}]
    mentions = [{"item_id": "c0_c0001", "concept": "empresa_atual", "mention_order": 1}]
    return _make_payload(
        concepts=concepts, sources=sources, items=items, item_fields=item_fields,
        chains=[], mentions=mentions, from_source=from_source, graph_fields=["topic"],
    )


class TestCodeOnlyEvidence:
    """A CODE-only concept (no chain) must collect evidence from its mentioning items."""

    def test_code_only_concept_has_evidence(self):
        html = _render(_make_code_only_payload(), min_frequency=0, min_source_count=0)
        ev = _extract_json(html, "EVIDENCE_DATA")
        all_records = [r for recs in ev.values() for r in recs]
        assert all_records, "CODE-only concept produced no evidence records"
        # Template ITEM fields must surface in the evidence record
        assert any("zona" in r and "criterio_5a" in r for r in all_records)

    def test_code_only_evidence_carries_citation(self):
        html = _render(_make_code_only_payload(), min_frequency=0, min_source_count=0)
        ev = _extract_json(html, "EVIDENCE_DATA")
        all_records = [r for recs in ev.values() for r in recs]
        assert any(r.get("text") == "trecho literal" for r in all_records)


class TestOntologyInfoNoSource:
    """Ontology panel must NOT embed SOURCE props (the 'Fonte' block)."""

    def test_ontology_panel_has_no_fonte_block(self):
        html = _render(_make_chain_payload(), min_frequency=0, min_source_count=0)
        m = re.search(r"function _showOntologyPanel\(nodeId\)\s*\{.*?\n\}", html, re.DOTALL)
        assert m, "_showOntologyPanel function not found"
        assert ">Fonte<" not in m.group(0)
        assert "SOURCE_PROPS[" not in m.group(0)



class TestNeo4jSafety:
    """payload.items must never carry a nested _fields map (would break Neo4j SET i = row)."""

    def test_items_have_no_nested_fields_key(self):
        from pathlib import Path as _P

        from synesis_graph.core import compile_project

        class _R:
            def info(self, *a): pass
            def warning(self, *a): pass
            def success(self, *a): pass
            def error(self, *a): pass

        proj = (_P(__file__).parent.parent.parent
                / "case-studies" / "Quinto_Andar" / "Dados_Lattes" / "lattes.synp")
        if not proj.exists():
            pytest.skip("lattes.synp case study not available")
        payload = compile_project(proj, _R())
        assert all("_fields" not in it for it in payload.items)
        # Extra fields live in the parallel map instead
        assert isinstance(payload.item_fields, dict)
        assert any(payload.item_fields.values())


class TestOntologyFieldsDoNotLeakToEvidence:
    """SCOPE ONTOLOGY fields the compiler inlines into ITEM data must NOT surface as
    item evidence (regression for Social_Acceptance: aspect/dimension/ontology_description
    were leaking into payload.item_fields and EV_ITEM_FIELDS)."""

    @staticmethod
    def _json_data():
        # Template: SCOPE ITEM fields (text, note, chain, zona) + SCOPE ONTOLOGY fields
        # (ontology_description, topic, aspect). The compiler inlines the ontology fields
        # into each corpus item's `data` block alongside the genuine ITEM fields.
        return {
            "project": {"name": "Leak"},
            "template": {
                "field_specs": {
                    "text": {"scope": "ITEM", "type": "QUOTATION"},
                    "note": {"scope": "ITEM", "type": "MEMO"},
                    "chain": {"scope": "ITEM", "type": "CHAIN", "relations": {}},
                    "zona": {"scope": "ITEM", "type": "ENUMERATED"},
                    "ontology_description": {"scope": "ONTOLOGY", "type": "TEXT"},
                    "topic": {"scope": "ONTOLOGY", "type": "TOPIC"},
                    "aspect": {"scope": "ONTOLOGY", "type": "TEXT"},
                }
            },
            "ontology": {
                "A": {"ontology_description": "concept A", "topic": "T1"},
                "B": {"ontology_description": "concept B", "topic": "T2"},
            },
            "bibliography": {"src1": {"title": "Source One"}},
            "corpus": [
                {
                    "id": "i0",
                    "source_ref": "@src1",
                    "data": {
                        "text": "excerpt one",
                        "note": ["note one"],
                        "chain": [{"from": "A", "relation": "INFLUENCES", "to": "B"}],
                        "zona": "Norte",
                        # Ontology fields inlined by the linker — must be filtered out:
                        "ontology_description": "concept A, concept B",
                        "topic": "T1, T2",
                        "aspect": "10, 15",
                    },
                }
            ],
        }

    def _payload(self):
        from synesis_graph.core import _build_graph_payload, analyze_template

        jd = self._json_data()
        (scalar, graph, chains, codes, vmaps, srcf, memo, quot) = analyze_template(
            jd["template"]
        )
        return _build_graph_payload(
            json_data=jd,
            scalar_fields=scalar,
            graph_fields=graph,
            chain_fields=chains,
            code_fields=codes,
            value_maps=vmaps,
            source_fields=srcf,
            memo_field_name=memo,
            quotation_field_name=quot,
        )

    def test_ontology_fields_excluded_from_item_fields(self):
        payload = self._payload()
        for fields in payload.item_fields.values():
            assert "ontology_description" not in fields
            assert "aspect" not in fields
            assert "topic" not in fields

    def test_genuine_item_field_preserved(self):
        payload = self._payload()
        # zona is SCOPE ITEM and not structural → must survive in item_fields.
        assert any(f.get("zona") == "Norte" for f in payload.item_fields.values())

    def test_ontology_fields_absent_from_rendered_ev_item_fields(self):
        payload = self._payload()
        html = _render(payload, min_frequency=0, min_source_count=0)
        ev_item_fields = _extract_json(html, "EV_ITEM_FIELDS")
        assert "aspect" not in ev_item_fields
        assert "ontology_description" not in ev_item_fields
        assert "zona" in ev_item_fields


class TestTaxonomyEdges:
    """taxonomy_edges derive from graph_fields (ontology), mirroring _sync_taxonomies."""

    def test_taxonomy_edges_from_graph_fields(self):
        from synesis_graph.core import _build_taxonomy_edges
        concepts = [
            {"props": {"name": "A"}, "relations": {"topic": ["Method"]}},
            {"props": {"name": "B"}, "relations": {"topic": ["Theme"]}},
        ]
        chains = [{"source": "A", "target": "B", "type": "APPLICATION", "item_id": "i0"}]
        edges = _build_taxonomy_edges(concepts, ["topic"], chains)
        types = {e["type"] for e in edges}
        assert "GROUPED_BY" in types  # topic → TAXONOMY_RELATION_MAP
        # A(Method) → B(Theme) chain crosses two topics → one IS_LINKED_TO
        assert any(e["type"] == "IS_LINKED_TO" for e in edges)

    def test_taxonomy_edges_empty_without_graph_fields(self):
        from synesis_graph.core import _build_taxonomy_edges
        concepts = [{"props": {"name": "A"}, "relations": {}}]
        assert _build_taxonomy_edges(concepts, [], []) == []
