"""ArcadeDB backend: schema declaration, indexes, and payload synchronization.

The graph writing itself is *not* reimplemented here. ArcadeDB speaks OpenCypher,
and the pilot against face85 proved the `_sync_*` statements from the Neo4j backend
run unchanged, producing an identical graph (210 concepts, 168 RELATES_TO, 348
MENTIONS, ...). Duplicating that Cypher would mean two copies of the same MERGE
semantics drifting apart, so this module reuses those functions and supplies only
what genuinely differs:

1. **Property declaration** — Cypher writes properties without declaring them in the
   schema, and ArcadeDB refuses to index an undeclared property. Every indexed
   property therefore needs `CREATE PROPERTY` first. Neo4j has no equivalent step.
2. **Index syntax** — `CREATE INDEX ... UNIQUE` / `... FULL_TEXT METADATA {...}` in
   ArcadeDB's SQL, not Cypher's `CREATE CONSTRAINT` / `CREATE FULLTEXT INDEX`.
3. **Clearing** — schema introspection is `SELECT FROM schema:indexes`, and index
   names come back as `Item[item_id]`, which needs backticks to be droppable.
"""

from __future__ import annotations

import logging
from typing import Any

from synesis_graph.arcadedb_client import ArcadeDBClient, ArcadeDBError
from synesis_graph.backends.neo4j import (
    _sync_concepts,
    _sync_entities,
    _sync_from_source,
    _sync_items,
    _sync_mentions,
    _sync_project_context,
    _sync_refers_to,
    _sync_sources,
    _sync_taxonomies,
)
from synesis_graph.core import (
    DEFAULT_ARCADEDB_ANALYZER,
    GraphPayload,
    SyncError,
    get_taxonomy_labels,
    resolve_arcadedb_analyzer,
    text_source_field_names,
)
from synesis_graph.embeddings import EmbeddingsSidecar
from synesis_graph.sanitize import validate_cypher_label

logger = logging.getLogger("synesis2graph")

# Structural property names the payload normalises from the template, mirroring
# `_create_search_indexes` in the Neo4j backend.
ITEM_TEXT_PROPS = ("citation", "description")
SOURCE_TEXT_PROPS = ("title", "abstract")

# Declared so schema introspection announces the context instead of showing an
# empty vertex type. Only the STRING-typed ones: the counts are integers and
# ArcadeDB would reject them under a STRING declaration.
PROJECT_CONTEXT_TEXT_PROPS = (
    "project_name",
    "description",
    "template_doc",
    "project_summary",
    "concept_label",
)


class _CypherRunner:
    """Adapts ArcadeDBClient to the `tx.run(query, **params)` interface.

    This is what lets the `_sync_*` functions be reused verbatim: they were written
    against a Neo4j transaction object and only ever call `.run()`. Rather than
    forking them for a second client, the client is dressed in the shape they
    expect.
    """

    def __init__(self, client: ArcadeDBClient):
        self._client = client

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        return self._client.command(query, params or None)


def sync_to_arcadedb(
    client: ArcadeDBClient,
    payload: GraphPayload,
    fulltext_analyzer: str = DEFAULT_ARCADEDB_ANALYZER,
    embeddings: EmbeddingsSidecar | None = None,
) -> SyncError | None:
    """Synchronizes the payload with ArcadeDB.

    Clears the database first, so the compiler stays the source of truth — the same
    contract as `sync_to_neo4j`.

    `embeddings`, when given, adds the vector property, its index and the vectors
    themselves. Absent, nothing about the existing behaviour changes.

    Returns None on success, SyncError on failure.
    """
    try:
        clear_database(client)
        _create_schema(client, payload)
        _create_constraints(client, payload)
        _create_search_indexes(client, payload, fulltext_analyzer)
        _execute_sync_transaction(client, payload)
        if embeddings is not None:
            error = _sync_embeddings(client, payload, embeddings)
            if error is not None:
                return error
        return None
    except ArcadeDBError as e:
        return SyncError(message="Synchronization failed", stage="sync", details=str(e))
    except Exception as e:  # noqa: BLE001 - surfaced to the caller as a typed error
        return SyncError(message="Synchronization failed", stage="sync", details=str(e))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def _concept_index_props(payload: GraphPayload) -> list[str]:
    """Concept properties that belong in the full-text index.

    Same rule as the Neo4j backend: the humanised name plus every SCOPE ONTOLOGY
    text field. `graph_fields` stay out — they become taxonomy nodes of their own,
    and indexing a closed vocabulary as prose only dilutes the index.
    """
    return [p for p in ["search_name", *payload.scalar_fields] if validate_cypher_label(p)]


