"""Tests for the ArcadeDB sync layer.

The client is faked, so these assert on the statements the backend emits — which is
where ArcadeDB differs from Neo4j. The graph-writing Cypher itself is reused from
the Neo4j backend and covered by its own tests; what matters here is the schema
declaration, the index syntax, the clearing, and the transaction boundary.

The integration tests at the bottom run against a live server when one is present.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from synesis_graph.arcadedb_client import ArcadeDBClient, ArcadeDBError
from synesis_graph.backends.arcadedb import (
    _create_schema,
    _create_search_indexes,
    clear_database,
    sync_to_arcadedb,
)
from synesis_graph.core import ProjectContextSpec, SyncError, build_project_context


class FakeClient:
    """Records every statement instead of sending it."""

    def __init__(self, query_results: dict[str, list[dict[str, Any]]] | None = None):
        self.statements: list[tuple[str, str, dict | None]] = []
        self.transactions: list[str] = []
        self._query_results = query_results or {}
        self.fail_on: str | None = None

    def command(self, statement, params=None, *, language="cypher", database=None):
        if self.fail_on and self.fail_on in statement:
            raise ArcadeDBError(f"forced failure on {self.fail_on}")
        self.statements.append((language, statement, params))
        return []

    def query(self, statement, params=None, *, language="cypher", database=None):
        self.statements.append((language, statement, params))
        for needle, rows in self._query_results.items():
            if needle in statement:
                return rows
        return []

    def begin(self, database=None):
        self.transactions.append("begin")
        return "AS-fake"

    def commit(self, database=None):
        self.transactions.append("commit")

    def rollback(self, database=None):
        self.transactions.append("rollback")

    # -- helpers for assertions --------------------------------------------
    def sql(self) -> list[str]:
        return [s for lang, s, _ in self.statements if lang == "sql"]

    def cypher(self) -> list[str]:
        return [s for lang, s, _ in self.statements if lang == "cypher"]

    def sql_matching(self, *needles: str) -> list[str]:
        return [s for s in self.sql() if all(n in s for n in needles)]


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


# ---------------------------------------------------------------------------
# Schema declaration — the step Neo4j does not need
# ---------------------------------------------------------------------------
class TestCreateSchema:
    def test_declares_structural_vertex_types(self, client, minimal_payload):
        _create_schema(client, minimal_payload)
        sql = client.sql()
        assert any("CREATE VERTEX TYPE Source" in s for s in sql)
        assert any("CREATE VERTEX TYPE Item" in s for s in sql)
        assert any("CREATE VERTEX TYPE Concept" in s for s in sql)

    def test_declares_taxonomy_types_from_graph_fields(self, client, minimal_payload):
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE VERTEX TYPE Topic")

    def test_declares_properties_backing_unique_indexes(self, client, minimal_payload):
        _create_schema(client, minimal_payload)
        sql = client.sql()
        assert any("CREATE PROPERTY Source.bibtex" in s for s in sql)
        assert any("CREATE PROPERTY Item.item_id" in s for s in sql)
        assert any("CREATE PROPERTY Concept.name" in s for s in sql)

    def test_declares_search_name_property(self, client, minimal_payload):
        """Without this the full-text index cannot be built at all."""
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE PROPERTY Concept.search_name")

    def test_declares_item_text_properties(self, client, minimal_payload):
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE PROPERTY Item.citation")
        assert client.sql_matching("CREATE PROPERTY Item.description")

    def test_declares_item_source_file(self, client, minimal_payload):
        """Undeclared properties are invisible to MCP introspection.

        The chat discovers the graph through `get_schema`; an Item whose audit trail
        is not declared would not announce that the trail exists at all — the trap
        `ProjectContext` already fell into.
        """
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE PROPERTY Item.source_file")

    def test_declares_item_source_line_as_integer(self, client, minimal_payload):
        """`source_line` is declared INTEGER, not left schema-less.

        It used to be omitted because `_declare_property` only wrote STRING, which
        ArcadeDB rejects for an integer. The typed helper removed that limitation, and
        an undeclared property is invisible to `get_schema` — which is how the chat
        discovers what the graph offers.
        """
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE PROPERTY Item.source_line IF NOT EXISTS INTEGER")

    def test_declares_item_annotation_id(self, client, minimal_payload):
        """The block identity is what makes counting units distinguishable in a query.

        `count(i)` counts analytical items; `count(DISTINCT i.annotation_id)` counts
        annotated excerpts. Comparing one against the other is what made an audit
        contradict a correct answer.
        """
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE PROPERTY Item.annotation_id")

    def test_declares_template_scalar_fields(self, client, payload_factory):
        payload = payload_factory(scalar_fields=["ontology_description"])
        _create_schema(client, payload)
        assert client.sql_matching("CREATE PROPERTY Concept.ontology_description")

    def test_uses_if_not_exists_everywhere(self, client, minimal_payload):
        """A re-export must not fail on a schema that is already there."""
        _create_schema(client, minimal_payload)
        for statement in client.sql():
            assert "IF NOT EXISTS" in statement

    def test_graph_fields_are_not_declared_as_concept_properties(self, client, payload_factory):
        """TOPIC/ORDERED become their own nodes; indexing them as prose would
        dilute the index."""
        payload = payload_factory(graph_fields=["topic"], scalar_fields=[])
        _create_schema(client, payload)
        assert not client.sql_matching("CREATE PROPERTY Concept.topic")

    def test_unsafe_label_is_skipped(self, client, payload_factory):
        payload = payload_factory()
        payload.concept_label = "Bad-Label; DROP"
        _create_schema(client, payload)
        assert not client.sql_matching("Bad-Label")

    def test_declares_entity_types_for_linked_projects(self, client, payload_factory):
        payload = payload_factory()
        payload.entities = {"Researcher": [{"entity_id": "r1"}]}
        _create_schema(client, payload)
        assert client.sql_matching("CREATE VERTEX TYPE Researcher")
        assert client.sql_matching("CREATE PROPERTY Researcher.entity_id")


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------
class TestSearchIndexes:
    def test_creates_fulltext_index_on_concept(self, client, minimal_payload):
        _create_search_indexes(client, minimal_payload)
        created = client.sql_matching("FULL_TEXT", "Concept")
        assert created
        assert "search_name" in created[0]

    def test_uses_sql_not_cypher_syntax(self, client, minimal_payload):
        """Cypher's CREATE FULLTEXT INDEX is rejected by ArcadeDB."""
        _create_search_indexes(client, minimal_payload)
        assert not any("FULLTEXT INDEX" in s for s in client.sql())
        assert client.sql_matching("FULL_TEXT")

    def test_analyzer_short_name_is_expanded_to_lucene_class(self, client, minimal_payload):
        _create_search_indexes(client, minimal_payload, "brazilian")
        created = client.sql_matching("FULL_TEXT", "Concept")
        assert "org.apache.lucene.analysis.br.BrazilianAnalyzer" in created[0]

    def test_analyzer_class_is_passed_through(self, client, minimal_payload):
        cls = "org.apache.lucene.analysis.en.EnglishAnalyzer"
        _create_search_indexes(client, minimal_payload, cls)
        assert client.sql_matching("FULL_TEXT", cls)

    def test_unsafe_analyzer_is_refused_not_escaped(self, client, minimal_payload):
        """The analyzer lands inside a JSON literal; a quote would break out."""
        _create_search_indexes(client, minimal_payload, 'x", "evil": "1')
        assert not client.sql_matching("FULL_TEXT")

    def test_index_creation_is_idempotent(self, client, minimal_payload):
        """ArcadeDB rejects a duplicate index over the same (type, properties)."""
        _create_search_indexes(client, minimal_payload)
        for statement in client.sql_matching("FULL_TEXT"):
            assert "IF NOT EXISTS" in statement

    def test_one_index_covers_all_properties_of_a_type(self, client, payload_factory):
        """Properties are indexed together, not one index each.

        This decides the index *name*, which is how queries address it:
        `Chain[search_name,ontology_description]`, not `Chain[search_name]`.
        Splitting them would also lose cross-field relevance scoring.
        """
        payload = payload_factory(scalar_fields=["ontology_description"])
        _create_search_indexes(client, payload)
        concept_indexes = client.sql_matching("FULL_TEXT", "Concept")
        assert len(concept_indexes) == 1
        assert "search_name, ontology_description" in concept_indexes[0]

    def test_item_is_indexed_on_citation_and_description(self, client, minimal_payload):
        _create_search_indexes(client, minimal_payload)
        created = client.sql_matching("FULL_TEXT", "Item")
        assert created and "citation" in created[0] and "description" in created[0]

    def test_source_text_fields_are_indexed(self, client, payload_factory):
        from synesis_graph.core import SourceFieldSpec

        payload = payload_factory(
            source_fields=[
                SourceFieldSpec("method", "TEXT"),
                SourceFieldSpec("knowledge_area", "ENUMERATED"),
            ]
        )
        _create_search_indexes(client, payload)
        created = client.sql_matching("FULL_TEXT", "Source")
        assert created
        assert "method" in created[0]
        # A closed vocabulary stays a property but never enters the index.
        assert "knowledge_area" not in created[0]


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------
class TestClearDatabase:
    def test_drops_indexes_with_backticked_names(self):
        """`DROP INDEX Item[item_id]` is a syntax error without backticks."""
        client = FakeClient({"schema:indexes": [{"name": "Item[item_id]"}]})
        clear_database(client)
        assert client.sql_matching("DROP INDEX `Item[item_id]`")

    def test_uses_schema_introspection_not_show_indexes(self):
        client = FakeClient({"schema:indexes": [{"name": "Item[item_id]"}]})
        clear_database(client)
        assert client.sql_matching("SELECT name FROM schema:indexes")
        assert not any("SHOW INDEX" in s.upper() for s in client.sql())

    def test_never_emits_drop_constraint(self):
        """ArcadeDB has no DROP CONSTRAINT; the pilot failed here."""
        client = FakeClient({"schema:indexes": [{"name": "Item[item_id]"}]})
        clear_database(client)
        assert not any("DROP CONSTRAINT" in s.upper() for s in client.sql())

    def test_deletes_all_nodes(self, client):
        clear_database(client)
        assert "MATCH (n) DETACH DELETE n" in client.cypher()

    def test_survives_a_database_without_schema(self, client):
        """A freshly created database has nothing to introspect."""
        clear_database(client)
        assert "MATCH (n) DETACH DELETE n" in client.cypher()

    def test_a_failing_index_drop_does_not_abort_clearing(self):
        client = FakeClient({"schema:indexes": [{"name": "Gone[x]"}]})
        client.fail_on = "DROP INDEX"
        clear_database(client)
        assert "MATCH (n) DETACH DELETE n" in client.cypher()


