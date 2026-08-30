"""CLI for synesis-graph."""

from __future__ import annotations

import io
import logging
import sys
import threading
from pathlib import Path

import click

# Force UTF-8 on Windows terminals before Rich or Click emit any output.
if hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer") and getattr(sys.stderr, "encoding", "").lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from synesis_graph import (
    BACKEND_ARCADEDB,
    BACKEND_ARCADEDB_EMBEDDED,
    BACKEND_HTML,
    BACKEND_NEO4J,
    __version__,
)
from synesis_graph.core import PipelineError
from synesis_graph.serve import DEFAULT_PORT as DEFAULT_SERVE_PORT

# ---------------------------------------------------------------------------
# Style helpers
# ---------------------------------------------------------------------------


def _tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(text: str, **kwargs) -> str:
    return click.style(text, **kwargs) if _tty() else text


def _configure_logging(verbose: int, quiet: int) -> None:
    """Set log level on the synesis2graph logger: -q → WARNING/ERROR, default → INFO, -v → DEBUG."""
    if quiet >= 2:
        level = logging.ERROR
    elif quiet == 1:
        level = logging.WARNING
    elif verbose >= 1:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s")
    logging.getLogger("synesis2graph").setLevel(level)

    # The Neo4j driver logs raw server notifications ("Neo.ClientNotification.
    # Schema.IndexOrConstraintDoesNotExist", stack-trace-shaped and addressed to
    # database engineers). They would inherit the root INFO level and bury the
    # readable pipeline output this tool is built around. Silence them below
    # WARNING; `-v` still brings them back for debugging.
    _driver_level = logging.DEBUG if verbose >= 1 else logging.WARNING
    for _name in ("neo4j.notifications", "neo4j", "neo4j.io", "neo4j.pool"):
        logging.getLogger(_name).setLevel(_driver_level)


# ---------------------------------------------------------------------------
# Main help
# ---------------------------------------------------------------------------


def _build_main_help() -> str:
    title = _c("SYNESIS GRAPH", fg="green", bold=True) + f" (v{__version__})"
    desc = "Universal pipeline from Synesis projects to graph databases and visualizations."

    usage = _c("Usage:", fg="yellow", bold=True) + " synesis-graph [OPTIONS] COMMAND [ARGUMENTS]..."

    # Grouped because the commands do different kinds of work, and the grouping
    # is what keeps the module's role legible: a backend exports and exits.
    groups = [
        (
            "Graph Backends",
            [
                ("neo4j", "Sync project to a Neo4j database (bolt://)"),
                ("arcadedb", "Sync project to an ArcadeDB database (http://)"),
                (
                    "arcadedb-embedded",
                    "Sync project to a local ArcadeDB database (no server)",
                ),
                ("html", "Render an interactive HTML graph visualization"),
            ],
        ),
        (
            "Local Database",
            [
                ("serve", "Keep a local database running for chat clients"),
            ],
        ),
    ]

    opt_rows = [
        ("-v, --verbose", "Increase log verbosity (DEBUG). Repeatable."),
        ("-q, --quiet", "Decrease log verbosity (-q WARNING, -qq ERROR). Repeatable."),
        ("--version", "Show version and exit"),
        ("--help", "Show this message and exit"),
    ]

    col = (
        max(
            max(len(name) for _, rows in groups for name, _ in rows),
            max(len(name) for name, _ in opt_rows),
        )
        + 2
    )

    options = (
        _c("Global Options:", fg="yellow", bold=True)
        + "\n"
        + "\n".join(f"  {_c(name.ljust(col), fg='cyan')}  {desc_}" for name, desc_ in opt_rows)
    )

    def _render_group(label: str, rows: list[tuple[str, str]]) -> str:
        lines = [_c("  " + label, fg="yellow", bold=True)]
        for name, desc_ in rows:
            lines.append(f"    {_c(name.ljust(col), fg='green', bold=True)}  {desc_}")
        return "\n".join(lines)

    commands = (
        _c("Commands:", fg="yellow", bold=True)
        + "\n\n"
        + "\n\n".join(_render_group(label, rows) for label, rows in groups)
    )

    hint = _c(
        "Run 'synesis-graph COMMAND --help' for options and examples of each backend.",
        fg="bright_black",
    )

    return "\n\n".join([title, desc, usage, options, commands, hint]) + "\n"


