"""Graph metrics: native Cypher and GDS (optional) computations."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from synesis_graph.backends.neo4j import _get_taxonomy_relation
from synesis_graph.core import GraphPayload
from synesis_graph.sanitize import sanitize_cypher_label, validate_cypher_label
from synesis_graph.ui import TaskReporter

logger = logging.getLogger("synesis2graph")


# ============================================================================
# GRAPH METRICS
# ============================================================================
def _is_gds_available(session: Any) -> bool:
    """Checks if the GDS plugin is installed."""
    try:
        result = session.run("RETURN gds.version() AS version")
        version = result.single()["version"]
        logger.info(f"GDS detectado: versão {version}")
        return True
    except Exception:
        return False


def _get_graph_strategy(payload: GraphPayload) -> str:
    """
    Determines the graph strategy for GDS metrics.

    Preference hierarchy:
    1. RELATES_TO - explicit relation (CHAIN templates)
    2. CO_TAXONOMY - weighted co-taxonomy (CODE templates with TOPIC)
    3. CO_CITATION - co-citation via Source (fallback)
    """
    if payload.chains:
        return "RELATES_TO"
    elif payload.graph_fields:
        return "CO_TAXONOMY"
    else:
        return "CO_CITATION"


def compute_metrics(session: Any, payload: GraphPayload, reporter: TaskReporter) -> None:
    """
    Calculates Neo4j graph metrics: native (Cypher) and advanced (GDS).

    Native metrics are always calculated.
    GDS metrics are calculated if the plugin is available.
    """
    concept_label = payload.concept_label
    graph_fields = payload.graph_fields

    # 1. Native metrics (always run)
    with reporter.step("Measuring the graph"):
        _compute_native_concept_metrics(session, concept_label)
        _compute_native_taxonomy_metrics(session, concept_label, graph_fields)
        _compute_native_source_metrics(session, concept_label)

    # 2. GDS metrics (optional with fallback)
    if not _is_gds_available(session):
        reporter.warning(
            "GDS not installed. Install the Graph Data Science plugin for "
            "advanced metrics (PageRank, Betweenness, Communities)."
        )
        return

    strategy = _get_graph_strategy(payload)
    reporter.info(f"GDS graph strategy: {strategy}")

    with reporter.step("Finding central concepts and communities"):
        try:
            _compute_gds_metrics(session, payload, strategy, reporter)
        except Exception as e:
            reporter.warning(f"Error calculating GDS metrics: {e}")




# ----------------------------------------------------------------------------
# NATIVE METRICS (Pure Cypher - always available)
# ----------------------------------------------------------------------------
def _compute_native_concept_metrics(session: Any, concept_label: str) -> None:
    """
    Calculates native metrics for concept nodes.

    Metrics:
    - degree: total degree (in + out)
    - in_degree: incoming relations
    - out_degree: outgoing relations
    - mention_count: Items that mention the concept
    - source_count: distinct Sources where it appears
    """
    if not validate_cypher_label(concept_label):
        return

    # Degree centrality (based on RELATES_TO)
    session.run(f"""
        MATCH (c:{concept_label})
        OPTIONAL MATCH (c)-[:RELATES_TO]->(out_node)
        OPTIONAL MATCH (c)<-[:RELATES_TO]-(in_node)
        WITH c, count(DISTINCT out_node) AS out_deg, count(DISTINCT in_node) AS in_deg
        SET c.out_degree = out_deg,
            c.in_degree = in_deg,
            c.degree = out_deg + in_deg
    """)

    # Mention count and source count
    session.run(f"""
        MATCH (c:{concept_label})
        OPTIONAL MATCH (c)<-[:MENTIONS]-(i:Item)
        OPTIONAL MATCH (i)-[:FROM_SOURCE]->(s:Source)
        WITH c, count(DISTINCT i) AS mentions, count(DISTINCT s) AS sources
        SET c.mention_count = mentions,
            c.source_count = sources
    """)


def _compute_native_taxonomy_metrics(
    session: Any, concept_label: str, graph_fields: list[str]
) -> None:
    """
    Calculates native metrics for taxonomy nodes (Topic, Aspect, Dimension, etc).

    Metrics:
    - concept_count: classified concepts
    - weighted_degree: sum of IS_LINKED_TO strengths (if exists)
    - aspect_diversity: distinct aspects (if Topic)
    - dimension_diversity: distinct dimensions (if Topic)
    """
    if not validate_cypher_label(concept_label):
        return

    for field_name in graph_fields:
        label = sanitize_cypher_label(field_name.capitalize())
        rel_type = _get_taxonomy_relation(field_name)

        if not validate_cypher_label(label) or not validate_cypher_label(rel_type):
            continue

        # Concept count
        session.run(f"""
            MATCH (t:{label})<-[:{rel_type}]-(c:{concept_label})
            WITH t, count(c) AS cnt
            SET t.concept_count = cnt
        """)

    # Topic-specific metrics (if exists)
    if "topic" in graph_fields:
        # Weighted degree (sum of IS_LINKED_TO strengths)
        session.run("""
            MATCH (t:Topic)
            OPTIONAL MATCH (t)-[r:IS_LINKED_TO]-()
            WITH t, coalesce(sum(r.strength), 0) AS wd
            SET t.weighted_degree = wd
        """)

        # Aspect diversity (if aspect exists)
        if "aspect" in graph_fields:
            session.run(f"""
                MATCH (t:Topic)<-[:GROUPED_BY]-(c:{concept_label})
                OPTIONAL MATCH (c)-[:QUALIFIED_BY]->(a:Aspect)
                WITH t, count(DISTINCT a) AS div
                SET t.aspect_diversity = div
            """)

        # Dimension diversity (if dimension exists)
        if "dimension" in graph_fields:
            session.run(f"""
                MATCH (t:Topic)<-[:GROUPED_BY]-(c:{concept_label})
                OPTIONAL MATCH (c)-[:BELONGS_TO]->(d:Dimension)
                WITH t, count(DISTINCT d) AS div
                SET t.dimension_diversity = div
            """)


def _compute_native_source_metrics(session: Any, concept_label: str) -> None:
    """
    Calculates native metrics for Source nodes.

    Metrics:
    - item_count: Items extracted from the source
    - concept_count: mentioned concepts
    """
    if not validate_cypher_label(concept_label):
        return

    session.run(f"""
        MATCH (s:Source)
        OPTIONAL MATCH (s)<-[:FROM_SOURCE]-(i:Item)
        OPTIONAL MATCH (i)-[:MENTIONS]->(c:{concept_label})
        WITH s, count(DISTINCT i) AS items, count(DISTINCT c) AS concepts
        SET s.item_count = items,
            s.concept_count = concepts
    """)


# ----------------------------------------------------------------------------
# GDS METRICS (requires Graph Data Science plugin)
# ----------------------------------------------------------------------------
def _compute_gds_metrics(
    session: Any, payload: GraphPayload, strategy: str, reporter: TaskReporter
) -> None:
    """
    Calculates GDS metrics (PageRank, Betweenness, Louvain).

    Graph projection depends on strategy:
    - RELATES_TO: uses explicit relation
    - CO_TAXONOMY: uses weighted co-taxonomy
    - CO_CITATION: uses co-citation via Source
    """
    concept_label = payload.concept_label
    graph_name = "synesis_metrics_graph"

    # Clear previous projection if exists
    _drop_gds_graph(session, graph_name)

    # Create projection based on strategy
    node_count, rel_count = _create_gds_projection(session, graph_name, payload, strategy)

    if node_count == 0 or rel_count == 0:
        reporter.warning("Empty graph - skipping GDS metrics")
        return

    reporter.info(f"GDS projection: {node_count} nodes, {rel_count} relationships")

    # Calculate metrics
    try:
        _run_pagerank(session, graph_name, concept_label)
        reporter.success("PageRank calculated")
    except Exception as e:
        reporter.warning(f"PageRank failed: {e}")

    try:
        # Betweenness can be slow on large graphs
        _run_betweenness(session, graph_name, concept_label)
        reporter.success("Betweenness calculated")
    except Exception as e:
        reporter.warning(f"Betweenness failed: {e}")

    try:
        _run_louvain(session, graph_name, concept_label)
        reporter.success("Communities (Louvain) calculated")
    except Exception as e:
        reporter.warning(f"Louvain failed: {e}")

    # Clear projection
    _drop_gds_graph(session, graph_name)


def _drop_gds_graph(session: Any, graph_name: str) -> None:
    """Removes GDS projection if exists."""
    with contextlib.suppress(Exception):
        # Use YIELD graphName to avoid deprecated 'schema' field warning
        session.run(f"CALL gds.graph.drop('{graph_name}', false) YIELD graphName")


def _create_gds_projection(
    session: Any, graph_name: str, payload: GraphPayload, strategy: str
) -> tuple[int, int]:
    """
    Creates GDS projection based on strategy.

    Uses the new gds.graph.project aggregation function API (GDS 2.x+)
    instead of the deprecated gds.graph.project.cypher procedure.

    Returns:
        Tuple (node_count, relationship_count)
    """
    concept_label = payload.concept_label

    if strategy == "RELATES_TO":
        # Native projection - more efficient
        result = session.run(f"""
            CALL gds.graph.project(
                '{graph_name}',
                '{concept_label}',
                'RELATES_TO'
            )
            YIELD nodeCount, relationshipCount
            RETURN nodeCount, relationshipCount
        """)

    elif strategy == "CO_TAXONOMY":
        # Projection via weighted co-taxonomy using aggregation function
        # Build taxonomy relations list dynamically
        taxonomy_rels = []
        for field_name in payload.graph_fields:
            rel_type = _get_taxonomy_relation(field_name)
            if validate_cypher_label(rel_type):
                taxonomy_rels.append(rel_type)

        if not taxonomy_rels:
            return (0, 0)

        rel_pattern = "|".join(taxonomy_rels)

        # New aggregation function API (replaces deprecated gds.graph.project.cypher)
        result = session.run(f"""
            MATCH (f1:{concept_label})-[:{rel_pattern}]->(t)<-[:{rel_pattern}]-(f2:{concept_label})
            WHERE f1 <> f2
            WITH f1, f2, count(DISTINCT t) AS weight
            WITH gds.graph.project(
                '{graph_name}',
                f1,
                f2,
                {{relationshipProperties: {{weight: weight}}}}
            ) AS g
            RETURN g.nodeCount AS nodeCount, g.relationshipCount AS relationshipCount
        """)

    else:  # CO_CITATION
        # Projection via co-citation (Source) using aggregation function
        result = session.run(f"""
            MATCH (f1:{concept_label})<-[:MENTIONS]-(:Item)-[:FROM_SOURCE]->(s:Source)
                  <-[:FROM_SOURCE]-(:Item)-[:MENTIONS]->(f2:{concept_label})
            WHERE f1 <> f2
            WITH f1, f2, count(DISTINCT s) AS weight
            WITH gds.graph.project(
                '{graph_name}',
                f1,
                f2,
                {{relationshipProperties: {{weight: weight}}}}
            ) AS g
            RETURN g.nodeCount AS nodeCount, g.relationshipCount AS relationshipCount
        """)

    record = result.single()
    return (record["nodeCount"], record["relationshipCount"])


def _run_pagerank(session: Any, graph_name: str, concept_label: str) -> None:
    """Executes PageRank and persists in nodes."""
    session.run(f"""
        CALL gds.pageRank.stream('{graph_name}')
        YIELD nodeId, score
        WITH gds.util.asNode(nodeId) AS node, score
        WHERE '{concept_label}' IN labels(node)
        SET node.pagerank = score
    """)


def _run_betweenness(session: Any, graph_name: str, concept_label: str) -> None:
    """Executes Betweenness Centrality and persists in nodes."""
    session.run(f"""
        CALL gds.betweenness.stream('{graph_name}')
        YIELD nodeId, score
        WITH gds.util.asNode(nodeId) AS node, score
        WHERE '{concept_label}' IN labels(node)
        SET node.betweenness = score
    """)


def _run_louvain(session: Any, graph_name: str, concept_label: str) -> None:
    """Executes Louvain (community detection) and persists in nodes."""
    session.run(f"""
        CALL gds.louvain.stream('{graph_name}')
        YIELD nodeId, communityId
        WITH gds.util.asNode(nodeId) AS node, communityId
        WHERE '{concept_label}' IN labels(node)
        SET node.community = communityId
    """)
