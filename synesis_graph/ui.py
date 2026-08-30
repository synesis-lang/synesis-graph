"""User-interface helpers for pipeline progress reporting."""

from __future__ import annotations

import sys
import time
from typing import Any

import click


def _tty() -> bool:
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


def _c(text: str, **kwargs: Any) -> str:
    return click.style(text, **kwargs) if _tty() else text


def _emit(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


# Label strings with optional color
def _label_info()  -> str: return _c("[INFO]",  fg="cyan")
def _label_ok()    -> str: return _c("[OK]  ",  fg="green", bold=True)
def _label_warn()  -> str: return _c("[WARN]",  fg="yellow", bold=True)
def _label_error() -> str: return _c("[ERROR]", fg="red",    bold=True)
def _label_step()  -> str: return _c("[STEP]",  fg="cyan",   bold=True)
def _label_dest()  -> str: return _c("[DEST]",  fg="bright_black")


class TaskReporter:
    """
    Reporter for visual pipeline feedback.

    Emits structured `[LABEL] message` lines to stderr — no Rich, no boxes.
    Colors are suppressed automatically when stderr is not a TTY (CI/pipes).
    """

    def __init__(self, title: str) -> None:
        self.stats: dict[str, int] = {"errors": 0, "warnings": 0, "successes": 0}
        self.start_time = time.time()
        from synesis_graph import __version__
        _emit(_c(f"SYNESIS GRAPH (v{__version__}) | {title}", fg="green", bold=True))
        _emit("Universal pipeline from Synesis projects to graph databases and visualizations.")

    def info(self, msg: str) -> None:
        _emit(f"{_label_info()} {msg}")

    def success(self, msg: str) -> None:
        self.stats["successes"] += 1
        _emit(f"{_label_ok()} {msg}")

    def warning(self, msg: str) -> None:
        self.stats["warnings"] += 1
        _emit(f"{_label_warn()} {msg}")

    def error(self, msg: str) -> None:
        self.stats["errors"] += 1
        _emit(f"{_label_error()} {msg}")

    def dest(self, path: str) -> None:
        _emit(f"{_label_dest()} {path}")

    def step(self, desc: str) -> _StepContext:
        return _StepContext(self, desc)

    def print_diagnostics(self, diagnostics: list[str]) -> None:
        for d in diagnostics:
            _emit(f"{_label_error()} {d}")

    def print_summary(self) -> None:
        duration = int(time.time() - self.start_time)
        ok = self.stats["errors"] == 0
        status = _c("SUCCESS", fg="green", bold=True) if ok else _c("FAIL", fg="red", bold=True)
        label = _label_ok() if ok else _label_error()
        # "em" was a stray Portuguese word in an otherwise English interface —
        # the sort of thing that reads as a typo and quietly costs trust.
        _emit(f"{label} {status} in {duration}s")


class _StepContext:
    """Context manager for a named pipeline step.

    A step ends in `[OK]` only if it neither raised nor was told it failed.

    That second clause is not redundant. This codebase reports failure by
    *returning* a typed error rather than raising one — `sync_to_arcadedb`,
    `ensure_database_exists` and four other call sites all do — and an early
    `return` inside a `with` block leaves the context manager with no exception
    to see. Judging on exceptions alone therefore printed `[OK]` for a sync that
    had just failed, immediately above the `[ERROR]` line reporting the same
    failure. Observed against a real server:

        [OK]    Building the graph (with 22585 concept vectors)
        [ERROR] Backend sync failed: ... Bad Gateway (HTTP 502)

    Two contradictory lines about one step make every log from this tool
    untrustworthy, which is worse than the underlying bug: a reader who has
    learned that `[OK]` can mean failure cannot use any of it.
    """

    def __init__(self, reporter: TaskReporter, description: str) -> None:
        self.reporter = reporter
        self.description = description
        self._failure: str | None = None

    def __enter__(self) -> _StepContext:
        _emit(f"{_label_step()} {self.description}...")
        return self

    def fail(self, detail: str = "") -> None:
        """Marks this step failed, for a caller that returns instead of raising.

        The caller still reports the error itself; this only stops the step from
        claiming success. Kept as an explicit call rather than inferred from the
        block's return value, because a context manager cannot see that value —
        Python gives `__exit__` the exception, and nothing else.
        """
        self._failure = detail

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc:
            self.reporter.error(f"{self.description}: {exc}")
        elif self._failure is not None:
            # The caller emits its own error line with the details; saying only
            # that the step did not succeed keeps this from being said twice.
            self.reporter.error(f"{self.description}: failed")
        else:
            self.reporter.success(f"{self.description}")
        return False
