"""The Item node must carry the audit trail back to the `.syn`.

The compiler already emits `traceability: {file, line}` per corpus item, and CSV/XLS
export it as `source_file`/`source_line`. The graph was the only export dropping it,
which left a chat answering from the graph unable to say where a quotation came from.

These tests pin two things: that the pair reaches the node, and that the path stored
is relative to the project root — an absolute path would leak the exporter's directory
layout to everyone the graph is shared with, and would not resolve on the machine that
later reads it.
"""

from __future__ import annotations

from typing import Any

from synesis_graph.core import (
    _build_item_row,
    _extract_corpus_data,
    project_root_from_includes,
    relative_source_file,
)

# ---------------------------------------------------------------------------
# relative_source_file (unit)
# ---------------------------------------------------------------------------


def test_absolute_path_becomes_relative_to_project_root():
    assert (
        relative_source_file("D:/proj/annotations.syn", "D:/proj") == "annotations.syn"
    )


def test_backslashes_are_normalised():
    """A graph exported on Windows is read by a chat that may run anywhere."""
    assert (
        relative_source_file("D:\\proj\\data\\annotations.syn", "D:/proj")
        == "data/annotations.syn"
    )


def test_path_outside_the_root_is_omitted():
    """Relativisation failed: omit, never keep the absolute path.

    Keeping it was the earlier behaviour, documented as "a wart, never a crash".
    It is worse than a wart: the absolute path leaks the exporting machine's
    directory layout to everyone the graph is shared with, and it does not
    resolve on the reader's machine — so the anchor it produces is a link that
    cannot open. An absent `source_file` is honest; a wrong one promises
    verification and fails.
    """
    assert relative_source_file("D:/elsewhere/a.syn", "D:/proj") == ""


def test_sibling_directory_is_not_inside_the_root():
    """The boundary is checked on path components, not on the raw prefix.

    `D:/proj-evil` starts with `D:/proj` as a string but is not inside it, and
    returning `-evil/a.syn` would be worse than returning nothing.
    """
    assert relative_source_file("D:/proj-evil/a.syn", "D:/proj") == ""


def test_empty_path_stays_empty():
    assert relative_source_file("", "D:/proj") == ""
    assert relative_source_file(None, "D:/proj") == ""


def test_no_root_omits_the_path():
    """Without a root there is nothing to relativise against, so nothing is written."""
    assert relative_source_file("D:/proj/a.syn", "") == ""


# ---------------------------------------------------------------------------
# project_root_from_includes (unit)
# ---------------------------------------------------------------------------


def _corpus_with(path: str) -> list[dict[str, Any]]:
    return [{"traceability": {"file": path, "line": 5}}]


def test_root_is_inferred_from_the_shared_suffix():
    project = {"includes": [{"path": "annotations.syn"}]}
    root = project_root_from_includes(project, _corpus_with("D:/proj/annotations.syn"))

    assert root == "D:/proj"


def test_root_inference_handles_a_nested_include():
    project = {"includes": [{"path": "data/annotations.syn"}]}
    root = project_root_from_includes(
        project, _corpus_with("D:/proj/data/annotations.syn")
    )

    assert root == "D:/proj"


def test_root_inference_tolerates_windows_separators():
    project = {"includes": [{"path": "annotations.syn"}]}
    root = project_root_from_includes(
        project, _corpus_with("D:\\proj\\annotations.syn")
    )

    assert root == "D:/proj"


def test_root_is_empty_when_nothing_matches():
    project = {"includes": [{"path": "annotations.syn"}]}

    assert project_root_from_includes(project, _corpus_with("D:/proj/other.syn")) == ""
    assert project_root_from_includes({}, _corpus_with("D:/proj/a.syn")) == ""
    assert project_root_from_includes(project, []) == ""


def test_root_inference_skips_items_without_traceability():
    """A corpus item with no location must not abort the search."""
    project = {"includes": [{"path": "annotations.syn"}]}
    corpus = [{}, {"traceability": {}}, {"traceability": {"file": "D:/proj/annotations.syn"}}]

    assert project_root_from_includes(project, corpus) == "D:/proj"


# ---------------------------------------------------------------------------
# _build_item_row (unit)
# ---------------------------------------------------------------------------


def test_traceability_reaches_the_item_row():
    row = _build_item_row("i1", "text", "memo", {}, "annotations.syn", 42)

    assert row["source_file"] == "annotations.syn"
    assert row["source_line"] == 42


