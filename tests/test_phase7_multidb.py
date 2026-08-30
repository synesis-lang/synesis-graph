from __future__ import annotations

from pathlib import Path
from typing import Any


class _DummyStep:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DummyReporter:
    def info(self, msg: str) -> None:
        return None

    def success(self, msg: str) -> None:
        return None

    def warning(self, msg: str) -> None:
        return None

    def error(self, msg: str) -> None:
        return None

    def step(self, desc: str) -> _DummyStep:
        return _DummyStep()

    def print_diagnostics(self, diagnostics) -> None:
        return None


def _write_full_config(path: Path) -> None:
    path.write_text(
        """
[neo4j]
uri = "bolt://127.0.0.1:7687"
user = "neo4j"
password = "secret"
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _make_payload(s2g: Any):
    return s2g.GraphPayload(
        project_name="demo_project",
        concept_label="Concept",
        scalar_fields=[],
        graph_fields=[],
        chain_fields=[],
        code_fields=[],
        source_fields=[],
        value_maps={},
        concepts=[{"props": {"name": "c1", "description": "d", "created": 1}, "relations": {}}],
        sources=[{"bibtex": "s1", "title": "Title"}],
        items=[{"item_id": "i1", "content": "text"}],
        chains=[],
        mentions=[{"item_id": "i1", "concept": "c1", "mention_order": 0}],
        from_source=[{"item_id": "i1", "ref": "s1"}],
    )


def test_load_config_missing_neo4j_section_returns_error(s2g, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        """
[html]
output_path = "./graph.html"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = s2g.load_config(cfg, s2g.BACKEND_NEO4J)
    assert isinstance(result, s2g.ConnectionError)
    assert result.stage == "config"
    assert "[neo4j]" in (result.details or "")


def test_run_pipeline_rejects_invalid_backend(s2g, tmp_path):
    project = tmp_path / "proj.synp"
    project.write_text("x", encoding="utf-8")
    cfg = tmp_path / "config.toml"
    _write_full_config(cfg)

    result = s2g.run_pipeline(project, cfg, DummyReporter(), backend="invalid")
    assert not result.success
    assert isinstance(result.error, s2g.ConnectionError)
    assert result.error.stage == "backend"


def test_neo4j_adapter_connect_failure_returns_connection_error(s2g, monkeypatch):
    class FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: Any, **_kwargs: Any):
            raise RuntimeError("auth failed")

    import synesis_graph.backends.base as _base
    monkeypatch.setattr(_base, "get_neo4j_driver_factory", lambda: FakeGraphDatabase)
    adapter = s2g.Neo4jBackendAdapter(
        s2g.Neo4jConfig(uri="bolt://127.0.0.1:7687", user="neo4j", password="wrong")
    )

    err = adapter.connect(DummyReporter())
    assert isinstance(err, s2g.ConnectionError)
    assert err.stage == "connection"
    assert "auth failed" in (err.details or "")


def test_neo4j_adapter_smoke_executes_and_closes_resources(s2g, monkeypatch):
    class FakeSession:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeDriver:
        def __init__(self):
            self.closed = False
            self.session_obj = FakeSession()

        def session(self, database: str):
            return self.session_obj

        def close(self):
            self.closed = True

    class FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: Any, **_kwargs: Any):
            return fake_driver

    fake_driver = FakeDriver()

    import synesis_graph.backends.base as _base
    monkeypatch.setattr(_base, "get_neo4j_driver_factory", lambda: FakeGraphDatabase)
    monkeypatch.setattr(
        _base,
        "ensure_database_exists",
        lambda driver, db_name, reporter, default_database="neo4j": (db_name, None),
    )
    monkeypatch.setattr(
        _base,
        "sync_to_neo4j",
        # `mode` is accepted, not ignored: the adapter must keep passing the
        # default when nobody asked for an incremental run.
        lambda session, payload, analyzer=None, mode="rebuild": (
            None if mode == "rebuild" else _unexpected_mode(mode)
        ),
    )
    monkeypatch.setattr(_base, "compute_metrics", lambda session, payload, reporter: None)

    adapter = s2g.Neo4jBackendAdapter(
        s2g.Neo4jConfig(uri="bolt://127.0.0.1:7687", user="neo4j", password="secret")
    )
    err = s2g.execute_backend_pipeline(adapter, _make_payload(s2g), DummyReporter())
    assert err is None
    assert fake_driver.session_obj.closed is True
    assert fake_driver.closed is True



def _unexpected_mode(mode: str):
    """Fails loudly if the adapter forwards a mode nobody asked for."""
    raise AssertionError(f"unexpected sync mode {mode!r}")


def _fake_neo4j(monkeypatch, s2g, captured):
    """Instala fakes de Neo4j e captura o database passado ao driver.session()."""

    class FakeSession:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeDriver:
        def __init__(self):
            self.closed = False
            self.session_obj = FakeSession()

        def session(self, database: str):
            captured["db"] = database
            return self.session_obj

        def close(self):
            self.closed = True

    class FakeGraphDatabase:
        @staticmethod
        def driver(uri: str, auth: Any, **_kwargs: Any):
            return FakeDriver()

    import synesis_graph.backends.base as _base
    monkeypatch.setattr(_base, "get_neo4j_driver_factory", lambda: FakeGraphDatabase)
    monkeypatch.setattr(
        _base,
        "ensure_database_exists",
        lambda driver, db_name, reporter, default_database="neo4j": (db_name, None),
    )
    monkeypatch.setattr(
        _base,
        "sync_to_neo4j",
        # `mode` is accepted, not ignored: the adapter must keep passing the
        # default when nobody asked for an incremental run.
        lambda session, payload, analyzer=None, mode="rebuild": (
            None if mode == "rebuild" else _unexpected_mode(mode)
        ),
    )
    monkeypatch.setattr(_base, "compute_metrics", lambda session, payload, reporter: None)