def _source_index_props(payload: GraphPayload) -> list[str]:
    """Source properties that belong in the full-text index."""
    text_fields = text_source_field_names(payload.source_fields)
    return [p for p in [*SOURCE_TEXT_PROPS, *text_fields] if validate_cypher_label(p)]


def _declare_property(client: ArcadeDBClient, type_name: str, prop: str) -> None:
    """Declares one STRING property, if the name is safe to interpolate."""
    if not validate_cypher_label(type_name) or not validate_cypher_label(prop):
        return
    client.command(f"CREATE PROPERTY {type_name}.{prop} IF NOT EXISTS STRING", language="sql")


def _create_schema(client: ArcadeDBClient, payload: GraphPayload) -> None:
    """Declares the types and the properties that will be indexed.

    **This step has no Neo4j counterpart and is not optional.** Cypher creates
    properties as it writes data, but leaves the schema empty (`properties: []`), and
    ArcadeDB refuses to build an index over a property it does not know:

        Cannot create the index on type 'Chain.search_name'
        because the property does not exist

    Only indexed properties are declared. Everything else stays schema-less, which
    preserves the template-driven flexibility the payload depends on — a project is
    free to carry any field without this module knowing its name.

    Types are declared too. Cypher would create them implicitly with the right
    nature (vertex/edge), but declaring them up front means a property can be
    declared before any data exists, so the order here does not depend on the sync
    having run.
    """
    concept_label = payload.concept_label

    # Vertex types: the structural ones plus the dynamic concept and taxonomy labels.
    # `ProjectContext` is declared unconditionally even though the payload may
    # carry none: declaring a type costs nothing, and an empty type is a clearer
    # signal to whoever inspects the schema than a type that sometimes exists.
    # None of its properties are declared — only indexed ones need to be, and the
    # context is read whole, never searched.
    vertex_types = ["Source", "Item", "ProjectContext"]
    if validate_cypher_label(concept_label):
        vertex_types.append(concept_label)
    vertex_types.extend(get_taxonomy_labels(payload.graph_fields))
    vertex_types.extend(payload.entities.keys())

    for type_name in vertex_types:
        if validate_cypher_label(type_name):
            client.command(f"CREATE VERTEX TYPE {type_name} IF NOT EXISTS", language="sql")

    # ProjectContext's properties are declared even though none is indexed.
    # Everything else in this function declares only what an index needs, but a
    # type with no declared properties shows up in `get_schema` as an empty
    # vertex — which is how an MCP client discovers the graph. Observed against
    # face85: introspection listed `ProjectContext (no properties)`, so a model
    # had no way to learn the context was there at all. Declaring them makes the
    # vertex self-announcing, which is the whole point of writing it.
    for prop in PROJECT_CONTEXT_TEXT_PROPS:
        _declare_property(client, "ProjectContext", prop)

    # Properties backing the uniqueness indexes.
    _declare_property(client, "Source", "bibtex")
    _declare_property(client, "Item", "item_id")
    if validate_cypher_label(concept_label):
        _declare_property(client, concept_label, "name")
    for label in get_taxonomy_labels(payload.graph_fields):
        _declare_property(client, label, "name")
    for label in payload.entities:
        _declare_property(client, label, "entity_id")

    # Properties backing the full-text indexes.
    if validate_cypher_label(concept_label):
        for prop in _concept_index_props(payload):
            _declare_property(client, concept_label, prop)
    for prop in ITEM_TEXT_PROPS:
        _declare_property(client, "Item", prop)
    for prop in _source_index_props(payload):
        _declare_property(client, "Source", prop)


