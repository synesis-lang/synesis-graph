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
from collections.abc import Sequence
from typing import Any

from synesis_graph.arcadedb_client import ArcadeDBError
from synesis_graph.arcadedb_transport import ArcadeDBTransport
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
#: Written into `ProjectContext`, where the audience is a program (or an
#: assistant) about to rank concepts by these scores and needing to know what
#: they actually measure. Precision matters more than brevity here.
SCOPE_NOTE = (
    "ArcadeDB's algo.* procedures run over the whole graph and accept no scope "
    "filter, while Neo4j's GDS projects only the concept subgraph. Scores therefore "
    "reflect Item/Source/taxonomy edges too and are not directly comparable."
)

#: The same caveat for the terminal, where the audience is the researcher who
#: just ran the command. They do not need to know what `algo.*` is; they need to
#: know that these numbers describe this graph and should not be compared with
#: numbers from the Neo4j export of the same project.
SCOPE_NOTE_SHORT = (
    "Centrality here counts every connection in the graph, including those to "
    "excerpts and sources. Numbers from a Neo4j export of the same project are "
    "calculated differently and are not directly comparable."
)


class _CypherRunner:
    """Adapts an ArcadeDBTransport to the `session.run(query, **params)` interface.

    The native metric functions were written against a Neo4j session and only call
    `.run()`, so dressing the client in that shape reuses them instead of copying
    their Cypher.
    """

    def __init__(self, client: ArcadeDBTransport):
        self._client = client

    def run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        return self._client.command(query, params or None)


#: Seconds per 100,000 vertices, per algorithm, measured end to end against the
#: Hostinger container (2 vCPUs) on a 272,193-vertex graph: PageRank 8.5s,
#: betweenness 109s. Rounded up rather than fitted — the point is to tell a
#: researcher whether to wait or come back later, and an estimate that runs
#: short reads as a stall.
#:
#: Louvain is not listed because it has not been measured to completion; it is
#: estimated at betweenness's rate, which is the pessimistic assumption.
_SECONDS_PER_100K = {"PageRank": 4.0, "Betweenness": 45.0, "Communities (Louvain)": 45.0}

#: Which metrics each `--metrics` choice runs. `all` is the default because the
#: numbers are research output, not diagnostics: centrality is what answers
#: "which concepts hold this corpus together". `fast` exists for an exploratory
#: re-export where the wait is not worth it, and drops only the expensive pair.
METRIC_SETS: dict[str, tuple[str, ...]] = {
    "all": ("PageRank", "Betweenness", "Communities (Louvain)"),
    "fast": ("PageRank",),
    "none": (),
}


def estimate_metric_seconds(vertex_count: int, names: Sequence[str]) -> float:
    """Rough wall-clock cost of running `names` over a graph of this size.

    Deliberately crude. It exists so the researcher can decide whether to wait,
    and for that a figure within a factor of two is enough — while a
    precise-looking number that proves wrong costs more trust than an admitted
    approximation. The rates come from one server; a slower box will take
    longer, which is why the output is phrased as "about".
    """
    per_100k = sum(_SECONDS_PER_100K.get(n, 0.0) for n in names)
    return per_100k * max(vertex_count, 0) / 100_000


def _announce_metric_cost(
    reporter: TaskReporter, vertex_count: int, names: Sequence[str]
) -> None:
    """Says what the expensive step will cost, before the silence starts.

    Centrality over a large graph is minutes of no output, and the researcher
    has no way to tell that from a hang. Telling them first turns waiting into a
    decision — and names `--metrics fast` as the way out, so the choice is real.
    """
    if not names:
        return
    seconds = estimate_metric_seconds(vertex_count, names)
    if seconds < 60:
        return
    reporter.info(
        f"Calculating {', '.join(names)} over {vertex_count:,} vertices. "
        f"This usually takes about {round(seconds / 60)} minute(s), with no "
        "output until it finishes. Use --metrics fast to skip the slow ones."
    )


def _count_vertices(client: ArcadeDBTransport) -> int:
    """Total vertices, which is what the `algo.*` procedures actually traverse.

    Not the concept count: these procedures run over the whole graph, so an
    estimate based on the ontology alone would be wrong by the ratio of the
    corpus to it — a factor of twelve on the measured project.
    """
    try:
        rows = client.query("MATCH (n) RETURN count(n) AS n")
        return int(rows[0].get("n", 0)) if rows else 0
    except ArcadeDBError:
        # Only feeds an estimate; failing to produce one must not stop the run.
        return 0


