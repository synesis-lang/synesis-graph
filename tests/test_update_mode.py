"""Etapa B: `--mode update` writes into the existing graph instead of replacing it.

Rebuild is always correct and stays the default. Update exists because a corpus
large enough to matter is expensive to rewrite -- the Quinto Andar corpus
(246,588 items) extrapolated to hours against a small server -- and most re-runs
change a fraction of it.

What update turns off is the clearing, in all three places it happens: the
ArcadeDB adapter's own `clear_destination` step, the `clear_database` inside
`sync_to_arcadedb`, and the one inside `sync_to_neo4j`. Missing any one of them
would wipe the graph an incremental run meant to keep, which is why the tests
below check each independently rather than only checking the end state of a
single path.

The sharp edge, and the reason for `_incompatible_with_update`: update does not
drop indexes, so it cannot apply a changed analyzer or embedding model. Letting
it through would leave the graph declaring one capability while answering under
another. It refuses instead, and says what to do.

Deletion is out of scope until Etapa E: update adds and changes, never removes.
That is a real gap, so it is announced at runtime rather than left to be
discovered.
"""

from __future__ import annotations

from typing import Any

import pytest

from synesis_graph.backends.arcadedb import incompatible_with_update, read_project_context
from synesis_graph.core import ProjectContextSpec


class _RecordingTx:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, query: str, **params: Any) -> Any:
        self.queries.append(query)
        return []

    @property
    def text(self) -> str:
        return "\n".join(self.queries)


def _context(**overrides: Any) -> ProjectContextSpec:
    base: dict[str, Any] = {
        "project_name": "P",
        "description": "d",
        "concept_label": "Concept",
        "template_name": "t",
        "template_doc": "",
        "project_summary": "",
        "compiler_version": "1",
        "synesis_graph_version": "1",
        "compiled_at": "",
        "generated_at": "",
        "source_count": 0,
        "item_count": 0,
        "concept_count": 0,
    }
    base.update(overrides)
    return ProjectContextSpec(**base)


# ---------------------------------------------------------------------------
# The clearing, in each of the three places it lives
# ---------------------------------------------------------------------------


def test_sync_to_neo4j_clears_in_rebuild_and_not_in_update(monkeypatch):
    from synesis_graph.backends import neo4j as mod
    from tests.conftest import _make_payload

    cleared: list[str] = []
    monkeypatch.setattr(mod, "clear_database", lambda s: cleared.append("cleared"))
    monkeypatch.setattr(mod, "_create_constraints", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_create_search_indexes", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_execute_sync_transaction", lambda *a, **k: None)

    assert mod.sync_to_neo4j(object(), _make_payload(), mode="rebuild") is None
    assert cleared == ["cleared"]

    assert mod.sync_to_neo4j(object(), _make_payload(), mode="update") is None
    assert cleared == ["cleared"], "update must not wipe the graph"


