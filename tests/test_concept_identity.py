"""A identidade de conceito no grafo é a do compilador (Spec ROLE/EXPORT §4)."""

import unicodedata

from synesis_graph.core import concept_identity


def test_case_and_whitespace_collapse_like_the_compiler():
    assert concept_identity("Big  Data") == concept_identity("big data")


def test_composed_and_decomposed_unicode_are_the_same_concept():
    composed = unicodedata.normalize("NFC", "inovação")
    decomposed = unicodedata.normalize("NFD", "inovação")
    assert composed != decomposed
    assert concept_identity(composed) == concept_identity(decomposed)


def test_separators_are_not_equivalent():
    assert concept_identity("pig_butchering") != concept_identity("pig-butchering")
