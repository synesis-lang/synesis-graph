"""Neo4j backend: sync functions, schema management, taxonomy relations."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal

from synesis_graph.core import (
    DEFAULT_FULLTEXT_ANALYZER,
    GraphPayload,
    ProjectContextSpec,
    SyncError,
    dedupe_index_props,
    get_taxonomy_labels,
    text_source_field_names,
)
from synesis_graph.linkage import REFERS_TO_RELATION
from synesis_graph.sanitize import sanitize_cypher_label, validate_cypher_label


def clear_database(session: Any) -> None:
    """
    Clears all data from the database, including constraints and indexes.

    Ensures that the source of truth is always the compiler data.
    """
    # Remove all existing constraints
    constraints = session.run("SHOW CONSTRAINTS").data()
    for c in constraints:
        constraint_name = c.get("name")
        if constraint_name:
            session.run(f"DROP CONSTRAINT {constraint_name} IF EXISTS")

    # Remove all existing indexes (except automatically created ones)
    indexes = session.run("SHOW INDEXES").data()
    for idx in indexes:
        if idx.get("owningConstraint") is None:  # Not a constraint index
            idx_name = idx.get("name")
            if idx_name:
                session.run(f"DROP INDEX {idx_name} IF EXISTS")

    # Clear all nodes and relationships
    session.run("MATCH (n) DETACH DELETE n")


def sync_to_neo4j(
    session: Any,
    payload: GraphPayload,
    fulltext_analyzer: str = DEFAULT_FULLTEXT_ANALYZER,
    mode: SyncMode = "rebuild",
) -> SyncError | None:
    """
    Synchronizes payload with Neo4j in a single transaction.

    In `rebuild` (the default, and what every caller did before the mode
    existed) the database is cleared first, so the compiler is the source of
    truth. In `update` nothing is cleared: the payload is merged into whatever
    is already there, which is what makes an incremental load possible on a
    corpus too large to rewrite.

    Args:
        session: Active Neo4j session
        payload: Data prepared for persistence
        mode: `rebuild` wipes and rewrites; `update` merges in place.

    Returns:
        None on success, SyncError on failure.
    """
    try:
        # Clear database before synchronizing (source of truth = compiler).
        # Skipped in update, where the existing graph IS part of the truth.
        if mode == "rebuild":
            clear_database(session)
        _create_constraints(
            session,
            payload.graph_fields,
            payload.concept_label,
            list(payload.entities.keys()),
        )
        # Constraints are `IF NOT EXISTS`, so they are safe to re-assert in both
        # modes. The full-text indexes are not: `_create_search_indexes` DROPs
        # and recreates, which in update would reindex the entire existing graph
        # to arrive at the index it already had. The analyzer cannot have changed
        # without `rebuild` (the pipeline refuses it), so there is nothing to
        # apply — and Neo4j keeps an existing index up to date as rows arrive.
        if mode == "rebuild":
            _create_search_indexes(session, payload, fulltext_analyzer)
        _execute_sync_transaction(session, payload, mode)
        return None
    except Exception as e:
        return SyncError(message="Synchronization failed", stage="sync", details=str(e))


def _create_constraints(
    session: Any,
    graph_fields: list[str],
    concept_label: str,
    entity_labels: list[str] | None = None,
) -> None:
    """Creates uniqueness constraints in Neo4j schema.

    `entity_labels` defaults to None so existing callers (including the legacy
    synesis2graph shim) keep working unchanged.
    """
    # Constraints for dynamic taxonomies
    for label in get_taxonomy_labels(graph_fields):
        if validate_cypher_label(label):
            session.run(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.name IS UNIQUE")

    # Fixed constraints
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Source) REQUIRE s.bibtex IS UNIQUE")
    session.run("CREATE CONSTRAINT IF NOT EXISTS FOR (i:Item) REQUIRE i.item_id IS UNIQUE")

    # Constraint for dynamic label (based on CHAIN/CODE field)
    if validate_cypher_label(concept_label):
        session.run(
            f"CREATE CONSTRAINT IF NOT EXISTS FOR (c:{concept_label}) REQUIRE c.name IS UNIQUE"
        )

    # Constraints for reified identity nodes (IDENTIFIES is a primary key)
    for label in entity_labels or ():
        if validate_cypher_label(label):
            session.run(
                f"CREATE CONSTRAINT IF NOT EXISTS FOR (e:{label}) REQUIRE e.entity_id IS UNIQUE"
            )


def _create_search_indexes(
    session: Any,
    payload: GraphPayload,
    fulltext_analyzer: str = DEFAULT_FULLTEXT_ANALYZER,
) -> None:
    """Creates full-text indexes over the template's text-bearing fields.

    Constraints guarantee integrity; these serve retrieval. Without them the only
    way into the graph is text2cypher with exact string matching, so a question
    that misses a concept's literal name retrieves nothing.

    Every indexed property is derived from the template — never hardcoded. Concept
    prose lives in `scalar_fields` (`ontology_description` in one project,
    `factor_description` in another), and Source prose is whichever SCOPE SOURCE
    field was declared TEXT. `graph_fields` are deliberately absent: TOPIC /
    ENUMERATED / ORDERED become taxonomy nodes of their own, and indexing a closed
    vocabulary as prose only dilutes the index.

    `citation` and `description` on Item are structural names the payload
    normalises from the template's QUOTATION and MEMO fields, so they hold
    regardless of what the template calls them.
    """

    def _props(alias: str, fields: list[str]) -> str:
        """Renders one index's property list, each property exactly once.

        The dedup is not cosmetic: Neo4j rejects a composite index that repeats a
        property (`RepeatedPropertyInCompositeSchema`), and the Source list
        prepends the structural `title`/`abstract` to the template's own TEXT
        fields — a template is free to declare a field called `title`.
        """
        unique = dedupe_index_props(f for f in fields if validate_cypher_label(f))
        return ", ".join(f"{alias}.{f}" for f in unique)

    def _create(name: str, pattern: str, props: str) -> None:
        """Drops and recreates one full-text index.

        The DROP is not optional. Neo4j refuses a second index over the same
        (label, properties) pair — `CREATE ... IF NOT EXISTS` then succeeds
        silently while leaving the OLD index in place, so a changed analyzer
        would never take effect and nothing would say so. Recreating is free
        here: the sync clears the database first anyway.
        """
        session.run(f"DROP INDEX {name} IF EXISTS")
        session.run(
            f"CREATE FULLTEXT INDEX {name} FOR {pattern} ON EACH [{props}] "
            f"OPTIONS {{indexConfig: {{`fulltext.analyzer`: $analyzer}}}}",
            analyzer=fulltext_analyzer,
        )

    # Concept: the humanised name plus every SCOPE ONTOLOGY text field.
    # `search_name`, not `name`: the raw snake_case indexes as a single token
    # (see humanize_concept_name), which no natural-language query can match.
    # Exact-identifier lookups go through MATCH on `name`, served by the
    # uniqueness constraint's RANGE index.
    if validate_cypher_label(payload.concept_label):
        concept_props = _props("c", ["search_name", *payload.scalar_fields])
        if concept_props:
            _create("concept_search", f"(c:{payload.concept_label})", concept_props)

    # Item: the literal excerpt and the analytical memo.
    _create("item_search", "(i:Item)", "i.citation, i.description")

    # Source: standard bibliographic prose plus the template's TEXT fields.
    source_text = text_source_field_names(payload.source_fields)
    source_props = _props("s", ["title", "abstract", *source_text])
    if source_props:
        _create("source_search", "(s:Source)", source_props)


def _execute_sync_transaction(
    session: Any, payload: GraphPayload, mode: SyncMode = "rebuild"
) -> None:
    """Executes all sync operations in a single transaction.

    `mode` defaults to `"rebuild"` because both backends call `clear_database`
    immediately before this function (`sync_to_neo4j`, `sync_to_arcadedb`), so
    the destination is known to be empty. This is the only place in the module
    that can know that, which is why the `_sync_*` functions themselves default
    to the conservative `"update"` instead.
    """
    with session.begin_transaction() as tx:
        # First, and inside the transaction: the context describes this snapshot,
        # so it must not outlive a sync that failed halfway. It depends on no
        # other node, hence the position.
        _sync_project_context(tx, payload.project_context, mode)
        _sync_sources(tx, payload.sources, mode=mode)
        _sync_items(tx, payload.items, mode=mode)
        _sync_from_source(tx, payload.from_source)
        _sync_concepts(
            tx, payload.chains, payload.concepts, payload.concept_label, mode=mode
        )
        _sync_taxonomies(tx, payload.concepts, payload.graph_fields, payload.concept_label)
        _sync_mentions(tx, payload.mentions, payload.concept_label)
        # Reified identities must exist before the edges that point at them.
        _sync_entities(tx, payload.entities)
        _sync_refers_to(tx, payload.refers_to_edges)
        tx.commit()


def _sync_project_context(
    tx: Any, context: ProjectContextSpec | None, mode: SyncMode = "rebuild"
) -> None:
    """Writes the project's own context as a single `ProjectContext` vertex.

    This is what makes the exported graph self-describing. Without it a consumer
    introspecting the schema learns that a taxonomy vertex has a `name`, but not
    what the scale means, what each value stands for, or how the researcher was
    supposed to code the field — all of it declared in the template and, until
    now, dropped at export time.

    `CREATE`, not `MERGE`, in rebuild: both backends wipe the graph first (see
    `clear_database`), so a single instance is guaranteed by the mechanism that
    already exists. A `MERGE` there would imply a uniqueness key this vertex does
    not need.

    **In update it must delete first.** Nothing was wiped, so a plain `CREATE`
    would leave a second `ProjectContext` beside the old one, and every consumer
    that reads the context — the MCP client above all — would get two conflicting
    descriptions of the same graph with no way to tell which is current. Deleting
    then creating keeps the "exactly one" invariant that the vertex's readers
    assume, and refreshes the counts, which is the point of re-syncing it.

    `context is None` for hand-built payloads, which carry no project to
    describe; writing nothing is the correct outcome, not an error.
    """
    if context is None:
        return
    if mode == "update":
        tx.run("MATCH (p:ProjectContext) DETACH DELETE p")
    # Sem lote de propósito: escreve um único vértice.
    tx.run(
        """
        CREATE (p:ProjectContext)
        SET p = $props, p.last_updated = timestamp()
    """,
        props=asdict(context),
    )


#: How a sync should treat rows that may already be in the destination.
#:
#: `"rebuild"` means the caller has just emptied the graph, so a vertex whose
#: key the payload guarantees to be unique can be written with `CREATE` — no
#: index lookup per row. `"update"` means the destination may hold anything, so
#: every write must deduplicate with `MERGE`.
#:
#: This only ever applies where the payload itself is measured to be unique:
#: `items` (246,588 rows, 0 duplicate keys), `sources` (2,981, 0) and the
#: ontology `concepts` (22,585, 0). Chains and taxonomies stay on `MERGE` in
#: both modes — see `_sync_concepts` and `_sync_taxonomies`, where the dedup is
#: doing real work rather than guarding against an accident.
SyncMode = Literal["rebuild", "update"]

#: The default everywhere is `"update"`, i.e. today's behaviour, so that no
#: caller changes what it does by not being updated. `rebuild` is opted into
#: explicitly by `_execute_sync_transaction`, which runs right after
#: `clear_database` in both backends and is the one place that can know the
#: destination is empty.
DEFAULT_SYNC_MODE: SyncMode = "update"


def _write_clause(mode: SyncMode, pattern: str) -> str:
    """`CREATE <pattern>` in rebuild, `MERGE <pattern>` in update.

    Exists so the choice is made in exactly one place and reads the same at
    every call site. `MERGE` costs an index lookup per row against an index that
    grows as the load proceeds, which is why a real corpus degrades
    quadratically: 10,000 nodes in 22.2s, 40,000 in 472.8s — extrapolating to
    roughly five hours for the 246,588-item Quinto Andar corpus. The same load
    with `CREATE` measured 12x faster.

    Safe only because the caller has just emptied the graph. Getting that wrong
    does not corrupt anything subtly — it duplicates vertices, which the
    integration tests count.
    """
    return f"{'CREATE' if mode == 'rebuild' else 'MERGE'} {pattern}"


#: Rows per `UNWIND` when no caller says otherwise. Deliberately far above any
#: corpus Synesis has produced (the largest measured is 246,588 items), so the
#: Neo4j path keeps making exactly one call per sync function and its behaviour
#: is unchanged. The ArcadeDB backend passes a much smaller value — see
#: `_run_in_batches` for why that server needs one.
DEFAULT_SYNC_BATCH_SIZE = 50_000


def _run_in_batches(
    tx: Any, query: str, rows: list[dict[str, Any]], batch_size: int
) -> None:
    """Runs `query` over `rows`, split into slices of at most `batch_size`.

    Exists because a single `UNWIND $rows` holding an entire corpus is what a
    Neo4j server shrugs off and an ArcadeDB container on a small VPS cannot: a
    real sync of 246,588 items against a 2-vCPU/8GB Hostinger box drove the JVM
    to 205% CPU and 79.7% of its heap, and a scaling test on that same server
    pinned the failure between 2,000 and 5,000 rows in one transaction — the
    non-linear jump from 500 rows (1.35s) to 2,000 rows (13.1s) is the signature
    of GC under pressure, not of network or proxy timeouts (both measured and
    ruled out first). See arcadedb_batch_sync_study.md for the full trail.

    `batch_size` has no effect when `len(rows) <= batch_size`: exactly one call
    to `tx.run` is made, with the full list — identical to calling `tx.run`
    directly, so a default large enough for ordinary Neo4j corpora costs
    nothing there.

    Empty `rows` makes no call at all, matching every `_sync_*` function's own
    "nothing to do" guard.
    """
    if not rows:
        return
    for start in range(0, len(rows), batch_size):
        tx.run(query, rows=rows[start : start + batch_size])


def ontology_concept_rows(concepts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The property dicts `_sync_concepts` writes for the ontology's concepts.

    Extracted so the bulk-load path (`backends/arcadedb.py`) writes exactly the
    same rows as the Cypher path. Two copies of "which concepts get written, and
    with which properties" would be free to disagree, and the disagreement would
    show up as a graph that differs depending on which path ran — the one thing
    the two-path design must never allow.

    Concepts without a `name` are skipped: `name` is the vertex's identity, and
    the UNIQUE index rejects a null one.
    """
    rows: list[dict[str, Any]] = []
    for row in concepts:
        props = row.get("props", {})
        if isinstance(props, dict) and props.get("name"):
            rows.append(props)
    return rows


