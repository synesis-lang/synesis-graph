"""Tests for HTML filter and helper functions in backends/html.py."""

from __future__ import annotations

from synesis_graph.backends.html import (
    _html_apply_filters,
    _html_build_hyperedges,
    _html_relation_color,
    _html_resolve_grouping,
    _html_slug,
)
from synesis_graph.core import GraphPayload
from tests.conftest import _make_payload

# ---------------------------------------------------------------------------
# _html_slug
# ---------------------------------------------------------------------------


class TestHtmlSlug:
    def test_plain_name(self):
        assert _html_slug("SocialCohesion") == "socialcohesion"

    def test_spaces_become_underscores(self):
        assert _html_slug("social cohesion") == "social_cohesion"

    def test_special_chars_removed(self):
        assert _html_slug("A@B#C") == "a_b_c"

    def test_hyphens_become_underscores(self):
        assert _html_slug("concept-2025") == "concept_2025"

    def test_leading_trailing_underscores_stripped(self):
        result = _html_slug("_concept_")
        assert not result.startswith("_")
        assert not result.endswith("_")

    def test_empty_string_returns_node(self):
        assert _html_slug("") == "node"

    def test_unicode_lowercased(self):
        result = _html_slug("Résilience")
        assert result == result.lower()

    def test_consecutive_special_chars_collapsed(self):
        result = _html_slug("A--B")
        assert "__" not in result


# ---------------------------------------------------------------------------
# _html_relation_color
# ---------------------------------------------------------------------------


class TestHtmlRelationColor:
    def test_known_relation_returns_fixed_color(self):
        color = _html_relation_color("ENABLES")
        assert color == "#4A6741"  # sage (cheatsheet palette)

    def test_influences_color(self):
        assert _html_relation_color("INFLUENCES") == "#1A3A5C"  # navy (cheatsheet palette)

    def test_unknown_relation_returns_palette_color(self):
        color = _html_relation_color("CUSTOM_REL")
        assert color.startswith("#")
        assert len(color) == 7

    def test_case_insensitive(self):
        assert _html_relation_color("enables") == _html_relation_color("ENABLES")

    def test_hyphen_normalized(self):
        assert _html_relation_color("RELATES-TO") == _html_relation_color("RELATES_TO")

    def test_consistent_color_for_same_relation(self):
        assert _html_relation_color("MY_REL") == _html_relation_color("MY_REL")


# ---------------------------------------------------------------------------
# _html_apply_filters
# ---------------------------------------------------------------------------


