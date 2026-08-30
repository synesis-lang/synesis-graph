"""The transport contract the ArcadeDB sync layer depends on.

The sync layer (`backends/arcadedb.py`, `metrics_arcadedb.py`) was typed against
`ArcadeDBClient`, the concrete HTTP client. That is more than it needs: across
both modules it calls exactly five methods — `command`, `query`, `begin`,
`commit` and `rollback` — and reads/writes one attribute, `database`.

Naming that smaller surface as a Protocol is what lets a second transport exist.
The embedded engine (`arcadedb-embedded`, in-process, no server) speaks the same
Cypher and the same SQL; only the wire changes. With the sync layer typed against
the contract instead of the class, adding that transport touches no query, no
schema statement and none of the eight `_sync_*` functions.

Structural typing is the point: `ArcadeDBClient` satisfies this without
inheriting from it, without registering, without importing this module. Nothing
about the HTTP client changes to accommodate a backend that does not exist yet —
the cost falls entirely on whatever new transport wants in, which is where it
belongs.

Deliberately absent: `is_ready`, `list_databases`, `create_database`,
`drop_database`, `server_command` and `close`. Those are preflight, setup and
teardown; they live in the adapter, where the two transports genuinely differ
(an in-process database has no server to probe and no credential to exercise).
Keeping them out is what makes this a contract two transports can both meet.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ArcadeDBTransport(Protocol):
    """What a transport must offer for the sync layer to run over it.

    Signatures mirror `ArcadeDBClient` exactly, including `language` as a
    keyword argument defaulting to `"cypher"`. That default is load-bearing: the
    sync statements are Cypher reused verbatim from the Neo4j backend, while
    schema and index work passes `language="sql"`. A transport that made
    `language` positional or mandatory would force all 20 call sites to change,
    which would defeat the purpose of having a contract at all.
    """

    #: Target database. Mutable: `prepare_destination` assigns it once the
    #: project name is known (`backends/base.py`), so it cannot be read-only.
    database: str | None

    def command(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
        *,
        language: str = "cypher",
        database: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Executes a statement that may modify data or schema.

        Returns a list — never `None`, even for a write that yields no rows. The
        sync layer iterates the result directly, so a transport whose engine
        returns nothing must coerce that to `[]` at its own boundary.
        """
        ...

    def query(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
        *,
        language: str = "cypher",
        database: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Executes a read-only statement.

        Returns a fully materialised list, not a lazy cursor: callers may iterate
        the result more than once, and a single-pass iterator would silently
        yield nothing on the second pass.

        `limit` raises the per-response row cap where a transport has one. The
        HTTP server caps a response at 20,000 rows and flags the remainder as
        truncated; the embedded engine has no such cap and ignores the argument.
        It is part of the contract because the caller cannot know which
        transport it holds, and asking for the whole of a large result must not
        depend on that.
        """
        ...

    def begin(self, database: str | None = None) -> str:
        """Opens a transaction; later calls join it until commit or rollback.

        The returned identifier is an implementation detail — the HTTP client
        returns its session id, and no caller consumes the value.
        """
        ...

    def commit(self, database: str | None = None) -> None:
        """Commits the open transaction."""
        ...

    def rollback(self, database: str | None = None) -> None:
        """Rolls the open transaction back."""
        ...
