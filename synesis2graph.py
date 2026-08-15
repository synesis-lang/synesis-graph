#!/usr/bin/env python3
"""
synesis2graph.py - Shim: re-exports the synesis-graph public API and CLI entry point.

All implementation lives in synesis_graph.*. This file exists so that:
  - `python synesis2graph.py --help/--version` works (direct script execution)
  - Legacy `from synesis2graph import run_pipeline` imports continue to work

Usage:
    synesis-graph neo4j --project ./my_project.synp
    synesis-graph html  --project ./my_project.synp --output graph.html --all
    synesis-graph --version
"""
from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import click
    _CLICK_AVAILABLE = True
except ImportError:
    _CLICK_AVAILABLE = False

# ============================================================================
# VERSION
# ============================================================================
__version__ = "0.2.1"
__version_info__ = (0, 2, 1)

# Phase 1: backend selection contract (re-exported from config for CLI use)
from synesis_graph.config import (  # noqa: E402
    BACKEND_NEO4J, BACKEND_ARCADEDB, BACKEND_HTML, SUPPORTED_BACKENDS,
)

# ============================================================================
# EXTERNAL IMPORTS
# ============================================================================
from synesis_graph.core import (  # noqa: E402
    get_neo4j_driver_factory,
    PipelineError,
    CompilationError,
    ConnectionError,
    SyncError,
    DependencyError,
    ChainFieldSpec,
    CodeFieldSpec,
    GraphPayload,
    PipelineResult,
    analyze_template,
    get_taxonomy_labels,
    load_json_project,
    compile_project,
    _build_graph_payload,
    _index_to_label,
    _extract_concepts,
    _extract_corpus_data,
    _build_source_props,
)

# ============================================================================
# LOGGING
# ============================================================================
logger = logging.getLogger("synesis2graph")


# ============================================================================
# SANITIZATION (Protection against Cypher Injection)
# ============================================================================
from synesis_graph.sanitize import (  # noqa: E402
    _CYPHER_LABEL_PATTERN,
    sanitize_cypher_label,
    sanitize_database_name,
    validate_cypher_label,
)


# ============================================================================
# USER INTERFACE
# ============================================================================
from synesis_graph.ui import TaskReporter, _StepContext  # noqa: E402


# ============================================================================
# NEO4J SYNCHRONIZATION
# ============================================================================
from synesis_graph.backends.neo4j import (  # noqa: E402
    clear_database, sync_to_neo4j,
    _create_constraints, _execute_sync_transaction, _sync_sources, _sync_items,
    _sync_from_source, TAXONOMY_RELATION_MAP, _get_taxonomy_relation,
    _sync_taxonomies, _sync_mentions, _sync_concepts,
)



# ============================================================================
# GRAPH METRICS
# ============================================================================
from synesis_graph.metrics import (  # noqa: E402
    _is_gds_available, _get_graph_strategy, compute_metrics,
    _compute_native_concept_metrics, _compute_native_taxonomy_metrics,
    _compute_native_source_metrics, _compute_gds_metrics, _drop_gds_graph,
    _create_gds_projection, _run_pagerank, _run_betweenness, _run_louvain,
)

# ============================================================================
# CONFIGURATION
# ============================================================================
from synesis_graph.config import (  # noqa: E402
    Neo4jConfig, HTMLConfig, PipelineConfig,
    _load_neo4j_config, _load_html_config,
    load_config, validate_backend_config,
    ensure_database_exists, get_database_name_from_project,
)

# ============================================================================
# BACKEND ADAPTERS (Phase 3)
# ============================================================================
from synesis_graph.backends.base import (  # noqa: E402
    BackendAdapter, Neo4jBackendAdapter,
)
from synesis_graph.backends.html import (  # noqa: E402
    _HTML_PALETTE, _HTML_RELATION_COLORS, _html_relation_color, _html_slug,
    _html_apply_filters, _html_resolve_grouping, _html_build_hyperedges,
    _html_render_payload, HTMLBackendAdapter,
)
from synesis_graph.pipeline import (  # noqa: E402
    build_backend_adapter, execute_backend_pipeline, run_pipeline,
)

# ============================================================================
# CLI — Click-based (same pattern as synesis and synesis-coder)
# ============================================================================

def _tty() -> bool:
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


def _c(text: str, **kwargs) -> str:
    if not _CLICK_AVAILABLE:
        return text
    return click.style(text, **kwargs) if _tty() else text