class TestHtmlApplyFilters:
    def _make_payload_for_filters(self, n_items: int = 3, n_sources: int = 2) -> GraphPayload:
        """Payload where Resilience/Trust/Cooperation each appear n_items times across n_sources."""
        sources = [{"ref": f"src{i}", "props": {}} for i in range(n_sources)]
        items = [
            {"item_id": f"item{i}_n0001", "citation": "text", "description": ""}
            for i in range(n_items)
        ]
        from_source = [
            {"item_id": f"item{i}_n0001", "ref": f"src{i % n_sources}"} for i in range(n_items)
        ]
        chains = [
            {
                "item_id": f"item{i}_n0001",
                "source": "Resilience",
                "target": "Trust",
                "type": "INFLUENCES",
            }
            for i in range(n_items)
        ]
        mentions = [
            {"item_id": f"item{i}_n0001", "concept": c}
            for i in range(n_items)
            for c in ("Resilience", "Trust")
        ]
        concepts = [
            {"props": {"name": "Resilience"}, "relations": {}},
            {"props": {"name": "Trust"}, "relations": {}},
        ]
        return _make_payload(
            concepts=concepts,
            items=items,
            chains=chains,
            mentions=mentions,
            sources=sources,
            from_source=from_source,
            graph_fields=[],
        )

    def test_concepts_pass_default_filters(self):
        payload = self._make_payload_for_filters(n_items=3, n_sources=2)
        kept, _ = _html_apply_filters(
            payload, min_frequency=3, min_source_count=2, max_nodes=200, include_isolated=False
        )
        assert "Resilience" in kept
        assert "Trust" in kept

    def test_low_frequency_concept_filtered_out(self):
        payload = self._make_payload_for_filters(n_items=1, n_sources=2)
        kept, _ = _html_apply_filters(
            payload, min_frequency=3, min_source_count=1, max_nodes=200, include_isolated=False
        )
        assert len(kept) == 0

    def test_low_source_count_filtered_out(self):
        payload = self._make_payload_for_filters(n_items=3, n_sources=1)
        kept, _ = _html_apply_filters(
            payload, min_frequency=3, min_source_count=2, max_nodes=200, include_isolated=False
        )
        assert len(kept) == 0

    def test_include_isolated_keeps_concepts_without_chains(self):
        concepts = [{"props": {"name": "Isolated"}, "relations": {}}]
        # 3 items across 2 sources → passes default filters (min_freq=3, min_src=2)
        items = [
            {"item_id": f"x{i}_n0001", "citation": "text", "description": ""}
            for i in range(3)
        ]
        from_source = [
            {"item_id": "x0_n0001", "ref": "src0"},
            {"item_id": "x1_n0001", "ref": "src0"},
            {"item_id": "x2_n0001", "ref": "src1"},
        ]
        mentions = [{"item_id": f"x{i}_n0001", "concept": "Isolated"} for i in range(3)]
        payload = _make_payload(
            concepts=concepts,
            items=items,
            mentions=mentions,
            from_source=from_source,
            graph_fields=[],
        )
        kept_no_isolated, _ = _html_apply_filters(
            payload, min_frequency=3, min_source_count=2, max_nodes=200, include_isolated=False
        )
        kept_with_isolated, _ = _html_apply_filters(
            payload, min_frequency=3, min_source_count=2, max_nodes=200, include_isolated=True
        )
        assert "Isolated" not in kept_no_isolated
        assert "Isolated" in kept_with_isolated

    def test_max_nodes_limits_output(self):
        concepts = [{"props": {"name": f"C{i}"}, "relations": {}} for i in range(10)]
        mentions = [
            {"item_id": f"i{j}_n0001", "concept": f"C{i}"}
            for i in range(10)
            for j in range(3)
        ]
        from_source = [
            {"item_id": f"i{j}_n0001", "ref": f"src{j % 2}"} for j in range(3) for _ in range(10)
        ]
        chains = [
            {
                "item_id": "i0_n0001",
                "source": f"C{i}",
                "target": f"C{(i + 1) % 10}",
                "type": "INFLUENCES",
            }
            for i in range(10)
        ]
        payload = _make_payload(
            concepts=concepts,
            mentions=mentions,
            from_source=from_source,
            chains=chains,
            graph_fields=[],
        )
        kept, _ = _html_apply_filters(
            payload, min_frequency=1, min_source_count=1, max_nodes=3, include_isolated=False
        )
        assert len(kept) <= 3

    def test_empty_payload_returns_empty_kept(self):
        payload = _make_payload(graph_fields=[])
        kept, chains = _html_apply_filters(
            payload, min_frequency=1, min_source_count=1, max_nodes=200, include_isolated=False
        )
        assert len(kept) == 0
        assert len(chains) == 0

    def test_chains_filtered_to_kept_concepts(self):
        payload = self._make_payload_for_filters(n_items=3, n_sources=2)
        kept, filtered_chains = _html_apply_filters(
            payload, min_frequency=3, min_source_count=2, max_nodes=200, include_isolated=False
        )
        for ch in filtered_chains:
            assert ch["source"] in kept
            assert ch["target"] in kept


# ---------------------------------------------------------------------------
# _html_resolve_grouping
# ---------------------------------------------------------------------------


