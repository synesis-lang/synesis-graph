"""Tests for the project context assembled from the compiler's canonical JSON.

`ProjectContextSpec` is what makes an exported graph self-describing: without it
a consumer introspecting the schema sees that `Aspect` has a `name`, but not that
`Aspect` is Dooyeweerd's modal scale, nor what its ordered values mean. All of
that is declared in the template and, until now, dropped at export time.

These tests pin the two things most likely to rot:

- **Locations must be stripped recursively.** They appear at two levels — on the
  field spec and inside each `values[]` entry — and they are absolute paths from
  the machine that compiled the project. A shallow strip would leave most of the
  noise behind and leak local paths into a shared graph.
- **Nothing may be hardcoded for one project.** Every label and field name comes
  from the template, so two projects with different templates must produce
  different specs. This is the same mistake already made once elsewhere in the
  ecosystem — a prompt written against one project's vocabulary.

The fixtures are built by hand rather than compiled: these are unit tests of the
assembly step, and a compiler run would make them slow and dependent on corpus
files that are not versioned.
"""

from __future__ import annotations

from synesis_graph.backends.neo4j import _execute_sync_transaction, _sync_project_context
from synesis_graph.core import (
    GraphPayload,
    ProjectContextSpec,
    build_project_context,
    strip_locations,
)

LOCATION = {"file": "D:\\GitHub\\project\\template.synt", "line": 76, "column": 9}


def make_json(
    *,
    description: str | None = "Um estudo qualitativo.",
    guidelines: str | None = "Descreva em uma frase.\nNão inclua nome próprio.",
    concept_field: str = "chain",
) -> dict:
    """A canonical-JSON shape carrying the parts the context needs."""
    return {
        "export_metadata": {
            "timestamp": "2026-08-23T20:09:07.269841+00:00",
            "compiler_version": "1.1",
            "export_mode": "universal",
            "chain_count": 2713,
            "item_count": 1614,
            "source_count": 452,
            "concept_count": 1388,
        },
        "project": {
            "name": "estudo",
            "description": description,
            "metadata": {"version": "1.0", "author": "Pesquisadora"},
            "location": LOCATION,
        },
        "template": {
            "name": "estudo_template",
            "metadata": {"version": "1.0"},
            "required_fields": {"SOURCE": ["description"], "ITEM": ["text"], "ONTOLOGY": []},
            "optional_fields": {"SOURCE": [], "ITEM": [], "ONTOLOGY": ["topic"]},
            "forbidden_fields": {"SOURCE": [], "ITEM": [], "ONTOLOGY": []},
            "bundled_fields": {"SOURCE": [], "ITEM": [["note", concept_field]], "ONTOLOGY": []},
            "optional_bundles": {"SOURCE": [], "ITEM": [], "ONTOLOGY": []},
            "field_specs": {
                "aspect": {
                    "name": "aspect",
                    "type": "ORDERED",
                    "scope": "ONTOLOGY",
                    "description": "Aspectos modais de Dooyeweerd",
                    "guidelines": guidelines,
                    "location": LOCATION,
                    "values": [
                        {
                            "index": 0,
                            "label": "Undefined",
                            "description": "n/a",
                            "location": LOCATION,
                        },
                        {
                            "index": 15,
                            "label": "Fiducial",
                            "description": "fé",
                            "location": LOCATION,
                        },
                    ],
                },
                concept_field: {
                    "name": concept_field,
                    "type": "CHAIN",
                    "scope": "ITEM",
                    "arity": ">= 2",
                    "relations": {"ENABLES": "Condição necessária"},
                    "location": LOCATION,
                },
            },
        },
    }


def build(json_data: dict) -> ProjectContextSpec:
    return build_project_context(
        json_data,
        concept_label="Chain",
        graph_fields=["topic", "aspect"],
        synesis_graph_version="0.2.1",
    )


class TestStripLocations:
    """Locations must go at every depth, not just the top level."""

    def test_removes_top_level_location(self):
        assert strip_locations({"name": "x", "location": LOCATION}) == {"name": "x"}

    def test_removes_location_nested_in_lists(self):
        cleaned = strip_locations({"values": [{"index": 0, "location": LOCATION}]})
        assert cleaned == {"values": [{"index": 0}]}

    def test_keeps_everything_else(self):
        assert strip_locations({"a": 1, "b": [2, 3], "c": None}) == {"a": 1, "b": [2, 3], "c": None}

    def test_leaves_scalars_alone(self):
        assert strip_locations("texto") == "texto"
        assert strip_locations(7) == 7

    def test_a_key_merely_containing_location_survives(self):
        # Only the exact key is dropped; `location_hint` is ordinary data.
        assert strip_locations({"location_hint": "x"}) == {"location_hint": "x"}