class _SynesisCommand(click.Command):
    def parse_args(self, ctx, args):
        if args == ["help"]:
            args = ["--help"]
        return super().parse_args(ctx, args)

    def format_epilog(self, ctx, formatter):
        if self.epilog:
            formatter.write("\n")
            for line in self.epilog.splitlines():
                formatter.write(line + "\n")


class _SynesisGroup(click.Group):
    command_class = _SynesisCommand

    def format_help(self, ctx, formatter):
        pass

    def get_help(self, ctx):
        out = _build_main_help()
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.buffer.flush()
            raise SystemExit(0)
        return out


# ---------------------------------------------------------------------------
# Example epilog helper
# ---------------------------------------------------------------------------


def _ex(*lines: str) -> str:
    import re

    out = [_c("Examples:", fg="yellow", bold=True)]
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if stripped.startswith("#"):
            out.append(indent + _c(stripped, fg="bright_black"))
        else:
            tokens = re.split(r"(\s+)", stripped)
            result = []
            for tok in tokens:
                if tok == "synesis-graph":
                    result.append(_c(tok, fg="green", bold=True))
                elif re.match(r"^--[\w-]+=?", tok):
                    result.append(_c(tok, fg="cyan"))
                elif tok in ("neo4j", "arcadedb-embedded", "arcadedb", "html", "serve"):
                    result.append(_c(tok, fg="green"))
                else:
                    result.append(tok)
            out.append(indent + "".join(result))
    return "\n".join(out)


_EPILOG_NEO4J = _ex(
    "  # Sync with default config (config.toml, bolt://127.0.0.1:7687):",
    "  synesis-graph neo4j --project project.synp",
    "",
    "  # Use a custom config file:",
    "  synesis-graph neo4j --project project.synp --config prod.toml",
    "",
    "  # Load from pre-compiled JSON (Synesis v3.0 export):",
    "  synesis-graph neo4j --json export.json --config prod.toml",
    "",
    "  # Target a specific named database:",
    "  synesis-graph neo4j --project project.synp --database my_corpus",
    "",
    "  # Link several projects: fields declared with IDENTIFIES/REFERS TO are",
    "  # reified into shared identity nodes (e.g. (:Researcher {entity_id})).",
    "  synesis-graph neo4j --project lattes.synp --project abstracts.synp",
    "",
    "  # Name the unified database for a linked graph with --database. Without",
    "  # it, the name is derived from the members (e.g. 'lattes_abstracts').",
    "  synesis-graph neo4j --project lattes.synp --project abstracts.synp --database Quinto_Andar",
)

_EPILOG_ARCADEDB = _ex(
    "  # Sync with default config (config.toml, http://localhost:2480):",
    "  synesis-graph arcadedb --project project.synp",
    "",
    "  # Use a custom config file:",
    "  synesis-graph arcadedb --project project.synp --config prod.toml",
    "",
    "  # Load from pre-compiled JSON (Synesis v3.0 export):",
    "  synesis-graph arcadedb --json export.json --config prod.toml",
    "",
    "  # Target a specific named database:",
    "  synesis-graph arcadedb --project project.synp --database my_corpus",
    "",
    "  # Link several projects, as with the neo4j backend:",
    "  synesis-graph arcadedb --project lattes.synp --project abstracts.synp --database Quinto_Andar",  # noqa: E501
    "",
    "  # Semantic search: embed the concept descriptions. Vectors are generated",
    "  # locally (no API key) and indexed as LSM_VECTOR alongside the full-text",
    "  # index, so a question can find a concept whose words it does not share.",
    "  synesis-graph arcadedb --project project.synp \\",
    "      --vector-embeddings ontology_description",
    "",
    "  # Add the topic for context. Fields are embedded in the order given:",
    "  synesis-graph arcadedb --project project.synp \\",
    "      --vector-embeddings ontology_description,topic",
    "",
    "  # Vectors are cached in <project>.embeddings.json and only the concepts",
    "  # whose text changed are recomputed. To force a full recompute:",
    "  synesis-graph arcadedb --project project.synp \\",
    "      --vector-embeddings ontology_description --rebuild-embeddings",
    "",
    "  # Embeddings need the optional extra (it brings torch, ~2 GB):",
    '  pip install "synesis-graph[embeddings]"',
    "",
    "  # The model is per project, in config.toml — a Portuguese corpus and an",
    "  # English one have different needs:",
    "  #   [arcadedb.embeddings]",
    '  #   model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"',
    '  #   fields = ["ontology_description", "topic"]',
    "",
    "  # Note: [arcadedb].uri is the HTTP endpoint (port 2480, the same one that",
    "  # serves ArcadeDB Studio) — not a bolt:// URL. No driver and no server",
    "  # plugin are required.",
)

