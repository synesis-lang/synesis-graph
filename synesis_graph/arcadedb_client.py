"""Thin HTTP client for the ArcadeDB server.

ArcadeDB exposes everything this pipeline needs over its HTTP/JSON API, so the
backend needs no driver: the transport is a few hundred lines of `urllib`.

Why not the BOLT protocol, which ArcadeDB also speaks? It would let the Neo4j
adapter be reused almost verbatim, but the BOLT plugin is not loaded by default —
enabling it means passing `-Darcadedb.server.plugins=Bolt:...` on *every* server
start, and ArcadeDB has no persistent config file for plugins. That turns a server
configuration chore into a requirement for every user of this tool. The HTTP API
works on a stock installation.

Why `urllib` rather than `requests`? The package declares exactly two runtime
dependencies and isolates each backend behind an optional extra. `urllib` is
stdlib, so the ArcadeDB backend adds nothing to install — matching how the HTML
backend already works, and avoiding an extra that exists only to make one POST.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("synesis2graph")

# The ArcadeDB server answers a normal command in well under a second; this bound
# only exists so a hung server surfaces as an error instead of a stalled pipeline.
# Index creation over a large corpus is the slow case, hence minutes rather than
# seconds.
DEFAULT_TIMEOUT = 300.0

# Session header for server-side transactions. ArcadeDB is stateless over HTTP:
# /begin returns this header and every statement carrying it joins that transaction.
SESSION_HEADER = "arcadedb-session-id"

#: Gateway statuses that mean "the hop in front of the database failed", not
#: "your request was wrong". Measured against a stock Hostinger container: the
#: same 50,000-row statement 502s under load and succeeds six times in a row
#: minutes later, and the server itself stays healthy throughout (74ms on
#: /api/v1/ready while a 502 was being returned). A reverse proxy — Traefik in
#: that deployment — is cutting the connection, so the failure is transient and
#: says nothing about the statement.
#:
#: 502 Bad Gateway, 503 Service Unavailable, 504 Gateway Timeout. 500 is
#: deliberately absent: that is the database answering, and repeating a
#: statement it rejected only repeats the rejection.
TRANSIENT_GATEWAY_STATUSES = frozenset({502, 503, 504})

#: How many times a request that provably never reached the database is retried.
#: Kept small: these are recovered in seconds or not at all, and a long retry
#: ladder only delays an honest error.
CONNECT_RETRY_ATTEMPTS = 3


class ArcadeDBError(Exception):
    """An ArcadeDB request failed.

    Carries the server's own diagnostic rather than a generic HTTP failure: the
    JSON error body names the exception class and the offending detail, which is
    what makes a schema or syntax problem debuggable.

    `retryable` answers the only question a caller can act on: is it worth
    trying again? It is set at the point the failure is classified, because that
    is where the evidence is — by the time an error reaches the pipeline, all
    that is left is a message.

    `applied_unknown` is the more dangerous flag, and it exists because *not
    knowing* is a distinct outcome from failing. When a gateway cuts the
    connection, the database may well have executed the statement and only the
    answer was lost. Retrying that single statement would then write it twice —
    and since `rebuild` writes with `CREATE`, twice means duplicated vertices,
    not a harmless no-op. Callers must recover by repeating the whole
    transaction, never the lost request on its own.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        detail: str | None = None,
        retryable: bool = False,
        applied_unknown: bool = False,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail
        self.retryable = retryable
        self.applied_unknown = applied_unknown

    def __str__(self) -> str:
        parts = [self.message]
        if self.status is not None:
            parts.append(f"(HTTP {self.status})")
        if self.detail:
            parts.append(f"— {self.detail}")
        return " ".join(parts)