class TestBuildProjectContext:
    """The context is assembled from what the compiler already emits.

    Written as Markdown, not JSON. Verified through the real MCP path against
    face85: as JSON the field specs reached the model as ~7.3k tokens in which
    53% of the keys were `null`; as prose the same content is 27% smaller and
    needs no parsing. GUIDELINES are already written with headings and line
    breaks — JSON escaping destroyed exactly that shape.
    """

    def test_description_comes_from_the_synp_block(self):
        assert build(make_json()).description == "Um estudo qualitativo."

    def test_missing_description_becomes_empty_string(self):
        # A project without a DESCRIPTION block yields None from the compiler;
        # the property stays a string so consumers need no special case.
        assert build(make_json(description=None)).description == ""

    def test_guidelines_are_readable_prose_not_escaped_json(self):
        doc = build(make_json()).template_doc
        assert "Descreva em uma frase." in doc
        assert "Não inclua nome próprio." in doc
        # The line break survives as a line break.
        assert "Descreva em uma frase.\nNão inclua nome próprio." in doc
        assert "\\\\n" not in doc

    def test_template_without_guidelines_does_not_break(self):
        doc = build(make_json(guidelines=None)).template_doc
        assert "Orientações de codificação" not in doc
        assert "`aspect`" in doc

    def test_null_keys_are_not_rendered(self):
        # Over half the keys in a real field_spec are null; printing them would
        # bury the content that matters under placeholders.
        doc = build(make_json()).template_doc
        assert "None" not in doc
        assert "null" not in doc

    def test_no_location_survives_anywhere(self):
        doc = build(make_json()).template_doc
        assert "location" not in doc
        assert "template.synt" not in doc

    def test_ordered_values_are_listed_with_index_and_meaning(self):
        doc = build(make_json()).template_doc
        assert "[0] Undefined: n/a" in doc
        assert "[15] Fiducial: fé" in doc

    def test_chain_relations_and_arity_are_stated(self):
        doc = build(make_json()).template_doc
        assert ">= 2" in doc
        assert "ENABLES: Condição necessária" in doc

    def test_field_rules_are_stated_per_scope(self):
        doc = build(make_json()).template_doc
        assert "**ITEM**" in doc
        assert "em conjunto `note`, `chain`" in doc
        assert "obrigatórios `text`" in doc

    def test_counts_measure_what_reaches_the_graph(self):
        # NOT the compiler's own counters: on the bibliometrics corpus its
        # `item_count` is 1614 (SOURCE blocks) while the sync writes 2713 `Item`
        # vertices. Caught by the end-to-end run, not by unit tests.
        context = build_project_context(
            make_json(),
            concept_label="Chain",
            graph_fields=[],
            synesis_graph_version="0.2.1",
            sources=[{}, {}],
            items=[{}, {}, {}],
            concepts=[{}],
        )
        assert (context.source_count, context.item_count, context.concept_count) == (2, 3, 1)
        # Also stated in prose, for a model reading the summary.
        assert "2 referências" in context.project_summary
        assert "3 trechos analisados" in context.project_summary

    def test_counts_are_zero_when_rows_are_not_supplied(self):
        context = build(make_json())
        assert (context.source_count, context.item_count, context.concept_count) == (0, 0, 0)

    def test_provenance_is_recorded(self):
        context = build(make_json())
        assert context.compiler_version == "1.1"
        assert context.compiled_at.startswith("2026-08-23")
        assert context.synesis_graph_version == "0.2.1"
        assert context.generated_at
        # The summary warns that this describes a snapshot, not the live project.
        assert "snapshot" in context.project_summary

    def test_taxonomy_labels_are_named(self):
        assert "`Topic`" in build(make_json()).template_doc
        assert "`Aspect`" in build(make_json()).template_doc

    def test_concept_label_is_stated_in_prose(self):
        doc = build(make_json()).template_doc
        assert "vértices `Chain`" in doc

    def test_navigation_states_the_edges_with_direction(self):
        """Without this the consumer has to guess the topology.

        Verified through the real MCP path against face85: guessing
        `MAPPED_TO_ASPECT` (a stale type still in the schema, with zero edges)
        instead of `QUALIFIED_BY` returned zero rows and no error — the silent
        failure that makes a model keep probing or declare the data absent.
        """
        doc = build(make_json()).template_doc
        assert "(Item)-[:FROM_SOURCE]->(Source)" in doc
        assert "(Item)-[:MENTIONS]->(Chain)" in doc
        # The taxonomy edge that was guessed wrong.
        assert "(Chain)-[:QUALIFIED_BY]->(Aspect)" in doc
        assert "(Chain)-[:GROUPED_BY]->(Topic)" in doc

    def test_navigation_gives_the_concept_to_source_path(self):
        doc = build(make_json()).template_doc
        assert "MATCH (c:Chain)<-[:MENTIONS]-(i:Item)-[:FROM_SOURCE]->(s:Source)" in doc

    def test_navigation_follows_the_project_concept_label(self):
        context = build_project_context(
            make_json(),
            concept_label="Code",
            graph_fields=["topic"],
            synesis_graph_version="0.2.1",
        )
        assert "(Item)-[:MENTIONS]->(Code)" in context.template_doc
        assert "(Chain)" not in context.template_doc

    def test_navigation_handles_a_field_outside_the_relation_map(self):
        # Fields the map does not know fall back to HAS_<FIELD>; the document
        # must say so rather than omit the edge.
        context = build_project_context(
            make_json(),
            concept_label="Chain",
            graph_fields=["metodologia"],
            synesis_graph_version="0.2.1",
        )
        assert "(Chain)-[:HAS_METODOLOGIA]->(Metodologia)" in context.template_doc

    def test_nothing_is_hardcoded_for_one_project(self):
        # The concept field is named by the template; a project calling it `code`
        # must produce a different document. This is the test that proves the
        # context adapts to any template instead of assuming one vocabulary.
        default = build(make_json()).template_doc
        other = build(make_json(concept_field="code")).template_doc
        assert "`chain`" in default and "`code`" not in default
        assert "`code`" in other and "`chain`" not in other

    def test_empty_json_does_not_raise(self):
        # A hand-built or partial payload must degrade, not crash.
        context = build_project_context(
            {}, concept_label="", graph_fields=[], synesis_graph_version=""
        )
        assert context.description == ""
        assert context.source_count == 0
        assert context.template_doc  # still a document, just an empty one