# ---------------------------------------------------------------------------
# Full sync
# ---------------------------------------------------------------------------
class TestSyncToArcadeDB:
    def test_successful_sync_returns_none(self, client, minimal_payload):
        assert sync_to_arcadedb(client, minimal_payload) is None

    def test_writes_inside_one_transaction(self, client, minimal_payload):
        sync_to_arcadedb(client, minimal_payload)
        assert client.transactions == ["begin", "commit"]

    def test_schema_is_created_before_the_transaction(self, client, minimal_payload):
        """DDL inside the write transaction would be a different failure mode."""
        sync_to_arcadedb(client, minimal_payload)
        first_merge = next(i for i, (_, s, _) in enumerate(client.statements) if "MERGE" in s)
        schema_positions = [
            i for i, (_, s, _) in enumerate(client.statements) if "CREATE PROPERTY" in s
        ]
        assert schema_positions and max(schema_positions) < first_merge

    def test_reuses_the_neo4j_cypher_for_writing(self, client, minimal_payload):
        sync_to_arcadedb(client, minimal_payload)
        cypher = " ".join(client.cypher())
        assert "MERGE (s:Source {bibtex: row.bibtex})" in cypher
        assert "MERGE (i:Item {item_id: row.item_id})" in cypher
        assert "RELATES_TO" in cypher

    def test_failure_returns_a_sync_error(self, client, minimal_payload):
        client.fail_on = "MERGE (s:Source"
        result = sync_to_arcadedb(client, minimal_payload)
        assert isinstance(result, SyncError)
        assert result.stage == "sync"

    def test_failure_rolls_the_transaction_back(self, client, minimal_payload):
        client.fail_on = "MERGE (s:Source"
        sync_to_arcadedb(client, minimal_payload)
        assert client.transactions == ["begin", "rollback"]

    def test_empty_payload_is_not_an_error(self, client, empty_payload):
        assert sync_to_arcadedb(client, empty_payload) is None


