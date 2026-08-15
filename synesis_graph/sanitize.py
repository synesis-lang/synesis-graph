"""Cypher label and Neo4j database name sanitization utilities."""

from __future__ import annotations

import re

_CYPHER_LABEL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sanitize_cypher_label(label: str) -> str:
    """
    Sanitizes string for safe use as label/relationship type in Cypher.

    Keeps only alphanumeric characters and underscore.
    Ensures it starts with a letter or underscore.
    """
    sanitized = "".join(c for c in label if c.isalnum() or c == "_")
    if sanitized and sanitized[0].isdigit():
        sanitized = "_" + sanitized
    return sanitized or "Unknown"


def sanitize_database_name(name: str) -> str:
    """
    Sanitizes string for use as Neo4j database name.

    Neo4j accepts only: ASCII letters, numbers, dots and hyphens.
    Underscores are converted to hyphens.
    """
    # Convert underscores to hyphens
    name = name.replace("_", "-")
    # Keep only valid characters
    sanitized = "".join(c for c in name if c.isalnum() or c in ".-")
    # Ensure it starts with a letter
    if sanitized and not sanitized[0].isalpha():
        sanitized = "db" + sanitized
    return sanitized.lower() or "synesis"


def sanitize_arcadedb_database_name(name: str) -> str:
    """
    Sanitizes string for use as an ArcadeDB database name.

    ArcadeDB is far more permissive than Neo4j: it accepts underscores, uppercase
    and accents, so `Quinto_Andar` survives intact instead of degrading to
    `quinto-andar`. Preserving the project name matters — it is what the researcher
    sees in Studio.

    Permissiveness is the hazard, though. The server accepts a name containing a
    space or a slash and turns it into a directory on disk: `create database
    "com/barra"` leaves a stray `com/` folder under `databases/`, and the name also
    has to survive being placed in a URL path. Separators and whitespace are
    therefore replaced with underscores rather than passed through.
    """
    sanitized = "".join(
        c if (c.isalnum() or c in "_-.") else "_" for c in name.strip()
    )
    # A leading dot would read as a hidden directory; a leading digit is accepted by
    # the server but makes an ambiguous identifier.
    if sanitized and not (sanitized[0].isalpha() or sanitized[0] == "_"):
        sanitized = "db_" + sanitized
    return sanitized or "synesis"


def validate_cypher_label(label: str) -> bool:
    """Validates if label is safe for direct use in Cypher."""
    return bool(_CYPHER_LABEL_PATTERN.match(label))
