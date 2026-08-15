"""ITEM template fields must reach the Neo4j node, not only the HTML preview.

The Item node used to carry just item_id/citation/description, while every other
template field (zone, confidence, ...) was diverted to the HTML-only `item_fields`
map. These tests pin the corrected contract: the fields land on the node itself,
without breaking the HTML path that consumes `item_fields`.
"""

from __future__ import annotations

from typing import Any

from synesis_graph.core import CodeFieldSpec, _build_graph_payload, _build_item_row

# ---------------------------------------------------------------------------
# _build_item_row (unit)
# ---------------------------------------------------------------------------


def test_template_fields_become_node_properties():
    row = _build_item_row("i1", "quoted text", "a memo", {"zone": "Result", "confidence": "High"})

    assert row["zone"] == "Result"
    assert row["confidence"] == "High"


def test_structural_properties_are_preserved():
    row = _build_item_row("i1", "quoted text", "a memo", {})

    assert row == {"item_id": "i1", "citation": "quoted text", "description": "a memo"}


def test_template_field_cannot_overwrite_structural_key():
    """A template is free to name a field `citation` — the quotation still wins."""
    row = _build_item_row(
        "i1",
        "the real quotation",
        "a memo",
        {"citation": "template value", "item_id": "spoofed", "zone": "Aim"},
    )

    assert row["citation"] == "the real quotation"
    assert row["item_id"] == "i1"
    assert row["zone"] == "Aim"  # non-colliding field still lands


def test_row_values_stay_flat():
    """Neo4j rejects nested maps; every value must be a scalar."""
    row = _build_item_row("i1", "text", "memo", {"zone": "Method", "score": "3"})

    assert all(isinstance(v, str) for v in row.values())


# ---------------------------------------------------------------------------
# _build_graph_payload (integration over the compiled-JSON shape)
# ---------------------------------------------------------------------------


def _json_with_item(data: dict[str, Any]) -> dict[str, Any]:
    """Minimal v3.x compiled-export shape carrying a single corpus item."""
    return {
        "project": {"name": "T"},
        "ontology": {},
        "bibliography": {"src1": {"title": "A title"}},
        "corpus": [{"id": "i001", "source_ref": "@src1", "data": data}],
    }


def _payload_for(data: dict[str, Any], code_fields: list[CodeFieldSpec] | None = None):
    return _build_graph_payload(
        json_data=_json_with_item(data),
        scalar_fields=[],
        graph_fields=[],
        chain_fields=[],
        code_fields=code_fields or [],
        value_maps={},
        source_fields=[],
        memo_field_name="memo",
        quotation_field_name="text",
    )


CHAIN_ITEM = {
    "text": "Trust enables cooperation.",
    "zone": "Result",
    "confidence": "High",
    "memo": "explicit causal claim",
    "chain": [{"from": "Trust", "relation": "ENABLES", "to": "Cooperation"}],
}


def test_chain_branch_exports_template_fields():
    payload = _payload_for(CHAIN_ITEM)

    (item,) = payload.items
    assert item["zone"] == "Result"
    assert item["confidence"] == "High"
    assert item["citation"] == "Trust enables cooperation."


def test_chain_branch_keeps_item_fields_for_html():
    """Non-regression: the HTML evidence view still gets its parallel map."""
    payload = _payload_for(CHAIN_ITEM)

    (item,) = payload.items
    assert payload.item_fields[item["item_id"]]["zone"] == "Result"


def test_code_branch_exports_template_fields():
    payload = _payload_for(
        {
            "text": "A quoted passage.",
            "zone": "Method",
            "codigo": ["Governance"],
        },
        code_fields=[CodeFieldSpec(field_name="codigo", description="")],
    )

    (item,) = payload.items
    assert item["zone"] == "Method"


def test_item_row_has_no_nested_values():
    """Whatever the template declares, the row stays driver-safe."""
    payload = _payload_for(CHAIN_ITEM)

    for item in payload.items:
        for value in item.values():
            assert not isinstance(value, dict | list)
