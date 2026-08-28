"""In-process ArcadeDB transport: same engine, no server.

`arcadedb-embedded` ships the real ArcadeDB engine plus a bundled JRE, so a
researcher can build and query a graph with nothing installed but `pip`. The
engine is identical to the one behind the HTTP backend — same Cypher, same SQL,
same indexes, same `algo.*` procedures — which is why this module contains no
query, no schema statement and no business logic. It is a transport, and only a
transport.

What it does contain is the five places where the embedded binding's surface
differs from `ArcadeDBClient`'s. All five were measured against
`arcadedb-embedded` 26.8.1, and every one of them is a silent failure if left
unhandled — the code runs, raises nothing, and returns the wrong answer:

1. `language` is the binding's **first positional** argument, not a keyword. This
   class keeps the client's keyword-with-default signature so the sync layer's 20
   call sites cannot tell the two transports apart.
2. `command` returns `None` for a write. The contract promises `list[dict]`, and
   the sync layer iterates results directly, so `None` becomes `[]` here.
3. `ResultSet` is a **single-pass** iterator: reading it twice yields `[]` the
   second time, with no error. Results are materialised at this boundary, which
   is the only place that knows the cursor has not been read yet.
4. Passing `None` as the parameters argument raises `Ambiguous overloads` from
   JPype — the Java overload cannot be resolved from a null. The argument is
   omitted entirely when there are no parameters (see `_args`).
5. The binding raises its own `ArcadeDBError`, a different class from this
   package's despite the identical name. Nine `except ArcadeDBError` sites in the
   sync layer and the adapter expect *ours*; unwrapped, every one of them would
   miss the failure and let it escape as an unhandled exception.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from synesis_graph.arcadedb_client import ArcadeDBError

logger = logging.getLogger("synesis2graph")


#: JDK logging configuration that silences the engine's own chatter.
#:
#: ArcadeDB narrates its internal work at INFO through `java.util.logging`:
#: every index build, every sub-index split, every page write. That is written
#: for someone tuning a database server. The audience here is a researcher
#: watching their corpus compile, for whom "Completed building index
#: 'Item_0_40627010708400': processed 0 records in 0ms" is noise that buries the
#: three lines they actually need — and, worse, reads like an error report.
#:
#: WARNING and above still come through: a real problem is not silenced.
_LOG_CONFIG = (
    "handlers=\n"
    ".level=WARNING\n"
    "com.arcadedb.level=WARNING\n"
    # The Polyglot engine announces at WARNING that it found no scripting
    # languages. Nothing here uses them, so it is a notice about an unused
    # feature — accurate, irrelevant, and alarming to read mid-export.
    "com.arcadedb.graalvm.level=SEVERE\n"
    "com.arcadedb.polyglot.level=SEVERE\n"
)


#: Lines the JVM writes straight to the process's stderr, before and around the
#: logging configuration it then honours. Neither is actionable:
#:
#: - the incubator notice is printed by the launcher itself, ahead of any
#:   `java.util.logging` setup, because the engine uses the vector API for
#:   similarity search — which is the feature working, not a problem;
#: - the Polyglot line reports that no scripting languages were found, which is
#:   a notice about a feature nothing here uses.
#:
#: Matched as substrings, and only these two: anything else the JVM says still
#: reaches the terminal.
_JVM_NOISE = (
    "Using incubator modules",
    "GraalVM Polyglot Engine: no languages found",
)


@contextlib.contextmanager
def _without_jvm_startup_noise() -> Iterator[None]:
    """Drops the JVM's two unactionable startup lines from stderr.

    They are written to file descriptor 2 by Java, not through Python, so
    replacing `sys.stderr` would not catch them: the descriptor itself has to be
    redirected. Everything captured is inspected afterwards and re-emitted
    unless it matches `_JVM_NOISE`, so a real Java error is never swallowed.

    On any failure the redirection is skipped entirely — noisy output is a far
    better outcome than a lost error or a broken export.
    """
    try:
        original = os.dup(2)
    except (AttributeError, OSError):  # pragma: no cover - platform dependent
        yield
        return

    with tempfile.TemporaryFile(mode="w+b") as capture:
        try:
            sys.stderr.flush()
            os.dup2(capture.fileno(), 2)
            yield
        finally:
            try:
                os.dup2(original, 2)
            finally:
                os.close(original)
            try:
                capture.seek(0)
                text = capture.read().decode("utf-8", errors="replace")
            except OSError:  # pragma: no cover - defensive
                text = ""
            kept = [
                line
                for line in text.splitlines()
                if line.strip() and not any(noise in line for noise in _JVM_NOISE)
            ]
            if kept:
                sys.stderr.write("\n".join(kept) + "\n")
                sys.stderr.flush()


def _quiet_jvm_args() -> list[str]:
    """JVM flags that keep the engine's logging out of the researcher's terminal.

    The properties file has to exist on disk — `java.util.logging` reads a path,
    not a string — so it is written once into the user's cache directory and
    reused. A failure to write it is not worth aborting an export over: the
    fallback is simply the noisy default.
    """
    try:
        config = Path(tempfile.gettempdir()) / "synesis-graph-jvm-logging.properties"
        # Compare before writing, not just existence: a file left by an older
        # version would otherwise silence the wrong things forever.
        if not config.exists() or config.read_text(encoding="utf-8") != _LOG_CONFIG:
            config.write_text(_LOG_CONFIG, encoding="utf-8")
        return [f"-Djava.util.logging.config.file={config}"]
    except OSError:  # pragma: no cover - depends on the filesystem
        return []


class ArcadeDBEmbeddedClient:
    """An `ArcadeDBTransport` backed by the in-process engine.

    Mirrors `ArcadeDBClient`'s surface deliberately: the sync layer is typed
    against the Protocol, so anything that diverges here would have to be
    absorbed by every call site instead of by this one class.

    Unlike the HTTP client, an instance owns an operating-system resource — the
    open database — so it must be closed. Use it as a context manager, or call
    `close()`; the adapter does the latter in its own `close()`.
    """

    def __init__(self, db_path: str | Path, *, database: str = "") -> None:
        """Opens the database at `db_path`, creating it when absent.

        `database` carries the project's database name for the Protocol's sake.
        It is metadata here, not a selector: an embedded database *is* the
        directory, so unlike the HTTP client there is nothing to switch between.
        The per-call `database=` argument is accepted and ignored for the same
        reason.
        """
        self._path = Path(db_path)
        self.database: str | None = database or self._path.name
        self._db: Any = None
        self._open()

    # -- lifecycle ----------------------------------------------------------
    def _open(self) -> None:
        """Opens or creates the database, translating both failure modes.

        The import is deferred to here, not module scope, so that
        `synesis-graph` still imports and runs its other backends when the
        optional `arcadedb-embedded` extra is absent — the same lazy pattern the
        embeddings provider uses.
        """
        try:
            import arcadedb_embedded
        except ImportError as e:  # pragma: no cover - exercised by the adapter
            raise ArcadeDBError(
                "The local graph engine is not available",
                detail=(
                    "The 'arcadedb-embedded' package ships with synesis-graph. "
                    "Reinstall with: pip install --force-reinstall synesis-graph"
                ),
            ) from e

        try:
            with _without_jvm_startup_noise():
                factory = arcadedb_embedded.DatabaseFactory(
                    str(self._path), jvm_kwargs={"jvm_args": _quiet_jvm_args()}
                )
                self._db = factory.open() if factory.exists() else factory.create()
        except Exception as e:
            raise ArcadeDBError(
                f"Could not open the embedded database at {self._path}",
                detail=str(e),
            ) from e

    def close(self) -> None:
        """Closes the database. Safe to call more than once."""
        if self._db is not None:
            try:
                self._db.close()
            finally:
                self._db = None

    def __enter__(self) -> ArcadeDBEmbeddedClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _args(params: dict[str, Any] | None) -> tuple[Any, ...]:
        """Positional arguments for the binding, omitting empty parameters.

        Never `(None,)`: JPype cannot resolve the Java overload from a null and
        raises `Ambiguous overloads found for ...LocalDatabase.command(...)`.
        An empty dict works too, but omitting the argument is closer to what the
        statement means and avoids constructing a Java map for nothing.
        """
        return (params,) if params else ()

    @staticmethod
    def _materialize(result_set: Any) -> list[dict[str, Any]]:
        """Turns the binding's return value into the list the contract promises.

        Two conversions in one place, because both stem from the same cursor:
        `None` (a write produced no result set) becomes `[]`, and a live
        `ResultSet` is drained now. Draining is not an optimisation — the cursor
        is single-pass, so a caller that iterated it twice would silently see no
        rows the second time.
        """
        if result_set is None:
            return []
        return [row.to_dict() for row in result_set]

    def _require_db(self) -> Any:
        if self._db is None:
            raise ArcadeDBError("The embedded database is closed")
        return self._db

    # -- queries and commands ----------------------------------------------
    def command(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
        *,
        language: str = "cypher",
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Executes a statement that may modify data or schema.

        `database` is accepted for signature compatibility and ignored: an
        embedded client is bound to one database for its lifetime.
        """
        db = self._require_db()
        try:
            return self._materialize(db.command(language, statement, *self._args(params)))
        except Exception as e:
            raise self._translate(e, statement) from e

    def query(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
        *,
        language: str = "cypher",
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Executes a read-only statement."""
        db = self._require_db()
        try:
            return self._materialize(db.query(language, statement, *self._args(params)))
        except Exception as e:
            raise self._translate(e, statement) from e

    @staticmethod
    def _translate(exc: Exception, statement: str) -> ArcadeDBError:
        """Re-raises the binding's failure as this package's `ArcadeDBError`.

        The binding defines a class with the same name in its own module, so
        `except ArcadeDBError` in the sync layer does **not** catch it. Nine such
        handlers exist; without this translation each would let an engine error
        escape as an unhandled exception, past the code written to report it.

        The statement is carried as the detail for the same reason the HTTP
        client carries the server's diagnostic: a schema or syntax failure is
        only debuggable next to the statement that caused it.
        """
        if isinstance(exc, ArcadeDBError):
            return exc
        return ArcadeDBError(str(exc) or type(exc).__name__, detail=statement[:200])

    # -- transactions -------------------------------------------------------
    def begin(self, database: str | None = None) -> str:
        """Opens a transaction; later statements join it until commit/rollback.

        Returns an empty string: the HTTP client returns its session id here, but
        the embedded engine tracks the transaction on the database object itself
        and has no identifier to hand back. No caller consumes the value — the
        sync layer's single `client.begin()` discards it.
        """
        db = self._require_db()
        try:
            db.begin()
        except Exception as e:
            raise self._translate(e, "begin") from e
        return ""

    def commit(self, database: str | None = None) -> None:
        """Commits the open transaction."""
        db = self._require_db()
        try:
            db.commit()
        except Exception as e:
            raise self._translate(e, "commit") from e

    def rollback(self, database: str | None = None) -> None:
        """Rolls the open transaction back."""
        db = self._require_db()
        try:
            db.rollback()
        except Exception as e:
            raise self._translate(e, "rollback") from e