def _build_main_help() -> str:
    title = _c("SYNESIS GRAPH", fg="green", bold=True) + f" (v{__version__})"
    desc = "Universal pipeline from Synesis projects to graph databases and visualizations."
    usage = _c("Usage:", fg="yellow", bold=True) + " synesis-graph [OPTIONS] COMMAND [ARGUMENTS]..."

    groups = [
        ("Graph Backends", [
            ("neo4j",      "Sync project to a Neo4j database (bolt://)"),
            ("html",       "Render an interactive HTML graph visualization"),
        ]),
    ]

    opt_rows = [
        ("-v, --verbose",  "Increase log verbosity (DEBUG). Repeatable."),
        ("-q, --quiet",    "Decrease log verbosity (-q WARNING, -qq ERROR). Repeatable."),
        ("--version",      "Show version and exit"),
        ("--help",         "Show this message and exit"),
    ]

    col = max(
        max(len(name) for _, rows in groups for name, _ in rows),
        max(len(name) for name, _ in opt_rows),
    ) + 2

    options = _c("Global Options:", fg="yellow", bold=True) + "\n" + "\n".join(
        f"  {_c(name.ljust(col), fg='cyan')}  {desc_}"
        for name, desc_ in opt_rows
    )

    def _render_group(label: str, rows: list) -> str:
        lines = [_c("  " + label, fg="yellow", bold=True)]
        for name, desc_ in rows:
            lines.append(f"    {_c(name.ljust(col), fg='green', bold=True)}  {desc_}")
        return "\n".join(lines)

    commands = _c("Commands:", fg="yellow", bold=True) + "\n\n" + "\n\n".join(
        _render_group(label, rows) for label, rows in groups
    )

    hint = _c(
        "Run 'synesis-graph COMMAND --help' for options and examples of each backend.",
        fg="bright_black",
    )

    return "\n\n".join([title, desc, usage, options, commands, hint]) + "\n"


def _ex(*lines: str) -> str:
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
                elif tok in ("neo4j", "html"):
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
    "  synesis-graph html --project project.synp --output graph.html --min-frequency 5 --max-nodes 100",
    "",
    "  # From pre-compiled JSON:",
    "  synesis-graph html --json export.json --output graph.html --all",
)


def _write_help_utf8() -> None:
    out = _build_main_help()
    if hasattr(sys.stdout, "buffer"):
        sys.stdout.buffer.write(out.encode("utf-8"))
        sys.stdout.buffer.flush()
    else:
        print(out)


def _validate_source(project, json_input) -> None:
    if not project and not json_input:
        raise click.UsageError("Provide either --project PATH or --json PATH.")
    if project and json_input:
        raise click.UsageError("--project and --json are mutually exclusive.")


def _run_and_exit(backend: str, project, json_input, config, html_options=None,
                   database=None) -> None:
    reporter = TaskReporter(f"Synesis → {backend}")
    result = run_pipeline(
        project_path=Path(project).resolve() if project else None,
        json_path=Path(json_input).resolve() if json_input else None,
        config_path=Path(config).resolve(),
        reporter=reporter,
        backend=backend,
        html_options=html_options,
        database=database,
    )
    reporter.print_summary()
    sys.exit(0 if result.success else 1)


# Shared decorators
def _source_options(fn):
    fn = click.option("--json", "json_input", default=None, metavar="PATH",
                      help="Path to a Synesis v3.0 JSON export (alternative to --project).")(fn)
    fn = click.option("--project", default=None, metavar="PATH",
                      help="Path to a Synesis project file (.synp).")(fn)
    return fn


def _config_option(fn):
    return click.option("--config", default="config.toml", show_default=True, metavar="PATH",
                        help="Path to the TOML configuration file.")(fn)


