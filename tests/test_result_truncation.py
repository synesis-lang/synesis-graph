"""The server caps a response, and ignoring that cap corrupted the metrics.

ArcadeDB returns at most 20,000 rows per response and flags the rest with
`"truncated": true`. The word `truncated` appeared nowhere in this codebase, so
the flag was never read.

What that cost, measured on the real corpus: `algo.pagerank()` runs over the
whole graph — it accepts no scope filter — and yielded 272,193 rows for 22,585
concepts. The first 20,000 were taken for the whole answer, so **2,585 concepts
(11.4% of the ontology) ended up with no PageRank and no betweenness**, while
the terminal printed `[OK] PageRank calculated (20000 nodes)`.

That is worse than a crash. A researcher asking "which concepts are most
central?" would have received a ranking that silently omitted a ninth of the
ontology, inside a run that reported success.

Two defences, and the tests below pin both:

1. The client refuses a truncated response instead of passing off a fragment as
   the whole answer.
2. Readers that can exceed the cap page through it.

**Filtering stays on the client**, and that is a template constraint, not a
preference: the concept label comes from each project's template (`Chain` here,
`Concept` elsewhere), as do the taxonomy labels. A server-side filter naming
types would break every other project. The tempting agnostic-looking filter,
`node.name IS NOT NULL`, was measured and is wrong: it matched 22,623 rows —
the concepts plus 38 taxonomy vertices.
"""

from __future__ import annotations

from typing import Any

import pytest


class _CappedServer:
    """A server that caps every response and flags it, like the real one."""

    def __init__(self, rows: list[dict[str, Any]], cap: int = 20_000) -> None:
        self.rows = rows
        self.cap = cap
        self.statements: list[str] = []

    def _page(self, statement: str) -> list[dict[str, Any]]:
        import re

        skip = int(m.group(1)) if (m := re.search(r"SKIP (\d+)", statement)) else 0
        limit = int(m.group(1)) if (m := re.search(r"LIMIT (\d+)", statement)) else len(self.rows)
        return self.rows[skip : skip + limit]

    def command(self, statement: str, params=None, **kw):
        self.statements.append(statement)
        page = self._page(statement)
        if len(page) > self.cap:
            from synesis_graph.arcadedb_client import ArcadeDBError

            raise ArcadeDBError(
                "The server returned only part of the answer",
                detail=f"{self.cap} rows, capped at {self.cap}.",
            )
        return page

    query = command


# ---------------------------------------------------------------------------
# The client refuses a half-answer
# ---------------------------------------------------------------------------


def test_a_truncated_response_is_refused(monkeypatch):
    """Returning the fragment silently is what caused the whole defect."""
    import urllib.request

    from synesis_graph.arcadedb_client import ArcadeDBClient, ArcadeDBError

    class _Resp:
        headers: dict[str, str] = {}

        def read(self):
            import json

            return json.dumps(
                {"result": [{"n": 1}] * 20_000, "limit": 20_000, "returned": 20_000,
                 "truncated": True}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    client = ArcadeDBClient(uri="http://x", user="u", password="p", database="d")

    with pytest.raises(ArcadeDBError) as excinfo:
        client.query("SELECT FROM Chain")

    assert "part of the answer" in str(excinfo.value)
    assert "20000" in str(excinfo.value.detail)


def test_a_complete_response_passes_through(monkeypatch):
    """The guard must not fire on the ordinary case."""
    import urllib.request

    from synesis_graph.arcadedb_client import ArcadeDBClient

    class _Resp:
        headers: dict[str, str] = {}

        def read(self):
            import json

            return json.dumps(
                {"result": [{"n": 1}], "limit": 20_000, "returned": 1, "truncated": False}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    client = ArcadeDBClient(uri="http://x", user="u", password="p", database="d")

    assert client.query("SELECT FROM Chain") == [{"n": 1}]


# ---------------------------------------------------------------------------
# Paging past the cap
# ---------------------------------------------------------------------------


def _algo_rows(n: int, concepts: int) -> list[dict[str, Any]]:
    """`n` rows from the whole graph, of which the first `concepts` are concepts."""
    return [{"node": f"#10:{i}", "value": 1.0 / (i + 1)} for i in range(n)]


def test_every_row_is_read_even_far_beyond_the_cap(monkeypatch):
    """272,193 rows on the real corpus; a single request would see 20,000."""
    from synesis_graph import metrics_arcadedb as mod

    server = _CappedServer(_algo_rows(45_000, 45_000))
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        mod, "_persist_scores", lambda c, label, rows, prop: seen.setdefault("n", len(rows))
    )

    mod._run_algorithm(
        server, "Chain", "CALL algo.pagerank() YIELD node, score", "score", "pagerank"
    )

    assert seen["n"] == 45_000, "no row may be dropped at a page boundary"


def test_paging_loses_and_duplicates_nothing(monkeypatch):
    """A boundary bug would show as a repeated or missing RID, not as an error."""
    from synesis_graph import metrics_arcadedb as mod

    server = _CappedServer(_algo_rows(45_000, 45_000))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        mod, "_persist_scores", lambda c, label, rows, prop: captured.setdefault("rows", rows)
    )

    mod._run_algorithm(
        server, "Chain", "CALL algo.pagerank() YIELD node, score", "score", "pagerank"
    )

    nodes = [r["node"] for r in captured["rows"]]
    assert len(nodes) == len(set(nodes)), "a row was returned on two pages"
    assert nodes == [f"#10:{i}" for i in range(45_000)], "order and completeness"


