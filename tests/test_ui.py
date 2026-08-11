"""Tests for ui.py — TaskReporter and label helpers."""

from __future__ import annotations

import io
import sys

import pytest

from synesis_graph.ui import (
    TaskReporter,
    _c,
    _emit,
    _label_dest,
    _label_error,
    _label_info,
    _label_ok,
    _label_step,
    _label_warn,
    _tty,
)

# ---------------------------------------------------------------------------
# TTY detection
# ---------------------------------------------------------------------------


def test_tty_returns_false_when_stderr_is_stringio(monkeypatch):
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert _tty() is False


def test_tty_returns_false_when_stderr_has_no_isatty(monkeypatch):
    class _NoTty:
        pass

    monkeypatch.setattr(sys, "stderr", _NoTty())
    assert _tty() is False


# ---------------------------------------------------------------------------
# _c passthrough when not a TTY
# ---------------------------------------------------------------------------


def test_c_returns_plain_text_when_not_tty(monkeypatch):
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    result = _c("hello", fg="red", bold=True)
    assert result == "hello"


# ---------------------------------------------------------------------------
# Labels produce [LABEL] strings (no TTY → no ANSI)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_no_tty(monkeypatch):
    """StringIO stderr so _tty() is always False in this module."""
    monkeypatch.setattr(sys, "stderr", io.StringIO())


def test_label_info_format():
    assert _label_info() == "[INFO]"


def test_label_ok_format():
    assert _label_ok().strip() == "[OK]"


def test_label_warn_format():
    assert _label_warn() == "[WARN]"


def test_label_error_format():
    assert _label_error() == "[ERROR]"


def test_label_step_format():
    assert _label_step() == "[STEP]"


def test_label_dest_format():
    assert _label_dest() == "[DEST]"


# ---------------------------------------------------------------------------
# _emit writes to stderr
# ---------------------------------------------------------------------------


def test_emit_writes_to_stderr(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    _emit("hello world")
    assert buf.getvalue() == "hello world\n"


# ---------------------------------------------------------------------------
# TaskReporter — stat tracking
# ---------------------------------------------------------------------------


@pytest.fixture
def reporter(monkeypatch) -> TaskReporter:
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    return TaskReporter("Test → Backend"), buf


def test_reporter_success_increments_stat(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("Test → X")
    r.success("done")
    assert r.stats["successes"] == 1
    assert r.stats["errors"] == 0


def test_reporter_error_increments_stat(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("Test → X")
    r.error("oops")
    assert r.stats["errors"] == 1


def test_reporter_warning_increments_stat(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("Test → X")
    r.warning("watch out")
    assert r.stats["warnings"] == 1


def test_reporter_accumulates_multiple_stats(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("Test → X")
    r.success("a")
    r.success("b")
    r.error("c")
    assert r.stats["successes"] == 2
    assert r.stats["errors"] == 1


# ---------------------------------------------------------------------------
# TaskReporter — output content
# ---------------------------------------------------------------------------


def test_reporter_header_written_on_init(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    TaskReporter("Synesis → HTML")
    output = buf.getvalue()
    assert "SYNESIS GRAPH" in output
    assert "Synesis → HTML" in output


def test_reporter_info_emits_info_label(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("X")
    r.info("status update")
    assert "[INFO]" in buf.getvalue()
    assert "status update" in buf.getvalue()


def test_reporter_dest_emits_dest_label(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("X")
    r.dest("/path/to/output.html")
    output = buf.getvalue()
    assert "[DEST]" in output
    assert "/path/to/output.html" in output


def test_reporter_print_summary_ok_when_no_errors(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("X")
    r.print_summary()
    output = buf.getvalue()
    assert "SUCCESS" in output
    assert "[OK]" in output


def test_reporter_print_summary_fail_when_errors(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("X")
    r.error("something broke")
    r.print_summary()
    output = buf.getvalue()
    assert "FAIL" in output
    assert "[ERROR]" in output


# ---------------------------------------------------------------------------
# _StepContext
# ---------------------------------------------------------------------------


def test_step_context_success_path(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("X")
    with r.step("Loading data"):
        pass
    output = buf.getvalue()
    assert "[STEP]" in output
    assert "Loading data" in output
    assert "[OK]" in output


def test_step_context_exception_calls_error(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("X")
    with pytest.raises(ValueError), r.step("Risky step"):
        raise ValueError("test error")
    output = buf.getvalue()
    assert "[ERROR]" in output
    assert "test error" in output
    assert r.stats["errors"] == 1


def test_print_diagnostics_emits_error_labels(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    r = TaskReporter("X")
    r.print_diagnostics(["diag line 1", "diag line 2"])
    output = buf.getvalue()
    assert output.count("[ERROR]") >= 2
    assert "diag line 1" in output
    assert "diag line 2" in output