def compute_metrics(
    client: ArcadeDBTransport,
    payload: GraphPayload,
    reporter: TaskReporter,
    metrics: str = "all",
) -> None:
    """Calculates and persists graph metrics.

    Native metrics always run: they are plain Cypher over the concepts and cost
    seconds. Advanced ones are attempted individually, so a failure in one does
    not cost the others — the same tolerance the Neo4j path applies to GDS.

    `metrics` selects which advanced metrics run (see `METRIC_SETS`). It defaults
    to `all` because these are research output rather than diagnostics; the
    researcher can trade them for time, but never by accident.
    """
    concept_label = payload.concept_label
    if not validate_cypher_label(concept_label):
        return

    session = _CypherRunner(client)

    with reporter.step("Measuring the graph"):
        _compute_native_concept_metrics(session, concept_label)
        _compute_native_taxonomy_metrics(session, concept_label, payload.graph_fields)
        _compute_native_source_metrics(session, concept_label)

    selected = METRIC_SETS.get(metrics, METRIC_SETS["all"])
    if not selected:
        reporter.info(
            "Skipping centrality and communities (--metrics none). The graph is "
            "complete; only these scores are absent."
        )
        return

    _announce_metric_cost(reporter, _count_vertices(client), selected)

    runners = {
        "PageRank": _run_pagerank,
        "Betweenness": _run_betweenness,
        "Communities (Louvain)": _run_louvain,
    }
    with reporter.step("Finding central concepts and communities"):
        for label in selected:
            try:
                updated = runners[label](client, concept_label)
                reporter.success(f"{label} calculated ({updated} nodes)")
            except ArcadeDBError as e:
                reporter.warning(f"{label} failed: {e}")

    reporter.info(SCOPE_NOTE_SHORT)


# ---------------------------------------------------------------------------
# Advanced metrics (algo.* — no plugin required)
# ---------------------------------------------------------------------------
def _run_pagerank(client: ArcadeDBTransport, concept_label: str) -> int:
    return _run_algorithm(
        client, concept_label, "CALL algo.pagerank() YIELD node, score", "score", "pagerank"
    )


def _run_betweenness(client: ArcadeDBTransport, concept_label: str) -> int:
    return _run_algorithm(
        client,
        concept_label,
        "CALL algo.betweenness({normalized:true}) YIELD node, score",
        "score",
        "betweenness",
    )


def _run_louvain(client: ArcadeDBTransport, concept_label: str) -> int:
    return _run_algorithm(
        client,
        concept_label,
        "CALL algo.louvain() YIELD node, communityId",
        "communityId",
        "community",
    )


def _run_algorithm(
    client: ArcadeDBTransport,
    concept_label: str,
    call: str,
    yield_field: str,
    property_name: str,
) -> int:
    """Runs one algo.* procedure and persists its result on the concept nodes.

    Returns the number of nodes updated, which is what makes the silent-failure mode
    detectable — see `_persist_scores`.

    **These procedures run over the whole graph.** They accept no scope filter,
    so on a real corpus `algo.pagerank()` yielded 272,193 rows — every Item and
    Source included — against a server that caps a response at 20,000 and flags
    the rest as `truncated`. That flag was not read, so the first 20,000 rows
    were taken for the whole answer: 2,585 of 22,585 concepts ended up with no
    PageRank and no betweenness, while the terminal reported `[OK] PageRank
    calculated (20000 nodes)`. A plausible number, in a run that claimed success.

    **One request, with the cap raised.** The 20,000 is a per-request default,
    not a server limit, so asking for more lifts it without touching the server's
    configuration or affecting any other client. That matters because each read
    of a procedure's output *re-runs the procedure*: paging betweenness over this
    corpus measured about 34 minutes across fourteen recomputations, against
    1m49s for the single request that asks for all of it.

    Paging remains as the fallback, for a deployment that caps responses below
    what is asked for. It is correct but expensive, so it is not the normal path.

    Filtering to concepts stays on the client, in `_persist_scores`. Doing it in
    the query would mean naming vertex types in Cypher, and the labels differ per
    project — the concept label comes from the template (`Chain` in one project,
    `Concept` in another), and so do the taxonomy labels. A server-side filter on
    `node.name IS NOT NULL` was measured and is wrong for exactly that reason: it
    matched 22,623 rows, the 22,585 concepts plus 38 taxonomy vertices.
    """
    statement = f"{call} RETURN node, {yield_field} AS value"
    try:
        rows = client.command(statement, limit=_ALGO_RESULT_LIMIT)
    except ArcadeDBError as e:
        if "part of the answer" not in str(e):
            raise
        # The server would not honour the raised cap. Correctness first: pay the
        # recomputations rather than persist a fraction of the scores.
        logger.warning(
            "This server caps responses below %d rows; reading the results in "
            "pages instead, which re-runs the algorithm once per page.",
            _ALGO_RESULT_LIMIT,
        )
        rows = _read_in_pages(client, statement)
    return _persist_scores(client, concept_label, rows, property_name)