def test_a_short_result_makes_a_single_request(monkeypatch):
    """Below the page size there is nothing to page, and every page recomputes."""
    from synesis_graph import metrics_arcadedb as mod

    server = _CappedServer(_algo_rows(10, 10))
    monkeypatch.setattr(mod, "_persist_scores", lambda *a: 0)

    mod._run_algorithm(
        server, "Chain", "CALL algo.pagerank() YIELD node, score", "score", "pagerank"
    )

    assert len(server.statements) == 1


def test_a_server_with_a_lower_cap_is_adapted_to(monkeypatch):
    """The page size is this module's guess; the cap is the server's to declare."""
    from synesis_graph import metrics_arcadedb as mod

    server = _CappedServer(_algo_rows(12_000, 12_000), cap=5_000)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        mod, "_persist_scores", lambda c, label, rows, prop: seen.setdefault("n", len(rows))
    )

    mod._run_algorithm(
        server, "Chain", "CALL algo.pagerank() YIELD node, score", "score", "pagerank"
    )

    assert seen["n"] == 12_000, "every row must still arrive, at a smaller page size"


def test_an_unrelated_error_is_not_mistaken_for_a_cap(monkeypatch):
    """Halving the page size would never fix a syntax error, only hide it."""
    from synesis_graph import metrics_arcadedb as mod

    calls = {"n": 0}

    class _Broken:
        def command(self, statement, params=None, **kw):
            calls["n"] += 1
            from synesis_graph.arcadedb_client import ArcadeDBError

            raise ArcadeDBError("Command text is null", status=500)

    from synesis_graph.arcadedb_client import ArcadeDBError

    with pytest.raises(ArcadeDBError):
        mod._run_algorithm(
            _Broken(), "Chain", "CALL algo.pagerank() YIELD node, score", "score", "pagerank"
        )

    assert calls["n"] == 1, "a rejected statement must not be retried at all"


# ---------------------------------------------------------------------------
# Staying template-agnostic
# ---------------------------------------------------------------------------


def test_the_concept_label_is_never_hardcoded():
    """Each project's template names its own concept and taxonomy vertices.

    `Chain` is one project's label; another yields `Concept` or `Factor`. A
    filter naming a type in Cypher would work for the corpus it was written
    against and silently mis-scope every other one.
    """
    import inspect

    from synesis_graph import metrics_arcadedb as mod

    src = inspect.getsource(mod)
    for label in ("Chain", "Concept)", "Topic", "Item", "Source"):
        assert f":{label}" not in src, f"{label} is a template's choice, not a constant"


def test_scores_are_written_only_to_the_concept_label(monkeypatch):
    """The client-side filter is what keeps Items and taxonomies clean.

    Verified against the real server too: after the fix, 22,585 of 22,585
    concepts carried a score and zero Items or Topics did.
    """
    from synesis_graph.metrics_arcadedb import _persist_scores

    class _Client:
        def __init__(self):
            self.written: list[dict[str, Any]] = []

        def query(self, statement, params=None, **kw):
            # Only two of the four RIDs belong to the concept type.
            return [{"@rid": "#10:0", "name": "c0"}, {"@rid": "#10:1", "name": "c1"}]

        def command(self, statement, params=None, **kw):
            if params:
                self.written = params["rows"]
            return []

    client = _Client()
    rows = [
        {"node": "#10:0", "value": 0.5},
        {"node": "#10:1", "value": 0.4},
        {"node": "#33:7", "value": 0.9},  # an Item
        {"node": "#41:2", "value": 0.8},  # a Topic
    ]

    written = _persist_scores(client, "Chain", rows, "pagerank")

    assert written == 2
    assert {r["name"] for r in client.written} == {"c0", "c1"}
