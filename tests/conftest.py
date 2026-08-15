from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from synesis_graph.core import ChainFieldSpec, CodeFieldSpec, GraphPayload

# ---------------------------------------------------------------------------
# synesis2graph shim fixture (original — kept for test_phase7_multidb.py)
# ---------------------------------------------------------------------------


def _purge_modules():
    """Remove synesis2graph and all synesis_graph.* submodules from sys.modules."""
    to_delete = [k for k in sys.modules if k == "synesis2graph" or k.startswith("synesis_graph")]
    for key in to_delete:
        del sys.modules[key]


@pytest.fixture
def s2g(monkeypatch):
    """Loads synesis2graph with a stubbed synesis dependency."""
    fake_synesis = types.ModuleType("synesis")

    class DummySynesisCompiler:
        def __init__(self, project_path):
            self.project_path = project_path

        def compile(self):
            raise RuntimeError("Dummy compiler should be monkeypatched in tests")

    fake_synesis.SynesisCompiler = DummySynesisCompiler
    monkeypatch.setitem(sys.modules, "synesis", fake_synesis)

    _purge_modules()

    module = importlib.import_module("synesis2graph")
    yield module

    _purge_modules()


# ---------------------------------------------------------------------------
# Minimal GraphPayload factory
# ---------------------------------------------------------------------------


def _make_payload(
    project_name: str = "TestProject",
    concepts: list[dict[str, Any]] | None = None,
    items: list[dict[str, Any]] | None = None,
    chains: list[dict[str, Any]] | None = None,
    mentions: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
    from_source: list[dict[str, Any]] | None = None,
    graph_fields: list[str] | None = None,
    scalar_fields: list[str] | None = None,
    chain_fields: list[ChainFieldSpec] | None = None,
    code_fields: list[CodeFieldSpec] | None = None,
    source_fields: list[str] | None = None,
    value_maps: dict[str, list[dict[str, Any]]] | None = None,
    item_fields: dict[str, dict[str, str]] | None = None,
    taxonomy_edges: list[dict[str, Any]] | None = None,
) -> GraphPayload:
    return GraphPayload(
        project_name=project_name,
        concept_label="Concept",
        scalar_fields=scalar_fields or [],
        graph_fields=["topic"] if graph_fields is None else graph_fields,
        chain_fields=chain_fields
        or [ChainFieldSpec(field_name="chain", relations={"INFLUENCES": "Causal influence"})],
        code_fields=code_fields or [],
        source_fields=source_fields or [],
        value_maps=value_maps or {},
        concepts=concepts or [],
        sources=sources or [],
        items=items or [],
        chains=chains or [],
        mentions=mentions or [],
        from_source=from_source or [],
        item_fields=item_fields or {},
        taxonomy_edges=taxonomy_edges or [],
    )


@pytest.fixture
def payload_factory():
    """Returns _make_payload so individual tests can customise fields."""
    return _make_payload


@pytest.fixture
def empty_payload() -> GraphPayload:
    """Payload with no data — for edge-case coverage."""
    return _make_payload()


@pytest.fixture
def minimal_payload() -> GraphPayload:
    """Three concepts, two sources — passes default filters (min_freq=3, min_src=2)."""
    concepts = [
        {"props": {"name": "Resilience"}, "relations": {"topic": ["Community"]}},
        {"props": {"name": "Trust"}, "relations": {"topic": ["Community"]}},
        {"props": {"name": "Cooperation"}, "relations": {"topic": ["Society"]}},
    ]
    sources = [
        {"ref": "smith2024", "props": {}},
        {"ref": "jones2023", "props": {}},
    ]
    items = [
        {"item_id": "i001_n0001", "citation": "People cooperate naturally.", "description": ""},
        {"item_id": "i002_n0001", "citation": "Trust enables resilience.", "description": ""},
        {"item_id": "i003_n0001", "citation": "Cooperation builds community.", "description": ""},
    ]
    from_source = [
        {"item_id": "i001_n0001", "ref": "smith2024"},
        {"item_id": "i002_n0001", "ref": "jones2023"},
        {"item_id": "i003_n0001", "ref": "smith2024"},
    ]
    chains = [
        {"item_id": "i001_n0001", "source": "Resilience", "target": "Trust", "type": "INFLUENCES"},
        {"item_id": "i002_n0001", "source": "Trust", "target": "Cooperation", "type": "ENABLES"},
        {
            "item_id": "i003_n0001",
            "source": "Cooperation",
            "target": "Resilience",
            "type": "INFLUENCES",
        },
    ]
    # Each concept mentioned in all 3 items across 2 sources → passes default filters
    mentions = [
        {"item_id": iid, "concept": c}
        for iid in ("i001_n0001", "i002_n0001", "i003_n0001")
        for c in ("Resilience", "Trust", "Cooperation")
    ]
    return _make_payload(
        concepts=concepts,
        sources=sources,
        items=items,
        chains=chains,
        mentions=mentions,
        from_source=from_source,
        graph_fields=["topic"],
    )


# ---------------------------------------------------------------------------
# TOML config strings for test_config.py
# ---------------------------------------------------------------------------

TOML_NEO4J_VALID = """\
[neo4j]
uri = "bolt://127.0.0.1:7687"
user = "neo4j"
password = "test"
"""

TOML_NEO4J_MISSING_PASSWORD = """\
[neo4j]
uri = "bolt://127.0.0.1:7687"
user = "neo4j"
"""

TOML_HTML_CUSTOM = """\
[html]
output_path = "./custom.html"
min_frequency = 5
min_source_count = 3
max_nodes = 100
"""

TOML_ARCADEDB_VALID = """\
[arcadedb]
uri = "http://localhost:2480"
user = "root"
password = "test"
database = "mycorpus"
"""

TOML_ARCADEDB_MINIMAL = """\
[arcadedb]
password = "test"
"""

TOML_ARCADEDB_MISSING_PASSWORD = """\
[arcadedb]
uri = "http://localhost:2480"
user = "root"
"""


@pytest.fixture
def toml_neo4j_valid() -> str:
    return TOML_NEO4J_VALID


@pytest.fixture
def toml_html_custom() -> str:
    return TOML_HTML_CUSTOM


@pytest.fixture
def config_file(tmp_path: Path):
    """Factory: writes content to a temp TOML file and returns its Path."""

    def _write(content: str, name: str = "config.toml") -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p

    return _write
