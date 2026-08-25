"""synesis-graph — Universal pipeline Synesis → Graph Databases."""

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

try:
    __version__ = _pkg_version("synesis-graph")
except PackageNotFoundError:
    # Fallback for a source tree that was never installed. Kept in step with
    # pyproject: it is written into every graph as `ProjectContext.
    # synesis_graph_version`, so a stale value here would misreport provenance.
    __version__ = "0.9.0"

# synesis2graph.py lives at the repo root — make it importable from anywhere.
_repo_root = Path(__file__).parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from synesis_graph.config import (  # noqa: E402,F401
    BACKEND_ARCADEDB,
    BACKEND_HTML,
    BACKEND_NEO4J,
    SUPPORTED_BACKENDS,
)
from synesis_graph.core import (  # noqa: E402,F401
    GraphPayload,
    PipelineResult,
    compile_project,
    load_json_project,
)
from synesis_graph.pipeline import run_pipeline  # noqa: E402,F401