# ---------------------------------------------------------------------------
# Integration — skipped unless a live server answers
# ---------------------------------------------------------------------------
def _live_client(database: str | None = None) -> ArcadeDBClient | None:
    password = os.environ.get("ARCADEDB_PASSWORD")
    if not password:
        return None
    client = ArcadeDBClient(
        uri=os.environ.get("ARCADEDB_HTTP_URI", "http://localhost:2480"),
        user=os.environ.get("ARCADEDB_USER", "root"),
        password=password,
        database=database,
        timeout=120.0,
    )
    return client if client.is_ready() else None


live = pytest.mark.skipif(
    _live_client() is None,
    reason="no live ArcadeDB (set ARCADEDB_PASSWORD and start the server)",
)


@pytest.fixture
def live_db():
    """Creates a throwaway database and drops it afterwards."""
    admin = _live_client()
    name = "synesis_sync_it"
    if admin.database_exists(name):
        admin.drop_database(name)
    admin.create_database(name)
    client = _live_client(name)
    try:
        yield client
    finally:
        client.close()
        admin.drop_database(name)


@live
def test_integration_sync_writes_the_expected_graph(live_db, minimal_payload):
    assert sync_to_arcadedb(live_db, minimal_payload, "brazilian") is None

    concepts = live_db.query("MATCH (c:Concept) RETURN count(c) AS n")[0]["n"]
    sources = live_db.query("MATCH (s:Source) RETURN count(s) AS n")[0]["n"]
    items = live_db.query("MATCH (i:Item) RETURN count(i) AS n")[0]["n"]
    relates = live_db.query("MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS n")[0]["n"]

    assert concepts == len(minimal_payload.concepts)
    assert sources == len(minimal_payload.sources)
    assert items == len(minimal_payload.items)
    assert relates == len(minimal_payload.chains)


