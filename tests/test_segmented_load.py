"""Segmented commits and the flag that says whether a load finished.

The whole-corpus transaction is what the server rejected: batches 1-47 of the
real export succeeded and the 48th came back `not found or expired` with the
container still up. These cover the two halves of the fix — commit in segments
so no transaction grows that large, and mark the graph so an interrupted load
cannot pass for a finished one.
"""

import time

from synesis_graph.backends.arcadedb import (
    LOAD_COMPLETE,
    LOAD_HEARTBEAT_PROP,
    LOAD_IN_PROGRESS,
    LOAD_OWNER_PROP,
    LOAD_STALE_AFTER_SECONDS,
    LOAD_STATE_PROP,
    _CypherRunner,
    _set_load_state,
    describe_running_load,
    load_is_incomplete,
)


class FakeClient:
    """Records the order of statements and transaction boundaries."""

    def __init__(self, fail_on: int | None = None):
        self.log: list[str] = []
        self.statements = 0
        self._fail_on = fail_on

    def command(self, query, params=None):
        self.statements += 1
        if self._fail_on is not None and self.statements == self._fail_on:
            raise RuntimeError("server dropped the transaction")
        self.log.append(f"stmt{self.statements}")
        return []

    def begin(self, database=None):
        self.log.append("begin")
        return "session"

    def commit(self, database=None):
        self.log.append("commit")

    def rollback(self, database=None):
        self.log.append("rollback")


def test_a_long_write_is_committed_in_segments():
    """25 statements at commit_every=10 must not ride on one transaction."""
    client = FakeClient()
    runner = _CypherRunner(client, commit_every=10)
    for _ in range(25):
        runner.run("CREATE (n)")

    assert client.log.count("commit") == 2, client.log
    # Every commit reopens, so writing never continues outside a transaction.
    assert client.log.count("begin") == 2


def test_the_commit_lands_between_statements_not_inside_one():
    client = FakeClient()
    runner = _CypherRunner(client, commit_every=3, heartbeat=True)
    for _ in range(6):
        runner.run("CREATE (n)")

    # stmt4 and stmt8 are the heartbeat refreshes that ride with each segment.
    assert client.log == [
        "stmt1", "stmt2", "stmt3", "stmt4", "commit", "begin",
        "stmt5", "stmt6", "stmt7", "stmt8", "commit", "begin",
    ]


def test_segmenting_is_off_by_default():
    """A runner built the old way must behave exactly as it always did."""
    client = FakeClient()
    runner = _CypherRunner(client)
    for _ in range(50):
        runner.run("CREATE (n)")

    assert "commit" not in client.log


def test_an_unfinished_load_is_recognised():
    assert load_is_incomplete({LOAD_STATE_PROP: LOAD_IN_PROGRESS}) is True


def test_a_finished_load_is_not_incomplete():
    assert load_is_incomplete({LOAD_STATE_PROP: LOAD_COMPLETE}) is False


def test_a_graph_predating_the_flag_is_not_condemned():
    """No flag means "written before this existed", not "broken"."""
    assert load_is_incomplete({"project_name": "x"}) is False
    assert load_is_incomplete(None) is False
    assert load_is_incomplete({}) is False


def test_recording_the_state_never_breaks_a_sync():
    """Bookkeeping that fails must not take the load down with it."""

    class Broken:
        def command(self, query, params=None):
            raise RuntimeError("no ProjectContext here")

    _set_load_state(Broken(), LOAD_COMPLETE)  # must not raise


def test_the_state_written_is_the_state_asked_for():
    client = FakeClient()
    _set_load_state(client, LOAD_COMPLETE)
    assert client.statements == 1


# -- who owns a standing flag ------------------------------------------------
#
# `in_progress` alone cannot say whether a loader is running or died. These pin
# the distinction, because the two need opposite handling: refuse, or clear.

def _flag(owner="host:123", age_seconds=0.0, state=LOAD_IN_PROGRESS):
    return {
        LOAD_STATE_PROP: state,
        LOAD_OWNER_PROP: owner,
        LOAD_HEARTBEAT_PROP: time.time() - age_seconds,
    }


def test_a_live_loader_is_reported_so_a_second_one_can_refuse():
    running = describe_running_load(_flag(owner="lab-pc:9910", age_seconds=5))
    assert running is not None
    assert "lab-pc:9910" in running


def test_a_dead_loaders_flag_ages_out_instead_of_locking_the_graph():
    """A crashed process cannot lower its own flag; the heartbeat expires it."""
    stale = _flag(age_seconds=LOAD_STALE_AFTER_SECONDS + 60)
    assert describe_running_load(stale) is None


def test_a_finished_load_leaves_nothing_to_wait_for():
    assert describe_running_load(_flag(state=LOAD_COMPLETE)) is None


def test_a_flag_with_no_readable_heartbeat_is_treated_as_abandoned():
    """Better a rare overlap than a flag no one can ever clear."""
    assert describe_running_load({LOAD_STATE_PROP: LOAD_IN_PROGRESS}) is None
    assert describe_running_load(
        {LOAD_STATE_PROP: LOAD_IN_PROGRESS, LOAD_HEARTBEAT_PROP: "nonsense"}
    ) is None


def test_the_heartbeat_is_refreshed_as_segments_commit():
    """Without this a long load would age into looking abandoned mid-run."""
    client = FakeClient()
    runner = _CypherRunner(client, commit_every=2, heartbeat=True)
    for _ in range(4):
        runner.run("CREATE (n)")

    # Two segments, each refreshing the heartbeat before committing.
    assert client.log.count("commit") == 2
    assert client.statements == 4 + 2