def _sync_sources(
    tx: Any,
    sources: list[dict[str, Any]],
    batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
    mode: SyncMode = DEFAULT_SYNC_MODE,
) -> None:
    """Synchronizes Source nodes (corresponding to SOURCE...END SOURCE block).

    `bibtex` is unique per source by construction — the compiler keys the
    bibliography by it — so `rebuild` may `CREATE` (see `_write_clause`).
    """
    _run_in_batches(
        tx,
        f"""
        UNWIND $rows AS row
        {_write_clause(mode, "(s:Source {bibtex: row.bibtex})")}
        SET s = row, s.last_updated = timestamp()
    """,
        sources,
        batch_size,
    )


def _sync_items(
    tx: Any,
    items: list[dict[str, Any]],
    batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
    mode: SyncMode = DEFAULT_SYNC_MODE,
) -> None:
    """`item_id` is unique per item by construction, so `rebuild` may `CREATE`.

    This is the function the 12x measurement was taken on: it carries the
    largest collection in any Synesis corpus by an order of magnitude.
    """
    _run_in_batches(
        tx,
        f"""
        UNWIND $rows AS row
        {_write_clause(mode, "(i:Item {item_id: row.item_id})")}
        SET i = row, i.last_updated = timestamp()
    """,
        items,
        batch_size,
    )


