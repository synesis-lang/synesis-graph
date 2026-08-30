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
import time
from typing import Any, Protocol

from synesis_graph.arcadedb_client import ArcadeDBError
from synesis_graph.arcadedb_transport import ArcadeDBTransport
from synesis_graph.backends.neo4j import (
    SyncMode,
    _sync_concepts,
    _sync_entities,
    _sync_from_source,
    _sync_items,
    _sync_mentions,
    _sync_project_context,
    _sync_refers_to,
    _sync_sources,
    _sync_taxonomies,
    ontology_concept_rows,
)
from synesis_graph.core import (
    DEFAULT_ARCADEDB_ANALYZER,
    GraphPayload,
    SyncError,
    declare_fulltext_capability,
    dedupe_index_props,
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
# Tipos aceitos em `CREATE PROPERTY`, verificados contra o ArcadeDB 26.7.3.
_DECLARABLE_TYPES = frozenset({"STRING", "DOUBLE", "FLOAT", "INTEGER", "LONG", "DECIMAL"})

# Métricas de rede calculadas por `compute_backend_metrics` e gravadas em cada
# conceito. Declaradas para que apareçam na introspecção: um cliente MCP descobre
# o grafo por `get_schema`, e uma propriedade não declarada é invisível ali —
# mesmo estando gravada.
#
# Observado ao vivo (2026-08-24): perguntado pelos conceitos "mais centrais", o
# modelo contou arestas à mão e devolveu um ranking de grau, porque não tinha
# como saber que `pagerank` já estava no banco. Os dois rankings divergem.
CONCEPT_METRIC_PROPS = (
    ("pagerank", "DOUBLE"),
    ("betweenness", "DOUBLE"),
    ("degree", "INTEGER"),
    ("in_degree", "INTEGER"),
    ("out_degree", "INTEGER"),
    ("community", "INTEGER"),
    ("mention_count", "INTEGER"),
    ("source_count", "INTEGER"),
)

PROJECT_CONTEXT_TEXT_PROPS = (
    "project_name",
    "description",
    "template_doc",
    "project_summary",
    "concept_label",
    # Which fields the graph can be searched by meaning, and with which model.
    # `embedding_dimensions` is an integer and stays out, like the counts: this
    # loop declares STRING. The typed helper makes declaring them possible, but
    # doing it belongs with the rest of the semantic-capability contract.
    "embedding_fields",
    "embedding_model",
    # Which fields it can be searched by WORD, and under which analyzer. The
    # consumer needs the exact field list because `SEARCH_INDEX` addresses a
    # composite index by it — `Concept[search_name, ontology_description]`.
    "fulltext_concept_fields",
    "fulltext_item_fields",
    "fulltext_source_fields",
    "fulltext_analyzer",
    # Which backend computed the network metrics, and over which projection —
    # ArcadeDB's `algo.*` accepts no scope filter and runs over the whole graph.
    # Without this, a consumer ranking by PageRank cannot tell that the score
    # includes edges to Items, Sources and taxonomy nodes.
    "metrics_backend",
    "metrics_scope",
)


class _CypherRunner:
    """Adapts an ArcadeDBTransport to the `tx.run(query, **params)` interface.

    This is what lets the `_sync_*` functions be reused verbatim: they were written
    against a Neo4j transaction object and only ever call `.run()`. Rather than
    forking them for a second client, the client is dressed in the shape they
    expect.
    """

    def __init__(self, client: ArcadeDBTransport):
        self._client = client

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        return self._client.command(query, params or None)


#: Context properties that describe how the graph's INDEXES were built, paired
#: with the human name of the setting that produces each. An `update` cannot
#: change any of them: it does not drop indexes, so a new analyzer or a new
#: embedding model would be declared on the context while the index kept
#: answering under the old one — the graph would advertise a capability it does
#: not have, which is worse than refusing.
_REBUILD_ONLY_SETTINGS: tuple[tuple[str, str], ...] = (
    ("fulltext_analyzer", "fulltext_analyzer"),
    ("embedding_model", "the embeddings model"),
    ("embedding_fields", "the embedded fields"),
)


def read_project_context(client: ArcadeDBTransport) -> dict[str, Any] | None:
    """Returns the stored `ProjectContext` properties, or None if there is none.

    None covers both "this database is empty" and "this graph predates the
    context vertex". Neither is an error: the first is an initial load, and the
    second simply has nothing to compare against.
    """
    try:
        rows = client.command("MATCH (p:ProjectContext) RETURN p LIMIT 1")
    except Exception:  # noqa: BLE001 - see below
        # A missing `ProjectContext` type is not a failure to report — it is the
        # empty case, and it is what an empty or older database looks like.
        # Deliberately broader than `ArcadeDBError`: how a nonexistent type
        # surfaces differs between the HTTP and embedded transports, and the
        # only consequence of catching too much here is falling back to "no
        # stored context", which is the safe answer. The caller re-runs the real
        # query moments later, so a genuine connection problem still surfaces.
        return None
    if not rows:
        return None
    row = rows[0]
    stored = row.get("p", row)
    return stored if isinstance(stored, dict) else None


def incompatible_with_update(
    stored: dict[str, Any] | None, payload: GraphPayload
) -> list[str]:
    """Names the settings that changed and that `update` cannot apply.

    Compares what the destination says it was built with against what this run
    declares. Empty list means the update is safe to proceed with.

    Reads the answer off the graph rather than off any local state, because the
    graph is the only thing that knows what it actually contains — a config file
    can be edited between runs, and a second researcher may have loaded it from
    another machine entirely.

    A stored value that is empty is treated as "not declared", not as a change:
    graphs built before a capability existed carry blanks, and refusing those
    would block updates on every older database for no reason.
    """
    if not stored or payload.project_context is None:
        return []
    wanted = payload.project_context
    changed = []
    for prop, human in _REBUILD_ONLY_SETTINGS:
        was = str(stored.get(prop) or "")
        now = str(getattr(wanted, prop, "") or "")
        if was and now and was != now:
            changed.append(f"{human}: graph has '{was}', this run asks for '{now}'")
    return changed


class ProgressReporter(Protocol):
    """The one thing this layer needs in order to speak to the researcher.

    Narrower than `TaskReporter` on purpose, and structural rather than
    imported: the sync layer reports outcomes by returning typed errors, and
    taking a whole UI object as a dependency to print two sentences would invert
    that. Anything with `info` and `warning` satisfies it, including `None`
    handling at the call site for the many callers that have no UI at all.
    """

    def info(self, msg: str) -> None: ...

    def warning(self, msg: str) -> None: ...


#: How many times a whole sync is attempted when a transient failure interrupts
#: it. Two retries: the observed outages recovered within a minute, and a longer
#: ladder mostly delays an honest error on a corpus that takes minutes per try.
SYNC_RETRY_ATTEMPTS = 3

#: Pause before starting a sync over. Long enough for a proxy or a server under
#: memory pressure to settle — the real one stopped accepting connections and
#: recovered on its own inside a minute — and short next to the sync itself.
_RETRY_BACKOFF_SECONDS = 5.0


#: Above this many items the sync takes long enough that silence needs
#: explaining. Set from measurement, not taste: 40,000 items synced against a
#: real server in ~64s, while 246,588 took 282s. Below the threshold the wait is
#: short enough that a warning would be noise.
_SLOW_SYNC_ITEM_COUNT = 50_000

#: Rows per second observed end-to-end against the Hostinger container, used
#: only to turn a row count into a rough minute figure. Deliberately pessimistic
#: — an estimate that runs short reads as a stall.
_OBSERVED_ITEMS_PER_SECOND = 700


def announce_scale(
    payload: GraphPayload,
    embeddings: EmbeddingsSidecar | None,
    say: Any,
) -> None:
    """Warns, before the silence starts, that this will take a while.

    The whole sync runs in one transaction, so nothing is visible in the
    database until it commits — a researcher who opens the graph to check
    progress sees an empty or unchanged one for minutes. Combined with a
    terminal that prints nothing, that is indistinguishable from a hang, and the
    natural response is to interrupt it, which throws away all the work.

    Saying the expected duration up front is what makes waiting a decision
    rather than a guess. The estimate is rough on purpose and phrased as such:
    a precise-looking number that proves wrong costs more trust than a vague one.
    """
    n = len(payload.items)
    if n < _SLOW_SYNC_ITEM_COUNT:
        return
    minutes = max(1, round(n / _OBSERVED_ITEMS_PER_SECOND / 60))
    say(
        f"Writing {n:,} coded excerpts to the database. "
        f"This usually takes around {minutes} minute(s) on a remote server."
    )
    if embeddings is not None and embeddings.vectors:
        say(f"Then {len(embeddings.vectors):,} concept vectors, for meaning-based search.")
    say(
        "Nothing will be printed until it finishes, and the graph stays empty "
        "until the very end — that is normal, not a freeze. Please leave it running."
    )


def _retry_is_safe(mode: SyncMode) -> bool:
    """Whether repeating an interrupted sync can be done without duplicating.

    Only in `rebuild`, and the reason is precise: a rebuild begins by clearing
    the graph, so a second attempt starts from the same empty state as the
    first, no matter how far the interrupted one got. That makes the whole sync
    idempotent even though the statements inside it are not — `rebuild` writes
    with `CREATE` (Etapa A) and bulk-loads vertices that deduplicate against
    nothing (Etapa C).

    `update` refuses on purpose. It does not clear, so a retry after a failure
    of unknown outcome would re-apply writes that may already have landed. The
    `MERGE` statements would absorb that, but the run is not made only of
    `MERGE`s, and "probably fine" is not a basis for writing to a researcher's
    corpus. An interrupted update is reported, and the researcher decides.
    """
    return mode == "rebuild"


#: Rows per `UNWIND` on this backend, overriding the shared 50,000 default.
#:
#: The default was written for Neo4j, whose driver streams over a long-lived
#: connection. Here every statement is one HTTP request through whatever proxy
#: sits in front of the server, and 50,000 items serialise to **32.6 MB** — a
#: request large enough that a stock reverse proxy drops it under load. That is
#: the `Bad Gateway (HTTP 502)` a real 246,588-item export hit three times in a
#: row, on a server whose own readiness probe answered in 74ms throughout.
#:
#: 5,000 rows is 3.3 MB, and was measured at the best throughput of the sizes
#: tried (6,625 rows/s, against 4,917 at 50,000): smaller requests also let the
#: server work while the next one is still arriving.
#:
#: This is the fix the first study specified as its Etapa 2 and that the second
#: study replaced before it was ever implemented — the gap only became visible
#: once the retry made the failure legible instead of fatal.
SYNC_BATCH_SIZE = 5_000

#: Vertex collections a bulk load may write, in the order the sync writes them.
#: Each is a payload attribute whose rows carry a key the compiler guarantees to
#: be unique — measured on the real corpus at 0 duplicates across 272,154 rows
#: (`items` 246,588, `concepts` 22,585, `sources` 2,981).
#:
#: Chains and taxonomies are absent by design, and not because of their size:
#: their rows repeat keys on purpose (302,392 chain endpoints resolve to 22,553
#: distinct concepts), so the `MERGE` that collapses them is doing real work.
#: A bulk load there would raise `DuplicatedKeyException` — loudly, which is the
#: good outcome, but it would still be wrong.
BULK_LOADABLE = ("sources", "items", "concepts")


def _bulk_loadable_rows(
    payload: GraphPayload, collection: str
) -> tuple[str, list[dict[str, Any]]]:
    """The vertex type and rows a bulk load would write for one collection."""
    if collection == "sources":
        return "Source", list(payload.sources)
    if collection == "items":
        return "Item", list(payload.items)
    return payload.concept_label, ontology_concept_rows(payload.concepts)


def supports_bulk_load(client: ArcadeDBTransport) -> bool:
    """Whether this transport can bulk-load vertices.

    Deliberately a duck-typed probe rather than a member of `ArcadeDBTransport`.
    Bulk loading is not something every transport can do — the embedded engine
    exposes ArcadeDB's GraphBatch in-process, while the HTTP client would need
    its own `/api/v1/batch` implementation — and putting it in the Protocol would
    oblige every transport to answer for a capability most do not have. A
    transport that offers it advertises it; one that does not is not broken, and
    the caller falls back to Cypher writes that build the same graph.
    """
    probe = getattr(client, "supports_bulk_vertices", None)
    return bool(probe and probe())


def _bulk_load_vertices(
    client: ArcadeDBTransport, payload: GraphPayload, mode: SyncMode
) -> frozenset[str]:
    """Writes the unique-keyed vertices through GraphBatch, if it is available.

    Returns the names of the collections it wrote, so the sync transaction can
    skip them. An empty set means nothing was bulk-loaded and the ordinary path
    must write everything — which is exactly what happens on an engine without
    GraphBatch, and is why this returns a set rather than a boolean.

    **Runs before the transaction opens, and that is not a style choice.**
    GraphBatch consumes an open transaction: measured against the real engine,
    the vertices land but the following `commit()` fails with
    `TransactionException: Transaction not begun`. Bypassing the transaction
    layer is where the speed comes from, so the two cannot be nested.

    **`rebuild` only.** GraphBatch never deduplicates, so against a destination
    that may already hold these keys it would either duplicate silently or, over
    a UNIQUE index, abort with `DuplicatedKeyException`. In `update` the whole
    point is that the destination already holds them.

    **What it gives up.** These vertices are no longer covered by the sync
    transaction: a failure partway leaves them written. That is acceptable here
    and nowhere else — a rebuild already wiped the graph, the compiler is the
    source of truth, and re-running is always safe. In `update` it would leave a
    half-applied state nobody could reconstruct, which is the reason update stays
    on the transactional path even though it would be faster here too.
    """
    if mode != "rebuild" or not supports_bulk_load(client):
        return frozenset()

    loaded: set[str] = set()
    for collection in BULK_LOADABLE:
        type_name, rows = _bulk_loadable_rows(payload, collection)
        if not rows or not validate_cypher_label(type_name):
            # Nothing to write, or a label the sanitizer rejects. Either way the
            # ordinary path handles it — leaving it out of `loaded` is what makes
            # this a fallback rather than a silent gap.
            continue
        client.bulk_create_vertices(type_name, rows)  # type: ignore[attr-defined]
        loaded.add(collection)
    return frozenset(loaded)


def sync_to_arcadedb(
    client: ArcadeDBTransport,
    payload: GraphPayload,
    fulltext_analyzer: str = DEFAULT_ARCADEDB_ANALYZER,
    embeddings: EmbeddingsSidecar | None = None,
    mode: SyncMode = "rebuild",
    reporter: ProgressReporter | None = None,
) -> SyncError | None:
    """Synchronizes the payload with ArcadeDB.

    In `rebuild` the database is cleared first, so the compiler stays the source
    of truth — the same contract as `sync_to_neo4j`. In `update` nothing is
    cleared and the payload merges into the existing graph.

    `embeddings`, when given, adds the vector property, its index and the vectors
    themselves. Absent, nothing about the existing behaviour changes.

    Returns None on success, SyncError on failure.
    """
    def attempt() -> SyncError | None:
        # Note the asymmetry with Neo4j: the ArcadeDB adapter ALSO clears in its
        # own `clear_destination` step, so a rebuild through the pipeline clears
        # twice. Harmless (the second finds an empty graph) but it means the mode
        # has to reach both places, not just the pipeline step.
        if mode == "rebuild":
            clear_database(client)
        # Idempotent in both modes: types, properties and UNIQUE indexes are all
        # created only when absent, so re-asserting them costs a check.
        _create_schema(client, payload)
        _create_constraints(client, payload)
        # Not idempotent, and deliberately so: this DROPs and recreates, which is
        # what makes a changed analyzer take effect. In update that would reindex
        # the whole existing graph to land on the index it already had — and the
        # analyzer cannot have changed, because the pipeline refuses that
        # combination (see `_incompatible_with_update`).
        if mode == "rebuild":
            _create_search_indexes(client, payload, fulltext_analyzer)
        # Declared right after the indexes are built, and from the same field
        # lists — so what the context announces cannot drift from what exists.
        # Before `_execute_sync_transaction`, which is what writes the context
        # vertex; declaring after it would announce nothing. Runs in both modes:
        # update rewrites the context vertex, so it must carry the declaration
        # even though it did not rebuild the index it describes.
        _declare_fulltext(payload, fulltext_analyzer)
        # Before the transaction, never inside it — see `_bulk_load_vertices`.
        bulk_loaded = _bulk_load_vertices(client, payload, mode)
        _execute_sync_transaction(client, payload, mode, skip=bulk_loaded)
        if embeddings is not None:
            error = _sync_embeddings(client, payload, embeddings)
            if error is not None:
                return error
        return None

    last: Exception | None = None
    for tries_left in range(SYNC_RETRY_ATTEMPTS - 1, -1, -1):
        try:
            return attempt()
        except ArcadeDBError as e:
            last = e
            if not (e.retryable and _retry_is_safe(mode) and tries_left):
                break
            # Restarting the whole sync, not the lost request. A gateway failure
            # leaves it unknown whether the statement was applied, so repeating
            # that one statement could write it twice; repeating the sync cannot,
            # because a rebuild clears the graph before writing anything.
            logger.warning("Sync interrupted, retrying: %s", e)
            if reporter is not None:
                # Said plainly, and without the HTTP vocabulary: the researcher
                # did nothing wrong and there is nothing for them to fix. What
                # they need to know is that the work restarts and no partial
                # graph survives — otherwise a second wait of the same length
                # reads as the tool having lost its way.
                reporter.warning(
                    "The server stopped responding for a moment. Starting the "
                    f"upload over — {tries_left} more attempt(s). Nothing was "
                    "left half-written; the database is rebuilt from scratch."
                )
            time.sleep(_RETRY_BACKOFF_SECONDS)
        except Exception as e:  # noqa: BLE001 - surfaced to the caller as typed
            last = e
            break
    if isinstance(last, ArcadeDBError) and last.retryable:
        # A transient failure that outlived every retry is a different problem
        # from a rejected statement, and the way out is different too. Saying so
        # here saves the researcher from re-reading a stack of gateway errors
        # looking for a mistake in their project.
        return SyncError(
            message="The server kept dropping the connection",
            stage="sync",
            details=(
                f"{last} — this is the server or its proxy, not your project. "
                "Nothing was left half-written. Try again when the server is "
                "less busy; if it keeps happening, the server may need more "
                "memory, or a longer timeout via [arcadedb].timeout."
            ),
        )
    return SyncError(message="Synchronization failed", stage="sync", details=str(last))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def _concept_index_props(payload: GraphPayload) -> list[str]:
    """Concept properties that belong in the full-text index.

    Same rule as the Neo4j backend: the humanised name plus every SCOPE ONTOLOGY
    text field. `graph_fields` stay out — they become taxonomy nodes of their own,
    and indexing a closed vocabulary as prose only dilutes the index.
    """
    return dedupe_index_props(
        p for p in ["search_name", *payload.scalar_fields] if validate_cypher_label(p)
    )


def _source_index_props(payload: GraphPayload) -> list[str]:
    """Source properties that belong in the full-text index."""
    text_fields = text_source_field_names(payload.source_fields)
    return dedupe_index_props(
        p for p in [*SOURCE_TEXT_PROPS, *text_fields] if validate_cypher_label(p)
    )


def _declare_property(
    client: ArcadeDBTransport, type_name: str, prop: str, declared_type: str = "STRING"
) -> None:
    """Declares one property, if the name is safe to interpolate.

    `declared_type` defaults to STRING because that is what every caller needed
    until the network metrics arrived: `pagerank` is a DOUBLE and `degree` an
    INTEGER, and declaring either as STRING makes ArcadeDB reject the value at
    sync time — the same trap that keeps the `ProjectContext` counts undeclared.

    Verified against ArcadeDB 26.7.3: DOUBLE, FLOAT, INTEGER, LONG and DECIMAL
    are all accepted, and values round-trip unchanged.

    The type is whitelisted rather than interpolated freely: it reaches an SQL
    string, and the property name is already guarded by `validate_cypher_label`.
    """
    if not validate_cypher_label(type_name) or not validate_cypher_label(prop):
        return
    if declared_type not in _DECLARABLE_TYPES:
        logger.warning(
            "Ignoring unknown property type %r for %s.%s", declared_type, type_name, prop
        )
        return
    client.command(
        f"CREATE PROPERTY {type_name}.{prop} IF NOT EXISTS {declared_type}", language="sql"
    )


def _create_schema(client: ArcadeDBTransport, payload: GraphPayload) -> None:
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

    # Audit trail back to the `.syn`. Declared so `get_schema` announces it: an
    # undeclared property is invisible to MCP introspection, which is how the chat
    # discovers the graph — the same trap `ProjectContext` fell into.
    #
    # `source_line` is declared INTEGER. It used to stay schema-less because
    # `_declare_property` only wrote STRING, which ArcadeDB rejects for an integer;
    # the typed helper removed that limitation, and leaving the property undeclared
    # kept it invisible to the introspection the chat relies on.
    _declare_property(client, "Item", "source_file")
    _declare_property(client, "Item", "source_line", "INTEGER")

    # Identity of the annotated BLOCK, shared by every Item it produced. This is what
    # makes the counting units distinguishable in a query: `count(i)` counts analytical
    # items, `count(DISTINCT i.annotation_id)` counts annotated excerpts. Without it
    # the chat could only guess the excerpt count by grouping on file and line.
    _declare_property(client, "Item", "annotation_id")

    # Métricas de rede do conceito. Declaradas com o tipo REAL — STRING faria o
    # servidor recusar o valor no sync. Só no rótulo de conceito: as taxonomias
    # não recebem métricas.
    if validate_cypher_label(concept_label):
        for prop, declared_type in CONCEPT_METRIC_PROPS:
            _declare_property(client, concept_label, prop, declared_type)
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


def _create_constraints(client: ArcadeDBTransport, payload: GraphPayload) -> None:
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


def _create_unique_index(client: ArcadeDBTransport, type_name: str, prop: str) -> None:
    if not validate_cypher_label(type_name) or not validate_cypher_label(prop):
        return
    client.command(f"CREATE INDEX IF NOT EXISTS ON {type_name} ({prop}) UNIQUE", language="sql")


def _create_search_indexes(
    client: ArcadeDBTransport,
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


def _declare_fulltext(payload: GraphPayload, fulltext_analyzer: str) -> None:
    """Tells the ProjectContext which full-text indexes this sync created.

    Only ArcadeDB declares this. Neo4j builds full-text indexes too, but they are
    queried through `db.index.fulltext.queryNodes`, not `SEARCH_INDEX` — announcing
    one backend's syntax for the other's graph would teach a query that always
    fails.

    The field lists come from the same helpers that fed `CREATE INDEX`, never
    recomputed: a second derivation of the same rule is free to disagree with the
    first, and the whole point of declaring is that the consumer can trust it.
    """
    concept_props = (
        _concept_index_props(payload) if validate_cypher_label(payload.concept_label) else []
    )
    declare_fulltext_capability(
        payload.project_context,
        concept_props,
        list(ITEM_TEXT_PROPS),
        _source_index_props(payload),
        resolve_arcadedb_analyzer(fulltext_analyzer),
    )


def _create_fulltext_index(
    client: ArcadeDBTransport, type_name: str, props: list[str], analyzer: str
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
    client: ArcadeDBTransport,
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
    client: ArcadeDBTransport,
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
def clear_database(client: ArcadeDBTransport) -> None:
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
def _execute_sync_transaction(
    client: ArcadeDBTransport,
    payload: GraphPayload,
    mode: SyncMode = "rebuild",
    skip: frozenset[str] = frozenset(),
) -> None:
    """Writes the whole payload inside one server-side transaction.

    The ordering matches `sync_to_neo4j` and is load-bearing: items and sources must
    exist before the edges that match on them, and reified identities before the
    REFERS_TO edges pointing at them.

    `mode="rebuild"` is what `sync_to_arcadedb` passes, and it is safe there
    because `clear_database` has just run. It matters far more on this backend
    than on Neo4j: the `MERGE` index lookup per row is what degraded the real
    corpus to an extrapolated five hours against the Hostinger container.

    `skip` names collections a bulk load already wrote (see `_bulk_load_vertices`).
    They are skipped by passing an empty list rather than by branching around the
    call: every `_sync_*` function already treats an empty collection as "nothing
    to do", so the edges that follow still run, still resolve their endpoints
    through the same `MATCH`, and there is no second code path to keep in step.
    Defaults to skipping nothing, so a caller that knows nothing about bulk
    loading behaves exactly as before.
    """
    bs = SYNC_BATCH_SIZE
    sources = [] if "sources" in skip else payload.sources
    items = [] if "items" in skip else payload.items
    # Only the ontology concepts are bulk-loadable; the chains still need their
    # own `MERGE` pass to create concepts that the ontology never declared.
    concepts = [] if "concepts" in skip else payload.concepts

    tx = _CypherRunner(client)
    client.begin()
    try:
        # Same position as in `sync_to_neo4j`: first, and inside the transaction,
        # so the context never outlives a sync that failed halfway.
        _sync_project_context(tx, payload.project_context, mode)
        _sync_sources(tx, sources, batch_size=bs, mode=mode)
        _sync_items(tx, items, batch_size=bs, mode=mode)
        _sync_from_source(tx, payload.from_source, batch_size=bs)
        _sync_concepts(
            tx, payload.chains, concepts, payload.concept_label, batch_size=bs, mode=mode
        )
        # `payload.concepts`, NOT the possibly-emptied `concepts`: a bulk load
        # wrote the concept vertices, but their taxonomy edges still have to be
        # built here, and they are derived from these same rows. Narrowing this
        # to the local would silently drop every GROUPED_BY/QUALIFIED_BY edge.
        _sync_taxonomies(
            tx, payload.concepts, payload.graph_fields, payload.concept_label, batch_size=bs
        )
        _sync_mentions(tx, payload.mentions, payload.concept_label, batch_size=bs)
        _sync_entities(tx, payload.entities, batch_size=bs)
        _sync_refers_to(tx, payload.refers_to_edges, batch_size=bs)
        client.commit()
    except Exception:
        # Leaving the transaction open would hold locks until the server expires
        # it, and the partial graph would outlive the failure.
        #
        # The rollback's own failure must never replace the original error. When
        # a proxy drops the connection mid-sync, the server usually discards the
        # transaction with it, so this rollback answers "Remote transaction not
        # found or expired" (HTTP 404) — and an unguarded `client.rollback()`
        # raised that *instead of* the 502 that actually happened. The caller
        # then saw a 404, which is not retryable, and abandoned a sync that
        # should have been restarted. The cause outranks the cleanup.
        try:
            client.rollback()
        except Exception as cleanup:  # noqa: BLE001 - the original error wins
            logger.debug("Rollback after a failed sync also failed: %s", cleanup)
        raise


def _sync_embeddings(
    client: ArcadeDBTransport,
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
