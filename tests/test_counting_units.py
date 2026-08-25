"""Counting units: a block, an item, a mention and a concept are different things.

An `ITEM` block with four chains yields four `Item` vertices — four analytical units
over one annotated excerpt. Both counts are legitimate answers to different questions,
and comparing one against the other is not.

That comparison is exactly what made an audit turn contradict a correct answer: it
counted `count(DISTINCT i.item_id)` (excerpts, as it thought) and read the result
against a number that meant mentions — reporting 11 where the bank said 20.

`annotation_id` is what makes the distinction expressible in a query instead of
guessable by grouping on file and line.
"""

from __future__ import annotations

from typing import Any

from synesis_graph.core import _build_item_row, _extract_corpus_data


def _extract(corpus: list[dict[str, Any]]) -> tuple[list, list, list]:
    sources, items, mentions, _, _, _ = _extract_corpus_data(
        corpus,
        bibliography={},
        relation_definitions={},
        code_field_names=["code"],
        chain_field_names=["chain"],
        source_fields=[],
        ontology_field_names=[],
        memo_field_name="note",
        quotation_field_name="citation",
    )
    return sources, items, mentions


# One block, one excerpt, four chains — the shape that produced the real defect.
FOUR_CHAINS = [
    {
        "id": "avelar2016_item0001",
        "source_ref": "@avelar2016",
        "data": {
            "citation": "duas variáveis independentes se mostraram significativas",
            "chain": [
                {"from": "trust", "relation": "leads to", "to": "acceptance"},
                {"from": "cost", "relation": "leads to", "to": "rejection"},
                {"from": "risk", "relation": "leads to", "to": "rejection"},
                {"from": "info", "relation": "leads to", "to": "trust"},
            ],
        },
        "traceability": {"file": "face85.syn", "line": 187},
    }
]


def test_one_block_yields_several_items():
    """Four chains over one excerpt: four analytical units, one annotation."""
    _, items, _ = _extract(FOUR_CHAINS)

    assert len(items) == 4
    assert len({i["item_id"] for i in items}) == 4


def test_all_items_of_a_block_share_one_annotation_id():
    """This is what lets a query count excerpts instead of items."""
    _, items, _ = _extract(FOUR_CHAINS)

    assert len({i["annotation_id"] for i in items}) == 1
    assert items[0]["annotation_id"] == "avelar2016_item0001"


def test_the_five_units_are_distinguishable():
    """The acceptance criterion: block, item, mention, concept and source differ.

    Reading any of these numbers as another is the error the divergence rule exists
    to prevent — and none of them is wrong on its own.
    """
    sources, items, mentions = _extract(FOUR_CHAINS)

    annotations = len({i["annotation_id"] for i in items})
    analytical_items = len(items)
    mention_edges = len(mentions)
    concepts = len({m["concept"] for m in mentions})

    assert annotations == 1
    assert analytical_items == 4
    # Each chain mentions its source and target concept.
    assert mention_edges == 8
    assert concepts == 6
    assert len(sources) == 1

    # The point of the test, stated as the assertion: these are five different
    # numbers over the same data, so a bare "20" answers nothing.
    assert len({annotations, analytical_items, mention_edges, concepts}) == 4


def test_code_branch_also_carries_the_block_identity():
    """The other branch of `_extract_corpus_data` must not diverge."""
    corpus = [
        {
            "id": "smith_item0001",
            "source_ref": "@smith",
            "data": {"citation": "a quotation", "code": ["trust", "cost"]},
        }
    ]

    _, items, _ = _extract(corpus)

    assert {i["annotation_id"] for i in items} == {"smith_item0001"}


def test_annotation_id_is_omitted_when_absent():
    """Absent means omitted, never an empty string.

    A consumer asking `WHERE i.annotation_id IS NOT NULL` must get an honest answer,
    the same rule the traceability pair follows.
    """
    row = _build_item_row("x_c0001", "quote", "", {})
    assert "annotation_id" not in row


def test_template_field_cannot_overwrite_the_block_identity():
    """Structural keys win: a template free to name a field `annotation_id` must not
    be able to rewrite which block an excerpt belongs to."""
    row = _build_item_row(
        "x_c0001", "quote", "", {"annotation_id": "forged"}, annotation_id="real_block"
    )
    assert row["annotation_id"] == "real_block"
