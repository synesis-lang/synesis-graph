"""Two fixes found by running the real pipeline against a real server.

**The false `[OK]`.** A failed sync printed `[OK]` and `[ERROR]` for the same
step, one line apart. `_StepContext` judged the outcome by whether an exception
propagated, and this codebase reports failure by *returning* a typed error — an
early `return` inside a `with` leaves the context manager nothing to see. Six
call sites were affected, both database syncs among them.

That matters more than the sync failure it hid: a reader who has learned that
`[OK]` can mean failure cannot trust any line of the output.

**The abandoned upload.** A large export failed with `Bad Gateway (HTTP 502)`
after minutes of work. The database was healthy the whole time — 74ms on its
readiness probe while returning 502s — so a reverse proxy was cutting
connections under load, and the same statement succeeded repeatedly minutes
later. Transient, so worth retrying; but *how* to retry is the whole question,
because these writes are not idempotent (a rebuild uses `CREATE`). The rule the
tests below pin: retry the request only when it provably never arrived, and
otherwise restart the whole sync, which a rebuild's initial clear makes safe.
"""

from __future__ import annotations

from typing import Any

import pytest

from synesis_graph.backends.arcadedb import (
    SYNC_RETRY_ATTEMPTS,
    _retry_is_safe,
    announce_scale,
)

# ---------------------------------------------------------------------------
# The step marker
# ---------------------------------------------------------------------------


def _lines(capsys) -> str:
    # stderr, not stdout: `_emit` writes there so the progress narration never
    # pollutes a piped stdout.
    return capsys.readouterr().err


def test_a_step_that_is_told_it_failed_does_not_claim_success(capsys):
    from synesis_graph.ui import TaskReporter

    reporter = TaskReporter("t")
    _lines(capsys)

    with reporter.step("Building the graph") as step:
        step.fail()

    out = _lines(capsys)
    assert "[OK]" not in out
    assert "[ERROR]" in out


def test_a_step_that_succeeds_still_says_so(capsys):
    from synesis_graph.ui import TaskReporter

    reporter = TaskReporter("t")
    _lines(capsys)

    with reporter.step("Building the graph"):
        pass

    assert "[OK]" in _lines(capsys)


def test_a_failed_step_counts_as_an_error_for_the_summary(capsys):
    """`print_summary` decides SUCCESS/FAIL from the error count.

    A step that failed silently would leave the run reporting SUCCESS overall,
    which is the same lie one level up.
    """
    from synesis_graph.ui import TaskReporter

    reporter = TaskReporter("t")
    with reporter.step("Building the graph") as step:
        step.fail()

    assert reporter.stats["errors"] == 1
    _lines(capsys)


def test_an_exception_still_reports_the_error(capsys):
    """The original behaviour must survive: raising is still a failure."""
    from synesis_graph.ui import TaskReporter

    reporter = TaskReporter("t")
    _lines(capsys)

    with pytest.raises(ValueError), reporter.step("Building the graph"):
        raise ValueError("boom")

    out = _lines(capsys)
    assert "[ERROR]" in out
    assert "[OK]" not in out


def test_every_sync_step_marks_its_own_failure():
    """The six call sites that return errors must all say so.

    Pinned as a whole rather than one test each: the defect was systemic, and a
    seventh call site added later would reintroduce it silently.
    """
    import inspect
    import re

    from synesis_graph import cli
    from synesis_graph.backends import base, html

    for module in (base, html, cli):
        src = inspect.getsource(module)
        for match in re.finditer(r"with reporter\.step\(([^\n]*)\)( as step)?:", src):
            assert match.group(2), (
                f"{module.__name__}: a step without `as step` cannot report failure; "
                f"at {match.group(1)[:60]}"
            )


# ---------------------------------------------------------------------------
# What may be retried, and what may not
# ---------------------------------------------------------------------------


def test_a_rebuild_may_be_restarted():
    """It clears first, so attempt two starts where attempt one did."""
    assert _retry_is_safe("rebuild") is True


def test_an_update_may_not_be_restarted():
    """It does not clear, so re-applying writes of unknown outcome could duplicate.

    The `MERGE` statements would absorb it, but the run is not made only of
    `MERGE`s, and "probably fine" is not a basis for writing to a corpus.
    """
    assert _retry_is_safe("update") is False


