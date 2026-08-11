"""Neo4j backend: sync functions, schema management, taxonomy relations."""

from __future__ import annotations

from typing import Any

from synesis_graph.core import (
    GraphPayload,
    SyncError,
    get_taxonomy_labels,
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


def sync_to_neo4j(session: Any, payload: GraphPayload) -> SyncError | None:
    """
    Synchronizes payload with Neo4j in a single transaction.

    Clears the database completely before synchronizing, ensuring that
    the compiler is the source of truth.

    Args:
        session: Active Neo4j session
        payload: Data prepared for persistence

    Returns:
        None on success, SyncError on failure.
    """
    try:
        # Clear database before synchronizing (source of truth = compiler)
        clear_database(session)
        _create_constraints(
            session,
            payload.graph_fields,
            payload.concept_label,
            list(payload.entities.keys()),
        )
        _execute_sync_transaction(session, payload)
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


def _execute_sync_transaction(session: Any, payload: GraphPayload) -> None:
    """Executes all sync operations in a single transaction."""
    with session.begin_transaction() as tx:
        _sync_sources(tx, payload.sources)
        _sync_items(tx, payload.items)
        _sync_from_source(tx, payload.from_source)
        _sync_concepts(tx, payload.chains, payload.concepts, payload.concept_label)
        _sync_taxonomies(tx, payload.concepts, payload.graph_fields, payload.concept_label)
        _sync_mentions(tx, payload.mentions, payload.concept_label)
        # Reified identities must exist before the edges that point at them.
        _sync_entities(tx, payload.entities)
        _sync_refers_to(tx, payload.refers_to_edges)
        tx.commit()


def _sync_sources(tx: Any, sources: list[dict[str, Any]]) -> None:
    """Synchronizes Source nodes (corresponding to SOURCE...END SOURCE block)."""
    if not sources:
        return
    tx.run(
        """
        UNWIND $rows AS row
        MERGE (s:Source {bibtex: row.bibtex})
        SET s = row, s.last_updated = timestamp()
    """,
        rows=sources,
    )


def _sync_items(tx: Any, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    tx.run(
        """
        UNWIND $rows AS row
        MERGE (i:Item {item_id: row.item_id})
        SET i = row, i.last_updated = timestamp()
    """,
        rows=items,
    )


def _sync_from_source(tx: Any, from_source: list[dict[str, Any]]) -> None:
    """Connects Item to the Source from which it was extracted."""
    if not from_source:
        return
    tx.run(
        """
        UNWIND $rows AS row
        MATCH (i:Item {item_id: row.item_id})
        MATCH (s:Source {bibtex: row.ref})
        MERGE (i)-[:FROM_SOURCE]->(s)
    """,
        rows=from_source,
    )


def _sync_entities(tx: Any, entities: dict[str, list[dict[str, Any]]]) -> None:
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
        tx.run(
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
            rows=rows,
        )


def _sync_refers_to(tx: Any, refers_to_edges: dict[str, list[dict[str, Any]]]) -> None:
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
        tx.run(
            f"""
            UNWIND $rows AS row
            MATCH (s:Source {{bibtex: row.from_bibtex}})
            MATCH (e:{label} {{entity_id: row.entity_id}})
            MERGE (s)-[r:{REFERS_TO_RELATION}]->(e)
            SET r.entity = row.entity, r.member = row.member
        """,
            rows=rows,
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
    tx: Any, concepts: list[dict[str, Any]], graph_fields: list[str], concept_label: str
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
        tx.run(query, rows=relation_rows)

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
            tx.run(
                """
                UNWIND $rows AS row
                UNWIND row.topics AS topic_val
                UNWIND row.aspects AS aspect_val
                MATCH (topic:Topic {name: topic_val})
                MATCH (aspect:Aspect {name: aspect_val})
                MERGE (topic)-[:MAPPED_TO_ASPECT]->(aspect)
            """,
                rows=mapping_rows,
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
            tx.run(
                """
                UNWIND $rows AS row
                UNWIND row.topics AS topic_val
                UNWIND row.dimensions AS dimension_val
                MATCH (topic:Topic {name: topic_val})
                MATCH (dimension:Dimension {name: dimension_val})
                MERGE (topic)-[:MAPPED_TO_DIMENSION]->(dimension)
            """,
                rows=mapping_rows,
            )

    # Topic -> Topic (IS_LINKED_TO) - connects topics via RELATES_TO between their concepts
    # strength = number of RELATES_TO relations between concepts of both topics
    if "topic" in graph_fields:
        cl = concept_label
        tx.run(
            f"MATCH (t1:Topic)<-[:GROUPED_BY]-(f1:{cl})-[:RELATES_TO]->(f2:{cl})"
            "-[:GROUPED_BY]->(t2:Topic) "
            "WHERE t1 <> t2 "
            "WITH t1, t2, count(*) AS strength "
            "MERGE (t1)-[r:IS_LINKED_TO]->(t2) "
            "SET r.strength = strength, r.last_updated = timestamp()"
        )


def _sync_mentions(tx: Any, mentions: list[dict[str, Any]], concept_label: str) -> None:
    """Connects Item to mentioned concept nodes."""
    if not mentions:
        return
    tx.run(
        f"""
        UNWIND $rows AS row
        MATCH (i:Item {{item_id: row.item_id}})
        MATCH (c:{concept_label} {{name: row.concept}})
        MERGE (i)-[:MENTIONS {{mention_order: row.mention_order}}]->(c)
    """,
        rows=mentions,
    )


def _sync_concepts(
    tx: Any, chains: list[dict[str, Any]], concepts: list[dict[str, Any]], concept_label: str
) -> None:
    """
    Creates concept nodes (dynamic label based on CHAIN/CODE field) and RELATES_TO relations.

    Nodes are created from:
    1. Ontology concepts (always)
    2. Source/target from chains (when they exist)

    The RELATES_TO relation connects concepts with type and description as
    edge attributes (only for templates with CHAIN field).
    """
    # First: create concept nodes from ontology
    if concepts:
        concept_rows: list[dict[str, Any]] = []
        for row in concepts:
            props = row.get("props", {})
            if isinstance(props, dict) and props.get("name"):
                concept_rows.append(props)

        tx.run(
            f"""
            UNWIND $rows AS row
            MERGE (c:{concept_label} {{name: row.name}})
            SET c = row
        """,
            rows=concept_rows,
        )

    # If there are no chains, nothing more to do
    if not chains:
        return

    # Second: create concept nodes from chains that don't exist in ontology
    tx.run(
        f"""
        UNWIND $rows AS row
        MERGE (s:{concept_label} {{name: row.source}})
        MERGE (t:{concept_label} {{name: row.target}})
    """,
        rows=chains,
    )

    # Third: create RELATES_TO relations with attributes
    # type is part of the MERGE key so that the same concept pair can have
    # multiple edges of different types (e.g. APPLICATION and METHODOLOGICAL)
    tx.run(
        f"""
        UNWIND $rows AS row
        MATCH (s:{concept_label} {{name: row.source}})
        MATCH (t:{concept_label} {{name: row.target}})
        MERGE (s)-[r:RELATES_TO {{type: row.type}}]->(t)
        SET r.description = row.description,
            r.item_id = row.item_id
    """,
        rows=chains,
    )