def _create_constraints(client: ArcadeDBClient, payload: GraphPayload) -> None:
    """Creates the uniqueness indexes.

    ArcadeDB expresses uniqueness as a UNIQUE index rather than Cypher's
    `CREATE CONSTRAINT`, but the guarantee is the same: a duplicate key is rejected
    with `DuplicatedKeyException`, which is what makes the payload's MERGE
    semantics idempotent.
    """
    concept_label = payload.concept_label

    _create_unique_index(client, "Source", "bibtex")
    _create_unique_index(client, "Item", "item_id")
    if validate_cypher_label(concept_label):
        _create_unique_index(client, concept_label, "name")
    for label in get_taxonomy_labels(payload.graph_fields):
        _create_unique_index(client, label, "name")
    for label in payload.entities:
        _create_unique_index(client, label, "entity_id")


def _create_unique_index(client: ArcadeDBClient, type_name: str, prop: str) -> None:
    if not validate_cypher_label(type_name) or not validate_cypher_label(prop):
        return
    client.command(f"CREATE INDEX IF NOT EXISTS ON {type_name} ({prop}) UNIQUE", language="sql")


def _create_search_indexes(
    client: ArcadeDBClient,
    payload: GraphPayload,
    fulltext_analyzer: str = DEFAULT_ARCADEDB_ANALYZER,
) -> None:
    """Creates full-text indexes over the template's text-bearing fields.

    Which fields those are is decided exactly as in the Neo4j backend — the rule is
    template-driven and backend-independent. What differs is the statement: ArcadeDB
    takes SQL with a METADATA block naming a Lucene analyzer class.

    Indexes are created after the data would be written only in the sense of
    ordering within a re-export; ArcadeDB indexes existing records retroactively
    (`totalIndexed` reports how many), so either order yields a populated index.
    """
    analyzer = resolve_arcadedb_analyzer(fulltext_analyzer)
    concept_label = payload.concept_label

    if validate_cypher_label(concept_label):
        concept_props = _concept_index_props(payload)
        if concept_props:
            _create_fulltext_index(client, concept_label, concept_props, analyzer)

    _create_fulltext_index(client, "Item", list(ITEM_TEXT_PROPS), analyzer)

    source_props = _source_index_props(payload)
    if source_props:
        _create_fulltext_index(client, "Source", source_props, analyzer)


def _create_fulltext_index(
    client: ArcadeDBClient, type_name: str, props: list[str], analyzer: str
) -> None:
    """Creates one full-text index.

    `IF NOT EXISTS` matters on re-export: ArcadeDB rejects a second index over the
    same (type, properties) pair with `Index '...' already exists`, and a re-export
    of the same project must not be an error.
    """
    if not validate_cypher_label(type_name) or not props:
        return
    prop_list = ", ".join(props)
    # The analyzer is a Java class name, not a data value; ArcadeDB's METADATA block
    # takes a JSON literal, so it is interpolated. Guarded below.
    if not _is_safe_analyzer(analyzer):
        logger.warning("Ignoring unsafe analyzer name: %r", analyzer)
        return
    client.command(
        f"CREATE INDEX IF NOT EXISTS ON {type_name} ({prop_list}) FULL_TEXT "
        f'METADATA {{"analyzer": "{analyzer}"}}',
        language="sql",
    )


def _is_safe_analyzer(analyzer: str) -> bool:
    """True when the analyzer name is a plain dotted identifier.

    The value reaches the statement as a JSON literal, so a quote or brace in it
    would break out of the METADATA block. Analyzer names are Java class names;
    anything else is refused rather than escaped.
    """
    return bool(analyzer) and all(c.isalnum() or c in "._-" for c in analyzer)