_EPILOG_ARCADEDB_EMBEDDED = _ex(
    "  # Export to a local database. No server, no port, no Java:",
    "  synesis-graph arcadedb-embedded --project project.synp",
    "",
    "  # The graph lands in ./databases/<project_name>/ by default.",
    "  # Point db_path elsewhere in config.toml — it is the SERVER ROOT, and the",
    "  # database is created in <db_path>/databases/<project_name>:",
    "  #   [arcadedb-embedded]",
    '  #   db_path = "."',
    "",
    "  # The config file is optional here: there is no credential to supply and",
    "  # no host to reach, so the defaults already describe a working setup.",
    "",
    "  # Semantic search works exactly as on the server backend:",
    "  synesis-graph arcadedb-embedded --project project.synp \\",
    "      --vector-embeddings ontology_description",
    "",
    "  # Nothing extra to install: the local engine ships with synesis-graph,",
    "  # and brings its own Java. Exporting works on a clean machine.",
    "",
    "  # Same engine as the arcadedb backend, reached in-process instead of over",
    "  # HTTP. Use that one for a shared server; use this one to work alone,",
    "  # offline, with nothing to install or keep running.",
    "",
    "  # To ask the graph questions from a chat client, publish it with the same",
    "  # root this command wrote to:",
    "  synesis-graph serve",
)


_EPILOG_SERVE = _ex(
    "  # Publish the local database and keep it running. Ctrl+C stops it:",
    "  synesis-graph serve",
    "",
    "  # Serve a graph exported elsewhere. Pass the SAME root the export used —",
    "  # not the database directory. Both add the databases/ level themselves:",
    "  #   export:  synesis-graph arcadedb-embedded ... --db-path D:/graphs",
    "  #   serve:   synesis-graph serve             --db-path D:/graphs",
    "  synesis-graph serve --db-path D:/graphs",
    "",
    "  # The password is generated once and remembered, so restarts keep working",
    "  # on their own. Set your own only if you want to choose it (8+ chars):",
    "  #   PowerShell:  $env:SYNESIS_DB_PASSWORD = \"...\"",
    "  #   bash:        export SYNESIS_DB_PASSWORD=...",
    "  synesis-graph serve",
    "",
    "  # Let a chat client modify the corpus. Off by default, and worth keeping",
    "  # off: a corpus is months of coding work, and reading it is the use case.",
    "  synesis-graph serve --allow-writes",
    "",
    "  # The command prints the entry to add to your chat client. It is one",
    "  # entry, not a whole file: paste it beside the servers already there,",
    "  # never over them.",
    "",
    "  # Or have it added for you, keeping every existing entry and backing up",
    "  # the file first. Opt-in: editing another application's configuration",
    "  # is your call, not this command's.",
    "  synesis-graph serve --install claude-desktop",
    "",
    "  # VS Code reads a different format (servers/, HTTP directly, no npx) and",
    "  # keeps it per workspace. This writes .vscode/mcp.json here:",
    "  synesis-graph serve --install vscode",
    "",
    "  # Claude Desktop's file is found on macOS and Windows. Elsewhere, or for",
    "  # an unusual install, point at it:",
    "  #   PowerShell:  $env:SYNESIS_MCP_CONFIG = \"C:/path/to/config.json\"",
    "  #   bash:        export SYNESIS_MCP_CONFIG=~/path/to/config.json",
    "",
    "  # Running an ArcadeDB server already? It owns port 2480 — pick another:",
    "  synesis-graph serve --port 2481 --install vscode",
)