class TestHtmlResolveGrouping:
    def test_explicit_group_by_field(self, minimal_payload):
        kept = {c["props"]["name"] for c in minimal_payload.concepts}
        cid_map, cname_map, legend, fname = _html_resolve_grouping(
            minimal_payload, kept, group_by="topic"
        )
        assert fname == "topic"
        assert len(legend) >= 1
        assert all(name in cid_map for name in kept)

    def test_fallback_to_first_graph_field_when_group_by_is_none(self, minimal_payload):
        kept = {c["props"]["name"] for c in minimal_payload.concepts}
        _, _, _, fname = _html_resolve_grouping(minimal_payload, kept, group_by=None)
        assert fname in minimal_payload.graph_fields or fname == "All"

    def test_no_graph_fields_groups_all_as_all(self):
        payload = _make_payload(graph_fields=[])
        kept = {"A", "B"}
        cid_map, cname_map, legend, fname = _html_resolve_grouping(payload, kept, group_by=None)
        assert fname == "All"
        assert all(v == "All" for v in cname_map.values())

    def test_legend_entries_have_required_keys(self, minimal_payload):
        kept = {c["props"]["name"] for c in minimal_payload.concepts}
        _, _, legend, _ = _html_resolve_grouping(minimal_payload, kept, group_by="topic")
        for entry in legend:
            assert "cid" in entry
            assert "color" in entry
            assert "label" in entry
            assert "count" in entry

    def test_concepts_missing_field_grouped_as_other(self):
        concepts = [
            {"props": {"name": "A"}, "relations": {}},
        ]
        payload = _make_payload(concepts=concepts, graph_fields=["topic"])
        kept = {"A"}
        _, cname_map, _, _ = _html_resolve_grouping(payload, kept, group_by="topic")
        assert cname_map["A"] == "Other"


# ---------------------------------------------------------------------------
# _html_build_hyperedges
# ---------------------------------------------------------------------------


class TestHtmlBuildHyperedges:
    def _make_multi_concept_payload(self) -> GraphPayload:
        concepts = [{"props": {"name": f"C{i}"}, "relations": {}} for i in range(5)]
        items = [{"item_id": "corpus1_n0001", "citation": "evidence text", "description": "note"}]
        from_source = [{"item_id": "corpus1_n0001", "ref": "smith2024"}]
        chains = [
            {
                "item_id": "corpus1_n0001",
                "source": f"C{i}",
                "target": f"C{i+1}",
                "type": "INFLUENCES",
            }
            for i in range(4)
        ]
        return _make_payload(
            concepts=concepts, items=items, from_source=from_source, chains=chains, graph_fields=[]
        )

    def test_hyperedge_built_for_corpus_with_3_plus_concepts(self):
        payload = self._make_multi_concept_payload()
        kept = {f"C{i}" for i in range(5)}
        hyperedges = _html_build_hyperedges(payload, kept, max_hyperedges=10)
        assert len(hyperedges) >= 1

    def test_hyperedge_has_required_fields(self):
        payload = self._make_multi_concept_payload()
        kept = {f"C{i}" for i in range(5)}
        hyperedges = _html_build_hyperedges(payload, kept, max_hyperedges=10)
        for he in hyperedges:
            assert "id" in he
            assert "label" in he
            assert "nodes" in he
            assert "relation" in he
            assert "source_item" in he

    def test_max_hyperedges_limits_output(self):
        payload = self._make_multi_concept_payload()
        kept = {f"C{i}" for i in range(5)}
        hyperedges = _html_build_hyperedges(payload, kept, max_hyperedges=0)
        assert len(hyperedges) == 0

    def test_empty_kept_produces_no_hyperedges(self):
        payload = self._make_multi_concept_payload()
        hyperedges = _html_build_hyperedges(payload, set(), max_hyperedges=10)
        assert len(hyperedges) == 0

    def test_fewer_than_3_concepts_no_hyperedge(self):
        concepts = [
            {"props": {"name": "A"}, "relations": {}},
            {"props": {"name": "B"}, "relations": {}},
        ]
        items = [{"item_id": "solo_n0001", "citation": "text", "description": ""}]
        from_source = [{"item_id": "solo_n0001", "ref": "src0"}]
        chains = [{"item_id": "solo_n0001", "source": "A", "target": "B", "type": "ENABLES"}]
        payload = _make_payload(
            concepts=concepts,
            items=items,
            from_source=from_source,
            chains=chains,
            graph_fields=[],
        )
        kept = {"A", "B"}
        hyperedges = _html_build_hyperedges(payload, kept, max_hyperedges=10)
        assert len(hyperedges) == 0
