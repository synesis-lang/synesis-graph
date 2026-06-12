import importlib
import sys
import types

import pytest


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

    if "synesis2graph" in sys.modules:
        del sys.modules["synesis2graph"]

    module = importlib.import_module("synesis2graph")
    yield module

    if "synesis2graph" in sys.modules:
        del sys.modules["synesis2graph"]