def test_sync_to_arcadedb_clears_in_rebuild_and_not_in_update(monkeypatch):
    """The second clear, inside the sync itself.

    ArcadeDB clears twice on a rebuild -- here and in the adapter's own step.
    Turning off only the adapter's would leave this one wiping the graph.
    """
    from synesis_graph.backends import arcadedb as mod
    from tests.conftest import _make_payload

    cleared: list[str] = []
    monkeypatch.setattr(mod, "clear_database", lambda c: cleared.append("cleared"))
    monkeypatch.setattr(mod, "_create_schema", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_create_constraints", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_create_search_indexes", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_execute_sync_transaction", lambda *a, **k: None)

    assert mod.sync_to_arcadedb(object(), _make_payload(), mode="rebuild") is None
    assert cleared == ["cleared"]

    assert mod.sync_to_arcadedb(object(), _make_payload(), mode="update") is None
    assert cleared == ["cleared"], "update must not wipe the graph"


def test_arcadedb_adapter_skips_its_clear_step_in_update():
    """The third clear: the pipeline step, before the sync runs at all."""
    from synesis_graph.backends.base import _ArcadeDBAdapterBase

    class _Adapter(_ArcadeDBAdapterBase):
        backend_name = "test"

        def preflight(self, reporter):
            return None

        def connect(self, reporter):
            return None

        def prepare_destination(self, payload, reporter):
            return None

    adapter = _Adapter()
    adapter.client = None  # would raise "not connected" if the step ran

    adapter.mode = "update"
    assert adapter.clear_destination(None, None) is None

    adapter.mode = "rebuild"
    assert adapter.clear_destination(None, None) is not None, (
        "rebuild must still clear -- here it reports the missing client"
    )


def test_search_indexes_are_rebuilt_only_in_rebuild(monkeypatch):
    """Dropping and recreating every full-text index is what update avoids.

    In update the analyzer cannot have changed (the guard refuses that), so
    there is nothing to apply -- and reindexing the whole graph to arrive at the
    index it already had is exactly the cost update exists to skip.
    """
    from synesis_graph.backends import arcadedb as mod
    from tests.conftest import _make_payload

    built: list[str] = []
    monkeypatch.setattr(mod, "clear_database", lambda c: None)
    monkeypatch.setattr(mod, "_create_schema", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_create_constraints", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_create_search_indexes", lambda *a, **k: built.append("built"))
    monkeypatch.setattr(mod, "_execute_sync_transaction", lambda *a, **k: None)

    mod.sync_to_arcadedb(object(), _make_payload(), mode="update")
    assert built == []

    mod.sync_to_arcadedb(object(), _make_payload(), mode="rebuild")
    assert built == ["built"]


# ---------------------------------------------------------------------------
# The ProjectContext vertex: exactly one, in both modes
# ---------------------------------------------------------------------------


def test_update_replaces_the_project_context_instead_of_adding_one():
    """Nothing was wiped, so a bare CREATE would leave two contexts behind.

    Two ProjectContext vertices means an MCP client reads two conflicting
    descriptions of the same graph with no way to tell which is current.
    """
    from synesis_graph.backends.neo4j import _sync_project_context

    tx = _RecordingTx()
    _sync_project_context(tx, _context(), mode="update")

    assert "MATCH (p:ProjectContext) DETACH DELETE p" in tx.text
    assert "CREATE (p:ProjectContext)" in tx.text


def test_rebuild_does_not_delete_the_project_context_first():
    """There is nothing to delete -- the graph was just wiped."""
    from synesis_graph.backends.neo4j import _sync_project_context

    tx = _RecordingTx()
    _sync_project_context(tx, _context(), mode="rebuild")

    assert "DETACH DELETE" not in tx.text
    assert "CREATE (p:ProjectContext)" in tx.text


# ---------------------------------------------------------------------------
# The 4.3 guard: settings update cannot apply
# ---------------------------------------------------------------------------


def test_a_changed_analyzer_is_refused():
    stored = {"fulltext_analyzer": "standard"}
    payload = type("P", (), {"project_context": _context(fulltext_analyzer="brazilian")})()

    changed = incompatible_with_update(stored, payload)

    assert len(changed) == 1
    assert "standard" in changed[0] and "brazilian" in changed[0]


def test_a_changed_embedding_model_is_refused():
    stored = {"embedding_model": "old-model"}
    payload = type("P", (), {"project_context": _context(embedding_model="new-model")})()

    assert incompatible_with_update(stored, payload)


def test_an_unchanged_setting_is_allowed():
    stored = {"fulltext_analyzer": "brazilian", "embedding_model": "m"}
    payload = type(
        "P",
        (),
        {"project_context": _context(fulltext_analyzer="brazilian", embedding_model="m")},
    )()

    assert incompatible_with_update(stored, payload) == []


def test_an_empty_destination_is_an_initial_load_not_a_conflict():
    """`update` against an empty graph must work -- it is just a first load."""
    payload = type("P", (), {"project_context": _context(fulltext_analyzer="brazilian")})()

    assert incompatible_with_update(None, payload) == []
    assert incompatible_with_update({}, payload) == []


def test_a_graph_that_never_declared_the_setting_is_not_a_conflict():
    """Older graphs carry blanks. Refusing those would block updates for nothing."""
    stored = {"fulltext_analyzer": ""}
    payload = type("P", (), {"project_context": _context(fulltext_analyzer="brazilian")})()

    assert incompatible_with_update(stored, payload) == []


def test_read_project_context_returns_none_when_the_type_is_absent():
    """A database with no ProjectContext at all is the empty case, not an error.

    The raised type is deliberately not `ArcadeDBError`: how a missing type
    surfaces differs between the HTTP and embedded transports, so the guard has
    to treat any failure of this probe as "nothing stored".
    """

    class _Raises:
        def command(self, *a, **k):
            raise RuntimeError("Type 'ProjectContext' not found")

    class _Empty:
        def command(self, *a, **k):
            return []

    assert read_project_context(_Raises()) is None
    assert read_project_context(_Empty()) is None


def test_the_adapter_refuses_the_sync_and_says_what_to_do(monkeypatch):
    """The message has to name the setting AND the way out."""
    from synesis_graph.backends import base as mod
    from synesis_graph.core import SyncError
    from tests.conftest import _make_payload

    payload = _make_payload()
    payload.project_context = _context(fulltext_analyzer="brazilian")

    monkeypatch.setattr(
        mod, "arcadedb_read_project_context", lambda c: {"fulltext_analyzer": "standard"}
    )

    class _Adapter(mod._ArcadeDBAdapterBase):
        backend_name = "test"
        _fulltext_analyzer = "brazilian"

        def preflight(self, reporter):
            return None

        def connect(self, reporter):
            return None

        def prepare_destination(self, payload, reporter):
            return None

    adapter = _Adapter()
    adapter.client = object()
    adapter.mode = "update"

    error = adapter.synchronize_payload(payload, None)

    assert isinstance(error, SyncError)
    assert "fulltext_analyzer" in error.details
    assert "--mode update" in error.details, "must name the flag to drop"


# ---------------------------------------------------------------------------
# Defaults: nothing changes for anyone who does not pass the flag
# ---------------------------------------------------------------------------


def test_rebuild_is_the_default_everywhere():
    """The whole chain defaults to today's behaviour.

    Etapa A's section 4.5 note applies: these functions are shared by both
    backends, so a default of `update` anywhere would silently change what an
    ordinary run does.
    """
    import inspect

    from synesis_graph.backends import arcadedb, base, neo4j
    from synesis_graph.pipeline import run_pipeline

    for fn in (
        neo4j.sync_to_neo4j,
        neo4j._execute_sync_transaction,
        neo4j._sync_project_context,
        arcadedb.sync_to_arcadedb,
        arcadedb._execute_sync_transaction,
        run_pipeline,
    ):
        assert inspect.signature(fn).parameters["mode"].default == "rebuild", fn

    assert base.BackendAdapter.mode == "rebuild"


def test_the_cli_defaults_to_rebuild_on_every_database_command():
    from click.testing import CliRunner

    from synesis_graph.cli import main

    for command in ("neo4j", "arcadedb", "arcadedb-embedded"):
        result = CliRunner().invoke(main, [command, "--help"])
        assert result.exit_code == 0, command
        assert "--mode" in result.output, command
        assert "rebuild" in result.output, command
        # The gap that has no code to enforce it yet must be stated in the help.
        # Click re-wraps the text, so match on words rather than a phrase.
        flat = " ".join(result.output.split())
        assert "does NOT remove anything" in flat, command


def test_the_html_backend_has_no_mode_flag():
    """It writes a fresh file; there is no destination to update in place."""
    from click.testing import CliRunner

    from synesis_graph.cli import main

    result = CliRunner().invoke(main, ["html", "--help"])
    assert result.exit_code == 0
    assert "--mode" not in result.output


# ---------------------------------------------------------------------------
# Against the real engine
# ---------------------------------------------------------------------------

pytest.importorskip(
    "arcadedb_embedded", reason="arcadedb-embedded unavailable on this platform"
)


def _count(client, label: str) -> int:
    return client.query(f"MATCH (n:{label}) RETURN count(n) AS n")[0]["n"]


def _index_names(client) -> set[str]:
    """Schema introspection is SQL in ArcadeDB, not Cypher (see arcadedb.py)."""
    return {r["name"] for r in client.query("SELECT name FROM schema:indexes", language="sql")}


def _payload(n_items: int, citation: str = "excerpt"):
    from tests.conftest import _make_payload

    return _make_payload(
        sources=[{"bibtex": "src1", "title": "Only source"}],
        items=[
            {"item_id": f"i{i}", "citation": f"{citation} {i}"} for i in range(n_items)
        ],
        from_source=[{"item_id": f"i{i}", "ref": "src1"} for i in range(n_items)],
        concepts=[
            {"props": {"name": f"c{i}"}, "relations": {"topic": ["T"]}} for i in range(5)
        ],
        mentions=[
            {"item_id": f"i{i}", "concept": f"c{i % 5}", "mention_order": 0}
            for i in range(n_items)
        ],
    )


def test_update_adds_new_items_and_keeps_the_old_ones(tmp_path):
    """The acceptance criterion: load A in rebuild, then A+10 in update."""
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import sync_to_arcadedb

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert sync_to_arcadedb(client, _payload(20), mode="rebuild") is None
        assert _count(client, "Item") == 20

        assert sync_to_arcadedb(client, _payload(30), mode="update") is None

        assert _count(client, "Item") == 30, "10 new, 20 kept, 0 duplicated"
        assert _count(client, "Source") == 1, "the re-sent source must not duplicate"
        assert _count(client, "Concept") == 5
        assert (
            client.query("MATCH ()-[r:FROM_SOURCE]->() RETURN count(r) AS n")[0]["n"] == 30
        )


def test_update_changes_a_field_without_changing_the_count(tmp_path):
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import sync_to_arcadedb

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert sync_to_arcadedb(client, _payload(20, "old"), mode="rebuild") is None
        assert sync_to_arcadedb(client, _payload(20, "new"), mode="update") is None

        assert _count(client, "Item") == 20
        rows = client.query(
            "MATCH (i:Item) WHERE i.item_id = 'i3' RETURN i.citation AS c"
        )
        assert len(rows) == 1, "no duplicate vertex for the changed item"
        assert rows[0]["c"] == "new 3", "the new value must win"


def test_update_against_an_empty_database_is_an_initial_load(tmp_path):
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import sync_to_arcadedb

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert sync_to_arcadedb(client, _payload(15), mode="update") is None
        assert _count(client, "Item") == 15


def test_update_leaves_exactly_one_project_context(tmp_path):
    """Three syncs, one context -- the invariant every consumer assumes."""
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import sync_to_arcadedb
    from synesis_graph.core import build_project_context

    payload = _payload(10)
    payload.project_context = _context(project_name="P")

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert sync_to_arcadedb(client, payload, mode="rebuild") is None
        assert _count(client, "ProjectContext") == 1

        assert sync_to_arcadedb(client, payload, mode="update") is None
        assert _count(client, "ProjectContext") == 1, "update must replace, not append"

        assert sync_to_arcadedb(client, payload, mode="update") is None
        assert _count(client, "ProjectContext") == 1

    assert build_project_context is not None  # imported to pin the real type exists


def test_update_preserves_the_indexes_the_rebuild_created(tmp_path):
    """Update does not drop indexes -- that is what makes it cheap, and what
    makes a changed analyzer impossible to apply."""
    from synesis_graph.arcadedb_embedded_client import ArcadeDBEmbeddedClient
    from synesis_graph.backends.arcadedb import sync_to_arcadedb

    with ArcadeDBEmbeddedClient(tmp_path / "graph") as client:
        assert sync_to_arcadedb(client, _payload(10), mode="rebuild") is None
        before = _index_names(client)

        assert sync_to_arcadedb(client, _payload(12), mode="update") is None
        after = _index_names(client)

    assert before, "the rebuild must have created indexes for this to mean anything"
    assert before <= after, "update must not drop what rebuild built"
