"""load_state.py - Se a carga de um grafo terminou, e quem a está fazendo.

Compartilhado pelos dois backends. A escrita em segmentos (ArcadeDB e Neo4j)
troca uma garantia por outra: uma carga interrompida deixa os segmentos já
commitados — um grafo parcial que não se declara incompleto. A marca no
`ProjectContext` e as duas guardas que a leem são a resposta, e não dependem do
transporte: por isso vivem aqui, e não em `arcadedb.py`, que o `neo4j.py` não
pode importar (o `arcadedb.py` já importa o `neo4j.py`).
"""

from __future__ import annotations

import time
from typing import Any

#: Property on `ProjectContext` recording whether a load ran to completion.
#:
#: Segmented commits mean an interrupted sync leaves committed segments behind.
#: Without a marker those leftovers are indistinguishable from a finished graph:
#: the vertices are there, the counts merely look low, and nothing says the load
#: stopped halfway. A researcher querying it would get answers that are wrong in
#: the one way that is hard to notice — quietly incomplete.
#:
#: So the flag is raised before the first write and lowered only after the last
#: one, embeddings included. `in_progress` on a graph nobody is currently
#: loading means exactly one thing: that load died, and the graph is partial.
LOAD_STATE_PROP = "load_state"
LOAD_IN_PROGRESS = "in_progress"
LOAD_COMPLETE = "complete"

#: Who raised the flag, and when. Written beside it, because `in_progress` alone
#: cannot answer the question that matters when a *new* load finds it set.
#:
#: Two very different situations look identical through the flag: another loader
#: is running right now, or a loader died and left the flag standing. The right
#: response is opposite in each — abort, because two writers on one graph corrupt
#: each other, versus proceed, because clearing the wreckage is exactly what a
#: retry is for. Recording the owner and the heartbeat is what tells them apart.
LOAD_OWNER_PROP = "load_owner"
LOAD_HEARTBEAT_PROP = "load_heartbeat"

#: How long a flag may stand without its owner refreshing the heartbeat before a
#: new load treats it as abandoned rather than live.
#:
#: Generous on purpose. The cost of waiting too long is a clear message telling
#: the researcher to retry; the cost of being too eager is two loaders writing the
#: same graph. Segments commit about every 30s against the remote server, and the
#: heartbeat rides with them, so a live loader refreshes far inside this window.
LOAD_STALE_AFTER_SECONDS = 900

def _load_owner_id() -> str:
    """Identifies this loader well enough for a human to act on it.

    Host and process: enough for the researcher who finds a live flag to know
    whether it is their own other window or a colleague's machine, which is the
    decision the message has to support.
    """
    import os
    import socket

    return f"{socket.gethostname()}:{os.getpid()}"


def describe_running_load(stored: dict[str, Any] | None) -> str | None:
    """Describes a load that appears to be running right now, or None.

    None means "no live owner": either no flag, a finished load, or a flag whose
    heartbeat went stale — the abandoned case, which a new load may clear.

    The staleness test is what keeps a crashed loader from locking the graph
    forever. A process that dies cannot lower its own flag, so a flag alone would
    be a permanent block requiring manual repair; the heartbeat gives it an
    expiry that a *living* loader keeps pushing forward.
    """
    if not load_is_incomplete(stored):
        return None
    assert stored is not None
    beat: object = stored.get(LOAD_HEARTBEAT_PROP)
    try:
        age = time.time() - float(beat)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        # A flag with no readable heartbeat predates this or was written by a
        # loader that never got to record one. Treated as abandoned: it cannot
        # be shown to be alive, and a stuck flag no one can clear is worse than
        # a rare overlap.
        return None
    if age > LOAD_STALE_AFTER_SECONDS:
        return None
    owner = str(stored.get(LOAD_OWNER_PROP) or "unknown")
    return f"{owner}, last seen {int(age)}s ago"


def load_is_incomplete(stored: dict[str, Any] | None) -> bool:
    """True when the stored context says a previous load never finished.

    Reads the answer off the graph, like `incompatible_with_update`: the graph is
    the only thing that knows what it actually contains, and the load that died
    may well have been run from another machine.

    A missing flag is not "incomplete". Graphs written before this existed carry
    no such property, and treating them as broken would condemn every older
    database on no evidence.
    """
    if not stored:
        return False
    return str(stored.get(LOAD_STATE_PROP) or "") == LOAD_IN_PROGRESS