_EPILOG_HTML = _ex(
    "  # Render with default filters (min 3 mentions, min 2 sources):",
    "  synesis-graph html --project project.synp --output graph.html",
    "",
    "  # Disable all filters (show every concept):",
    "  synesis-graph html --project project.synp --output graph.html --all",
    "",
    "  # Color communities by a taxonomy field:",
    "  synesis-graph html --project project.synp --output graph.html --group-by topic",
    "",
    "  # Tune filters manually:",
    "  synesis-graph html --project project.synp --output graph.html --min-frequency 5 --max-nodes 100",  # noqa: E501
    "",
    "  # From pre-compiled JSON:",
    "  synesis-graph html --json export.json --output graph.html --all",
)


# ---------------------------------------------------------------------------
# Shared source options (project or json, mutually exclusive)
# ---------------------------------------------------------------------------


def _source_options(fn):
    fn = click.option(
        "--json",
        "json_input",
        default=None,
        type=click.Path(path_type=Path),
        help="Path to a Synesis v3.0 JSON export (alternative to --project).",
    )(fn)
    fn = click.option(
        "--project",
        default=None,
        multiple=True,
        type=click.Path(path_type=Path),
        help=(
            "Path to a Synesis project file (.synp). Repeat to link several "
            "projects: identities declared with IDENTIFIES/REFERS TO are "
            "reified into shared nodes across them."
        ),
    )(fn)
    return fn


def _mode_option(fn):
    """`--mode rebuild|update`, shared by the three database backends.

    Not a config-file setting on purpose. The mode describes one run ("this
    export is incremental"), not the installation, and a stale `mode = "update"`
    left in a config file would keep quietly skipping the wipe long after anyone
    remembered putting it there — with no deletion support yet, that accumulates
    removed data indefinitely.
    """
    return click.option(
        "--mode",
        type=click.Choice(["rebuild", "update"]),
        default="rebuild",
        show_default=True,
        help="rebuild: wipe the graph and write it again — always correct, and "
        "the right choice unless the export is slow. update: write only what "
        "changed, keeping the existing graph. Update is much faster on a large "
        "corpus, but it does NOT remove anything: material deleted from the "
        "project stays in the graph until the next rebuild.",
    )(fn)


def _metrics_option(fn):
    """`--metrics all|fast|none`, for the two backends that compute them.

    Default `all`, deliberately: centrality and communities are research
    findings, not diagnostics — they answer which concepts hold a corpus
    together. The flag exists so a researcher in a hurry can trade them for
    time, never so they are lost without anyone choosing that.
    """
    return click.option(
        "--metrics",
        type=click.Choice(["all", "fast", "none"]),
        default="all",
        show_default=True,
        help="all: every measure, including centrality and communities — the "
        "slowest step on a large graph, and often several minutes. fast: skips "
        "betweenness and communities, keeping PageRank. none: skips them "
        "entirely; the graph is still complete, only these scores are absent.",
    )(fn)


def _config_option(fn):
    return click.option(
        "--config",
        default="config.toml",
        show_default=True,
        type=click.Path(path_type=Path),
        help="Path to the TOML configuration file.",
    )(fn)


# ---------------------------------------------------------------------------
# Entry point group
# ---------------------------------------------------------------------------


@click.group(cls=_SynesisGroup, invoke_without_command=True)
@click.version_option(version=__version__, prog_name="synesis-graph")
@click.option(
    "-v",
    "--verbose",
    count=True,
    default=0,
    help="Increase log verbosity (-v for DEBUG). Repeatable.",
)
@click.option(
    "-q",
    "--quiet",
    count=True,
    default=0,
    help="Decrease log verbosity (-q WARNING, -qq ERROR). Repeatable.",
)
@click.pass_context
def main(ctx, verbose: int, quiet: int) -> None:
    """Universal pipeline from Synesis projects to graph databases."""
    _configure_logging(verbose, quiet)
    if ctx.invoked_subcommand is None:
        out = _build_main_help()
        if hasattr(sys.stdout, "buffer"):
            sys.stdout.buffer.write(out.encode("utf-8"))
            sys.stdout.buffer.flush()
        else:
            click.echo(out)