class FakeTx:
    """Records the statements a sync would send, instead of running them."""

    def __init__(self):
        self.runs: list[tuple[str, dict]] = []

    def run(self, statement, **params):
        self.runs.append((statement, params))
        return []


class TestSyncProjectContext:
    """The write itself: what statement the backend emits, and when it stays quiet.

    Both backends share this function — `arcadedb.py` imports its write helpers
    from `neo4j.py` — so covering it here covers both.
    """

    def test_emits_a_create_for_the_vertex(self):
        tx = FakeTx()
        _sync_project_context(tx, build(make_json()))
        assert len(tx.runs) == 1
        statement, params = tx.runs[0]
        assert "CREATE (p:ProjectContext)" in statement
        assert params["props"]["description"] == "Um estudo qualitativo."

    def test_writes_every_field_of_the_spec(self):
        tx = FakeTx()
        context = build(make_json())
        _sync_project_context(tx, context)
        assert set(tx.runs[0][1]["props"]) >= set(vars(context))

    def test_no_property_is_none(self):
        # Neo4j rejects a null inside `SET p = $props`; the builder turns every
        # absent value into an empty string precisely so this holds.
        tx = FakeTx()
        _sync_project_context(tx, build(make_json(description=None)))
        assert all(v is not None for v in tx.runs[0][1]["props"].values())

    def test_none_context_writes_nothing(self):
        # Hand-built payloads carry no project to describe.
        tx = FakeTx()
        _sync_project_context(tx, None)
        assert tx.runs == []

    def test_uses_create_not_merge(self):
        # Both backends wipe the graph before syncing, so a single instance is
        # already guaranteed; MERGE would imply a uniqueness key this vertex
        # does not have.
        tx = FakeTx()
        _sync_project_context(tx, build(make_json()))
        assert "MERGE" not in tx.runs[0][0]


class TestInsideTheSyncTransaction:
    """Where the write sits in the sync, not just what it emits.

    The context describes one snapshot, so it must not survive a sync that
    failed halfway — which means inside the transaction, before the commit.
    """

    class Tx:
        def __init__(self):
            self.runs: list[str] = []

        def run(self, statement, **_params):
            self.runs.append(statement)
            return []

        def commit(self):
            self.runs.append("COMMIT")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class Session:
        def __init__(self, tx):
            self._tx = tx

        def begin_transaction(self):
            return self._tx

    def payload(self, context):
        return GraphPayload(
            project_name="p",
            concept_label="Chain",
            scalar_fields=[],
            graph_fields=[],
            chain_fields=[],
            code_fields=[],
            source_fields=[],
            value_maps={},
            concepts=[],
            sources=[],
            items=[],
            chains=[],
            mentions=[],
            from_source=[],
            project_context=context,
        )

    def run_sync(self, context):
        tx = self.Tx()
        _execute_sync_transaction(self.Session(tx), self.payload(context))
        return tx.runs

    def test_context_is_written_before_the_commit(self):
        runs = self.run_sync(build(make_json()))
        written = [i for i, r in enumerate(runs) if "ProjectContext" in r]
        assert written, "the context was never written"
        assert written[0] < runs.index("COMMIT")

    def test_legacy_payload_syncs_without_a_context(self):
        # `project_context` defaults to None, so payloads built before this
        # feature keep working untouched.
        assert not any("ProjectContext" in r for r in self.run_sync(None))