@live
def test_integration_fulltext_index_is_queryable(live_db, payload_factory):
    """The contract the pilot exposed: an index needs its property declared.

    `search_name` is supplied explicitly here because the shared payload fixtures
    are built by hand, while in the real pipeline `_extract_concepts` derives it
    from `name` (`governança_corporativa` -> `governança corporativa`). The index
    reads `search_name`, so a concept without one is correctly unsearchable.
    """
    payload = payload_factory(
        concepts=[
            {
                "props": {
                    "name": "governança_corporativa",
                    "search_name": "governança corporativa",
                },
                "relations": {},
            }
        ]
    )
    sync_to_arcadedb(live_db, payload, "brazilian")

    # Accent folding is the reason to configure a corpus-language analyzer: the
    # query has no cedilla, the indexed term does.
    rows = live_db.query(
        "SELECT name FROM Concept WHERE SEARCH_INDEX('Concept[search_name]', :q) = true",
        {"q": "governanca"},
        language="sql",
    )
    assert [r["name"] for r in rows] == ["governança_corporativa"]


@live
def test_integration_undeclared_property_cannot_be_indexed(live_db):
    """Documents the trap in executable form: this is why _create_schema exists."""
    live_db.command("MERGE (c:Undeclared {name: 'x'})")

    with pytest.raises(ArcadeDBError) as excinfo:
        live_db.command("CREATE INDEX ON Undeclared (name) FULL_TEXT", language="sql")

    assert "does not exist" in str(excinfo.value)


@live
def test_integration_resync_is_idempotent(live_db, minimal_payload):
    """A re-export must not trip over its own schema or indexes."""
    assert sync_to_arcadedb(live_db, minimal_payload, "brazilian") is None
    assert sync_to_arcadedb(live_db, minimal_payload, "brazilian") is None

    concepts = live_db.query("MATCH (c:Concept) RETURN count(c) AS n")[0]["n"]
    assert concepts == len(minimal_payload.concepts)


