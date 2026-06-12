"""CLI for synesis-graph."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from synesis_graph import (
    BACKEND_GRAPHQLITE,
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
                ("graphqlite", "Sync project to a GraphQLite SQLite file"),
                ("html", "Render an interactive HTML graph visualization"),
            ],
        ),
    ]

    opt_rows = [
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
        _c("Options:", fg="yellow", bold=True)
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
                elif tok in ("neo4j", "graphqlite", "html"):
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
)

_EPILOG_GRAPHQLITE = _ex(
    "  # Sync to a local SQLite file (default: ./graphs/{project}.db):",
    "  synesis-graph graphqlite --project project.synp",
    "",
    "  # Custom output path:",
    "  synesis-graph graphqlite --project project.synp --config custom.toml",
    "",
    "  # Load from pre-compiled JSON:",
    "  synesis-graph graphqlite --json export.json",
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
        type=click.Path(path_type=Path),
        help="Path to a Synesis project file (.synp).",
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
@click.pass_context
def main(ctx) -> None:
    """Universal pipeline from Synesis projects to graph databases."""
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
@click.option("--database", default=None, help="Neo4j database name (overrides config).")
def neo4j(project, json_input, config, database):
    """Sync a Synesis project to a Neo4j database."""
    _validate_source(project, json_input)
    from synesis2graph import TaskReporter, run_pipeline

    reporter = TaskReporter("Synesis → Neo4j")
    html_options = None
    if database:
        # pass database override via html_options workaround not needed — TaskReporter handles it
        pass
    result = run_pipeline(
        project_path=Path(project).resolve() if project else None,
        json_path=Path(json_input).resolve() if json_input else None,
        config_path=Path(config).resolve(),
        reporter=reporter,
        backend=BACKEND_NEO4J,
        html_options=html_options,
    )
    _report_result(reporter, result)


# ---------------------------------------------------------------------------
# graphqlite subcommand
# ---------------------------------------------------------------------------


@main.command(cls=_SynesisCommand, epilog=_EPILOG_GRAPHQLITE)
@_source_options
@_config_option
def graphqlite(project, json_input, config):
    """Sync a Synesis project to a GraphQLite SQLite file."""
    _validate_source(project, json_input)
    from synesis2graph import TaskReporter, run_pipeline

    reporter = TaskReporter("Synesis → GraphQLite")
    result = run_pipeline(
        project_path=Path(project).resolve() if project else None,
        json_path=Path(json_input).resolve() if json_input else None,
        config_path=Path(config).resolve(),
        reporter=reporter,
        backend=BACKEND_GRAPHQLITE,
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
    from synesis2graph import TaskReporter, run_pipeline

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

    result = run_pipeline(
        project_path=Path(project).resolve() if project else None,
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


def _report_result(reporter, result) -> None:
    reporter.print_summary()
    if result.success:
        sys.exit(0)
    else:
        sys.exit(1)
