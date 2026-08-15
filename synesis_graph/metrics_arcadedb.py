"""Graph metrics for the ArcadeDB backend.

Two layers, mirroring `metrics.py`:

- **Native metrics** (degree, mention_count, source_count, taxonomy counts) are plain
  Cypher and are reused from the Neo4j implementation unchanged.
- **Advanced metrics** (PageRank, betweenness, communities) replace Neo4j's `gds.*`
  with ArcadeDB's built-in `algo.*` library. No plugin is required, so the "GDS not
  installed" degradation that the Neo4j path warns about does not exist here.

Two ArcadeDB behaviours shape this module, both measured against a live server rather
than assumed. See `_persist_scores` and `SCOPE_NOTE`.
"""

from __future__ import annotations

import logging
from typing import Any

from synesis_graph.arcadedb_client import ArcadeDBClient, ArcadeDBError
from synesis_graph.core import GraphPayload
from synesis_graph.metrics import (
    _compute_native_concept_metrics,
    _compute_native_source_metrics,
    _compute_native_taxonomy_metrics,
)
from synesis_graph.sanitize import validate_cypher_label
from synesis_graph.ui import TaskReporter

logger = logging.getLogger("synesis2graph")

# Why the scores differ from Neo4j's, in one place so it can be quoted in the CLI,
# the CHANGELOG and the README without drifting.
SCOPE_NOTE = (
    "ArcadeDB's algo.* procedures run over the whole graph and accept no scope "
    "filter, while Neo4j's GDS projects only the concept subgraph. Scores therefore "
    "reflect Item/Source/taxonomy edges too and are not directly comparable."
)


class _CypherRunner:
    """Adapts ArcadeDBClient to the `session.run(query, **params)` interface.

    The native metric functions were written against a Neo4j session and only call
    `.run()`, so dressing the client in that shape reuses them instead of copying
    their Cypher.
    """

    def __init__(self, client: ArcadeDBClient):
        self._client = client

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        return self._client.command(query, params or None)


def compute_metrics(
    client: ArcadeDBClient, payload: GraphPayload, reporter: TaskReporter
) -> None:
    """Calculates and persists graph metrics.

    Native metrics always run. Advanced ones are attempted individually, so a failure
    in one does not cost the others — the same tolerance the Neo4j path applies to
    GDS.
    """
    concept_label = payload.concept_label
    if not validate_cypher_label(concept_label):
        return

    session = _CypherRunner(client)

    with reporter.step("Calculating Native Metrics"):
        _compute_native_concept_metrics(session, concept_label)
        _compute_native_taxonomy_metrics(session, concept_label, payload.graph_fields)
        _compute_native_source_metrics(session, concept_label)

    with reporter.step("Calculating Graph Algorithms"):
        for label, run in (
            ("PageRank", _run_pagerank),
            ("Betweenness", _run_betweenness),
            ("Communities (Louvain)", _run_louvain),
        ):
            try:
                updated = run(client, concept_label)
                reporter.success(f"{label} calculated ({updated} nodes)")
            except ArcadeDBError as e:
                reporter.warning(f"{label} failed: {e}")

    reporter.info(SCOPE_NOTE)


# ---------------------------------------------------------------------------
# Advanced metrics (algo.* — no plugin required)
# ---------------------------------------------------------------------------
def _run_pagerank(client: ArcadeDBClient, concept_label: str) -> int:
    return _run_algorithm(
        client, concept_label, "CALL algo.pagerank() YIELD node, score", "score", "pagerank"
    )


def _run_betweenness(client: ArcadeDBClient, concept_label: str) -> int:
    return _run_algorithm(
        client,
        concept_label,
        "CALL algo.betweenness({normalized:true}) YIELD node, score",
        "score",
        "betweenness",
    )


def _run_louvain(client: ArcadeDBClient, concept_label: str) -> int:
    return _run_algorithm(
        client,
        concept_label,
        "CALL algo.louvain() YIELD node, communityId",
        "communityId",
        "community",
    )


def _run_algorithm(
    client: ArcadeDBClient,
    concept_label: str,
    call: str,
    yield_field: str,
    property_name: str,
) -> int:
    """Runs one algo.* procedure and persists its result on the concept nodes.

    Returns the number of nodes updated, which is what makes the silent-failure mode
    detectable — see `_persist_scores`.
    """
    rows = client.command(f"{call} RETURN node, {yield_field} AS value")
    return _persist_scores(client, concept_label, rows, property_name)


def _persist_scores(
    client: ArcadeDBClient,
    concept_label: str,
    rows: list[dict[str, Any]],
    property_name: str,
) -> int:
    """Writes algorithm results onto the concept nodes.

    Neither obvious approach works, and both fail *quietly*:

    1. `CALL algo.pagerank() YIELD node, score SET node.pagerank = score` returns a
       plausible count, reports `stats: null`, and writes nothing. `YIELD node` is a
       serialized RID string (`"#1:0"`), not a bindable vertex.

    2. `... MATCH (c:Label) WHERE id(c) = id(node) SET c.pagerank = score` — the
       rebind this project first assumed — is worse than useless: `id()` of a string
       is not comparable to `id()` of a vertex, so the predicate degenerates and the
       MATCH becomes a cartesian product. Measured on a 3-concept graph with 4 items,
       it "updated" 7 nodes, writing concept scores onto Items.

    So the RID is resolved client-side: one SQL query maps `@rid` to the concept's
    `name`, which is the payload's unique key, and a single UNWIND writes the values
    back. Rows whose RID is not a concept are dropped here — that is the scope filter
    the algorithms themselves do not offer.
    """
    if not rows:
        return 0

    name_by_rid = {
        r["@rid"]: r["name"]
        for r in client.query(f"SELECT @rid, name FROM {concept_label}", language="sql")
        if r.get("@rid") and r.get("name")
    }

    updates = [
        {"name": name_by_rid[rid], "value": row.get("value")}
        for row in rows
        if (rid := row.get("node")) in name_by_rid and row.get("value") is not None
    ]
    if not updates:
        return 0

    client.command(
        f"UNWIND $rows AS row MATCH (c:{concept_label} {{name: row.name}}) "
        f"SET c.{property_name} = row.value",
        {"rows": updates},
    )
    return len(updates)