def test_absent_traceability_writes_no_keys():
    """Omitted, not null: `WHERE i.source_file IS NOT NULL` must stay honest.

    A graph exported before this existed and one whose item simply had no location
    have to look the same to a consumer.
    """
    row = _build_item_row("i1", "text", "memo", {})

    assert "source_file" not in row
    assert "source_line" not in row


def test_line_zero_is_written():
    """0 is a line number, not absence — `if source_line` would swallow it."""
    row = _build_item_row("i1", "text", "memo", {}, "a.syn", 0)

    assert row["source_line"] == 0


def test_template_field_cannot_overwrite_the_audit_trail():
    """Same protection the quotation already had, for the same reason.

    A template free to name a field `source_file` must not be able to rewrite where
    a quotation came from.
    """
    row = _build_item_row(
        "i1",
        "text",
        "memo",
        {"source_file": "spoofed.syn", "source_line": "999", "zone": "Aim"},
        "real.syn",
        7,
    )

    assert row["source_file"] == "real.syn"
    assert row["source_line"] == 7
    assert row["zone"] == "Aim"  # non-colliding field still lands


# ---------------------------------------------------------------------------
# _extract_corpus_data (integration of the two branches)
# ---------------------------------------------------------------------------


def _extract(corpus: list[dict[str, Any]], project_root: str = "") -> list[dict[str, Any]]:
    _, items, _, _, _, _ = _extract_corpus_data(
        corpus,
        bibliography={},
        relation_definitions={},
        code_field_names=["code"],
        chain_field_names=["chain"],
        source_fields=[],
        ontology_field_names=[],
        memo_field_name="note",
        quotation_field_name="citation",
        project_root=project_root,
    )
    return items


def test_code_branch_carries_traceability():
    corpus = [
        {
            "id": "smith_item0001",
            "source_ref": "@smith",
            "data": {"citation": "a quotation", "code": ["trust"]},
            "traceability": {"file": "D:/proj/annotations.syn", "line": 12},
        }
    ]

    items = _extract(corpus, project_root="D:/proj")

    assert len(items) == 1
    assert items[0]["source_file"] == "annotations.syn"
    assert items[0]["source_line"] == 12


def test_chain_branch_carries_traceability():
    corpus = [
        {
            "id": "smith_item0001",
            "source_ref": "@smith",
            "data": {
                "citation": "a quotation",
                "chain": [{"from": "flood", "relation": "causes", "to": "solidarity"}],
            },
            "traceability": {"file": "D:/proj/annotations.syn", "line": 30},
        }
    ]

    items = _extract(corpus, project_root="D:/proj")

    assert len(items) == 1
    assert items[0]["source_file"] == "annotations.syn"
    assert items[0]["source_line"] == 30


def test_corpus_without_traceability_still_builds():
    """A JSON exported by an older compiler must not break the sync."""
    corpus = [
        {
            "id": "smith_item0001",
            "source_ref": "@smith",
            "data": {"citation": "a quotation", "code": ["trust"]},
        }
    ]

    items = _extract(corpus)

    assert len(items) == 1
    assert "source_file" not in items[0]
    assert "source_line" not in items[0]


# ---------------------------------------------------------------------------
# Explicit project root (Stage 5)
# ---------------------------------------------------------------------------


def test_explicit_root_beats_inference():
    """The caller that knows the root must not depend on a textual inference.

    `compile_project` has the `.synp` in hand, so the root is known. Inferring it
    from the redundancy between `project.includes[]` and `traceability.file` is a
    fallback for callers that cannot know — and it can fail.
    """
    corpus = [
        {
            "id": "smith_item0001",
            "source_ref": "@smith",
            "data": {"citation": "a quotation", "code": ["trust"]},
            # The includes say nothing useful; only the explicit root can resolve this.
            "traceability": {"file": "D:/real/root/notes.syn", "line": 5},
        }
    ]

    items = _extract(corpus, project_root="D:/real/root")

    assert items[0]["source_file"] == "notes.syn"


def test_failed_relativisation_omits_rather_than_leaks():
    """No root, or a path outside it: write nothing.

    Keeping the absolute path leaks the exporting machine's directory layout and
    produces an anchor that cannot open on the reader's machine.
    """
    corpus = [
        {
            "id": "smith_item0001",
            "source_ref": "@smith",
            "data": {"citation": "a quotation", "code": ["trust"]},
            "traceability": {"file": "C:/somebody-else/secret/notes.syn", "line": 5},
        }
    ]

    items = _extract(corpus, project_root="D:/real/root")

    assert "source_file" not in items[0]
    # The line alone is useless without the file, but it is not a leak; what
    # matters is that no path reaches the graph.
    assert items[0].get("source_file", "") == ""