def _sync_from_source(
    tx: Any, from_source: list[dict[str, Any]], batch_size: int = DEFAULT_SYNC_BATCH_SIZE
) -> None:
    """Connects Item to the Source from which it was extracted."""
    _run_in_batches(
        tx,
        """
        UNWIND $rows AS row
        MATCH (i:Item {item_id: row.item_id})
        MATCH (s:Source {bibtex: row.ref})
        MERGE (i)-[:FROM_SOURCE]->(s)
    """,
        from_source,
        batch_size,
    )


def _sync_entities(
    tx: Any,
    entities: dict[str, list[dict[str, Any]]],
    batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
) -> None:
    """Creates reified identity nodes from IDENTIFIES (multi-project linkage).

    One node per distinct primary-key value, labelled with the entity name
    (`researcher` -> `:Researcher`). The label cannot be parameterized by the
    driver, so it is interpolated — always validated first, since it originates
    from the user's template.

    The node is born only from IDENTIFIES: an orphan REFERS TO creates no stub.
    """
    if not entities:
        return
    for label, rows in entities.items():
        if not rows or not validate_cypher_label(label):
            continue
        _run_in_batches(
            tx,
            f"""
            UNWIND $rows AS row
            MERGE (e:{label} {{entity_id: row.entity_id}})
            SET e.entity = row.entity,
                e.member = row.member,
                e.source_bibtex = row.source_bibtex,
                e.last_updated = timestamp()
            WITH e, row
            MATCH (s:Source {{bibtex: row.source_bibtex}})
            MERGE (s)-[:IDENTIFIED_AS]->(e)
        """,
            rows,
            batch_size,
        )