class _FlakyClient:
    """Fails the first `attempts_to_fail` syncs with a transient gateway error.

    The exception class is looked up at call time rather than imported at module
    level, and that is load-bearing: `tests/test_phase7_multidb.py` reloads the
    whole package through its `s2g` fixture, after which a module-level
    `ArcadeDBError` is a *different class* from the one the reloaded
    `sync_to_arcadedb` catches. The retry then silently never fires, and these
    tests fail only when run alongside that file.
    """

    def __init__(self, attempts_to_fail: int) -> None:
        self.remaining = attempts_to_fail
        self.syncs = 0

    def sync(self) -> None:
        self.syncs += 1
        if self.remaining > 0:
            self.remaining -= 1
            from synesis_graph.arcadedb_client import (
                ArcadeDBError as _Current,
            )

            raise _Current(
                "The server's proxy closed the connection",
                status=502,
                retryable=True,
                applied_unknown=True,
            )


def _patched_sync(monkeypatch, client: _FlakyClient):
    """Points `sync_to_arcadedb`'s internals at the flaky double."""
    from synesis_graph.backends import arcadedb as mod

    # Hooked on `_create_schema`, not `clear_database`: the latter is skipped in
    # update mode, so failing there would make the update test pass for the
    # wrong reason. This one runs in both modes.
    monkeypatch.setattr(mod, "clear_database", lambda c: None)
    monkeypatch.setattr(mod, "_create_schema", lambda *a, **k: client.sync())
    monkeypatch.setattr(mod, "_create_constraints", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_create_search_indexes", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_bulk_load_vertices", lambda *a, **k: frozenset())
    monkeypatch.setattr(mod, "_execute_sync_transaction", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_RETRY_BACKOFF_SECONDS", 0.0)
    return mod


def test_a_transient_failure_restarts_the_whole_sync(monkeypatch):
    from tests.conftest import _make_payload

    client = _FlakyClient(attempts_to_fail=1)
    mod = _patched_sync(monkeypatch, client)

    error = mod.sync_to_arcadedb(object(), _make_payload(), mode="rebuild")

    assert error is None, "the second attempt should have succeeded"
    assert client.syncs == 2


def test_retries_are_bounded(monkeypatch):
    """A server that never recovers must produce an error, not an endless loop."""
    from tests.conftest import _make_payload

    client = _FlakyClient(attempts_to_fail=99)
    mod = _patched_sync(monkeypatch, client)

    error = mod.sync_to_arcadedb(object(), _make_payload(), mode="rebuild")

    assert error is not None
    assert client.syncs == SYNC_RETRY_ATTEMPTS


def test_an_update_is_not_restarted(monkeypatch):
    """One attempt only — restarting could duplicate what already landed."""
    from tests.conftest import _make_payload

    client = _FlakyClient(attempts_to_fail=1)
    mod = _patched_sync(monkeypatch, client)

    error = mod.sync_to_arcadedb(object(), _make_payload(), mode="update")

    assert error is not None
    assert client.syncs == 1


def test_a_rejected_statement_is_not_retried(monkeypatch):
    """A statement the database refused will be refused again.

    Only failures marked `retryable` restart; a syntax or schema error must
    surface at once instead of being attempted three times.
    """
    from synesis_graph.backends import arcadedb as mod
    from tests.conftest import _make_payload

    calls = {"n": 0}

    def _reject(c):
        calls["n"] += 1
        from synesis_graph.arcadedb_client import ArcadeDBError as _Current

        raise _Current("Command text is null", status=500)

    monkeypatch.setattr(mod, "clear_database", _reject)
    monkeypatch.setattr(mod, "_RETRY_BACKOFF_SECONDS", 0.0)

    error = mod.sync_to_arcadedb(object(), _make_payload(), mode="rebuild")

    assert error is not None
    assert calls["n"] == 1


def test_the_final_error_blames_the_server_not_the_project(monkeypatch):
    """After every retry, the message has to say whose problem this is.

    A researcher reading a stack of gateway errors will otherwise go looking for
    a mistake in their own coding.
    """
    from tests.conftest import _make_payload

    client = _FlakyClient(attempts_to_fail=99)
    mod = _patched_sync(monkeypatch, client)

    error = mod.sync_to_arcadedb(object(), _make_payload(), mode="rebuild")

    assert error is not None
    assert "not your project" in error.details
    assert "half-written" in error.details


# ---------------------------------------------------------------------------
# Telling the researcher what to expect
# ---------------------------------------------------------------------------


def _said(payload, embeddings=None) -> str:
    lines: list[str] = []
    announce_scale(payload, embeddings, lines.append)
    return "\n".join(lines)


def test_a_long_upload_warns_before_the_silence():
    from tests.conftest import _make_payload

    payload = _make_payload(items=[{"item_id": f"i{i}"} for i in range(60_000)])

    said = _said(payload)

    assert "60,000" in said
    assert "minute" in said
    # The two things that make waiting a decision rather than a guess.
    assert "normal" in said
    assert "graph stays empty" in said


def test_a_short_upload_says_nothing():
    """Below the threshold the wait is short, and a warning would be noise."""
    from tests.conftest import _make_payload

    payload = _make_payload(items=[{"item_id": f"i{i}"} for i in range(100)])

    assert _said(payload) == ""


def test_the_vector_step_is_announced_too(monkeypatch):
    """It runs after the graph, adding to a wait that was already long."""
    from tests.conftest import _make_payload

    payload = _make_payload(items=[{"item_id": f"i{i}"} for i in range(60_000)])
    sidecar: Any = type("S", (), {"vectors": {f"c{i}": [] for i in range(500)}})()

    said = _said(payload, sidecar)

    assert "500" in said
    assert "meaning-based search" in said


# ---------------------------------------------------------------------------
# What a real 502 mid-transaction exposed
# ---------------------------------------------------------------------------


def test_a_failing_rollback_does_not_replace_the_real_error(monkeypatch):
    """The cause outranks the cleanup.

    When a proxy drops the connection mid-sync the server usually discards the
    transaction with it, so the rollback answers `Remote transaction not found
    or expired` (HTTP 404). An unguarded `client.rollback()` raised *that*
    instead of the 502 — and 404 is not retryable, so a sync that should have
    restarted was abandoned. Observed on a real 246,588-item export.
    """
    from synesis_graph.backends import arcadedb as mod
    from tests.conftest import _make_payload

    class _DropsMidTransaction:
        def __init__(self):
            self.writes = 0

        def command(self, statement, params=None, **kw):
            from synesis_graph.arcadedb_client import ArcadeDBError as _Current

            if "UNWIND" in statement:
                self.writes += 1
                raise _Current(
                    "proxy closed", status=502, retryable=True, applied_unknown=True
                )
            return []

        def query(self, statement, params=None, **kw):
            return []

        def begin(self, database=None):
            return "AS-1"

        def commit(self, database=None):
            pass

        def rollback(self, database=None):
            from synesis_graph.arcadedb_client import ArcadeDBError as _Current

            raise _Current("Remote transaction not found or expired", status=404)

    for name in ("_create_schema", "_create_constraints", "_create_search_indexes"):
        monkeypatch.setattr(mod, name, lambda *a, **k: None)
    monkeypatch.setattr(mod, "clear_database", lambda c: None)
    monkeypatch.setattr(mod, "_bulk_load_vertices", lambda *a, **k: frozenset())
    monkeypatch.setattr(mod, "_RETRY_BACKOFF_SECONDS", 0.0)

    client = _DropsMidTransaction()
    payload = _make_payload(items=[{"item_id": "i1"}], sources=[{"bibtex": "s1"}])

    error = mod.sync_to_arcadedb(client, payload, mode="rebuild")

    assert client.writes == SYNC_RETRY_ATTEMPTS, "the 404 must not stop the retries"
    assert error is not None
    assert "502" in error.details, "the reported cause must be the 502, not the 404"


def test_this_backend_sends_smaller_requests_than_the_shared_default():
    """50,000 items serialise to 32.6MB — large enough for a proxy to drop.

    The shared default was written for Neo4j's streaming driver; here every
    statement is one HTTP request through whatever proxy fronts the server.
    """
    from synesis_graph.backends.arcadedb import SYNC_BATCH_SIZE
    from synesis_graph.backends.neo4j import DEFAULT_SYNC_BATCH_SIZE

    assert SYNC_BATCH_SIZE < DEFAULT_SYNC_BATCH_SIZE


def test_every_batched_sync_call_gets_the_smaller_size():
    """A function left on the default would still send 32MB requests.

    Checked across the whole transaction rather than per call: the defect was
    that nobody passed a size at all, and one forgotten call reintroduces it.
    """
    import inspect

    from synesis_graph.backends import arcadedb as mod

    src = inspect.getsource(mod._execute_sync_transaction)
    for fn in (
        "_sync_sources",
        "_sync_items",
        "_sync_from_source",
        "_sync_concepts",
        "_sync_taxonomies",
        "_sync_mentions",
        "_sync_entities",
        "_sync_refers_to",
    ):
        call = src[src.index(f"{fn}(") :]
        call = call[: call.index("\n        _") if "\n        _" in call else len(call)]
        assert "batch_size=bs" in call, f"{fn} still uses the shared default"