if _CLICK_AVAILABLE:
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
            _write_help_utf8()
            raise SystemExit(0)

    @click.group(cls=_SynesisGroup, invoke_without_command=True)
    @click.version_option(version=__version__, prog_name="synesis-graph")
    @click.option("-v", "--verbose", count=True, default=0,
                  help="Increase log verbosity (-v for DEBUG). Repeatable.")
    @click.option("-q", "--quiet", count=True, default=0,
                  help="Decrease log verbosity (-q WARNING, -qq ERROR). Repeatable.")
    @click.pass_context
    def main(ctx, verbose: int, quiet: int) -> None:
        """Universal pipeline from Synesis projects to graph databases."""
        from synesis_graph.cli import _configure_logging
        _configure_logging(verbose, quiet)
        if ctx.invoked_subcommand is None:
            _write_help_utf8()

    @main.command(cls=_SynesisCommand, name="neo4j", epilog=_EPILOG_NEO4J)
    @_source_options
    @_config_option
    @click.option("--database", default=None,
                  help="Neo4j database name (overrides config).")
    def cmd_neo4j(project, json_input, config, database):
        """Sync a Synesis project to a Neo4j database."""
        _validate_source(project, json_input)
        _run_and_exit(BACKEND_NEO4J, project, json_input, config, database=database)

    @main.command(cls=_SynesisCommand, name="html", epilog=_EPILOG_HTML)
    @_source_options
    @_config_option
    @click.option("--output", "html_output", default=None, metavar="PATH",
                  help="Output HTML file (default: ./graph.html).")
    @click.option("--group-by", "group_by", default=None, metavar="FIELD",
                  help="Template graph field for community colouring.")
    @click.option("--min-frequency", "min_frequency", type=int, default=None, metavar="N",
                  help="Hide concepts mentioned in fewer than N items (default: 3).")
    @click.option("--min-source-count", "min_source_count", type=int, default=None, metavar="N",
                  help="Hide concepts appearing in fewer than N sources (default: 2).")
    @click.option("--max-nodes", "max_nodes", type=int, default=None, metavar="N",
                  help="Limit to top-N concepts by degree (default: 200; 0 = unlimited).")
    @click.option("--max-hyperedges", "max_hyperedges", type=int, default=None, metavar="N",
                  help="Maximum hyperedges to render (default: 50).")
    @click.option("--include-isolated", "include_isolated", is_flag=True, default=False,
                  help="Include concepts with no chain connections.")
    @click.option("--all", "html_all", is_flag=True, default=False,
                  help="Disable all filters (show every concept).")
    def cmd_html(project, json_input, config, html_output, group_by, min_frequency,
                 min_source_count, max_nodes, max_hyperedges, include_isolated, html_all):
        """Render an interactive HTML graph visualization from a Synesis project."""
        _validate_source(project, json_input)
        html_options: Dict[str, Any] = {}
        if html_output:
            html_options["output_path"] = html_output
        if html_all:
            html_options.update({"min_frequency": 0, "min_source_count": 0,
                                  "max_nodes": 0, "include_isolated": True})
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
        _run_and_exit(BACKEND_HTML, project, json_input, config, html_options)

else:
    # Fallback: argparse when click is not installed
    import argparse

    def main() -> int:  # type: ignore[misc]
        import argparse as _ap
        parser = _ap.ArgumentParser(description="Synesis Direct Link → Graph Databases")
        parser.add_argument("--version", "-v", action="version",
                            version=f"synesis-graph {__version__}")
        src = parser.add_mutually_exclusive_group(required=True)
        src.add_argument("--project", default=None)
        src.add_argument("--json", default=None, dest="json_input")
        parser.add_argument("--config", default="config.toml")
        parser.add_argument("--backend", choices=SUPPORTED_BACKENDS, default=BACKEND_NEO4J)
        parser.add_argument("--html-output", default=None)
        parser.add_argument("--html-group-by", default=None)
        parser.add_argument("--html-min-frequency", type=int, default=None)
        parser.add_argument("--html-min-source-count", type=int, default=None)
        parser.add_argument("--html-max-nodes", type=int, default=None)
        parser.add_argument("--html-max-hyperedges", type=int, default=None)
        parser.add_argument("--html-include-isolated", action="store_true", default=False)
        parser.add_argument("--html-all", action="store_true", default=False)
        args = parser.parse_args()

        html_options: Optional[Dict[str, Any]] = None
        if args.backend == BACKEND_HTML:
            html_options = {}
            if args.html_output:
                html_options["output_path"] = args.html_output
            if args.html_all:
                html_options.update({"min_frequency": 0, "min_source_count": 0,
                                     "max_nodes": 0, "include_isolated": True})
            else:
                if args.html_group_by:
                    html_options["group_by"] = args.html_group_by
                if args.html_min_frequency is not None:
                    html_options["min_frequency"] = args.html_min_frequency
                if args.html_min_source_count is not None:
                    html_options["min_source_count"] = args.html_min_source_count
                if args.html_max_nodes is not None:
                    html_options["max_nodes"] = args.html_max_nodes
                if args.html_max_hyperedges is not None:
                    html_options["max_hyperedges"] = args.html_max_hyperedges
                if args.html_include_isolated:
                    html_options["include_isolated"] = True

        reporter = TaskReporter(f"Synesis Direct Link ({args.backend})")
        result = run_pipeline(
            project_path=Path(args.project).resolve() if args.project else None,
            json_path=Path(args.json_input).resolve() if args.json_input else None,
            config_path=Path(args.config).resolve(),
            reporter=reporter, backend=args.backend, html_options=html_options,
        )
        reporter.print_summary()
        return 0 if result.success else 1


if __name__ == "__main__":
    if _CLICK_AVAILABLE:
        main(standalone_mode=True)
    else:
        sys.exit(main())