class ArcadeDBClient:
    """Client for one ArcadeDB server, optionally bound to a database.

    A single instance is safe to reuse across the whole pipeline: the underlying
    HTTP calls are stateless except for the explicit transaction, which is tracked
    in `session_id`.
    """

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        database: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        # Tolerate a trailing slash so a URL pasted from a browser works.
        self.uri = uri.rstrip("/")
        self.user = user
        self.password = password
        self.database = database
        self.timeout = timeout
        self.session_id: str | None = None
        #: Multiplier on the connect-retry backoff. Exists so tests can drive
        #: the retry path at full speed; production never changes it.
        self.retry_backoff_scale = 1.0

        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self._auth_header = f"Basic {token}"

    # -- transport ----------------------------------------------------------
    def _send_with_connect_retry(
        self, request: urllib.request.Request, path: str, attempts: int
    ) -> tuple[str, dict[str, str]]:
        """Sends one request, retrying only failures that provably wrote nothing.

        The distinction this method draws is the whole point of it:

        **Connection-level failures are retried here.** If the socket never
        opened — connection refused, DNS failure, the server still starting —
        the database cannot have seen the statement, so repeating it is safe
        whether or not the statement is idempotent. Observed on the real server,
        which stopped accepting connections under load and recovered on its own
        within a minute; without this, that minute is a failed export.

        **Everything else is classified and raised, never retried here.** A
        gateway 502 or a client-side timeout means the request was in flight and
        its outcome is unknown: the database may have applied it and lost only
        the reply. Retrying such a statement on its own would duplicate it, and
        with `rebuild` writing through `CREATE` that means duplicated vertices
        rather than a harmless repeat. Those are marked `applied_unknown` and
        left for the caller to recover by redoing the entire transaction, which
        is idempotent because a rebuild clears first.
        """
        last: ArcadeDBError | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return (
                        response.read().decode("utf-8"),
                        {k.lower(): v for k, v in response.headers.items()},
                    )
            except urllib.error.HTTPError as exc:
                # The interesting part of a 4xx/5xx is the JSON body, not the
                # status line: it carries ArcadeDB's exception class and message.
                raw_error = ""
                try:
                    raw_error = exc.read().decode("utf-8")
                    parsed = json.loads(raw_error)
                    detail = parsed.get("detail") or parsed.get("exception")
                    message = parsed.get("error") or exc.reason
                except Exception:
                    detail = raw_error[:500] or None
                    message = str(exc.reason)
                gateway = exc.code in TRANSIENT_GATEWAY_STATUSES
                raise ArcadeDBError(
                    self._gateway_message(exc.code) if gateway else str(message),
                    status=exc.code,
                    detail=detail,
                    retryable=gateway,
                    applied_unknown=gateway,
                ) from exc
            except TimeoutError as exc:
                # Ours, not the server's: it may still be working on it.
                raise ArcadeDBError(
                    f"ArcadeDB did not answer within {self.timeout:g}s",
                    detail=(
                        f"{path} — the server may still be applying this statement. "
                        "Raise the timeout with [arcadedb].timeout if this recurs."
                    ),
                    retryable=True,
                    applied_unknown=True,
                ) from exc
            except urllib.error.URLError as exc:
                # A URLError wrapping a socket timeout is the same "in flight,
                # outcome unknown" case as TimeoutError above; anything else
                # failed to connect and provably wrote nothing.
                if isinstance(exc.reason, TimeoutError):
                    raise ArcadeDBError(
                        f"ArcadeDB did not answer within {self.timeout:g}s",
                        detail=f"{path} — the server may still be applying this statement.",
                        retryable=True,
                        applied_unknown=True,
                    ) from exc
                last = ArcadeDBError(
                    f"Cannot reach ArcadeDB at {self.uri}",
                    detail=str(exc.reason),
                    retryable=True,
                )
                if attempt + 1 < attempts:
                    # Exponential, and short: a server that is coming back does
                    # so in seconds. Jitter is unnecessary because a single
                    # client is not a thundering herd. Scaled by
                    # `retry_backoff_scale` so tests exercise the retry without
                    # paying for the wait.
                    time.sleep(2**attempt * self.retry_backoff_scale)
                    logger.debug(
                        "Retrying %s after connection failure (%d/%d)",
                        path,
                        attempt + 1,
                        attempts,
                    )
                    continue
        assert last is not None  # only reachable once the retry loop is exhausted
        raise last

    @staticmethod
    def _gateway_message(status: int) -> str:
        """Names the hop that failed, instead of repeating the raw status.

        "Bad Gateway (HTTP 502)" reads as a database error and sends the reader
        looking in the wrong place. The database was healthy every time this was
        observed; it is the reverse proxy in front of it that gave up.
        """
        return (
            f"The server's proxy closed the connection (HTTP {status}) before "
            "ArcadeDB answered"
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        with_session: bool = False,
        attempts: int = CONNECT_RETRY_ATTEMPTS,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Performs one HTTP request, returning (body, response headers).

        Headers come back because /begin communicates the transaction id there
        rather than in the body.
        """
        url = f"{self.uri}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None

        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", self._auth_header)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if with_session and self.session_id:
            request.add_header(SESSION_HEADER, self.session_id)

        raw, headers = self._send_with_connect_retry(request, path, attempts)

        if not raw:
            # /begin and other control endpoints legitimately answer with no body.
            return {}, headers

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ArcadeDBError(
                "ArcadeDB returned a non-JSON response",
                detail=raw[:500],
            ) from exc

        # A 200 can still carry an error field; treat it as a failure rather than
        # letting an empty `result` look like a successful no-op.
        if isinstance(body, dict) and "error" in body:
            raise ArcadeDBError(
                str(body.get("error")),
                detail=body.get("detail") or body.get("exception"),
            )

        return body, headers

    # -- queries and commands ----------------------------------------------
    def _execute(
        self,
        endpoint: str,
        statement: str,
        params: dict[str, Any] | None,
        language: str,
        database: str | None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        db = database or self.database
        if not db:
            raise ArcadeDBError("No database selected", detail=statement[:200])

        payload: dict[str, Any] = {"language": language, "command": statement}
        if params:
            payload["params"] = params
        if limit is not None:
            payload["limit"] = limit

        body, _ = self._request(
            "POST", f"/api/v1/{endpoint}/{db}", payload, with_session=True
        )
        if body.get("truncated"):
            # The server caps a response at `limit` rows (20,000 by default) and
            # says so here. Ignoring the flag is how 2,585 of 22,585 concepts
            # silently ended up with no PageRank while the terminal reported
            # "[OK] PageRank calculated (20000 nodes)" — a plausible number,
            # written into a run that claimed success. A researcher asking for
            # "the most central concepts" would have got an answer that quietly
            # excluded 11% of the ontology.
            #
            # Refusing is the only safe response: a partial result that looks
            # complete is worse than no result, and only the caller knows how to
            # ask for less — either by raising `limit` or by paging.
            raise ArcadeDBError(
                "The server returned only part of the answer",
                detail=(
                    f"{body.get('returned')} rows, capped at {body.get('limit')}. "
                    "Raise the request's `limit`, or read the result in pages."
                ),
            )
        result = body.get("result", [])
        return result if isinstance(result, list) else [result]

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

        Defaults to Cypher because the sync layer reuses the Neo4j statements
        verbatim; schema and index work passes `language="sql"`, since those are
        ArcadeDB-specific and have no Cypher equivalent.

        `limit` raises the server's per-response row cap for this one request.
        See `query` — the reasoning is the same and matters most here, because
        the `algo.*` procedures are issued through `command`.
        """
        return self._execute("command", statement, params, language, database, limit)

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

        `limit` raises the row cap the server applies to a single response,
        default 20,000. It is a **per-request** ceiling, so nothing about the
        server's configuration changes and no other client is affected — which
        is what makes this usable against a stock installation.

        Measured on the real corpus, this is the difference between reading a
        272,193-row result once (8.5s) and reading it in fourteen pages, each of
        which re-runs the procedure that produced it (about 34 minutes for
        betweenness). Pass it whenever the row count is known to exceed the
        default and the whole result is genuinely needed.
        """
        return self._execute("query", statement, params, language, database, limit)

    # -- transactions -------------------------------------------------------
    def begin(self, database: str | None = None) -> str:
        """Opens a server-side transaction and remembers its session id.

        Subsequent `command`/`query` calls join it automatically until `commit`
        or `rollback`.
        """
        db = database or self.database
        if not db:
            raise ArcadeDBError("No database selected", detail="begin")
        if self.session_id:
            raise ArcadeDBError("A transaction is already open", detail=self.session_id)

        _, headers = self._request("POST", f"/api/v1/begin/{db}")
        session_id = headers.get(SESSION_HEADER)
        if not session_id:
            raise ArcadeDBError(
                "ArcadeDB did not return a transaction session id",
                detail=f"expected the '{SESSION_HEADER}' response header",
            )
        self.session_id = session_id
        return session_id

    def commit(self, database: str | None = None) -> None:
        """Commits the open transaction."""
        self._finish_transaction("commit", database)

    def rollback(self, database: str | None = None) -> None:
        """Rolls the open transaction back."""
        self._finish_transaction("rollback", database)

    def _finish_transaction(self, action: str, database: str | None) -> None:
        db = database or self.database
        if not self.session_id:
            raise ArcadeDBError(f"No open transaction to {action}")
        try:
            self._request("POST", f"/api/v1/{action}/{db}", with_session=True)
        finally:
            # Clear the id even when the server rejects the call: the transaction
            # is gone either way, and keeping a stale id would poison every later
            # statement by attaching it to a session the server has forgotten.
            self.session_id = None

    # -- server / database management --------------------------------------
    def server_command(self, command: str) -> dict[str, Any]:
        """Sends a control command to the server (`create database`, ...).

        Database lifecycle lives here rather than in SQL: ArcadeDB has no
        `CREATE DATABASE` statement, which is one of the three places the Neo4j
        pipeline could not be reused as-is.
        """
        body, _ = self._request("POST", "/api/v1/server", {"command": command})
        return body

    def list_databases(self) -> list[str]:
        body, _ = self._request("GET", "/api/v1/databases")
        result = body.get("result", [])
        return [str(name) for name in result] if isinstance(result, list) else []

    def database_exists(self, database: str | None = None) -> bool:
        db = database or self.database
        return db in self.list_databases() if db else False

    def create_database(self, database: str | None = None) -> None:
        """Creates the database when missing.

        Idempotent by checking first: ArcadeDB's `create database` fails on an
        existing name, and a re-export of the same project must not be an error.
        """
        db = database or self.database
        if not db:
            raise ArcadeDBError("No database selected", detail="create_database")
        if self.database_exists(db):
            logger.debug("ArcadeDB database already exists: %s", db)
            return
        self.server_command(f"create database {db}")

    def drop_database(self, database: str | None = None) -> None:
        db = database or self.database
        if not db:
            raise ArcadeDBError("No database selected", detail="drop_database")
        self.server_command(f"drop database {db}")

    def is_ready(self) -> bool:
        """True when the server answers its readiness probe.

        Used by preflight to fail with "server not reachable" before the project
        is compiled, rather than after.

        Asks once, deliberately: this exists to answer *fast* so a researcher
        pointing at the wrong URL learns it immediately instead of after a
        compilation. Retrying a probe would defeat its purpose — the retry that
        matters happens later, around the writes that would otherwise be lost.
        """
        try:
            self._request("GET", "/api/v1/ready", attempts=1)
            return True
        except ArcadeDBError:
            return False

    def close(self) -> None:
        """Releases the open transaction, if any.

        There is no connection to close — `urllib` opens one per request — but an
        abandoned server-side transaction would hold locks until it expires.
        """
        if self.session_id:
            try:
                self.rollback()
            except ArcadeDBError as exc:
                logger.debug("Rollback during close failed: %s", exc)
                self.session_id = None
