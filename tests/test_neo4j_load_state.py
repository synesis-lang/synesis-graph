"""The load mark and its two guards, on the Neo4j backend.

Neo4j now commits in segments too (see `_SegmentedTx`), so an interrupted load
leaves the segments that already committed: a partial graph whose vertices are
real and whose counts merely look low. ArcadeDB answered that in 0.12.0 with a
`load_state` mark on `ProjectContext` and two guards that read it; these pin the
same contract on Neo4j.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import SimpleNamespace

from synesis_graph.backends import base, neo4j
from synesis_graph.backends.load_state import (
    LOAD_COMPLETE,
    LOAD_HEARTBEAT_PROP,
    LOAD_IN_PROGRESS,
    LOAD_OWNER_PROP,
    LOAD_STALE_AFTER_SECONDS,
    LOAD_STATE_PROP,
)


class _Tx:
    def __init__(self, log: list):
        self._log = log
        self._closed = False

    def run(self, query, **params):
        self._log.append(("run", " ".join(query.split()), params))
        return []

    def commit(self):
        self._log.append(("commit",))
        self._closed = True

    def rollback(self):
        self._log.append(("rollback",))
        self._closed = True

    def closed(self):
        return self._closed


class _Session:
    def __init__(self, stored: dict | None = None):
        self.log: list = []
        self._stored = stored

    def begin_transaction(self):
        self.log.append(("begin",))
        return _Tx(self.log)

    def run(self, query, **params):
        self.log.append(("auto", " ".join(query.split()), params))
        stored = self._stored

        class _Result:
            def single(self_inner):
                return None if stored is None else {"p": stored}

            def consume(self_inner):
                return None

        return _Result()


@dataclass
class _Context:
    project: str = "p"


def _payload(context=True):
    return SimpleNamespace(
        project_context=_Context() if context else None,
        sources=[], items=[], from_source=[], chains=[], concepts=[],
        concept_label="Concept", graph_fields=[], field_specs={}, mentions=[],
        entities={}, refers_to_edges=[],
    )


def _queries(log, kind="run"):
    return [e[1] for e in log if e[0] == kind]


class TestTheMark:
    def test_raised_in_the_first_segment_right_after_the_context(self):
        session = _Session()
        neo4j._execute_sync_transaction(session, _payload())
        runs = [e for e in session.log if e[0] == "run"]
        assert "CREATE (p:ProjectContext)" in runs[0][1]
        assert f"p.{LOAD_STATE_PROP} = $state" in runs[1][1]
        assert runs[1][2]["state"] == LOAD_IN_PROGRESS
        assert runs[1][2]["owner"] and runs[1][2]["beat"] > 0

    def test_lowered_only_after_the_last_segment(self, monkeypatch):
        session = _Session()
        for name in ("clear_database", "_create_constraints", "_create_search_indexes"):
            monkeypatch.setattr(neo4j, name, lambda *a, **k: None)
        assert neo4j.sync_to_neo4j(session, _payload()) is None
        ultimo = [e for e in session.log if e[0] == "auto"][-1]
        assert f"p.{LOAD_STATE_PROP} = $state" in ultimo[1]
        assert ultimo[2]["state"] == LOAD_COMPLETE
        # the final commit comes before the mark is lowered
        assert session.log.index(("commit",)) < session.log.index(ultimo)

    def test_a_failed_sync_leaves_the_mark_raised(self, monkeypatch):
        session = _Session()
        for name in ("clear_database", "_create_constraints", "_create_search_indexes"):
            monkeypatch.setattr(neo4j, name, lambda *a, **k: None)

        def boom(*a, **k):
            raise RuntimeError("server dropped the transaction")

        monkeypatch.setattr(neo4j, "_sync_items", boom)
        assert neo4j.sync_to_neo4j(session, _payload()) is not None
        assert not any(e[0] == "auto" and LOAD_STATE_PROP in e[1] for e in session.log)

    def test_no_context_no_mark(self):
        session = _Session()
        neo4j._execute_sync_transaction(session, _payload(context=False))
        assert not any(LOAD_STATE_PROP in q for q in _queries(session.log))

    def test_heartbeat_refreshed_as_segments_commit(self):
        session = _Session()
        tx = neo4j._SegmentedTx(session, commit_rows=2, heartbeat=True)
        tx.run("UNWIND $rows AS r CREATE (n)", rows=[{}, {}])
        beats = [q for q in _queries(session.log) if LOAD_HEARTBEAT_PROP in q]
        assert len(beats) == 1
        assert session.log.index(("commit",)) > max(
            i for i, e in enumerate(session.log) if e[0] == "run" and LOAD_HEARTBEAT_PROP in e[1])

    def test_no_heartbeat_without_context(self):
        session = _Session()
        tx = neo4j._SegmentedTx(session, commit_rows=1)
        tx.run("CREATE (n)")
        assert not any(LOAD_HEARTBEAT_PROP in q for q in _queries(session.log))


class TestReadingTheContext:
    def test_reads_the_stored_properties(self):
        stored = {LOAD_STATE_PROP: LOAD_IN_PROGRESS}
        assert neo4j.read_project_context(_Session(stored)) == stored

    def test_an_empty_or_unreachable_graph_reads_as_none(self):
        assert neo4j.read_project_context(_Session(None)) is None

        class Broken:
            def run(self, *a, **k):
                raise RuntimeError("no such label")

        assert neo4j.read_project_context(Broken()) is None


def _adapter(mode, stored, monkeypatch):
    chamadas: list = []
    monkeypatch.setattr(base, "neo4j_read_project_context", lambda session: stored)
    monkeypatch.setattr(base, "sync_to_neo4j", lambda *a, **k: chamadas.append(k) or None)
    adapter = base.Neo4jBackendAdapter(SimpleNamespace(fulltext_analyzer="x"))
    adapter.session = object()
    adapter.mode = mode
    reporter = SimpleNamespace(step=lambda *a, **k: _Step())
    return adapter, reporter, chamadas


class _Step:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def fail(self):
        pass


def _live():
    return {LOAD_STATE_PROP: LOAD_IN_PROGRESS, LOAD_OWNER_PROP: "maquina:42",
            LOAD_HEARTBEAT_PROP: time.time()}


def _abandoned():
    return {LOAD_STATE_PROP: LOAD_IN_PROGRESS, LOAD_OWNER_PROP: "maquina:42",
            LOAD_HEARTBEAT_PROP: time.time() - LOAD_STALE_AFTER_SECONDS - 60}


class TestTheGuards:
    def test_both_modes_refuse_a_graph_someone_else_is_loading(self, monkeypatch):
        for mode in ("rebuild", "update"):
            adapter, reporter, chamadas = _adapter(mode, _live(), monkeypatch)
            erro = adapter.synchronize_payload(_payload(), reporter)
            assert erro is not None and "Another load" in erro.message
            assert "maquina:42" in erro.details
            assert chamadas == []

    def test_update_refuses_a_half_written_graph(self, monkeypatch):
        adapter, reporter, chamadas = _adapter("update", _abandoned(), monkeypatch)
        erro = adapter.synchronize_payload(_payload(), reporter)
        assert erro is not None and "half-written" in erro.message
        assert chamadas == []

    def test_a_rebuild_clears_an_abandoned_mark(self, monkeypatch):
        adapter, reporter, chamadas = _adapter("rebuild", _abandoned(), monkeypatch)
        assert adapter.synchronize_payload(_payload(), reporter) is None
        assert len(chamadas) == 1

    def test_a_finished_graph_updates_normally(self, monkeypatch):
        stored = {LOAD_STATE_PROP: LOAD_COMPLETE}
        adapter, reporter, chamadas = _adapter("update", stored, monkeypatch)
        assert adapter.synchronize_payload(_payload(), reporter) is None
        assert len(chamadas) == 1
