"""The researcher decides what the metrics cost, and knows it in advance.

Centrality is research output, not diagnostics — it answers which concepts hold
a corpus together — so the default computes everything. But on a real graph that
is minutes of silence, and a wait nobody warned about is indistinguishable from
a hang.

Three things follow, and they are what these tests pin:

1. **One request instead of fourteen.** The 20,000-row response cap is a
   per-request default, not a server limit, so asking for more lifts it without
   touching the server. That matters because reading a procedure's output
   re-runs the procedure: betweenness measured 1m49s asking for everything at
   once, against roughly 34 minutes paged.
2. **The cost is announced first**, with a rough estimate and the flag that
   avoids it.
3. **`--metrics all|fast|none`** puts the trade-off in the researcher's hands,
   with `all` as the default so the numbers are never lost by accident.
"""

from __future__ import annotations

import inspect

import pytest

from synesis_graph.metrics_arcadedb import (
    METRIC_SETS,
    compute_metrics,
    estimate_metric_seconds,
)


class _Reporter:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def info(self, m):
        self.messages.append(("info", m))

    def warning(self, m):
        self.messages.append(("warning", m))

    def error(self, m):
        self.messages.append(("error", m))

    def success(self, m):
        self.messages.append(("success", m))

    def step(self, _label):
        class _S:
            def __enter__(self_inner):
                return self_inner

            def fail(self_inner, detail=""):
                pass

            def __exit__(self_inner, *exc):
                return False

        return _S()

    @property
    def text(self) -> str:
        return " ".join(m for _, m in self.messages)


class _Client:
    """Answers the vertex count and every algo call."""

    def __init__(self, vertices: int = 300_000) -> None:
        self.vertices = vertices
        self.limits: list[int | None] = []
        self.algo_calls: list[str] = []

    def command(self, statement, params=None, *, language="cypher", database=None, limit=None):
        if "CALL algo." in statement:
            self.algo_calls.append(statement)
            self.limits.append(limit)
            return [{"node": "#10:0", "value": 1.0}]
        return []

    def query(self, statement, params=None, *, language="cypher", database=None, limit=None):
        if "count(n)" in statement:
            return [{"n": self.vertices}]
        if "@rid" in statement:
            return [{"@rid": "#10:0", "name": "c0"}]
        return []


# ---------------------------------------------------------------------------
# (A) One request, with the cap raised
# ---------------------------------------------------------------------------


def test_the_algorithm_output_is_read_in_one_request(minimal_payload):
    """Every extra read re-runs the algorithm; that is the whole cost."""
    client = _Client()

    compute_metrics(client, minimal_payload, _Reporter(), metrics="fast")

    assert len(client.algo_calls) == 1
    assert "SKIP" not in client.algo_calls[0], "paging is the fallback, not the default"


def test_the_request_asks_for_more_than_the_default_cap(minimal_payload):
    """Without this the server returns 20,000 rows and flags the rest truncated."""
    from synesis_graph.metrics_arcadedb import _ALGO_RESULT_LIMIT

    client = _Client()

    compute_metrics(client, minimal_payload, _Reporter(), metrics="fast")

    assert client.limits == [_ALGO_RESULT_LIMIT]
    assert _ALGO_RESULT_LIMIT > 272_193, "must exceed the largest measured result"


def test_every_transport_accepts_the_limit_argument():
    """The Protocol and both implementations must agree, or one silently caps.

    Checked as a contract rather than through behaviour: a transport missing the
    argument fails only on the code path that passes it, which is exactly the
    expensive one nobody runs in a unit test.
    """
    from synesis_graph.arcadedb_client import ArcadeDBClient
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.arcadedb_transport import ArcadeDBTransport

    for cls in (ArcadeDBTransport, ArcadeDBClient, ArcadeDBEmbeddedClient):
        for method in ("command", "query"):
            params = inspect.signature(getattr(cls, method)).parameters
            assert "limit" in params, f"{cls.__name__}.{method} is missing `limit`"


# ---------------------------------------------------------------------------
# (B) Paging survives as the fallback
# ---------------------------------------------------------------------------


def test_a_server_that_refuses_the_raised_cap_falls_back_to_paging(minimal_payload):
    """Correctness first: pay the recomputations rather than persist a fragment."""
    from synesis_graph.arcadedb_client import ArcadeDBError

    class _Capped(_Client):
        def command(self, statement, params=None, *, language="cypher", database=None, limit=None):
            if "CALL algo." in statement and limit is not None:
                raise ArcadeDBError("The server returned only part of the answer")
            return super().command(
                statement, params, language=language, database=database, limit=limit
            )

    client = _Capped()

    compute_metrics(client, minimal_payload, _Reporter(), metrics="fast")

    assert any("SKIP" in c for c in client.algo_calls), "must page when the cap stands"


