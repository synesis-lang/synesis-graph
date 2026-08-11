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


def validate_cypher_label(label: str) -> bool:
    """Validates if label is safe for direct use in Cypher."""
    return bool(_CYPHER_LABEL_PATTERN.match(label))