def _sync_refers_to(
    tx: Any,
    refers_to_edges: dict[str, list[dict[str, Any]]],
    batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
) -> None:
    """Creates (:Source)-[:REFERS_TO {entity}]->(:<Entity>) edges.

    The entity label rides as a relationship property rather than becoming part
    of the type, so a single relationship type serves every entity.
    Many edges may point at the same node — that is the n:1 case (many articles
    collected from one researcher's CV).
    """
    if not refers_to_edges:
        return
    for label, rows in refers_to_edges.items():
        if not rows or not validate_cypher_label(label):
            continue
        _run_in_batches(
            tx,
            f"""
            UNWIND $rows AS row
            MATCH (s:Source {{bibtex: row.from_bibtex}})
            MATCH (e:{label} {{entity_id: row.entity_id}})
            MERGE (s)-[r:{REFERS_TO_RELATION}]->(e)
            SET r.entity = row.entity, r.member = row.member
        """,
            rows,
            batch_size,
        )


# Mapping of fields to semantic relationship names
TAXONOMY_RELATION_MAP: dict[str, str] = {
    "topic": "GROUPED_BY",
    "aspect": "QUALIFIED_BY",
    "dimension": "BELONGS_TO",
    "confidence": "RATED_AS",
}


def _get_taxonomy_relation(field_name: str) -> str:
    """Returns semantic relationship name for the field, or HAS_* as fallback."""
    return TAXONOMY_RELATION_MAP.get(field_name.lower(), f"HAS_{field_name.upper()}")