# ---------------------------------------------------------------------------
# neo4j subcommand
# ---------------------------------------------------------------------------


@main.command(cls=_SynesisCommand, epilog=_EPILOG_NEO4J)
@_source_options
@_config_option
@click.option(
    "--database",
    default=None,
    help="Neo4j database name. Also names the unified graph when linking several "
    "--project files (otherwise the name is derived from the members).",
)
@_mode_option
def neo4j(project, json_input, config, database, mode):
    """Sync a Synesis project to a Neo4j database."""
    _validate_source(project, json_input)
    from synesis_graph.pipeline import run_pipeline
    from synesis_graph.ui import TaskReporter

    reporter = TaskReporter("Synesis → Neo4j")
    head, extras = _split_projects(project)
    result = run_pipeline(
        project_path=head,
        json_path=Path(json_input).resolve() if json_input else None,
        config_path=Path(config).resolve(),
        reporter=reporter,
        backend=BACKEND_NEO4J,
        database=database or None,
        extra_projects=extras,
        mode=mode,
    )
    _report_result(reporter, result)


# ---------------------------------------------------------------------------
# arcadedb subcommand
# ---------------------------------------------------------------------------


@main.command(cls=_SynesisCommand, epilog=_EPILOG_ARCADEDB)
@_source_options
@_config_option
@click.option(
    "--database",
    default=None,
    help="ArcadeDB database name. Also names the unified graph when linking several "
    "--project files (otherwise the name is derived from the members).",
)
@click.option(
    "--vector-embeddings",
    "vector_embeddings",
    default=None,
    metavar="FIELD,FIELD",
    help="Ontology fields to embed for semantic search, comma-separated "
    "(e.g. ontology_description,topic). Each is checked against the "
    "template; TEXT and TOPIC fields are the ones worth embedding. "
    "Overrides [arcadedb.embeddings].fields. Needs the extra: "
    'pip install "synesis-graph[embeddings]"',
)
@click.option(
    "--rebuild-embeddings",
    is_flag=True,
    default=False,
    help="Recompute every vector instead of reusing the cached "
    "<project>.embeddings.json. Only needed when the vectors are suspect "
    "for a reason the model/field/text hashes cannot see.",
)
@_mode_option
@_metrics_option
def arcadedb(
    project, json_input, config, database, vector_embeddings, rebuild_embeddings, mode, metrics
):
    """Sync a Synesis project to an ArcadeDB database."""
    _validate_source(project, json_input)
    from synesis_graph.pipeline import run_pipeline
    from synesis_graph.ui import TaskReporter

    fields = _split_fields(vector_embeddings)
    if rebuild_embeddings and not fields:
        # Silently doing nothing would read as "the rebuild happened".
        raise click.UsageError(
            "--rebuild-embeddings needs --vector-embeddings (or "
            "[arcadedb.embeddings].fields in the config)."
        )

    reporter = TaskReporter("Synesis → ArcadeDB")
    head, extras = _split_projects(project)
    result = run_pipeline(
        project_path=head,
        json_path=Path(json_input).resolve() if json_input else None,
        config_path=Path(config).resolve(),
        reporter=reporter,
        backend=BACKEND_ARCADEDB,
        database=database or None,
        extra_projects=extras,
        vector_embeddings=fields,
        rebuild_embeddings=rebuild_embeddings,
        mode=mode,
        metrics=metrics,
    )
    _report_result(reporter, result)


# ---------------------------------------------------------------------------
# arcadedb-embedded subcommand
# ---------------------------------------------------------------------------


