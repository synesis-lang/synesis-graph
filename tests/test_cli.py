"""CLI contract tests for synesis-graph.

These lock the --help / --version output as a regression contract. Assertions
target stable structural anchors (not byte-for-byte snapshots) because ANSI
colour is TTY-gated: _tty() returns False under CliRunner, so output is plain.
"""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version

from click.testing import CliRunner

from synesis_graph.cli import main


def _run(*args: str):
    runner = CliRunner()
    return runner.invoke(main, list(args))


def test_version_reports_package_version():
    result = _run("--version")
    assert result.exit_code == 0
    assert version("synesis-graph") in result.output


def test_help_lists_all_backends():
    result = _run("--help")
    assert result.exit_code == 0
    out = result.output
    assert "SYNESIS GRAPH" in out
    assert "Usage:" in out
    assert "Commands:" in out
    for cmd in ("neo4j", "html"):
        assert cmd in out


def test_subcommand_help_shows_examples():
    result = _run("neo4j", "--help")
    assert result.exit_code == 0
    assert "Examples:" in result.output
    assert "--project" in result.output


def test_html_subcommand_help_shows_filter_flags():
    result = _run("html", "--help")
    assert result.exit_code == 0
    out = result.output
    for flag in ("--output", "--min-frequency", "--all"):
        assert flag in out


def test_direct_module_version_runs():
    """`python -m synesis_graph.cli --version` must work post-refactor."""
    result = subprocess.run(
        [sys.executable, "-m", "synesis_graph.cli", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(__file__).split("tests")[0],
    )
    assert result.returncode == 0
    assert version("synesis-graph") in result.stdout


def test_help_alias_works_for_subcommands():
    """`synesis-graph COMMAND help` must be equivalent to `--help`."""
    for cmd in ("neo4j", "html"):
        result = _run(cmd, "help")
        assert result.exit_code == 0, f"'{cmd} help' returned exit {result.exit_code}"
        assert "Usage:" in result.output