def _read_in_pages(client: ArcadeDBTransport, statement: str) -> list[dict[str, Any]]:
    """Reads a procedure's whole output in pages, adapting to the server's cap.

    The expensive path, and the fallback rather than the default: every page
    re-runs the procedure that produced it. `SKIP`/`LIMIT` over procedure output
    is verified stable on the real server — the same page returns the same RIDs
    across independent runs — which is what makes paging safe at all.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    page_size = _ALGO_PAGE_SIZE
    while True:
        try:
            page = client.command(f"{statement} SKIP {offset} LIMIT {page_size}")
        except ArcadeDBError as e:
            # A server configured with a lower response cap than this page size
            # truncates it, and the client refuses a truncated answer rather than
            # passing off a fragment as the whole. Halving finds whatever cap the
            # server actually has, instead of failing on a number guessed here.
            if "part of the answer" not in str(e) or page_size <= _MIN_ALGO_PAGE_SIZE:
                raise
            page_size //= 2
            logger.debug("Response cap below %d; retrying at %d", page_size * 2, page_size)
            continue
        rows.extend(page)
        if len(page) < page_size:
            return rows
        # `page_size`, not `_ALGO_PAGE_SIZE`: after an adaptive halving the two
        # differ, and advancing by the constant would skip everything between
        # what was read and where the next page starts — losing rows silently,
        # which is the same failure this whole function exists to end.
        offset += page_size


#: Rows per page when reading a whole type back. Comfortably under the server's
#: 20,000-row response cap, with room for a server configured lower.
_SELECT_PAGE_SIZE = 5_000

#: Row cap asked for when reading an `algo.*` procedure's output in one request.
#:
#: Deliberately far above any corpus this tool has produced: the measured one
#: yields 272,193 rows (every vertex in the graph, not just the concepts), and
#: the cost of asking for more than arrives is nil — the server returns what it
#: has. Asking too little, by contrast, means either a refused truncation or a
#: fall back to paging, which re-runs the algorithm once per page.
_ALGO_RESULT_LIMIT = 5_000_000

#: Rows per page when reading an `algo.*` procedure's output in the fallback.
#:
#: Larger than `_SELECT_PAGE_SIZE`, and deliberately so: **every page re-runs the
#: algorithm**. Measured on the real corpus, the procedure itself costs ~5.2s
#: (aggregating to one row takes just as long as returning 20,000), while the
#: rows themselves are almost free. So the page count, not the page size, is what
#: costs — 272,193 rows at 10,000 per page is 28 recomputations, at 20,000 it is
#: 14, and the time roughly halves.
#:
#: Not raised further: the server caps a response at 20,000 by default, and a
#: page above that limit comes back flagged `truncated`, which the client now
#: refuses. Sitting exactly at the cap keeps the cost down without depending on
#: a server configured more generously than the default.
_ALGO_PAGE_SIZE = 20_000

#: Floor for the adaptive page size. Below this, the number of recomputations
#: would cost more than the run is worth, and a server capping responses this low
#: is better reported than worked around.
_MIN_ALGO_PAGE_SIZE = 1_000


def _select_all(client: ArcadeDBTransport, statement: str) -> list[dict[str, Any]]:
    """Reads every row of `statement`, page by page.

    ArcadeDB caps a response at 20,000 rows and flags it with `truncated`. A
    single `SELECT` over a real ontology quietly returned exactly that many and
    the rest were never seen: 2,585 of 22,585 concepts ended up with no PageRank
    and no betweenness, while the run reported success. The client now refuses a
    truncated response outright, so the fix here is to stop producing one.

    `SKIP`/`LIMIT` needs a stable order to page safely — without one the server
    may return the same row on two pages and omit another. `@rid` is unique,
    immutable for a stored record, and already indexed.
    """
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = client.query(
            f"{statement} ORDER BY @rid SKIP {offset} LIMIT {_SELECT_PAGE_SIZE}",
            language="sql",
        )
        rows.extend(page)
        if len(page) < _SELECT_PAGE_SIZE:
            return rows
        offset += _SELECT_PAGE_SIZE


def _persist_scores(
    client: ArcadeDBTransport,
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
        for r in _select_all(client, f"SELECT @rid, name FROM {concept_label}")
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