def test_prepare_destination_derives_db_name_from_project_name(s2g, monkeypatch):
    """O nome do banco Neo4j vem de payload.project_name, sanitizado."""
    captured: dict = {}
    _fake_neo4j(monkeypatch, s2g, captured)

    adapter = s2g.Neo4jBackendAdapter(
        s2g.Neo4jConfig(uri="bolt://127.0.0.1:7687", user="neo4j", password="secret")
    )
    payload = _make_payload(s2g)
    payload.project_name = "Quinto_Andar"
    assert adapter.connect(DummyReporter()) is None
    assert adapter.prepare_destination(payload, DummyReporter()) is None
    # underscore -> hifen (regra do Neo4j), minusculo
    assert adapter.db_name == "quinto-andar"
    assert captured["db"] == "quinto-andar"


def test_database_flag_overrides_project_name(s2g, tmp_path, monkeypatch):
    """--database Quinto_Andar sobrescreve payload.project_name -> nomeia o agregado.

    Sem --database, o payload mantem o nome derivado (aqui 'lattes_abstracts',
    injetado pelo mock); com --database, run_pipeline o substitui e o banco Neo4j
    resulta 'quinto-andar' (sanitizado).
    """
    import synesis_graph.pipeline as _pipe
    from synesis_graph.pipeline import run_pipeline

    captured: dict = {}
    _fake_neo4j(monkeypatch, s2g, captured)

    cfg = tmp_path / "config.toml"
    _write_full_config(cfg)

    # Evita o compilador real (dummy no fixture s2g): devolve um payload pronto,
    # simulando o resultado do link step com nome derivado dos membros.
    def fake_compile_and_link(project_paths, reporter):
        payload = _make_payload(s2g)
        payload.project_name = "lattes_abstracts"
        return payload

    monkeypatch.setattr(_pipe, "_compile_and_link", fake_compile_and_link)

    p1 = tmp_path / "lattes.synp"
    p2 = tmp_path / "abstracts.synp"
    p1.write_text("x", encoding="utf-8")
    p2.write_text("x", encoding="utf-8")

    result = run_pipeline(
        project_path=p1,
        config_path=cfg,
        reporter=DummyReporter(),
        backend="neo4j",
        database="Quinto_Andar",
        extra_projects=[p2],
    )
    assert result.success, result.error
    assert captured["db"] == "quinto-andar"


def test_no_database_flag_keeps_derived_name(s2g, tmp_path, monkeypatch):
    """Sem --database, o nome derivado (dos membros) e preservado."""
    import synesis_graph.pipeline as _pipe
    from synesis_graph.pipeline import run_pipeline

    captured: dict = {}
    _fake_neo4j(monkeypatch, s2g, captured)

    cfg = tmp_path / "config.toml"
    _write_full_config(cfg)

    def fake_compile_and_link(project_paths, reporter):
        payload = _make_payload(s2g)
        payload.project_name = "lattes_abstracts"
        return payload

    monkeypatch.setattr(_pipe, "_compile_and_link", fake_compile_and_link)

    p1 = tmp_path / "lattes.synp"
    p2 = tmp_path / "abstracts.synp"
    p1.write_text("x", encoding="utf-8")
    p2.write_text("x", encoding="utf-8")

    result = run_pipeline(
        project_path=p1,
        config_path=cfg,
        reporter=DummyReporter(),
        backend="neo4j",
        database=None,
        extra_projects=[p2],
    )
    assert result.success, result.error
    assert captured["db"] == "lattes-abstracts"  # derivado, sanitizado


def test_run_pipeline_stats_consistent_between_backends(s2g, tmp_path, monkeypatch):
    project = tmp_path / "proj.synp"
    project.write_text("x", encoding="utf-8")
    cfg = tmp_path / "config.toml"
    _write_full_config(cfg)
    payload = _make_payload(s2g)

    class FakeAdapter:
        def __init__(self, backend_name: str):
            self._backend_name = backend_name

        @property
        def backend_name(self) -> str:
            return self._backend_name

        def preflight(self, reporter):
            return None

        def connect(self, reporter):
            return None

        def prepare_destination(self, payload, reporter):
            return None

        def clear_destination(self, payload, reporter):
            return None

        def synchronize_payload(self, payload, reporter):
            return None

        def compute_backend_metrics(self, payload, reporter):
            return None

        def close(self):
            return None

    import synesis_graph.pipeline as _pipeline
    monkeypatch.setattr(_pipeline, "compile_project", lambda project_path, reporter: payload)
    monkeypatch.setattr(
        _pipeline,
        "build_backend_adapter",
        lambda backend, config, config_path, project_path: FakeAdapter(backend),
    )

    neo4j_result = s2g.run_pipeline(project, cfg, DummyReporter(), backend=s2g.BACKEND_NEO4J)
    html_result = s2g.run_pipeline(project, cfg, DummyReporter(), backend=s2g.BACKEND_HTML)

    assert neo4j_result.success is True
    assert html_result.success is True
    assert neo4j_result.stats == html_result.stats
    assert neo4j_result.stats == {
        "concepts": len(payload.concepts),
        "sources": len(payload.sources),
        "items": len(payload.items),
        "chains": len(payload.chains),
    }