# ---------------------------------------------------------------------------
# Vector embeddings
# ---------------------------------------------------------------------------
# The declared type for a vector property. Verified against ArcadeDB 26.7.3:
# `LIST OF FLOAT`, `FLOAT[]` and `LIST` are all rejected by the SQL parser —
# only `ARRAY_OF_FLOATS` is accepted. Getting this wrong fails at schema
# creation, before any data is written.
VECTOR_PROPERTY_TYPE = "ARRAY_OF_FLOATS"

VECTOR_PROPERTY_NAME = "embedding"


def create_vector_schema(
    client: ArcadeDBClient,
    concept_label: str,
    dimensions: int,
    *,
    similarity: str = "COSINE",
    quantization: str = "INT8",
) -> None:
    """Declares the embedding property and its LSM_VECTOR index.

    Declaration is mandatory for the same reason it is for the full-text indexes:
    Cypher writes the property without declaring it, and ArcadeDB refuses to index
    a property the schema does not know.

    `dimensions` must come from the model that produced the vectors. A mismatch
    between the index and the data is not rejected at insert time — it surfaces
    later as wrong neighbours, which is exactly the kind of silent failure this
    backend has already been bitten by.
    """
    if not validate_cypher_label(concept_label) or dimensions <= 0:
        return

    client.command(
        f"CREATE PROPERTY {concept_label}.{VECTOR_PROPERTY_NAME} "
        f"IF NOT EXISTS {VECTOR_PROPERTY_TYPE}",
        language="sql",
    )

    if not _is_safe_metadata_word(similarity) or not _is_safe_metadata_word(quantization):
        logger.warning(
            "Ignoring unsafe vector index metadata: similarity=%r quantization=%r",
            similarity,
            quantization,
        )
        return

    client.command(
        f"CREATE INDEX IF NOT EXISTS ON {concept_label} ({VECTOR_PROPERTY_NAME}) "
        f"LSM_VECTOR METADATA {{dimensions: {int(dimensions)}, "
        f"similarity: '{similarity}', quantization: '{quantization}'}}",
        language="sql",
    )


def _is_safe_metadata_word(value: str) -> bool:
    """True when a METADATA value is a bare identifier safe to interpolate."""
    return bool(value) and value.isalnum()


