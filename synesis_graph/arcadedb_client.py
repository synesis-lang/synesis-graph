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


class ArcadeDBError(Exception):
    """An ArcadeDB request failed.

    Carries the server's own diagnostic rather than a generic HTTP failure: the
    JSON error body names the exception class and the offending detail, which is
    what makes a schema or syntax problem debuggable.
    """

    def __init__(self, message: str, *, status: int | None = None, detail: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail

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

        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self._auth_header = f"Basic {token}"

    # -- transport ----------------------------------------------------------
    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        with_session: bool = False,
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

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
                headers = {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            # The interesting part of a 4xx/5xx is the JSON body, not the status
            # line: it carries ArcadeDB's exception class and message.
            raw_error = ""
            try:
                raw_error = exc.read().decode("utf-8")
                parsed = json.loads(raw_error)
                detail = parsed.get("detail") or parsed.get("exception")
                message = parsed.get("error") or exc.reason
            except Exception:
                detail = raw_error[:500] or None
                message = str(exc.reason)
            raise ArcadeDBError(str(message), status=exc.code, detail=detail) from exc
        except urllib.error.URLError as exc:
            raise ArcadeDBError(
                f"Cannot reach ArcadeDB at {self.uri}",
                detail=str(exc.reason),
            ) from exc
        except TimeoutError as exc:
            raise ArcadeDBError(
                f"ArcadeDB request timed out after {self.timeout}s",
                detail=path,
            ) from exc

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
    ) -> list[dict[str, Any]]:
        db = database or self.database
        if not db:
            raise ArcadeDBError("No database selected", detail=statement[:200])

        payload: dict[str, Any] = {"language": language, "command": statement}
        if params:
            payload["params"] = params

        body, _ = self._request(
            "POST", f"/api/v1/{endpoint}/{db}", payload, with_session=True
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
    ) -> list[dict[str, Any]]:
        """Executes a statement that may modify data or schema.

        Defaults to Cypher because the sync layer reuses the Neo4j statements
        verbatim; schema and index work passes `language="sql"`, since those are
        ArcadeDB-specific and have no Cypher equivalent.
        """
        return self._execute("command", statement, params, language, database)

    def query(
        self,
        statement: str,
        params: dict[str, Any] | None = None,
        *,
        language: str = "cypher",
        database: str | None = None,
    ) -> list[dict[str, Any]]:
        """Executes a read-only statement."""
        return self._execute("query", statement, params, language, database)

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
        """
        try:
            self._request("GET", "/api/v1/ready")
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
