"""What a metric will cost, and whether it is allowed to finish.

The estimate used to be linear in vertices and to carry rates from one server to
every other. On the real corpus it announced "about 4 minutes" for work that took
eleven, and two of three metrics exceeded a timeout the researcher had been told
was ample. These pin the model against the measurements that exposed it.
"""

from synesis_graph.metrics_arcadedb import (
    _COST_EXPONENT,
    _REFERENCE_EDGES,
    _REFERENCE_VERTICES,
    _warn_if_over_budget,
    calibration_from,
    estimate_metric_seconds,
)

# Measured end to end on the reference corpus (275,687 vertices, 820,388 edges).
MEASURED = {
    "PageRank": 8.5,
    "Betweenness": 109.0,
    "Communities (Louvain)": 640.0,
}


class Reporter:
    def __init__(self):
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def warning(self, text):
        self.warnings.append(text)

    def info(self, text):
        self.infos.append(text)


def test_every_estimate_lands_near_what_was_actually_measured():
    """Within 50%. It was out by 5x for Louvain before the rate was measured."""
    for name, actual in MEASURED.items():
        predicted = estimate_metric_seconds(
            _REFERENCE_VERTICES, [name], edge_count=_REFERENCE_EDGES
        )
        assert 0.5 < predicted / actual < 1.5, f"{name}: {predicted:.0f}s vs {actual:.0f}s"


def test_the_expensive_pair_is_not_modelled_as_linear():
    """Betweenness is O(V·E) and Louvain O(E·log V); neither is linear.

    A linear model is what made a graph 1.3% larger cross from finishing to
    timing out with no warning.
    """
    assert _COST_EXPONENT["Betweenness"] > 1.0
    assert _COST_EXPONENT["Communities (Louvain)"] > 1.0
    assert _COST_EXPONENT["PageRank"] == 1.0


def test_doubling_the_graph_more_than_doubles_the_expensive_metrics():
    for name in ("Betweenness", "Communities (Louvain)"):
        one = estimate_metric_seconds(_REFERENCE_VERTICES, [name],
                                      edge_count=_REFERENCE_EDGES)
        two = estimate_metric_seconds(_REFERENCE_VERTICES * 2, [name],
                                      edge_count=_REFERENCE_EDGES * 2)
        assert two > one * 2.05, f"{name} scales linearly"


def test_a_denser_graph_costs_more_at_the_same_vertex_count():
    """These algorithms traverse edges; vertices alone misread density."""
    sparse = estimate_metric_seconds(100_000, ["Betweenness"], edge_count=200_000)
    dense = estimate_metric_seconds(100_000, ["Betweenness"], edge_count=800_000)
    assert dense > sparse * 3


def test_a_slow_server_scales_every_later_estimate():
    """PageRank taking twice as long means the rest will too."""
    expected = estimate_metric_seconds(
        _REFERENCE_VERTICES, ["PageRank"], edge_count=_REFERENCE_EDGES
    )
    slow = calibration_from(expected * 2, _REFERENCE_VERTICES, _REFERENCE_EDGES)
    assert slow is not None and 1.9 < slow < 2.1

    plain = estimate_metric_seconds(
        _REFERENCE_VERTICES, ["Betweenness"], edge_count=_REFERENCE_EDGES
    )
    scaled = estimate_metric_seconds(
        _REFERENCE_VERTICES, ["Betweenness"],
        edge_count=_REFERENCE_EDGES, calibration=slow,
    )
    assert 1.9 < scaled / plain < 2.1


def test_calibration_declines_to_guess_from_nothing():
    assert calibration_from(0.0, _REFERENCE_VERTICES, _REFERENCE_EDGES) is None
    assert calibration_from(5.0, 0, 0) is None


def test_work_that_cannot_finish_is_announced_before_it_starts():
    """The whole point: say it beforehand instead of after the budget is spent."""
    reporter = Reporter()
    _warn_if_over_budget(
        reporter,
        _REFERENCE_VERTICES,
        ["Communities (Louvain)"],
        edge_count=_REFERENCE_EDGES,
        timeout=300.0,  # the default that this metric exceeded on the real run
    )
    assert len(reporter.warnings) == 1
    text = reporter.warnings[0]
    assert "Communities (Louvain)" in text
    # Must name a way out, not just report doom.
    assert "timeout" in text and "--metrics fast" in text


def test_work_that_fits_is_not_warned_about():
    reporter = Reporter()
    _warn_if_over_budget(
        reporter,
        _REFERENCE_VERTICES,
        ["PageRank"],
        edge_count=_REFERENCE_EDGES,
        timeout=300.0,
    )
    assert reporter.warnings == []


def test_no_timeout_means_no_budget_to_exceed():
    reporter = Reporter()
    _warn_if_over_budget(
        reporter, _REFERENCE_VERTICES, list(MEASURED), edge_count=_REFERENCE_EDGES
    )
    assert reporter.warnings == []


def test_the_suggested_timeout_actually_covers_the_work():
    """A suggestion that still times out would be worse than none."""
    reporter = Reporter()
    _warn_if_over_budget(
        reporter,
        _REFERENCE_VERTICES,
        ["Communities (Louvain)"],
        edge_count=_REFERENCE_EDGES,
        timeout=300.0,
    )
    import re

    match = re.search(r"timeout = (\d+)", reporter.warnings[0])
    assert match, reporter.warnings[0]
    suggested = int(match.group(1))
    cost = estimate_metric_seconds(
        _REFERENCE_VERTICES, ["Communities (Louvain)"], edge_count=_REFERENCE_EDGES
    )
    assert suggested > cost
