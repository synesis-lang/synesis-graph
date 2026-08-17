"""Tests for embedding field selection and text extraction (no ML dependency)."""

from __future__ import annotations

import json

import pytest

from synesis_graph.embeddings import (
    SCHEMA_VERSION,
    ConceptText,
    EmbeddingFieldError,
    EmbeddingsSidecar,
    build_concept_text,
    build_sidecar,
    load_sidecar,
    ontology_field_types,
    resolve_fields,
    sidecar_path,
)

# ---------------------------------------------------------------------------
# Fixtures mirroring the two real corpora
# ---------------------------------------------------------------------------

# face85: one TEXT field, TOPIC as a graph field. Confirmed against the corpus.
FACE85_TEMPLATE = {
    "field_specs": {
        "ontology_description": {"scope": "ONTOLOGY", "type": "TEXT"},
        "aspect": {"scope": "ONTOLOGY", "type": "ORDERED", "values": [{"1": "a"}]},
        "topic": {"scope": "ONTOLOGY", "type": "TOPIC"},
        "citation": {"scope": "ITEM", "type": "QUOTATION"},
    }
}

# Social_Acceptance: nine ontology fields, including the constant one.
SOCIAL_TEMPLATE = {
    "field_specs": {
        "ontology_description": {"scope": "ONTOLOGY", "type": "TEXT"},
        "topic": {"scope": "ONTOLOGY", "type": "TOPIC"},
        "reasoning": {"scope": "ONTOLOGY", "type": "TEXT"},
        "rgt_element_a": {"scope": "ONTOLOGY", "type": "TEXT"},
        "theoretical_significance": {"scope": "ONTOLOGY", "type": "SCALE"},
        "confidence": {"scope": "ONTOLOGY", "type": "ENUMERATED"},
    }
}


def _concept(name, description="", topic=None, **props):
    """Builds a concept the way core._extract_concepts does."""
    return {
        "props": {
            "name": name,
            "search_name": name.replace("_", " "),
            "ontology_description": description,
            **props,
        },
        "relations": {"topic": topic or []},
    }


@pytest.fixture
def face85_payload(payload_factory):
    return payload_factory(
        scalar_fields=["ontology_description"],
        graph_fields=["aspect", "topic"],
        concepts=[
            _concept("acumulação_flexível", "Regime de organização da produção.", ["Labor"]),
            _concept("vieses_cognitivos", "Desvios sistemáticos de julgamento.", ["Cognition"]),
            _concept("endividamento_empresarial", "Grau de alavancagem da firma.", ["Finance"]),
        ],
    )


# ---------------------------------------------------------------------------
# ontology_field_types
# ---------------------------------------------------------------------------


def test_reads_only_ontology_scoped_fields():
    types_ = ontology_field_types(FACE85_TEMPLATE)
    assert types_ == {
        "ontology_description": "TEXT",
        "aspect": "ORDERED",
        "topic": "TOPIC",
    }
    assert "citation" not in types_


def test_field_type_is_uppercased():
    tpl = {"field_specs": {"d": {"scope": "ontology", "type": "text"}}}
    assert ontology_field_types(tpl) == {"d": "TEXT"}


def test_missing_type_defaults_to_text():
    tpl = {"field_specs": {"d": {"scope": "ONTOLOGY"}}}
    assert ontology_field_types(tpl) == {"d": "TEXT"}


def test_empty_template_yields_no_fields():
    assert ontology_field_types({}) == {}


# ---------------------------------------------------------------------------
# resolve_fields — validation
# ---------------------------------------------------------------------------


def test_accepts_text_and_topic(face85_payload):
    result = resolve_fields(["ontology_description", "topic"], FACE85_TEMPLATE, face85_payload)
    fields, warnings = result
    assert fields == ["ontology_description", "topic"]
    assert warnings == []


def test_unknown_field_is_an_error_listing_the_available_ones(face85_payload):
    result = resolve_fields(["descricao"], FACE85_TEMPLATE, face85_payload)
    assert isinstance(result, EmbeddingFieldError)
    assert "descricao" in result.message
    # The message must be actionable: it names what the user could have typed.
    assert "ontology_description" in (result.details or "")
    assert "topic" in (result.details or "")
    assert result.stage == "embeddings"


def test_closed_vocabulary_warns_but_is_included(face85_payload):
    """ORDERED is a bad choice, not an impossible one — the user may know better."""
    values = ["Econômico", "Social", "Político"]
    for c, val in zip(face85_payload.concepts, values, strict=True):
        c["relations"]["aspect"] = [val]

    fields, warnings = resolve_fields(["aspect"], FACE85_TEMPLATE, face85_payload)
    assert fields == ["aspect"]
    assert any("closed vocabulary" in w for w in warnings)


