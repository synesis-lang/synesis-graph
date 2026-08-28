"""Phase B: publish a local database to chat clients over MCP.

The export commands write a graph and exit. This one does the opposite: it opens
the graph a researcher already built and keeps it reachable, so Claude Desktop,
Claude Code or the VSCode extension can ask questions of it.

The engine does almost all of this on its own — `arcadedb-embedded` bundles the
real ArcadeDB server, and its MCP plugin is auto-discovered at startup. Three
things still need doing, and each is a reason this lives in code rather than in a
snippet a researcher is told to paste:

1. **The MCP endpoint starts disabled.** The embedded distribution ships without
   the `config/mcp-config.json` that the standalone server reads, so the plugin
   registers and then refuses every call. Enabling it is one HTTP request, and it
   does not persist — it has to happen on every start.
2. **Read-only is not the default.** The same request that enables MCP decides
   whether a chat client may write. A corpus is the output of months of coding
   work; handing an assistant `execute_command` on it by default would be the
   wrong trade, so writes are off unless explicitly asked for.
3. **A password has to exist, and the engine keeps the first one forever.**
   The server refuses to start without one, and `root_password` is honoured only
   while `config/server-users.jsonl` is being created. Every later start reads
   the stored hash and ignores what it is given — silently, so a freshly
   generated password produces HTTP 403 after an apparently successful startup.
   One is therefore generated on the first start and remembered beside the
   server's own state, never in the project directory where a `.syn` file might
   be committed.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import secrets
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from synesis_graph.arcadedb_embedded_client import (
    _quiet_jvm_args,
    _without_jvm_startup_noise,
)
from synesis_graph.core import ConnectionError, DependencyError, PipelineError

logger = logging.getLogger("synesis2graph")

DEFAULT_PORT = 2480
#: Name of the entry this package manages in a chat client's configuration.
#: Stable on purpose: re-running `serve` updates that one entry instead of
#: accumulating a new one per session.
CLIENT_ENTRY_NAME = "synesis-local"
CONFIG_FILENAME = "claude_desktop_config.json"
#: Requests to the local server are answered in milliseconds; a long timeout here
#: only turns a wrong port into a long wait.
_TIMEOUT = 10.0


#: Where the engine stores the server users it created on a first start.
_USERS_FILE = Path("config") / "server-users.jsonl"


def server_root_is_initialized(root: Path) -> bool:
    """True when this root already has a root user with a password set.

    `root_password` is honoured only while that file is being created. On every
    later start the engine reads the stored hash and ignores whatever is passed
    in — silently, so a freshly generated password simply stops working and the
    researcher sees HTTP 403 "User/Password not valid" with nothing to act on.
    """
    return (root / _USERS_FILE).is_file()


@dataclass
class ServeOptions:
    """What the researcher chose, before any of it is acted on."""

    db_path: Path
    port: int = DEFAULT_PORT
    #: Off by default. See the module docstring: a chat client that can write is
    #: a different risk from one that can read, and the difference should be a
    #: decision rather than an inheritance.
    allow_writes: bool = False
    #: Given, the session reuses it (a stable `claude_desktop_config.json` entry
    #: survives restarts). Absent, one is generated and printed.
    password: str | None = None


@dataclass
class ServeHandle:
    """A running server, and everything a client needs to reach it."""

    server: Any
    port: int
    user: str
    password: str
    databases: list[str]

    @property
    def endpoint(self) -> str:
        return f"http://localhost:{self.port}/api/v1/mcp"

    @property
    def basic_auth(self) -> str:
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode("ascii")
        return f"Basic {token}"

    def stop(self) -> None:
        try:
            self.server.stop()
        except Exception as e:  # pragma: no cover - shutdown is best-effort
            logger.debug("Error stopping the server: %s", e)



#: Companion to `_USERS_FILE`: the plaintext the engine's hash was made from.
#: The engine stores only a hash, and a Basic header needs the original, so it
#: has to be kept somewhere. Next to the server's own state is the honest place
#: — inside the graph directory the researcher already treats as the database,
#: not in the project folder where a `.syn` file might be committed.
_PASSWORD_FILE = Path("config") / "synesis-server-password"


def _remembered_password(root: Path) -> str | None:
    """The password from an earlier start of this same root, if we stored it."""
    try:
        text = (root / _PASSWORD_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _remember_password(root: Path, password: str) -> None:
    """Stores the password for the next start. Never fatal if it fails."""
    target = root / _PASSWORD_FILE
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(password, encoding="utf-8")
        if hasattr(os, "chmod"):
            with contextlib.suppress(OSError):
                os.chmod(target, 0o600)
    except OSError as e:  # pragma: no cover - depends on the filesystem
        logger.debug("Could not remember the server password: %s", e)



def _reset_server_credentials(root: Path) -> PipelineError | None:
    """Removes the stored server user so the engine accepts a new password.

    Only the credential file is touched. The graphs are in `databases/`, and the
    engine recreates the user on the next start from the password it is handed.
    The old file is kept beside it, so nothing is destroyed outright.
    """
    users = root / _USERS_FILE
    try:
        if users.is_file():
            users.replace(users.with_suffix(users.suffix + ".superseded"))
        return None
    except OSError as e:
        return ConnectionError(
            message="The stored server credential could not be reset",
            stage="serve",
            details=(
                f"{users}: {e}. Delete that file by hand and run again, or set "
                f"SYNESIS_DB_PASSWORD to the password used the first time."
            ),
        )


def generate_password() -> str:
    """A per-session password, strong enough that printing it is the weak link."""
    return secrets.token_urlsafe(24)


def find_databases(root: Path) -> list[str]:
    """Names the databases the server will find under `root`.

    The server looks in `<root>/databases/`, never in `<root>` itself. Checking
    the same place beforehand is what turns the layout mistake into a message: an
    export written to `<root>/<project>` would otherwise leave the server running
    happily over nothing, reporting success while every query comes back empty.
    """
    databases_dir = root / "databases"
    if not databases_dir.is_dir():
        return []
    return sorted(p.name for p in databases_dir.iterdir() if p.is_dir())


def _mcp_config_payload(allow_writes: bool) -> dict[str, bool]:
    """The MCP permission set, written out field by field.

    Every flag is stated rather than relying on the server's defaults, because
    the defaults are what a future version is free to change — and the one thing
    this must not do is quietly widen what a chat client may do to a corpus.
    """
    return {
        "enabled": True,
        "allowReads": True,
        "allowInsert": allow_writes,
        "allowUpdate": allow_writes,
        "allowDelete": allow_writes,
        "allowSchemaChange": allow_writes,
        # Never, regardless of --allow-writes: administrative calls reach beyond
        # the corpus to the server itself, which is not what "let the assistant
        # edit my data" means.
        "allowAdmin": False,
    }


def enable_mcp(
    port: int, user: str, password: str, *, allow_writes: bool = False
) -> PipelineError | None:
    """Turns the MCP endpoint on for this session.

    Idempotent, and deliberately called on every start: the setting lives in the
    running server, not on disk, so a restart silently reverts to disabled.
    """
    body = json.dumps(_mcp_config_payload(allow_writes)).encode("utf-8")
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    request = urllib.request.Request(
        f"http://localhost:{port}/api/v1/mcp/config",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            response.read()
        return None
    except urllib.error.HTTPError as e:
        return ConnectionError(
            message="The MCP endpoint could not be enabled",
            stage="serve",
            details=f"HTTP {e.code} from the server: {e.read()[:200]!r}",
        )
    except urllib.error.URLError as e:
        return ConnectionError(
            message="The MCP endpoint could not be enabled",
            stage="serve",
            details=f"The server did not answer on port {port}: {e.reason}",
        )


def start_server(options: ServeOptions) -> ServeHandle | PipelineError:
    """Starts the engine over `db_path` and enables MCP.

    Returns a handle, or the first thing that went wrong — never a half-started
    server: if enabling MCP fails, the engine is stopped before returning, since
    a server nobody can query is worse than no server at all.
    """
    try:
        import arcadedb_embedded
    except ImportError:
        return DependencyError(
            message="The local graph engine is not available",
            stage="dependency",
            details=(
                "The 'arcadedb-embedded' package ships with synesis-graph. "
                "Reinstall with: pip install --force-reinstall synesis-graph"
            ),
        )

    root = options.db_path.resolve()
    databases = find_databases(root)
    if not databases:
        return ConnectionError(
            message="No database found to serve",
            stage="serve",
            details=(
                f"Looked in {root / 'databases'}. Export one first with: "
                f"synesis-graph arcadedb-embedded --project <project>.synp "
                f"--db-path {root}"
            ),
        )

    # A root already initialised keeps the password from its first start, so a
    # generated one would be rejected. Requiring the researcher to remember it
    # is the wrong trade for a local, single-user graph: the credential exists
    # to satisfy the engine, not to protect a shared system. Reuse it instead,
    # and keep it out of the project directory by storing it beside the server's
    # own state.
    remembered = _remembered_password(root)
    password = options.password or remembered or generate_password()

    if server_root_is_initialized(root) and not options.password and not remembered:
        # The stored credential is unknown to us and to the researcher, and it
        # guards nothing: a local server, reachable only from this machine,
        # whose password exists because the engine demands one. Refusing here
        # would hand over a chore ("delete this file") whose only possible answer
        # is yes. Resetting the credential is safe and reversible-by-nature —
        # the databases live in `databases/`, which is not touched.
        error = _reset_server_credentials(root)
        if error:
            return error
        logger.debug("Reset the unknown server credential at %s", root / _USERS_FILE)

    # The engine refuses anything shorter, and its message arrives only after a
    # failed start. Saying so here names the variable the researcher set.
    if len(password) < 8:
        return ConnectionError(
            message="The database password is too short",
            stage="serve",
            details=(
                "The engine requires at least 8 characters. "
                "Set SYNESIS_DB_PASSWORD to a longer value, or unset it to have "
                "one generated."
            ),
        )

    # Start the JVM quietly before the server does it noisily: `create_server`
    # would otherwise bring it up with the engine's default INFO logging, and the
    # first thing the researcher sees would be page-writer chatter.
    # An already-running JVM cannot be reconfigured, and that is fine: it means
    # something else in this process started it, quietly or not.
    with _without_jvm_startup_noise(), contextlib.suppress(Exception):
        arcadedb_embedded.jvm.start_jvm(jvm_args=_quiet_jvm_args())

    try:
        with _without_jvm_startup_noise():
            server = arcadedb_embedded.create_server(
                root_path=str(root),
                root_password=password,
                config={"http_port": options.port},
            )
            server.start()
    except Exception as e:
        # Guessing at the cause is worse than naming the two likely ones: the
        # engine's own message is specific (a short password, a locked file),
        # and appending "is the port in use?" to it sends the reader chasing
        # the wrong thing.
        hint = (
            f" Port {options.port} may already be in use."
            if "port" in str(e).lower() or "bind" in str(e).lower()
            else ""
        )
        return ConnectionError(
            message="The local server could not be started",
            stage="serve",
            details=f"{e}.{hint}",
        )

    mcp_error = enable_mcp(
        options.port, "root", password, allow_writes=options.allow_writes
    )
    if mcp_error:
        # Best-effort: we are already returning a failure, and a stop that also
        # fails must not replace the real diagnosis with a shutdown message.
        with contextlib.suppress(Exception):
            server.stop()
        return mcp_error

    # Remember it so the next start can reuse it without the researcher having
    # to. Stored beside the server's own state, never in the project directory.
    _remember_password(root, password)

    return ServeHandle(
        server=server,
        port=options.port,
        user="root",
        password=password,
        databases=databases,
    )


def server_entry(handle: ServeHandle, *, windows: bool) -> dict[str, Any]:
    """The one `mcpServers` entry describing this running server.

    Just the entry, without the surrounding file: what goes *around* it depends
    on what the researcher already has, and assuming an empty file is how a
    working configuration gets destroyed.
    """
    return {
        # `npx` alone routinely fails to launch on Windows; the shim does not.
        "command": "npx.cmd" if windows else "npx",
        "args": [
            "-y",
            "mcp-remote",
            handle.endpoint,
            "--header",
            f"Authorization: {handle.basic_auth}",
            "--transport",
            "http-only",
        ],
    }


def client_config_snippet(handle: ServeHandle, *, windows: bool) -> str:
    """The entry rendered as JSON, for pasting by hand.

    Deliberately *not* wrapped in `{"mcpServers": {...}}`. A researcher who
    already has entries — most of them will — would paste that wrapper over the
    ones they have and lose them. Showing the entry alone makes the shape of the
    edit obvious: it goes beside the others.
    """
    entry = {CLIENT_ENTRY_NAME: server_entry(handle, windows=windows)}
    return json.dumps(entry, indent=2)



def vscode_server_entry(handle: ServeHandle) -> dict[str, Any]:
    """The same server, in the shape VS Code reads.

    VS Code speaks HTTP to an MCP server directly: no `npx`, no `mcp-remote`
    bridge, and the key is `servers` rather than `mcpServers`. Emitting Claude
    Desktop's shape here would produce a file VS Code silently ignores, so the
    two are kept apart rather than approximated.
    """
    return {
        "type": "http",
        "url": handle.endpoint,
        "headers": {"Authorization": handle.basic_auth},
    }


def vscode_config_snippet(handle: ServeHandle) -> str:
    """The VS Code entry as JSON, for `.vscode/mcp.json`."""
    return json.dumps({CLIENT_ENTRY_NAME: vscode_server_entry(handle)}, indent=2)


def install_into_vscode(
    handle: ServeHandle, *, path: Path, name: str = CLIENT_ENTRY_NAME
) -> tuple[Path, bool] | PipelineError:
    """Adds (or updates) this server in a VS Code `mcp.json`.

    Same contract as the Claude Desktop installer — preserve everything already
    there, back up before writing, refuse rather than overwrite what cannot be
    parsed — but against `servers`, which is VS Code's key.
    """
    config: dict[str, Any] = {}
    if path.exists():
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return ConnectionError(
                message="The VS Code MCP configuration could not be read",
                stage="serve",
                details=f"{path}: {e}. Fix or move the file, or add the entry by hand.",
            )
        if not isinstance(config, dict):
            return ConnectionError(
                message="The VS Code MCP configuration has an unexpected shape",
                stage="serve",
                details=f"{path} does not contain a JSON object.",
            )

    servers = config.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    replaced = name in servers
    servers[name] = vscode_server_entry(handle)
    config["servers"] = servers

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".synesis-backup")
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        rendered = json.dumps(config, indent=2, ensure_ascii=False) + chr(10)
        path.write_text(rendered, encoding="utf-8")
    except OSError as e:
        return ConnectionError(
            message="The VS Code MCP configuration could not be written",
            stage="serve",
            details=(
                f"{path}: {e}. The file may be read-only or locked by a running "
                f"editor. Add the entry by hand — the entry is printed below."
            ),
        )

    return path, replaced


def claude_desktop_config_path() -> Path | None:
    """Where Claude Desktop keeps its configuration on this platform.

    Only the two platforms with an official Claude Desktop build are claimed.
    Elsewhere — Linux, where there is no official build and third-party packages
    put the file wherever they like — this returns None, and the caller prints
    the entry instead of writing to a path it guessed. An override is honoured
    first, so an unusual install is a setting rather than a dead end.

    Returning a path is not a promise that it exists or is writable; that is the
    caller's to discover and report.
    """
    override = os.environ.get("SYNESIS_MCP_CONFIG")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "Claude" / CONFIG_FILENAME if appdata else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / CONFIG_FILENAME
    return None


def install_into_claude_desktop(
    handle: ServeHandle, *, path: Path | None = None, name: str = CLIENT_ENTRY_NAME
) -> tuple[Path, bool] | PipelineError:
    """Adds (or updates) this server's entry in Claude Desktop's configuration.

    Hand-editing JSON is exactly the step a qualitative researcher should not
    have to take: a missing comma silently disables every MCP server they have,
    and the failure surfaces later, somewhere else, as "Claude cannot see my
    data". Doing the edit here removes that whole class of problem.

    Everything already in the file is preserved — other servers, and the
    top-level keys Claude Desktop keeps beside `mcpServers` (preferences, paths).
    The file is read, one key is set, and the result is written back; a backup of
    the previous content is kept next to it.

    Returns `(path, replaced)` where `replaced` says whether an entry of this
    name was already there.
    """
    target = path or claude_desktop_config_path()
    if target is None:
        return ConnectionError(
            message="Unknown location for the Claude Desktop configuration",
            stage="serve",
            details=f"Unsupported platform: {sys.platform}. Add the entry by hand.",
        )

    config: dict[str, Any] = {}
    if target.exists():
        try:
            config = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            # Refuse rather than overwrite: the file is unreadable to us but may
            # be perfectly good to a human, and rewriting it would discard work.
            return ConnectionError(
                message="The Claude Desktop configuration could not be read",
                stage="serve",
                details=f"{target}: {e}. Fix or move the file, or add the entry by hand.",
            )
        if not isinstance(config, dict):
            return ConnectionError(
                message="The Claude Desktop configuration has an unexpected shape",
                stage="serve",
                details=f"{target} does not contain a JSON object.",
            )

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    replaced = name in servers
    servers[name] = server_entry(handle, windows=sys.platform == "win32")
    config["mcpServers"] = servers

    if not target.parent.is_dir():
        # Creating the tree would invent a configuration for an application that
        # is not installed here — a file nothing reads, in a place the user did
        # not choose.
        return ConnectionError(
            message="Claude Desktop does not appear to be installed here",
            stage="serve",
            details=(
                f"No directory at {target.parent}. Add the entry by hand, or set "
                f"SYNESIS_MCP_CONFIG to the configuration file's real location."
            ),
        )

    try:
        if target.exists():
            backup = target.with_suffix(target.suffix + ".synesis-backup")
            backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        rendered = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        target.write_text(rendered, encoding="utf-8")
    except OSError as e:
        return ConnectionError(
            message="The Claude Desktop configuration could not be written",
            stage="serve",
            details=(
                f"{target}: {e}. The file may be read-only, owned by another "
                f"user, or locked by a running Claude Desktop. Add the entry by "
                f"hand — the entry is printed below."
            ),
        )

    return target, replaced
