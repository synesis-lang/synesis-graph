"""Full-text indexes must be derived from the template, never hardcoded.

Field names differ per project (`ontology_description` here, `factor_description`
there), so an index naming a fixed property would be created successfully and
index nothing. These tests pin the derivation and the exclusion of closed
vocabularies (TOPIC / ENUMERATED / ORDERED) from the prose indexes.
"""

from __future__ import annotations

from typing import Any

from synesis_graph.backends.neo4j import _create_search_indexes
from synesis_graph.core import (
    DEFAULT_FULLTEXT_ANALYZER,
    SourceFieldSpec,
    _extract_concepts,
    analyze_template,
    humanize_concept_name,
    source_field_names,
    text_source_field_names,
)

from .conftest import _make_payload


class FakeSession:
    """Captures Cypher statements instead of executing them."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params: list[dict[str, Any]] = []

    def run(self, query: str, **kwargs: Any) -> None:
        self.statements.append(" ".join(query.split()))
        self.params.append(kwargs)

    def indexes(self) -> list[str]:
        return [s for s in self.statements if "CREATE FULLTEXT INDEX" in s]


# ---------------------------------------------------------------------------
# analyze_template: SOURCE fields carry their declared type
# ---------------------------------------------------------------------------

TEMPLATE = {
    "field_specs": {
        "description": {"scope": "SOURCE", "type": "TEXT"},
        "knowledge_area": {
            "scope": "SOURCE",
            "type": "ENUMERATED",
            "values": [{"index": 1, "label": "Economics"}],
        },
        "method": {"scope": "SOURCE", "type": "TEXT"},
        "ontology_description": {"scope": "ONTOLOGY", "type": "TEXT"},
        "topic": {"scope": "ONTOLOGY", "type": "TOPIC"},
    }
}


def test_source_fields_carry_declared_type():
    (*_, source_fields, _memo, _quot) = analyze_template(TEMPLATE)

    by_name = {s.field_name: s for s in source_fields}
    assert by_name["description"].field_type == "TEXT"
    assert by_name["knowledge_area"].field_type == "ENUMERATED"


def test_all_source_field_names_are_still_available():
    """Node properties keep every SOURCE field, indexed or not."""
    (*_, source_fields, _memo, _quot) = analyze_template(TEMPLATE)

    assert set(source_field_names(source_fields)) == {
        "description",
        "knowledge_area",
        "method",
    }


def test_only_text_source_fields_are_indexable():
    (*_, source_fields, _memo, _quot) = analyze_template(TEMPLATE)

    assert set(text_source_field_names(source_fields)) == {"description", "method"}


def test_plain_strings_are_tolerated():
    """Hand-built payloads predate the specs and must not break."""
    assert source_field_names(["a", "b"]) == ["a", "b"]
    assert text_source_field_names(["a", "b"]) == ["a", "b"]


# ---------------------------------------------------------------------------
# _create_search_indexes
# ---------------------------------------------------------------------------


def test_concept_index_uses_template_scalar_fields(payload_factory):
    payload = payload_factory(scalar_fields=["ontology_description"])
    session = FakeSession()

    _create_search_indexes(session, payload)

    (concept_idx,) = [s for s in session.indexes() if "concept_search" in s]
    assert "c.ontology_description" in concept_idx


def test_concept_index_reads_search_name_not_name():
    """Raw snake_case indexes as one token; only the humanised copy is searchable."""
    session = FakeSession()

    _create_search_indexes(session, _make_payload())

    (concept_idx,) = [s for s in session.indexes() if "concept_search" in s]
    assert "c.search_name" in concept_idx
    assert "c.name," not in concept_idx and "[c.name" not in concept_idx


def test_concept_index_excludes_taxonomy_fields(payload_factory):
    """TOPIC/ENUMERATED/ORDERED become their own nodes, not prose."""
    payload = payload_factory(scalar_fields=["ontology_description"], graph_fields=["topic"])
    session = FakeSession()

    _create_search_indexes(session, payload)

    (concept_idx,) = [s for s in session.indexes() if "concept_search" in s]
    assert "topic" not in concept_idx


def test_source_index_excludes_enumerated_fields(payload_factory):
    payload = payload_factory(
        source_fields=[
            SourceFieldSpec("description", "TEXT"),
            SourceFieldSpec("knowledge_area", "ENUMERATED"),
        ]
    )
    session = FakeSession()

    _create_search_indexes(session, payload)

    (source_idx,) = [s for s in session.indexes() if "source_search" in s]
    assert "s.description" in source_idx
    assert "knowledge_area" not in source_idx


def test_item_index_uses_structural_names(payload_factory):
    """citation/description are normalised from QUOTATION/MEMO whatever they're called."""
    session = FakeSession()

    _create_search_indexes(session, payload_factory())

    (item_idx,) = [s for s in session.indexes() if "item_search" in s]
    assert "i.citation" in item_idx
    assert "i.description" in item_idx


