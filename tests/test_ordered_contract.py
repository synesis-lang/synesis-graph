"""Contract tests for ORDERED values arriving from the compiler.

Since synesis canonicalised ORDERED (the stored datum is always the INDEX, an
`int` — writing the label is now error E088), the graph receives a single type
for these fields. `_index_to_label` therefore resolves every value to the
declared label, and taxonomy nodes stop fragmenting on spelling variants.

These tests pin that contract. The end-to-end check in `test_linkage.py` that
would also cover it is skipped whenever the Davi corpus is absent (field data,
not versioned), so the guarantee needs its own test here.
"""

from __future__ import annotations

from synesis_graph.core import _extract_concepts, _index_to_label, analyze_template

ASPECT_MAP = [
    {"index": 0, "label": "Indefinido"},
    {"index": 2, "label": "Espacial"},
    {"index": 11, "label": "Econômico"},
]


class TestIndexToLabel:
    """The compiler now guarantees `int`; every value resolves to a label."""

    def test_index_resolves_to_label(self):
        assert _index_to_label(11, ASPECT_MAP) == "Econômico"

    def test_index_zero_resolves(self):
        # 0 is falsy — it must not be mistaken for a missing value.
        assert _index_to_label(0, ASPECT_MAP) == "Indefinido"

    def test_unknown_index_degrades_to_string(self):
        assert _index_to_label(99, ASPECT_MAP) == "99"

    def test_empty_map_degrades_to_string(self):
        assert _index_to_label(11, []) == "11"


class TestNoSpellingFragmentation:
    """Two spellings of one aspect must never become two taxonomy values.

    Under the old mixed contract a label reached the graph untouched, so
    'Econômico' and 'ECONÔMICO' produced distinct nodes. With indices this is
    unreachable: the datum is 11 and there is exactly one canonical label.
    """

    def test_same_index_always_yields_same_label(self):
        assert _index_to_label(11, ASPECT_MAP) == _index_to_label(11, ASPECT_MAP)

    def test_distinct_indices_yield_distinct_labels(self):
        labels = {_index_to_label(i, ASPECT_MAP) for i in (0, 2, 11)}
        assert len(labels) == 3

    def test_concepts_carry_canonical_labels(self):
        ontology = {
            "conceito_a": {"aspect": 11},
            "conceito_b": {"aspect": 11},
            "conceito_c": {"aspect": 2},
        }
        concepts = _extract_concepts(
            ontology,
            scalar_fields=[],
            graph_fields=["aspect"],
            value_maps={"aspect": ASPECT_MAP},
        )
        values = {v for c in concepts for v in c["relations"]["aspect"]}
        assert values == {"Econômico", "Espacial"}

    def test_no_numeric_value_reaches_the_graph(self):
        ontology = {f"c{i}": {"aspect": idx} for i, idx in enumerate((0, 2, 11))}
        concepts = _extract_concepts(
            ontology,
            scalar_fields=[],
            graph_fields=["aspect"],
            value_maps={"aspect": ASPECT_MAP},
        )
        values = [v for c in concepts for v in c["relations"]["aspect"]]
        assert not [v for v in values if str(v).lstrip("-").isdigit()]


class TestTemplateAnalysis:
    """ORDERED becomes a taxonomy node and carries its index->label map."""

    def _template(self) -> dict:
        return {
            "field_specs": {
                "aspect": {
                    "scope": "ONTOLOGY",
                    "type": "ORDERED",
                    "values": ASPECT_MAP,
                },
                "ontology_description": {"scope": "ONTOLOGY", "type": "TEXT"},
            }
        }

    def test_ordered_is_a_graph_field(self):
        _, graph_fields, *_ = analyze_template(self._template())
        assert "aspect" in graph_fields

    def test_ordered_populates_value_maps(self):
        _, _, _, _, value_maps, _, _, _ = analyze_template(self._template())
        assert value_maps["aspect"] == ASPECT_MAP

    def test_text_field_stays_scalar(self):
        scalar_fields, graph_fields, *_ = analyze_template(self._template())
        assert "ontology_description" in scalar_fields
        assert "ontology_description" not in graph_fields