def test_a_payload_with_no_context_gets_no_heartbeat():
    """The flag lives on `ProjectContext`; without one there is nothing to mark.

    A hand-built payload writes no context vertex, and a `MATCH` against a type
    this sync never created is a statement about someone else's graph.
    """
    client = FakeClient()
    runner = _CypherRunner(client, commit_every=2)  # heartbeat off by default
    for _ in range(4):
        runner.run("CREATE (n)")

    assert client.statements == 4
    assert client.log.count("commit") == 2


def test_both_modes_refuse_a_graph_someone_else_is_loading():
    """`clear_destination` carries this for rebuild but returns early in update.

    Without the check on the update path too, an update would merge into a graph
    another process is actively writing — the one case where two writers really
    do corrupt each other's work.
    """
    import inspect

    from synesis_graph.backends import base

    source = inspect.getsource(base._ArcadeDBAdapterBase)
    rebuild_path = source.index("def clear_destination")
    update_path = source.index("def synchronize_payload")
    assert "arcadedb_describe_running_load" in source[rebuild_path:update_path]
    assert "arcadedb_describe_running_load" in source[update_path:]


# -- the same fix, on the Neo4j backend -------------------------------------
#
# A whole corpus in one transaction is bounded by the server's transaction
# memory: a stock 1 GB Neo4j caps one at 717 MB, and the real corpus hit that
# ceiling 149s in with nothing written.

from synesis_graph.backends.neo4j import SYNC_COMMIT_ROWS, _SegmentedTx


class FakeNeo4jTx:
    def __init__(self, log):
        self._log = log
        self._closed = False

    def run(self, query, **params):
        self._log.append("stmt")
        return []

    def commit(self):
        self._log.append("commit")
        self._closed = True

    def rollback(self):
        self._log.append("rollback")
        self._closed = True

    def closed(self):
        return self._closed


class FakeNeo4jSession:
    def __init__(self):
        self.log: list[str] = []

    def begin_transaction(self):
        self.log.append("begin")
        return FakeNeo4jTx(self.log)


def test_neo4j_commits_by_rows_not_by_statements():
    """Counting statements is what made the first attempt fail identically.

    `_run_in_batches` puts 50,000 rows in each statement, so "commit every 10
    statements" meant 500,000 rows — almost exactly the 717 MB ceiling it was
    supposed to stay under.
    """
    session = FakeNeo4jSession()
    tx = _SegmentedTx(session, commit_rows=100)
    # One statement carrying more than the threshold must commit on its own.
    tx.run("UNWIND $rows AS r CREATE (n)", rows=[{}] * 250)
    assert session.log.count("commit") == 1


def test_a_phase_smaller_than_the_threshold_still_rides_one_segment():
    """Below the threshold nothing commits early — that is the intended shape."""
    session = FakeNeo4jSession()
    tx = _SegmentedTx(session, commit_rows=100_000)
    for _ in range(5):
        tx.run("UNWIND $rows AS r CREATE (n)", rows=[{}] * 1_000)
    assert session.log.count("commit") == 0
    tx.commit()
    assert session.log.count("commit") == 1


def test_rows_accumulate_across_statements_and_across_phases():
    """The old counter reset per phase; memory does not."""
    session = FakeNeo4jSession()
    tx = _SegmentedTx(session, commit_rows=1_000)
    for _ in range(4):
        tx.run("UNWIND $rows AS r CREATE (n)", rows=[{}] * 300)
    assert session.log.count("commit") == 1


def test_a_statement_without_rows_still_counts_as_work():
    session = FakeNeo4jSession()
    tx = _SegmentedTx(session, commit_rows=3)
    for _ in range(3):
        tx.run("CREATE (p:ProjectContext)")
    assert session.log.count("commit") == 1


def test_neo4j_segmenting_can_be_switched_off():
    session = FakeNeo4jSession()
    tx = _SegmentedTx(session, commit_rows=0)
    for _ in range(50):
        tx.run("UNWIND $rows AS r CREATE (n)", rows=[{}] * 10_000)
    assert session.log.count("commit") == 0


def test_a_failed_neo4j_sync_rolls_back_the_open_segment():
    session = FakeNeo4jSession()
    tx = _SegmentedTx(session, commit_rows=100_000)
    tx.run("CREATE (n)")
    tx.close()
    assert "rollback" in session.log


def test_closing_an_already_committed_segment_is_not_an_error():
    """The cleanup path must never raise over its own bookkeeping."""
    session = FakeNeo4jSession()
    tx = _SegmentedTx(session, commit_rows=1)
    tx.run("CREATE (n)")  # commits and reopens
    tx.commit()
    tx.close()  # must not raise


def test_a_segment_stays_well_under_a_stock_servers_transaction_ceiling():
    """500,000 rows measured at ~717 MB, which is the stock limit exactly."""
    assert SYNC_COMMIT_ROWS <= 250_000


def test_both_backends_segment_by_default():
    from synesis_graph.backends.arcadedb import SYNC_BATCH_SIZE
    from synesis_graph.backends.arcadedb import SYNC_COMMIT_EVERY as arcade_every

    assert SYNC_COMMIT_ROWS > 0
    # ArcadeDB counts statements, but its 5,000-row batches keep a segment at
    # 50,000 rows — an order of magnitude under where Neo4j's ceiling sits.
    assert arcade_every * SYNC_BATCH_SIZE <= SYNC_COMMIT_ROWS