def test_reexport_is_safe(payload_factory):
    """Each index is dropped first, so a re-export never hits IndexAlreadyExists."""
    session = FakeSession()

    _create_search_indexes(session, payload_factory())

    assert all("IF EXISTS" in s for s in session.statements if "DROP INDEX" in s)


def test_unsafe_field_names_are_rejected(payload_factory):
    """Template-derived names are interpolated, so they must be validated."""
    payload = payload_factory(scalar_fields=["ok_field", "bad-name; DROP"])
    session = FakeSession()

    _create_search_indexes(session, payload)

    concept_idx = next(
        s for s in session.indexes() if "concept_search" in s and "CREATE" in s
    )
    assert "ok_field" in concept_idx
    assert "bad-name" not in concept_idx


# ---------------------------------------------------------------------------
# search_name: the humanised copy that makes the index reachable
# ---------------------------------------------------------------------------


def test_underscores_become_spaces():
    assert humanize_concept_name("governança_corporativa") == "governança corporativa"


def test_humanised_name_has_no_underscore():
    """The whole point: Lucene must see words, not one token."""
    assert "_" not in humanize_concept_name("web_of_science")


def test_name_without_underscore_is_unchanged():
    assert humanize_concept_name("Trust") == "Trust"


def test_concepts_carry_both_names():
    """`name` stays the MERGE key; `search_name` is what the index reads."""
    concepts = _extract_concepts(
        {"governança_corporativa": {}}, scalar_fields=[], graph_fields=[], value_maps={}
    )

    props = concepts[0]["props"]
    assert props["name"] == "governança_corporativa"
    assert props["search_name"] == "governança corporativa"


def test_template_field_cannot_overwrite_search_name():
    """A SCOPE ONTOLOGY field named `search_name` must not clobber the derived one."""
    concepts = _extract_concepts(
        {"a_b": {"search_name": "hijacked", "name": "hijacked"}},
        scalar_fields=["search_name", "name"],
        graph_fields=[],
        value_maps={},
    )

    props = concepts[0]["props"]
    assert props["search_name"] == "a b"
    assert props["name"] == "a_b"


# ---------------------------------------------------------------------------
# Analyzer configuration
# ---------------------------------------------------------------------------


def test_default_analyzer_is_applied(payload_factory):
    """The analyzer rides as a query parameter, not interpolated into the Cypher."""
    session = FakeSession()

    _create_search_indexes(session, payload_factory())

    used = [p["analyzer"] for p in session.params if "analyzer" in p]
    assert used and all(a == DEFAULT_FULLTEXT_ANALYZER for a in used)


def test_configured_analyzer_is_applied(payload_factory):
    session = FakeSession()

    _create_search_indexes(session, payload_factory(), "brazilian")

    assert all("$analyzer" in s for s in session.indexes())
    assert all(p.get("analyzer") == "brazilian" for p in session.params if p)


def test_indexes_are_dropped_before_creation(payload_factory):
    """Neo4j refuses a second index on the same (label, properties) pair, so
    `CREATE ... IF NOT EXISTS` would silently keep a stale analyzer."""
    session = FakeSession()

    _create_search_indexes(session, payload_factory())

    def _at(fragment: str) -> int:
        return next(i for i, s in enumerate(session.statements) if fragment in s)

    for name in ("concept_search", "item_search"):
        assert _at(f"DROP INDEX {name}") < _at(f"CREATE FULLTEXT INDEX {name}")


# ---------------------------------------------------------------------------
# A template may declare a field named like a structural bibliographic prop
# ---------------------------------------------------------------------------


def test_source_index_lists_a_repeated_property_once(payload_factory):
    """`title` is prepended structurally AND declarable by the template.

    Quinto_Andar declares `FIELD title TYPE TEXT SCOPE SOURCE` for the candidate's
    name. Neo4j rejects the resulting composite index with
    `RepeatedPropertyInCompositeSchema`, which failed the sync after a 41k-item
    compile.
    """
    payload = payload_factory(
        source_fields=[
            SourceFieldSpec("title", "TEXT"),
            SourceFieldSpec("headline", "TEXT"),
        ]
    )
    session = FakeSession()

    _create_search_indexes(session, payload)

    (source_idx,) = [s for s in session.indexes() if "source_search" in s]
    props = source_idx.split("ON EACH [")[1].split("]")[0]
    names = [p.strip() for p in props.split(",")]
    assert names.count("s.title") == 1
    assert len(names) == len(set(names))
    assert "s.headline" in names


def test_concept_index_lists_a_repeated_property_once(payload_factory):
    """`search_name` is prepended; a template declaring it must not double it."""
    payload = payload_factory(scalar_fields=["search_name", "ontology_description"])
    session = FakeSession()

    _create_search_indexes(session, payload)

    concept_idx = next(s for s in session.indexes() if "concept_search" in s)
    props = concept_idx.split("ON EACH [")[1].split("]")[0]
    names = [p.strip() for p in props.split(",")]
    assert names.count("c.search_name") == 1
    assert len(names) == len(set(names))