@main.command(
    "arcadedb-embedded", cls=_SynesisCommand, epilog=_EPILOG_ARCADEDB_EMBEDDED
)
@_source_options
@_config_option
@click.option(
    "--db-path",
    "db_path",
    default=None,
    metavar="DIR",
    help="Server root for the local database. The database is created in "
    "<DIR>/databases/<project_name>. Overrides [arcadedb-embedded].db_path "
    "(default: the current directory).",
)
@click.option(
    "--database",
    default=None,
    help="Database name. Also names the unified graph when linking several "
    "--project files (otherwise the name is derived from the members).",
)
@click.option(
    "--vector-embeddings",
    "vector_embeddings",
    default=None,
    metavar="FIELD,FIELD",
    help="Ontology fields to embed for semantic search, comma-separated "
    "(e.g. ontology_description,topic). Each is checked against the "
    "template; TEXT and TOPIC fields are the ones worth embedding. "
    "Overrides [arcadedb-embedded.embeddings].fields. Needs the extra: "
    'pip install "synesis-graph[embeddings]"',
)
@click.option(
    "--rebuild-embeddings",
    is_flag=True,
    default=False,
    help="Recompute every vector instead of reusing the cached "
    "<project>.embeddings.json. Only needed when the vectors are suspect "
    "for a reason the model/field/text hashes cannot see.",
)
@_mode_option
@_metrics_option
def arcadedb_embedded(
    project, json_input, config, db_path, database, vector_embeddings, rebuild_embeddings,
    mode, metrics
):
    """Sync a Synesis project to a local ArcadeDB database (no server)."""
    _validate_source(project, json_input)
    from synesis_graph.pipeline import run_pipeline
    from synesis_graph.ui import TaskReporter

    fields = _split_fields(vector_embeddings)
    if rebuild_embeddings and not fields:
        # Silently doing nothing would read as "the rebuild happened".
        raise click.UsageError(
            "--rebuild-embeddings needs --vector-embeddings (or "
            "[arcadedb-embedded.embeddings].fields in the config)."
        )

    reporter = TaskReporter("Synesis → local graph")
    head, extras = _split_projects(project)
    result = run_pipeline(
        project_path=head,
        json_path=Path(json_input).resolve() if json_input else None,
        config_path=Path(config).resolve(),
        reporter=reporter,
        backend=BACKEND_ARCADEDB_EMBEDDED,
        database=database or None,
        extra_projects=extras,
        vector_embeddings=fields,
        rebuild_embeddings=rebuild_embeddings,
        cli_overrides={"db_path": db_path},
        mode=mode,
        metrics=metrics,
    )
    if result.success:
        # A finished export is a means, not an end. Without this the researcher
        # is left with a directory and no idea what to do with it.
        root = db_path or "."
        reporter.info(
            "Your graph is ready. To ask questions about it from a chat client, run: "
            f"synesis-graph serve --db-path {root}"
        )
    _report_result(reporter, result)


# ---------------------------------------------------------------------------
# html subcommand
# ---------------------------------------------------------------------------