def sync_vectors(
    client: ArcadeDBClient,
    concept_label: str,
    vectors: dict[str, list[float]],
    *,
    batch_size: int = 500,
) -> int:
    """Writes vectors onto existing concept nodes, returning how many landed.

    Matches on `name`, the concept's unique key, rather than on anything derived
    from a procedure's output. That is deliberate: the metrics module documents
    how `WHERE id(c) = id(node)` silently wrote scores onto the wrong node type,
    and the safe pattern established there is to bind by the business key.

    The count is read back from the database instead of assuming
    `len(vectors)` — a name present in the sidecar but absent from the graph
    would otherwise be reported as written.
    """
    if not validate_cypher_label(concept_label) or not vectors:
        return 0

    rows = [{"name": name, "v": vector} for name, vector in sorted(vectors.items())]
    for start in range(0, len(rows), batch_size):
        client.command(
            f"UNWIND $rows AS row MATCH (c:{concept_label} {{name: row.name}}) "
            f"SET c.{VECTOR_PROPERTY_NAME} = row.v",
            {"rows": rows[start : start + batch_size]},
            language="cypher",
        )

    result = client.query(
        f"SELECT count(*) AS n FROM {concept_label} WHERE {VECTOR_PROPERTY_NAME} IS NOT NULL",
        language="sql",
    )
    return int(result[0].get("n", 0)) if result else 0


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------
def clear_database(client: ArcadeDBClient) -> None:
    """Removes all data and indexes, leaving the compiler as the source of truth.

    Two ArcadeDB specifics, both found while piloting face85:

    - Introspection is `SELECT FROM schema:indexes`. Neo4j's `SHOW INDEXES` /
      `SHOW CONSTRAINTS` do not exist.
    - Index names come back as `Item[item_id]`. Feeding that to `DROP INDEX`
      unquoted is a syntax error (`mismatched input '['`), so names are wrapped in
      backticks.

    Dropping indexes is what makes a changed analyzer take effect: `CREATE INDEX ...
    IF NOT EXISTS` would otherwise leave the old index in place and report success.
    """
    try:
        indexes = client.query("SELECT name FROM schema:indexes", language="sql")
    except ArcadeDBError:
        # A brand-new database has no schema to introspect yet.
        indexes = []

    for row in indexes:
        name = row.get("name")
        if not name:
            continue
        try:
            client.command(f"DROP INDEX `{name}` IF EXISTS", language="sql")
        except ArcadeDBError as e:
            # Automatic sub-indexes (one per bucket) disappear with their parent,
            # so a failure here usually means "already gone".
            logger.debug("Could not drop index %s: %s", name, e)

    client.command("MATCH (n) DETACH DELETE n")


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _execute_sync_transaction(client: ArcadeDBClient, payload: GraphPayload) -> None:
    """Writes the whole payload inside one server-side transaction.

    The ordering matches `sync_to_neo4j` and is load-bearing: items and sources must
    exist before the edges that match on them, and reified identities before the
    REFERS_TO edges pointing at them.
    """
    tx = _CypherRunner(client)
    client.begin()
    try:
        # Same position as in `sync_to_neo4j`: first, and inside the transaction,
        # so the context never outlives a sync that failed halfway.
        _sync_project_context(tx, payload.project_context)
        _sync_sources(tx, payload.sources)
        _sync_items(tx, payload.items)
        _sync_from_source(tx, payload.from_source)
        _sync_concepts(tx, payload.chains, payload.concepts, payload.concept_label)
        _sync_taxonomies(tx, payload.concepts, payload.graph_fields, payload.concept_label)
        _sync_mentions(tx, payload.mentions, payload.concept_label)
        _sync_entities(tx, payload.entities)
        _sync_refers_to(tx, payload.refers_to_edges)
        client.commit()
    except Exception:
        # Leaving the transaction open would hold locks until the server expires it,
        # and the partial graph would outlive the failure.
        client.rollback()
        raise


def _sync_embeddings(
    client: ArcadeDBClient,
    payload: GraphPayload,
    embeddings: EmbeddingsSidecar,
) -> SyncError | None:
    """Adds the vector index and writes the vectors, after the nodes exist.

    Runs outside the sync transaction because the index is DDL: creating it
    inside the same transaction that writes the nodes gives ArcadeDB a schema
    change and a data change to reconcile at commit, and the index is cheap to
    rebuild if this step fails.
    """
    if not embeddings.vectors:
        return None

    dimensions = embeddings.dimensions
    if not dimensions:
        return SyncError(
            message="Embeddings carry no dimension count",
            stage="sync",
            details=(
                "The sidecar must record `dimensions`; the vector index "
                "cannot be created without it."
            ),
        )

    widths = {len(v) for v in embeddings.vectors.values()}
    if widths != {dimensions}:
        return SyncError(
            message="Embedding width does not match the recorded dimensions",
            stage="sync",
            details=(
                f"Sidecar declares {dimensions} dimensions but holds vectors of "
                f"width {sorted(widths)}. Regenerate with --rebuild-embeddings."
            ),
        )

    create_vector_schema(client, payload.concept_label, dimensions)
    written = sync_vectors(client, payload.concept_label, embeddings.vectors)

    if written < len(embeddings.vectors):
        # Names in the sidecar that no longer exist as concepts. Worth surfacing:
        # it means the sidecar is stale relative to the compiled ontology.
        logger.warning(
            "%d of %d embeddings matched a concept; the sidecar may be stale",
            written,
            len(embeddings.vectors),
        )
    return None


__all__ = [
    "VECTOR_PROPERTY_NAME",
    "VECTOR_PROPERTY_TYPE",
    "clear_database",
    "create_vector_schema",
    "sync_to_arcadedb",
    "sync_vectors",
]