# ---------------------------------------------------------------------------
# (C) The cost is announced before the silence
# ---------------------------------------------------------------------------


def test_a_slow_run_announces_its_cost(minimal_payload):
    reporter = _Reporter()

    compute_metrics(_Client(vertices=300_000), minimal_payload, reporter, metrics="all")

    assert "minute" in reporter.text
    assert "300,000 vertices" in reporter.text
    # The way out has to be named, or the estimate is just bad news.
    assert "--metrics fast" in reporter.text


def test_a_fast_run_says_nothing(minimal_payload):
    """Below a minute the warning would be noise."""
    reporter = _Reporter()

    compute_metrics(_Client(vertices=500), minimal_payload, reporter, metrics="all")

    assert "minute" not in reporter.text


def test_the_estimate_grows_with_the_graph():
    small = estimate_metric_seconds(10_000, METRIC_SETS["all"])
    large = estimate_metric_seconds(1_000_000, METRIC_SETS["all"])

    assert large > small * 50


def test_the_estimate_counts_every_vertex_not_just_concepts(minimal_payload):
    """`algo.*` traverses the whole graph — items and sources included.

    Estimating from the ontology alone was wrong by a factor of twelve on the
    measured project.
    """
    client = _Client(vertices=272_193)
    reporter = _Reporter()

    compute_metrics(client, minimal_payload, reporter, metrics="all")

    assert "272,193" in reporter.text


def test_a_failing_vertex_count_does_not_stop_the_run(minimal_payload):
    """The count only feeds an estimate. Losing it must not cost the metrics."""
    from synesis_graph.arcadedb_client import ArcadeDBError

    class _NoCount(_Client):
        def query(self, statement, params=None, *, language="cypher", database=None, limit=None):
            if "count(n)" in statement:
                raise ArcadeDBError("nope")
            return super().query(
                statement, params, language=language, database=database, limit=limit
            )

    client = _NoCount()

    compute_metrics(client, minimal_payload, _Reporter(), metrics="fast")

    assert len(client.algo_calls) == 1


# ---------------------------------------------------------------------------
# (D) The researcher chooses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "choice, expected",
    [("all", 3), ("fast", 1), ("none", 0)],
)
def test_each_choice_runs_what_it_promises(minimal_payload, choice, expected):
    client = _Client()

    compute_metrics(client, minimal_payload, _Reporter(), metrics=choice)

    assert len(client.algo_calls) == expected


def test_all_is_the_default_everywhere():
    """These are findings, not diagnostics: they must never be lost silently."""
    from synesis_graph.backends.base import BackendAdapter
    from synesis_graph.pipeline import run_pipeline

    assert inspect.signature(compute_metrics).parameters["metrics"].default == "all"
    assert inspect.signature(run_pipeline).parameters["metrics"].default == "all"
    assert BackendAdapter.metrics == "all"


def test_fast_keeps_pagerank_and_drops_the_expensive_pair():
    """PageRank is 4s per 100k vertices; the other two are about 45s each."""
    assert METRIC_SETS["fast"] == ("PageRank",)
    assert set(METRIC_SETS["all"]) - set(METRIC_SETS["fast"]) == {
        "Betweenness",
        "Communities (Louvain)",
    }


def test_skipping_says_the_graph_is_still_complete(minimal_payload):
    """`none` must not read as a broken export."""
    reporter = _Reporter()

    compute_metrics(_Client(), minimal_payload, reporter, metrics="none")

    assert "complete" in reporter.text


def test_an_unknown_choice_falls_back_to_all(minimal_payload):
    """A typo must not silently drop the numbers."""
    client = _Client()

    compute_metrics(client, minimal_payload, _Reporter(), metrics="nonsense")

    assert len(client.algo_calls) == 3


def test_the_cli_offers_the_flag_on_both_metric_backends():
    from click.testing import CliRunner

    from synesis_graph.cli import main

    for command in ("arcadedb", "arcadedb-embedded"):
        result = CliRunner().invoke(main, [command, "--help"])
        assert result.exit_code == 0, command
        flat = " ".join(result.output.split())
        assert "--metrics" in flat, command
        assert "[default: all]" in flat, command


def test_the_html_backend_has_no_metrics_flag():
    """It renders a file; there are no server-side algorithms to choose."""
    from click.testing import CliRunner

    from synesis_graph.cli import main

    result = CliRunner().invoke(main, ["html", "--help"])
    assert "--metrics" not in result.output