@main.command(cls=_SynesisCommand, epilog=_EPILOG_HTML)
@_source_options
@_config_option
@click.option(
    "--output",
    "html_output",
    default=None,
    type=click.Path(path_type=Path),
    help="Output HTML file path (default: ./graph.html).",
)
@click.option(
    "--group-by",
    "group_by",
    default=None,
    metavar="FIELD",
    help="Template graph field for community colouring.",
)
@click.option(
    "--min-frequency",
    "min_frequency",
    type=int,
    default=None,
    metavar="N",
    help="Hide concepts mentioned in fewer than N items (default: 3).",
)
@click.option(
    "--min-source-count",
    "min_source_count",
    type=int,
    default=None,
    metavar="N",
    help="Hide concepts appearing in fewer than N sources (default: 2).",
)
@click.option(
    "--max-nodes",
    "max_nodes",
    type=int,
    default=None,
    metavar="N",
    help="Limit to top-N concepts by degree (default: 200; 0 = unlimited).",
)
@click.option(
    "--max-hyperedges",
    "max_hyperedges",
    type=int,
    default=None,
    metavar="N",
    help="Maximum hyperedges to render (default: 50).",
)
@click.option(
    "--include-isolated",
    "include_isolated",
    is_flag=True,
    default=False,
    help="Include concepts with no chain connections.",
)
@click.option(
    "--all",
    "html_all",
    is_flag=True,
    default=False,
    help="Disable all filters (show every concept).",
)
def html(
    project,
    json_input,
    config,
    html_output,
    group_by,
    min_frequency,
    min_source_count,
    max_nodes,
    max_hyperedges,
    include_isolated,
    html_all,
):
    """Render an interactive HTML graph visualization from a Synesis project."""
    _validate_source(project, json_input)
    from synesis_graph.pipeline import run_pipeline
    from synesis_graph.ui import TaskReporter

    reporter = TaskReporter("Synesis → HTML")

    html_options: dict = {}
    if html_output:
        html_options["output_path"] = str(html_output)
    if html_all:
        html_options.update(
            {"min_frequency": 0, "min_source_count": 0, "max_nodes": 0, "include_isolated": True}
        )
    else:
        if group_by is not None:
            html_options["group_by"] = group_by
        if min_frequency is not None:
            html_options["min_frequency"] = min_frequency
        if min_source_count is not None:
            html_options["min_source_count"] = min_source_count
        if max_nodes is not None:
            html_options["max_nodes"] = max_nodes
        if max_hyperedges is not None:
            html_options["max_hyperedges"] = max_hyperedges
        if include_isolated:
            html_options["include_isolated"] = True

    head, extras = _split_projects(project)
    if extras:
        # The HTML view is a CONCEPT graph (ontology layer). Rendering reified
        # identity nodes there needs its own layer design — not yet decided —
        # so refuse rather than silently merge without reifying.
        raise click.UsageError(
            "The html backend does not support linking multiple projects yet. "
            "Use a single --project, or use the neo4j backend to reify identities."
        )

    result = run_pipeline(
        project_path=head,
        json_path=Path(json_input).resolve() if json_input else None,
        config_path=Path(config).resolve(),
        reporter=reporter,
        backend=BACKEND_HTML,
        html_options=html_options,
    )
    _report_result(reporter, result)


# ---------------------------------------------------------------------------
# serve subcommand
# ---------------------------------------------------------------------------