# ---------------------------------------------------------------------------
# ProjectContext — the vertex that makes the exported graph self-describing
# ---------------------------------------------------------------------------
class TestProjectContext:
    """What is specific to ArcadeDB here: the type must be declared up front.

    The write itself is shared with the Neo4j backend (`_sync_project_context`)
    and covered in `test_project_context.py`; what ArcadeDB adds is that Cypher
    will not create the vertex type implicitly, so `_create_schema` has to.
    """

    def test_declares_the_vertex_type(self, client, minimal_payload):
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE VERTEX TYPE ProjectContext")

    def test_declares_the_type_even_without_a_context(self, client, empty_payload):
        # Declaring costs nothing, and an empty type reads better to whoever
        # inspects the schema than a type that sometimes exists.
        _create_schema(client, empty_payload)
        assert client.sql_matching("CREATE VERTEX TYPE ProjectContext")

    def test_declares_its_text_properties(self, client, minimal_payload):
        """Unlike every other type here, these are declared without being indexed.

        A type with no declared properties appears in `get_schema` as an empty
        vertex, so an MCP client introspecting the graph cannot tell there is a
        context to read. Observed against face85 before this was added.
        """
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE PROPERTY ProjectContext.template_doc")
        assert client.sql_matching("CREATE PROPERTY ProjectContext.description")

    def test_does_not_declare_the_integer_counts(self, client, minimal_payload):
        # `_declare_property` emits STRING; the counts are integers.
        _create_schema(client, minimal_payload)
        assert not client.sql_matching("CREATE PROPERTY ProjectContext.item_count")

    def test_declares_the_concept_network_metrics(self, client, minimal_payload):
        """PageRank e companhia são calculados no sync e ficavam invisíveis.

        Observado ao vivo: perguntado pelos conceitos mais centrais, um cliente
        MCP contou arestas à mão porque `get_schema` não anunciava `pagerank` —
        que já estava gravado. Os dois rankings divergem.
        """
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE PROPERTY Concept.pagerank")
        assert client.sql_matching("CREATE PROPERTY Concept.betweenness")
        assert client.sql_matching("CREATE PROPERTY Concept.community")

    def test_metrics_are_declared_with_numeric_types(self, client, minimal_payload):
        """STRING faria o servidor recusar o valor no sync.

        É a mesma armadilha que mantém as contagens do `ProjectContext` fora da
        declaração; aqui ela é resolvida declarando o tipo real.
        """
        _create_schema(client, minimal_payload)
        sql = client.sql()
        assert any("Concept.pagerank IF NOT EXISTS DOUBLE" in s for s in sql)
        assert any("Concept.degree IF NOT EXISTS INTEGER" in s for s in sql)

    def test_declares_the_semantic_capability_properties(self, client, minimal_payload):
        """Which fields the graph can be searched by meaning, and with which model.

        Undeclared, they would be written but invisible to `get_schema` — and the
        capability exists precisely so a client can discover it without guessing.
        """
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE PROPERTY ProjectContext.embedding_fields")
        assert client.sql_matching("CREATE PROPERTY ProjectContext.embedding_model")

    def test_does_not_declare_the_embedding_dimensions(self, client, minimal_payload):
        # Integer, like the counts.
        _create_schema(client, minimal_payload)
        assert not client.sql_matching("CREATE PROPERTY ProjectContext.embedding_dimensions")

    def test_context_is_written_inside_the_transaction(self, client, payload_factory):
        payload = payload_factory()
        payload.project_context = build_project_context(
            {"project": {"name": "p", "description": "D"}},
            concept_label="Concept",
            graph_fields=[],
            synesis_graph_version="0.1",
        )
        sync_to_arcadedb(client, payload)
        written = [i for i, s in enumerate(client.cypher()) if "ProjectContext" in s]
        assert written, "the context was never written"
        assert client.transactions == ["begin", "commit"]

    def test_payload_without_context_writes_none(self, client, minimal_payload):
        sync_to_arcadedb(client, minimal_payload)
        assert not [s for s in client.cypher() if "ProjectContext" in s]


class TestFulltextDeclaration:
    """The declared capability must not drift from the indexes actually built."""

    def test_declaration_matches_the_indexes_created(self, client, payload_factory):
        """Same field lists feed `CREATE INDEX` and the declaration.

        Recomputing the rule in the declaration would be a second implementation,
        free to disagree with the first — and the whole point of declaring is that
        the consumer can trust it without introspecting.
        """
        payload = payload_factory(scalar_fields=["ontology_description"])
        payload.project_context = ProjectContextSpec(
            project_name="p",
            description="",
            concept_label=payload.concept_label,
            template_name="t",
            template_doc="",
            project_summary="",
            compiler_version="",
            synesis_graph_version="",
            compiled_at="",
            generated_at="",
            source_count=0,
            item_count=0,
            concept_count=0,
        )

        sync_to_arcadedb(client, payload, "brazilian")

        context = payload.project_context
        assert context.fulltext_concept_fields == "search_name,ontology_description"
        # The very identifier `SEARCH_INDEX` expects.
        assert client.sql_matching("CREATE INDEX", "search_name")
        assert "brazilian" in context.fulltext_analyzer.lower()

    def test_declares_the_context_properties(self, client, minimal_payload):
        """Undeclared properties are invisible to `get_schema`, which is how the
        chat discovers what the graph offers."""
        _create_schema(client, minimal_payload)
        assert client.sql_matching("CREATE PROPERTY ProjectContext.fulltext_concept_fields")
        assert client.sql_matching("CREATE PROPERTY ProjectContext.fulltext_analyzer")