def _sync_taxonomies(
    tx: Any,
    concepts: list[dict[str, Any]],
    graph_fields: list[str],
    concept_label: str,
    batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
) -> None:
    """
    Creates taxonomy nodes and semantic relationships from concept nodes.

    Relations:
    - Concept -> Topic via GROUPED_BY
    - Concept -> Aspect via QUALIFIED_BY
    - Concept -> Dimension via BELONGS_TO
    - Topic -> Topic via IS_LINKED_TO (self-referential)
    - Topic -> Aspect via MAPPED_TO_ASPECT
    - Topic -> Dimension via MAPPED_TO_DIMENSION

    Takes no `mode`, on purpose: MERGE nos dois modos. A taxonomy value is
    shared by design — one `topic` covers many concepts — so the `MERGE` here is
    what turns thousands of mentions of "Lideranca" into the single Topic node
    the researcher expects to click on. There is no rebuild shortcut available,
    because the duplication is in the payload rather than in the destination.
    """
    if not concepts:
        return

    # First: create taxonomy nodes and Concept -> Taxonomy relations
    for field_name in graph_fields:
        label = sanitize_cypher_label(field_name.capitalize())
        rel_type = _get_taxonomy_relation(field_name)

        if not validate_cypher_label(label) or not validate_cypher_label(rel_type):
            continue

        relation_rows: list[dict[str, Any]] = []
        for row in concepts:
            props = row.get("props", {})
            relations = row.get("relations", {})
            if not isinstance(props, dict) or not isinstance(relations, dict):
                continue

            concept_name = props.get("name")
            raw_vals = relations.get(field_name)
            if concept_name is None or raw_vals is None:
                continue

            vals = raw_vals if isinstance(raw_vals, list) else [raw_vals]
            vals = [v for v in vals if v is not None]
            if not vals:
                continue

            relation_rows.append({"concept": concept_name, "vals": vals})

        if not relation_rows:
            continue

        query = f"""
            UNWIND $rows AS row
            MATCH (c:{concept_label} {{name: row.concept}})
            UNWIND row.vals AS val
            MERGE (t:{label} {{name: val}})
            MERGE (c)-[:{rel_type}]->(t)
        """
        _run_in_batches(tx, query, relation_rows, batch_size)

    # Second: create mapping relations between taxonomies
    # Topic -> Aspect (MAPPED_TO_ASPECT)
    if "topic" in graph_fields and "aspect" in graph_fields:
        mapping_rows: list[dict[str, Any]] = []
        for row in concepts:
            relations = row.get("relations", {})
            if not isinstance(relations, dict):
                continue
            topics_raw = relations.get("topic")
            aspects_raw = relations.get("aspect")
            if topics_raw is None or aspects_raw is None:
                continue
            topics = topics_raw if isinstance(topics_raw, list) else [topics_raw]
            aspects = aspects_raw if isinstance(aspects_raw, list) else [aspects_raw]
            topics = [t for t in topics if t is not None]
            aspects = [a for a in aspects if a is not None]
            if topics and aspects:
                mapping_rows.append({"topics": topics, "aspects": aspects})

        if mapping_rows:
            _run_in_batches(
                tx,
                """
                UNWIND $rows AS row
                UNWIND row.topics AS topic_val
                UNWIND row.aspects AS aspect_val
                MATCH (topic:Topic {name: topic_val})
                MATCH (aspect:Aspect {name: aspect_val})
                MERGE (topic)-[:MAPPED_TO_ASPECT]->(aspect)
            """,
                mapping_rows,
                batch_size,
            )

    # Topic -> Dimension (MAPPED_TO_DIMENSION)
    if "topic" in graph_fields and "dimension" in graph_fields:
        mapping_rows: list[dict[str, Any]] = []
        for row in concepts:
            relations = row.get("relations", {})
            if not isinstance(relations, dict):
                continue
            topics_raw = relations.get("topic")
            dimensions_raw = relations.get("dimension")
            if topics_raw is None or dimensions_raw is None:
                continue
            topics = topics_raw if isinstance(topics_raw, list) else [topics_raw]
            dimensions = dimensions_raw if isinstance(dimensions_raw, list) else [dimensions_raw]
            topics = [t for t in topics if t is not None]
            dimensions = [d for d in dimensions if d is not None]
            if topics and dimensions:
                mapping_rows.append({"topics": topics, "dimensions": dimensions})

        if mapping_rows:
            _run_in_batches(
                tx,
                """
                UNWIND $rows AS row
                UNWIND row.topics AS topic_val
                UNWIND row.dimensions AS dimension_val
                MATCH (topic:Topic {name: topic_val})
                MATCH (dimension:Dimension {name: dimension_val})
                MERGE (topic)-[:MAPPED_TO_DIMENSION]->(dimension)
            """,
                mapping_rows,
                batch_size,
            )

    # Topic -> Topic (IS_LINKED_TO) - connects topics via RELATES_TO between their concepts
    # strength = number of RELATES_TO relations between concepts of both topics
    if "topic" in graph_fields:
        cl = concept_label
        # Sem lote de propósito: esta é uma agregação server-side sobre dados
        # já gravados, não um `UNWIND $rows`. Não há lista a fatiar — o custo
        # é da travessia, e dividi-la mudaria o `count(*)` de cada grupo.
        tx.run(
            f"MATCH (t1:Topic)<-[:GROUPED_BY]-(f1:{cl})-[:RELATES_TO]->(f2:{cl})"
            "-[:GROUPED_BY]->(t2:Topic) "
            "WHERE t1 <> t2 "
            "WITH t1, t2, count(*) AS strength "
            "MERGE (t1)-[r:IS_LINKED_TO]->(t2) "
            "SET r.strength = strength, r.last_updated = timestamp()"
        )