@main.command(cls=_SynesisCommand, epilog=_EPILOG_SERVE)
@click.option(
    "--db-path",
    "db_path",
    default=".",
    show_default=True,
    type=click.Path(path_type=Path),
    help="Server root holding the database. The databases themselves live in "
    "<DIR>/databases/ — the same layout the export writes.",
)
@click.option(
    "--port",
    default=DEFAULT_SERVE_PORT,
    show_default=True,
    type=int,
    help="HTTP port for the server and its MCP endpoint.",
)
@click.option(
    "--allow-writes",
    is_flag=True,
    default=False,
    help="Let chat clients modify the corpus. Off by default: reading is the "
    "use case, and a corpus is months of coding work. Administrative "
    "operations stay blocked either way.",
)
@click.option(
    "--install",
    type=click.Choice(["claude-desktop", "vscode"]),
    default=None,
    help="Add this server to a chat client's configuration, keeping every "
    "entry already there and backing the file up first. Off by default: "
    "editing another application's configuration is the researcher's call, "
    "not this command's. 'vscode' writes .vscode/mcp.json in the current "
    "directory; for Claude Desktop, SYNESIS_MCP_CONFIG points at the file "
    "when it lives somewhere unusual.",
)
def serve(db_path, port, allow_writes, install):
    """Keep a local database running for chat clients (Ctrl+C to stop)."""
    import os

    from synesis_graph.serve import ServeOptions, client_config_snippet, start_server
    from synesis_graph.ui import TaskReporter

    reporter = TaskReporter("Synesis → local MCP server")
    with reporter.step("Starting the local server") as step:
        handle = start_server(
            ServeOptions(
                db_path=Path(db_path),
                port=port,
                allow_writes=allow_writes,
                # An env var lets the password outlive the session without ever
                # being written to a project file — the client config can then
                # stay valid across restarts.
                password=os.environ.get("SYNESIS_DB_PASSWORD") or None,
            )
        )
        if isinstance(handle, PipelineError):
            step.fail()

    if isinstance(handle, PipelineError):
        reporter.error(f"{handle.message} — {handle.details}")
        raise SystemExit(1)

    reporter.info(f"Serving {len(handle.databases)} database(s): {', '.join(handle.databases)}")
    reporter.info(f"MCP endpoint: {handle.endpoint}")
    if allow_writes:
        reporter.warning("Writes are ENABLED for chat clients.")
    else:
        reporter.info("Read-only: chat clients cannot modify the corpus.")

    click.echo()
    installed = False
    if install:
        from synesis_graph.serve import install_into_claude_desktop, install_into_vscode

        if install == "vscode":
            # Workspace-local by design: an MCP entry that names a port and a
            # password belongs to the project it was started for, not to every
            # window the editor opens.
            outcome = install_into_vscode(handle, path=Path(".vscode") / "mcp.json")
            client, restart = "VS Code", "Reload the VS Code window to pick it up."
        else:
            outcome = install_into_claude_desktop(handle)
            client = "Claude Desktop"
            restart = "Restart Claude Desktop to pick it up (quit fully, then reopen)."
        if isinstance(outcome, PipelineError):
            # Not fatal: the server is up and usable. Fall back to the manual
            # route rather than ending a working session over a config file.
            reporter.warning(f"{outcome.message} — {outcome.details}")
        else:
            target, replaced = outcome
            verb = "Updated" if replaced else "Added"
            reporter.success(f"{verb} 'synesis-local' in {client} ({target})")
            reporter.info(restart)
            installed = True

    if not installed:
        click.echo(
            _c(
                "Add this entry to the \"mcpServers\" section of your chat client's "
                "configuration, alongside any entries already there:",
                fg="yellow",
                bold=True,
            )
        )
        if install == "vscode":
            from synesis_graph.serve import vscode_config_snippet

            click.echo(vscode_config_snippet(handle))
        else:
            click.echo(client_config_snippet(handle, windows=os.name == "nt"))
        if install is None:
            click.echo(
                _c(
                    "(or re-run with --install claude-desktop / --install vscode "
                    "to have it added for you, keeping the entries already there)",
                    fg="bright_black",
                )
            )
    click.echo()
    if not os.environ.get("SYNESIS_DB_PASSWORD"):
        # Neither half of the old notice survives: the password is generated
        # once and remembered, and when the entry was installed there is no
        # password "above" to refer to.
        click.echo(
            _c(
                "The password was generated for this graph and is remembered, so "
                "restarts keep working. Set SYNESIS_DB_PASSWORD to choose your own.",
                fg="bright_black",
            )
        )
    click.echo(_c("Serving. Press Ctrl+C to stop.", fg="green", bold=True))

    try:
        # The engine runs on its own (non-daemon JVM) threads; this process just
        # has to stay alive and own the interrupt.
        threading.Event().wait()
    except KeyboardInterrupt:
        click.echo()
        reporter.info("Stopping the server")
    finally:
        handle.stop()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validate_source(project, json_input) -> None:
    if not project and not json_input:
        raise click.UsageError("Provide either --project or --json.")
    if project and json_input:
        raise click.UsageError("--project and --json are mutually exclusive.")
    if json_input and len(_as_tuple(project)) > 1:
        raise click.UsageError("--json accepts a single source; use --project to link many.")


def _as_tuple(project) -> tuple:
    """Normalizes --project into a tuple (it is `multiple=True`, but may be None)."""
    if not project:
        return ()
    if isinstance(project, (list, tuple)):
        return tuple(project)
    return (project,)


def _split_projects(project) -> tuple[Path | None, list[Path]]:
    """Splits --project into (head, extras) for run_pipeline.

    A single --project keeps the legacy single-project path untouched; two or
    more trigger the link step.
    """
    paths = [Path(p).resolve() for p in _as_tuple(project)]
    if not paths:
        return None, []
    return paths[0], paths[1:]


def _split_fields(value: str | None) -> list[str]:
    """Splits a comma-separated field list, tolerating spaces after commas.

    `--vector-embeddings "a, b"` is what a user naturally types, and an
    unstripped " b" would be reported as an unknown field.
    """
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _report_result(reporter, result) -> None:
    if not result.success and result.error and reporter.stats["errors"] == 0:
        # Early-exit errors (validation, config) bypass reporter.error() — surface them here.
        detail = f" — {result.error.details}" if result.error.details else ""
        reporter.error(f"{result.error.message}{detail}")
    reporter.print_summary()
    if result.success:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
