"""The graph declares which full-text indexes it carries, and under which analyzer.

The chat was working around, in natural language, a problem this layer already
solves: it instructed the model to cut a search term before its first accented
character, because `CONTAINS 'psicologicos'` misses `psicológicos`.
`SEARCH_INDEX` with a language analyzer finds it deterministically.

The consumer cannot use it without knowing that the index exists, over WHICH
fields, and under which name — `SEARCH_INDEX` addresses a composite index by its
exact field list, so guessing is not an option.
"""

from __future__ import annotations

from synesis_graph.core import (
    ProjectContextSpec,
    _analyzer_folds_accents,
    declare_fulltext_capability,
)


def _context() -> ProjectContextSpec:
    return ProjectContextSpec(
        project_name="face85",
        description="",
        concept_label="Chain",
        template_name="t",
        template_doc="",
        project_summary="resumo",
        compiler_version="",
        synesis_graph_version="",
        compiled_at="",
        generated_at="",
        source_count=0,
        item_count=0,
        concept_count=0,
    )


BRAZILIAN = "org.apache.lucene.analysis.br.BrazilianAnalyzer"
STANDARD = "org.apache.lucene.analysis.standard.StandardAnalyzer"


def test_declares_the_exact_field_lists():
    context = _context()
    declare_fulltext_capability(
        context, ["search_name", "ontology_description"], ["citation"], ["title"], BRAZILIAN
    )

    assert context.fulltext_concept_fields == "search_name,ontology_description"
    assert context.fulltext_item_fields == "citation"
    assert context.fulltext_source_fields == "title"
    assert context.fulltext_analyzer == BRAZILIAN


def test_prose_names_the_index_as_search_index_expects_it():
    """A composite index is addressed by its field list, not by a bare type name."""
    context = _context()
    declare_fulltext_capability(
        context, ["search_name", "ontology_description"], ["citation"], [], BRAZILIAN
    )

    assert "`Chain[search_name, ontology_description]`" in context.project_summary
    assert "`Item[citation]`" in context.project_summary


def test_language_analyzer_is_announced_as_accent_folding():
    context = _context()
    declare_fulltext_capability(context, ["search_name"], ["citation"], [], BRAZILIAN)

    assert "sem acento" in context.project_summary


def test_standard_analyzer_says_so_honestly():
    """`StandardAnalyzer` does no stemming and no accent folding.

    Presenting full-text as accent-insensitive under the default configuration
    would be wrong, and the consumer has no other way to find out.
    """
    context = _context()
    declare_fulltext_capability(context, ["search_name"], ["citation"], [], STANDARD)

    assert "não" in context.project_summary
    assert "psicologicos" in context.project_summary


def test_nothing_declared_when_there_is_no_index():
    """A half-declared capability is worse than none."""
    context = _context()
    declare_fulltext_capability(context, [], [], [], BRAZILIAN)

    assert context.fulltext_concept_fields == ""
    assert context.fulltext_analyzer == ""
    assert "Busca lexical" not in context.project_summary


def test_tolerates_absent_context():
    declare_fulltext_capability(None, ["x"], ["y"], [], BRAZILIAN)


def test_analyzer_folding_is_decided_by_class_name():
    assert _analyzer_folds_accents(BRAZILIAN)
    assert _analyzer_folds_accents("portuguese")
    assert _analyzer_folds_accents("org.apache.lucene.analysis.de.GermanAnalyzer")
    assert not _analyzer_folds_accents(STANDARD)
    assert not _analyzer_folds_accents("")