def test_template_without_ontology_fields_is_an_error(face85_payload):
    tpl = {"field_specs": {"citation": {"scope": "ITEM", "type": "QUOTATION"}}}
    result = resolve_fields(["citation"], tpl, face85_payload)
    assert isinstance(result, EmbeddingFieldError)
    assert "no SCOPE ONTOLOGY" in result.message


def test_field_order_is_preserved(face85_payload):
    fields, _ = resolve_fields(["topic", "ontology_description"], FACE85_TEMPLATE, face85_payload)
    assert fields == ["topic", "ontology_description"]


# ---------------------------------------------------------------------------
# resolve_fields — constant detection
# ---------------------------------------------------------------------------


def test_constant_field_is_skipped_with_a_warning(payload_factory):
    """theoretical_significance is 0 in all 1388 Social_Acceptance concepts."""
    concepts = [_concept(f"C{i}", f"desc {i}", theoretical_significance=0) for i in range(5)]
    payload = payload_factory(concepts=concepts)

    fields, warnings = resolve_fields(
        ["ontology_description", "theoretical_significance"], SOCIAL_TEMPLATE, payload
    )
    assert fields == ["ontology_description"]
    assert any("constant" in w for w in warnings)


def test_a_skipped_field_does_not_also_warn_that_it_was_included(payload_factory):
    """theoretical_significance is SCALE *and* constant — two warnings would contradict.

    Measured on Social_Acceptance: it fired "Including it anyway" immediately
    before "Skipping it".
    """
    concepts = [_concept(f"C{i}", f"d{i}", theoretical_significance=0) for i in range(4)]
    payload = payload_factory(concepts=concepts)

    fields, warnings = resolve_fields(
        ["ontology_description", "theoretical_significance"], SOCIAL_TEMPLATE, payload
    )
    assert "theoretical_significance" not in fields
    assert not any("Including it anyway" in w for w in warnings)
    assert sum("theoretical_significance" in w for w in warnings) == 1


def test_all_fields_constant_is_an_error(payload_factory):
    concepts = [_concept(f"C{i}", "", theoretical_significance=0) for i in range(3)]
    payload = payload_factory(concepts=concepts)

    result = resolve_fields(["theoretical_significance"], SOCIAL_TEMPLATE, payload)
    assert isinstance(result, EmbeddingFieldError)
    assert "No usable fields" in result.message


def test_field_missing_from_every_concept_counts_as_constant(payload_factory):
    payload = payload_factory(concepts=[_concept("A", "x"), _concept("B", "y")])
    fields, warnings = resolve_fields(
        ["ontology_description", "reasoning"], SOCIAL_TEMPLATE, payload
    )
    assert fields == ["ontology_description"]
    assert any("reasoning" in w for w in warnings)


# ---------------------------------------------------------------------------
# build_concept_text
# ---------------------------------------------------------------------------


def test_name_leads_and_is_humanized():
    c = _concept("governança_corporativa", "Estrutura de poder.")
    text = build_concept_text(c, ["ontology_description"])
    assert text.startswith("governança corporativa")
    assert "_" not in text


def test_topic_is_read_from_relations_not_props():
    """TOPIC is a graph field: core puts it in `relations`, never in `props`."""
    c = _concept("Acceptability", "A judgement.", topic=["Worldview"])
    assert "Worldview" in build_concept_text(c, ["topic"])


def test_multivalued_topic_is_joined():
    c = _concept("X", "d", topic=["Economics", "Risk"])
    text = build_concept_text(c, ["topic"])
    assert "Economics" in text and "Risk" in text


def test_fields_appear_in_the_requested_order():
    c = _concept("X", "DESC", topic=["TOP"])
    assert build_concept_text(c, ["topic", "ontology_description"]).index(
        "TOP"
    ) < build_concept_text(c, ["topic", "ontology_description"]).index("DESC")


def test_empty_field_does_not_leave_a_dangling_separator():
    c = _concept("X", "", topic=["Labor"])
    text = build_concept_text(c, ["ontology_description", "topic"])
    assert ".." not in text
    assert not text.endswith(".")


def test_whitespace_is_normalized():
    """A reformatted .syno must not invalidate the corpus."""
    a = _concept("X", "uma   descrição\n  com   espaços")
    b = _concept("X", "uma descrição com espaços")
    assert build_concept_text(a, ["ontology_description"]) == build_concept_text(
        b, ["ontology_description"]
    )


def test_concept_with_no_fields_still_yields_its_name():
    assert build_concept_text(_concept("solo_name"), []) == "solo name"


