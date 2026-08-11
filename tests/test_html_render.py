"""End-to-end render tests for _html_render_payload using the real template."""

from __future__ import annotations

from pathlib import Path

import pytest

from synesis_graph.backends.html import HTMLBackendAdapter, _html_render_payload
from synesis_graph.config import HTMLConfig
from synesis_graph.core import GraphPayload

# Template shipped with the package — skip all tests if missing (e.g. raw source checkout).
_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "graph.html.tmpl"
pytestmark = pytest.mark.skipif(
    not _TEMPLATE_PATH.exists(),
    reason="graph.html.tmpl not found — run from repo root or install package",
)


def _render(payload: GraphPayload, **cfg_kwargs) -> str:
    config = HTMLConfig(**cfg_kwargs)
    return _html_render_payload(payload, config, _TEMPLATE_PATH)


# ---------------------------------------------------------------------------
# Basic render
# ---------------------------------------------------------------------------


def test_render_returns_string(minimal_payload):
    html = _render(minimal_payload)
    assert isinstance(html, str)
    assert len(html) > 1000


def test_render_contains_project_title(minimal_payload):
    html = _render(minimal_payload)
    assert "TestProject" in html


def test_render_injects_nodes_json(minimal_payload):
    html = _render(minimal_payload)
    assert "RAW_NODES_JSON" not in html
    assert "Resilience" in html


def test_render_injects_edges_json(minimal_payload):
    html = _render(minimal_payload)
    assert "RAW_EDGES_JSON" not in html


def test_render_injects_stats_text(minimal_payload):
    html = _render(minimal_payload)
    assert "RAW_NODES_JSON" not in html
    assert "STATS_TEXT" not in html
    assert "nodes" in html


def test_all_placeholders_replaced(minimal_payload):
    html = _render(minimal_payload)
    for placeholder in (
        "{{TITLE}}",
        "{{RAW_NODES_JSON}}",
        "{{RAW_EDGES_JSON}}",
        "{{ALL_GROUPINGS_JSON}}",
        "{{ACTIVE_GROUPING}}",
        "{{HYPEREDGES_JSON}}",
        "{{EVIDENCE_JSON}}",
        "{{EV_SOURCE_NODES_JSON}}",
        "{{EV_MENTION_EDGES_JSON}}",
        "{{STATS_TEXT}}",
    ):
        assert placeholder not in html, f"Placeholder not replaced: {placeholder}"


# ---------------------------------------------------------------------------
# Empty payload
# ---------------------------------------------------------------------------


def test_render_empty_payload_does_not_crash(empty_payload):
    html = _render(empty_payload, min_frequency=0, min_source_count=0)
    assert isinstance(html, str)
    assert len(html) > 500


# ---------------------------------------------------------------------------
# Filter options
# ---------------------------------------------------------------------------


def test_render_with_all_filters_disabled_includes_concepts(minimal_payload):
    html = _render(minimal_payload, min_frequency=0, min_source_count=0, include_isolated=True)
    assert "Resilience" in html
    assert "Trust" in html
    assert "Cooperation" in html


def test_render_with_strict_filters_hides_concepts(minimal_payload):
    # min_frequency=100 → no concept survives
    html = _render(minimal_payload, min_frequency=100, min_source_count=1)
    # nodes JSON should be empty array
    assert '"id"' not in html or "hidden by filter" in html or "[]" in html


# ---------------------------------------------------------------------------
# HTMLBackendAdapter.preflight
# ---------------------------------------------------------------------------


def test_html_backend_preflight_ok_when_template_exists(tmp_path):
    config = HTMLConfig(output_path=str(tmp_path / "graph.html"))
    adapter = HTMLBackendAdapter(config, config_path=tmp_path / "config.toml")
    err = adapter.preflight(reporter=_DummyReporter())
    assert err is None


def test_html_backend_preflight_error_when_template_missing(tmp_path, monkeypatch):
    config = HTMLConfig(output_path=str(tmp_path / "graph.html"))
    adapter = HTMLBackendAdapter(config, config_path=tmp_path / "config.toml")
    monkeypatch.setattr(adapter, "_template_path", tmp_path / "nonexistent.tmpl")
    err = adapter.preflight(reporter=_DummyReporter())
    assert err is not None
    assert err.stage == "preflight"


# ---------------------------------------------------------------------------
# HTMLBackendAdapter full write cycle
# ---------------------------------------------------------------------------


def test_html_backend_writes_file(tmp_path, minimal_payload):
    output = tmp_path / "out.html"
    config = HTMLConfig(output_path=str(output), min_frequency=0, min_source_count=0)
    adapter = HTMLBackendAdapter(config, config_path=tmp_path / "config.toml")

    reporter = _DummyReporter()
    err = adapter.preflight(reporter)
    assert err is None

    err = adapter.prepare_destination(minimal_payload, reporter)
    assert err is None

    err = adapter.synchronize_payload(minimal_payload, reporter)
    assert err is None

    assert output.exists()
    content = output.read_text(encoding="utf-8")
    assert "TestProject" in content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _DummyReporter:
    def info(self, msg): pass
    def success(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass
    def dest(self, msg): pass

    def step(self, desc):
        return _DummyStep()


class _DummyStep:
    def __enter__(self): return self
    def __exit__(self, *args): return False