def _sync_mentions(
    tx: Any,
    mentions: list[dict[str, Any]],
    concept_label: str,
    batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
) -> None:
    """Connects Item to mentioned concept nodes."""
    _run_in_batches(
        tx,
        f"""
        UNWIND $rows AS row
        MATCH (i:Item {{item_id: row.item_id}})
        MATCH (c:{concept_label} {{name: row.concept}})
        MERGE (i)-[:MENTIONS {{mention_order: row.mention_order}}]->(c)
    """,
        mentions,
        batch_size,
    )


def _sync_concepts(
    tx: Any,
    chains: list[dict[str, Any]],
    concepts: list[dict[str, Any]],
    concept_label: str,
    batch_size: int = DEFAULT_SYNC_BATCH_SIZE,
    mode: SyncMode = DEFAULT_SYNC_MODE,
) -> None:
    """
    Creates concept nodes (dynamic label based on CHAIN/CODE field) and RELATES_TO relations.

    Nodes are created from:
    1. Ontology concepts (always)
    2. Source/target from chains (when they exist)

    The RELATES_TO relation connects concepts with type and description as
    edge attributes (only for templates with CHAIN field).

    `mode` reaches only the first of the three statements. The ontology declares
    each concept once (22,585 rows, 0 duplicate names measured on the real
    corpus), so that one may `CREATE` in rebuild. The two chain statements
    cannot: 302,392 chain endpoints resolve to 22,553 distinct concepts, each
    named about thirteen times, and their `MERGE` is what collapses the
    repetition. Turning those into `CREATE` would multiply the concept nodes by
    roughly thirteen and silently corrupt every traversal.
    """
    # First: create concept nodes from ontology
    if concepts:
        concept_rows = ontology_concept_rows(concepts)

        _run_in_batches(
            tx,
            f"""
            UNWIND $rows AS row
            {_write_clause(mode, f"(c:{concept_label} {{name: row.name}})")}
            SET c = row
        """,
            concept_rows,
            batch_size,
        )

    # If there are no chains, nothing more to do
    if not chains:
        return

    # Second: create concept nodes from chains that don't exist in ontology.
    # MERGE nos dois modos de propósito: a mesma chave chega ~13x aqui, e é
    # este MERGE que a deduplica. Ver a docstring.
    _run_in_batches(
        tx,
        f"""
        UNWIND $rows AS row
        MERGE (s:{concept_label} {{name: row.source}})
        MERGE (t:{concept_label} {{name: row.target}})
    """,
        chains,
        batch_size,
    )

    # Third: create RELATES_TO relations with attributes
    # type is part of the MERGE key so that the same concept pair can have
    # multiple edges of different types (e.g. APPLICATION and METHODOLOGICAL)
    _run_in_batches(
        tx,
        f"""
        UNWIND $rows AS row
        MATCH (s:{concept_label} {{name: row.source}})
        MATCH (t:{concept_label} {{name: row.target}})
        MERGE (s)-[r:RELATES_TO {{type: row.type}}]->(t)
        SET r.description = row.description,
            r.item_id = row.item_id
    """,
        chains,
        batch_size,
    )
