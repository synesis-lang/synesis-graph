"""CLI for synesis-graph."""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import click

# Force UTF-8 on Windows terminals before Rich or Click emit any output.
if hasattr(sys.stdout, "buffer") and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "buffer") and getattr(sys.stderr, "encoding", "").lower() != "utf-8":
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from synesis_graph import (
    BACKEND_ARCADEDB,
    BACKEND_HTML,
    BACKEND_NEO4J,
    __version__,
)

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

    groups = [
        (
            "Graph Backends",
            [
                ("neo4j", "Sync project to a Neo4j database (bolt://)"),
                ("arcadedb", "Sync project to an ArcadeDB database (http://)"),
                ("html", "Render an interactive HTML graph visualization"),
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
                elif tok in ("neo4j", "arcadedb", "html"):
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
    "  # Note: [arcadedb].uri is the HTTP endpoint (port 2480, the same one that",
    "  # serves ArcadeDB Studio) — not a bolt:// URL. No driver and no server",
    "  # plugin are required.",
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
def neo4j(project, json_input, config, database):
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
def arcadedb(project, json_input, config, database):
    """Sync a Synesis project to an ArcadeDB database."""
    _validate_source(project, json_input)
    from synesis_graph.pipeline import run_pipeline
    from synesis_graph.ui import TaskReporter

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
