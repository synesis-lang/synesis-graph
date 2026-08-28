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
    for cmd in ("neo4j", "arcadedb", "html"):
        assert cmd in out


def test_subcommand_help_shows_examples():
    result = _run("neo4j", "--help")
    assert result.exit_code == 0
    assert "Examples:" in result.output
    assert "--project" in result.output


def test_arcadedb_subcommand_help_shows_examples_and_flags():
    result = _run("arcadedb", "--help")
    assert result.exit_code == 0
    out = result.output
    assert "Examples:" in out
    for flag in ("--project", "--json", "--config", "--database"):
        assert flag in out


def test_arcadedb_help_warns_the_uri_is_http_not_bolt():
    """Pointing [arcadedb].uri at the BOLT port is the likely first mistake."""
    result = _run("arcadedb", "--help")
    assert "bolt://" in result.output
    assert "2480" in result.output


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
    for cmd in ("neo4j", "arcadedb", "html"):
        result = _run(cmd, "help")
        assert result.exit_code == 0, f"'{cmd} help' returned exit {result.exit_code}"
        assert "Usage:" in result.output


# ---------------------------------------------------------------------------
# arcadedb-embedded
# ---------------------------------------------------------------------------


def test_main_help_lists_the_embedded_backend():
    """It belongs to Graph Backends: it exports and exits, like the others."""
    result = _run("--help")
    assert result.exit_code == 0
    assert "arcadedb-embedded" in result.output
    assert "no server" in result.output


def test_embedded_help_documents_the_root_not_the_database_directory():
    """`--db-path` is the server root; the database goes one level in.

    Documenting it here is what stops someone pointing the flag at a database
    directory, which makes the serving side start and find nothing at all.
    """
    result = _run("arcadedb-embedded", "--help")
    assert result.exit_code == 0
    out = result.output
    assert "--db-path" in out
    assert "databases/<project_name>" in out


def test_embedded_requires_a_source():
    """Neither --project nor --json is a usage error, as on every backend."""
    result = _run("arcadedb-embedded")
    assert result.exit_code != 0


def test_embedded_rejects_rebuild_without_fields():
    """Rebuilding nothing would read as "the rebuild happened"."""
    result = _run(
        "arcadedb-embedded", "--project", "x.synp", "--rebuild-embeddings"
    )
    assert result.exit_code != 0
    assert "--vector-embeddings" in result.output


def test_embedded_names_its_own_config_section_in_help():
    """The error must not send the reader to [arcadedb], a different mode."""
    result = _run("arcadedb-embedded", "--help")
    assert "[arcadedb-embedded.embeddings]" in result.output


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def test_main_help_separates_serving_from_exporting():
    """A long-running command does not belong under "Graph Backends".

    The grouping is what keeps the module's role legible: backends export and
    exit; `serve` publishes and stays.
    """
    result = _run("--help")
    assert result.exit_code == 0
    out = result.output
    assert "Local Database" in out
    assert "serve" in out


def test_serve_help_documents_the_read_only_default():
    """Whether a chat client may write is a decision, so it must be visible."""
    result = _run("serve", "--help")
    assert result.exit_code == 0
    out = result.output
    assert "--allow-writes" in out
    assert "Off by default" in out


def test_serve_reports_an_empty_root_instead_of_serving_nothing(tmp_path):
    """Starting over no database is the silent failure this guards against."""
    result = _run("serve", "--db-path", str(tmp_path))
    assert result.exit_code != 0
    assert "No database found" in result.output


def test_serve_example_does_not_teach_the_layout_mistake():
    """The example must pass the export's root, never the databases/ directory.

    Both commands add the `databases/` level themselves, so `--db-path
    ./databases` makes `serve` look in `databases/databases` and find nothing —
    and a server over the wrong root starts, reports success, and answers every
    query with no rows. An example is the likeliest place for that to be copied
    from, which is why it is pinned here.
    """
    result = _run("serve", "--help")
    assert result.exit_code == 0
    assert "--db-path ./databases" not in result.output
    assert "--db-path databases" not in result.output


def test_every_example_in_every_epilog_parses():
    """An example that no longer parses is worse than no example.

    `--install` became a choice option, and the epilog kept illustrating it
    bare — which now fails with "Option '--install' requires an argument". The
    text a researcher is most likely to copy was the text that stopped working,
    and nothing caught it. This checks the examples against the real parsers:
    options and their value shapes, without running any pipeline.
    """
    import shlex

    import click

    from synesis_graph.cli import main

    checked = 0
    for name, command in main.commands.items():
        for line in (command.epilog or "").splitlines():
            stripped = line.strip()
            if not stripped.startswith("synesis-graph "):
                continue
            argv = shlex.split(stripped.replace("\\", ""))[1:]
            if argv[:1] != [name]:
                continue  # an example naming another command; checked under that one
            # Parse only: `make_context` resolves options and their values and
            # then stops, so nothing connects, compiles or writes. Running the
            # command with `--help` appended would instead feed "--help" to
            # whichever option came last, which is not what is being checked.
            try:
                ctx = command.make_context(name, argv[1:], resilient_parsing=False)
                ctx.close()
            except click.UsageError as e:
                message = str(e)
                # A missing required source is expected: several examples are
                # fragments ending in "...". Only option-level breakage matters.
                assert "requires an argument" not in message, (
                    f"{name}: example no longer parses -> {stripped} ({message})"
                )
                assert "no such option" not in message.lower(), (
                    f"{name}: example uses an option that no longer exists -> {stripped}"
                )
            checked += 1

    assert checked > 10, f"only {checked} examples checked — did the parsing break?"