# ---------------------------------------------------------------------------
# Hashing — the invalidation contract
# ---------------------------------------------------------------------------


def test_same_text_hashes_the_same():
    assert ConceptText("a", "hello").hash == ConceptText("b", "hello").hash


def test_different_text_hashes_differently():
    assert ConceptText("a", "hello").hash != ConceptText("a", "world").hash


def test_fields_hash_depends_on_order():
    """Order changes the concatenated text, so it must change the hash."""
    a = EmbeddingsSidecar(fields=["topic", "description"])
    b = EmbeddingsSidecar(fields=["description", "topic"])
    assert a.fields_hash != b.fields_hash


def test_fields_hash_is_stable_across_instances():
    a = EmbeddingsSidecar(fields=["topic", "description"])
    b = EmbeddingsSidecar(fields=["topic", "description"])
    assert a.fields_hash == b.fields_hash


def test_field_names_cannot_collide_by_concatenation():
    """['ab','c'] and ['a','bc'] must not hash alike — hence the separator."""
    assert (
        EmbeddingsSidecar(fields=["ab", "c"]).fields_hash
        != EmbeddingsSidecar(fields=["a", "bc"]).fields_hash
    )


# ---------------------------------------------------------------------------
# build_sidecar
# ---------------------------------------------------------------------------


def test_builds_one_entry_per_concept(face85_payload):
    sidecar = build_sidecar(face85_payload, ["ontology_description"])
    assert len(sidecar.concepts) == 3
    assert "vieses_cognitivos" in sidecar.concepts


def test_concept_without_a_name_is_skipped(payload_factory):
    payload = payload_factory(concepts=[{"props": {}, "relations": {}}])
    assert build_sidecar(payload, ["ontology_description"]).concepts == {}


def test_concept_with_empty_text_is_skipped(payload_factory):
    """An empty string embeds to a meaningless point that still ranks as a neighbour."""
    payload = payload_factory(concepts=[{"props": {"name": ""}, "relations": {}}])
    assert build_sidecar(payload, ["ontology_description"]).concepts == {}


def test_stage_one_sidecar_carries_no_vectors(face85_payload):
    sidecar = build_sidecar(face85_payload, ["ontology_description"])
    assert sidecar.vectors == {}
    entry = sidecar.to_dict()["concepts"]["vieses_cognitivos"]
    assert "vector" not in entry
    assert entry["hash"].startswith("sha256:")


# ---------------------------------------------------------------------------
# Sidecar file
# ---------------------------------------------------------------------------


def test_sidecar_path_derives_from_the_project(tmp_path):
    assert sidecar_path(tmp_path / "face85.synp").name == "face85.embeddings.json"


def test_written_file_is_valid_json_with_the_schema_version(tmp_path, face85_payload):
    path = tmp_path / "p.embeddings.json"
    build_sidecar(face85_payload, ["ontology_description"]).write(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["fields"] == ["ontology_description"]
    assert raw["fields_hash"].startswith("sha256:")


def test_writing_twice_is_byte_identical(tmp_path, face85_payload):
    """Determinism is what makes "nothing changed" verifiable."""
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    build_sidecar(face85_payload, ["ontology_description"]).write(a)
    build_sidecar(face85_payload, ["ontology_description"]).write(b)
    assert a.read_bytes() == b.read_bytes()


def test_accented_names_survive_the_round_trip(tmp_path, face85_payload):
    path = tmp_path / "p.json"
    build_sidecar(face85_payload, ["ontology_description"]).write(path)
    assert "acumulação_flexível" in load_sidecar(path).concepts


def test_round_trip_preserves_text_and_hash(tmp_path, face85_payload):
    path = tmp_path / "p.json"
    original = build_sidecar(face85_payload, ["ontology_description"])
    original.write(path)
    loaded = load_sidecar(path)
    assert loaded.fields == original.fields
    for name, ct in original.concepts.items():
        assert loaded.concepts[name].text == ct.text
        assert loaded.concepts[name].hash == ct.hash


def test_missing_file_loads_as_none(tmp_path):
    assert load_sidecar(tmp_path / "absent.json") is None


def test_malformed_file_loads_as_none(tmp_path):
    """The sidecar is a cache: regenerating is always correct, crashing is not."""
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_sidecar(path) is None


def test_unknown_schema_version_loads_as_none(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"schema_version": 999, "concepts": {}}), encoding="utf-8")
    assert load_sidecar(path) is None


def test_file_without_schema_version_loads_as_none(tmp_path):
    path = tmp_path / "pre.json"
    path.write_text(json.dumps({"fields": [], "concepts": {}}), encoding="utf-8")
    assert load_sidecar(path) is None
