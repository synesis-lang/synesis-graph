"""synesis-graph — Universal pipeline Synesis → Graph Databases."""

import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

try:
    __version__ = _pkg_version("synesis-graph")
except PackageNotFoundError:
    __version__ = "0.2.0"

# synesis2graph.py lives at the repo root — make it importable from anywhere.
_repo_root = Path(__file__).parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from synesis2graph import (  # noqa: E402, F401  (public API re-export; sys.path set above)
    BACKEND_GRAPHQLITE,
    BACKEND_HTML,
    BACKEND_NEO4J,
    SUPPORTED_BACKENDS,
    GraphPayload,
    PipelineResult,
    compile_project,
    load_json_project,
    run_pipeline,
)
