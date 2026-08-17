"""Field selection and text extraction for vector embeddings.

This module is deliberately free of any machine-learning dependency: it decides
*which* text becomes a vector and writes it to a sidecar file, but never computes
one. Generation lands in a separate module behind the optional `[embeddings]`
extra, so the decision that most affects retrieval quality — what text represents
a concept — can be inspected before installing anything.

The sidecar is keyed by concept name and records the model, the field composition
and a hash per concept, because each of those invalidates the vectors in a way
that is otherwise silent (see `EmbeddingsSidecar`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from synesis_graph.core import GraphPayload, PipelineError, humanize_concept_name

# Ontology field types whose values are free text and therefore carry the meaning
# a semantic query is looking for. Mirrors FULLTEXT_FIELD_TYPES in core.py, and
# for the same reason: ORDERED/ENUMERATED/SCALE hold a closed vocabulary, so
# embedding them places every concept sharing a category at the same point.
#
# TOPIC is the deliberate exception. Its vocabulary is closed (32 values across
# 1388 concepts in Social_Acceptance), but a topic like `Economics` or `Risk`
# situates a concept in the semantic field a question evokes, and it is short
# enough not to dominate the description it accompanies.
EMBEDDABLE_FIELD_TYPES: frozenset[str] = frozenset({"TEXT", "TOPIC"})

# Types that are never embedded, listed separately from "not embeddable" so the
# warning can tell the user *why* their field was refused.
CLOSED_VOCABULARY_TYPES: frozenset[str] = frozenset({"ORDERED", "ENUMERATED", "SCALE"})

SIDECAR_SUFFIX = ".embeddings.json"

# Bumped when the sidecar layout changes in a way older readers cannot handle.
# A file without the key predates versioning and must be regenerated.
SCHEMA_VERSION = 1


@dataclass
class EmbeddingFieldError(PipelineError):
    """A requested field cannot be used for embeddings."""

    pass


@dataclass
class ConceptText:
    """The text of one concept, and the hash that decides whether to recompute it."""

    name: str
    text: str

    @property
    def hash(self) -> str:
        """Hash of the normalized text.

        Normalization happens in `build_concept_text`, not here: hashing raw text
        would make a reformatted `.syno` — same words, different whitespace —
        invalidate the whole corpus and trigger a full recompute.
        """
        digest = hashlib.sha256(self.text.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"


@dataclass
class EmbeddingsSidecar:
    """The `<project>.embeddings.json` payload.

    Every field beyond `concepts` exists to catch a specific silent failure:

    - `model`/`dimensions`: swapping models leaves vectors that are individually
      valid and mutually meaningless. Without the record, the only symptom is
      worse search results.
    - `fields`/`fields_hash`: vectors built from different field compositions are
      not comparable — the distance between them measures the composition, not
      the meaning. An ontology coded in phases can produce exactly this.
    - `schema_version`: lets the format evolve without guessing.
    """

    fields: list[str]
    concepts: dict[str, ConceptText] = field(default_factory=dict)
    model: str | None = None
    dimensions: int | None = None
    vectors: dict[str, list[float]] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    @property
    def fields_hash(self) -> str:
        """Hash of the field composition, order included.

        Order matters because it changes the concatenated text and therefore the
        vector, so this is a hash of the list, not of a set.
        """
        digest = hashlib.sha256("\x1f".join(self.fields).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def to_dict(self) -> dict[str, Any]:
        """Serializable form. Vectors are omitted while absent (stage 1)."""
        return {
            "schema_version": self.schema_version,
            "model": self.model,
            "dimensions": self.dimensions,
            "fields": list(self.fields),
            "fields_hash": self.fields_hash,
            "concepts": {
                name: _concept_entry(ct, self.vectors.get(name))
                for name, ct in sorted(self.concepts.items())
            },
        }

    def write(self, path: Path) -> None:
        """Writes the sidecar deterministically.

        `sort_keys` and sorted concepts are not cosmetic: an unchanged corpus must
        produce a byte-identical file, otherwise "nothing changed" is impossible to
        verify and the file churns in every diff.
        """
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _concept_entry(ct: ConceptText, vector: list[float] | None) -> dict[str, Any]:
    entry: dict[str, Any] = {"hash": ct.hash, "text": ct.text}
    if vector is not None:
        entry["vector"] = vector
    return entry


def sidecar_path(project_path: Path) -> Path:
    """Sidecar path for a project: `face85.synp` -> `face85.embeddings.json`."""
    return project_path.with_suffix("").with_name(project_path.stem + SIDECAR_SUFFIX)


def ontology_field_types(template_data: dict[str, Any]) -> dict[str, str]:
    """Maps SCOPE ONTOLOGY field names to their declared type.

    Accepts either a full template dict (`{"field_specs": {...}}`) or the
    `field_specs` mapping itself, which is what `GraphPayload` carries.

    Reads the specs directly rather than going through `analyze_template`,
    because that function splits fields by destination (scalar vs graph) and
    drops the declared type — the very thing the selection rule needs.
    """
    specs = template_data.get("field_specs", template_data)
    return {
        name: str(spec.get("type", "TEXT")).upper()
        for name, spec in specs.items()
        if isinstance(spec, dict) and str(spec.get("scope", "")).upper() == "ONTOLOGY"
    }


def _concept_field_value(concept: dict[str, Any], field_name: str) -> str:
    """Reads a field from a concept, whichever half of the payload holds it.

    A concept carries scalar fields in `props` and closed-vocabulary fields in
    `relations` (as lists, since a concept may hold several values). TOPIC is
    embeddable *and* lives in `relations`, so both have to be read.
    """
    props = concept.get("props", {})
    if field_name in props:
        value = props[field_name]
        return "" if value is None else str(value)

    relations = concept.get("relations", {})
    if field_name in relations:
        values = relations[field_name]
        if isinstance(values, list):
            return ", ".join(str(v) for v in values if v is not None)
        return "" if values is None else str(values)

    return ""


def _normalize(text: str) -> str:
    """Collapses whitespace so formatting changes do not invalidate vectors."""
    return " ".join(text.split())


def build_concept_text(concept: dict[str, Any], fields: list[str]) -> str:
    """Assembles the text that represents a concept.

    The concept's humanized name always leads: it is the most specific thing said
    about the concept, and `humanize_concept_name` already exists because the raw
    snake_case is unusable for natural-language matching (see its docstring).

    Empty fields are skipped rather than contributing an empty segment, so a
    concept missing an optional field is not penalized with a trailing separator
    that shifts its vector.
    """
    name = concept.get("props", {}).get("name", "")
    parts = [humanize_concept_name(name)] if name else []
    for field_name in fields:
        value = _normalize(_concept_field_value(concept, field_name))
        if value:
            parts.append(value)
    return _normalize(". ".join(part.rstrip(".") for part in parts if part))


def resolve_fields(
    requested: list[str],
    template_data: dict[str, Any],
    payload: GraphPayload,
) -> tuple[list[str], list[str]] | EmbeddingFieldError:
    """Validates requested fields against the template and the actual corpus.

    Returns `(fields, warnings)`. Rejects, rather than warns, only when the field
    does not exist — every other objection is advisory, because a template may
    legitimately want a field this rule would not pick.

    Constant fields are dropped automatically: a field holding one distinct value
    across the corpus adds the same text to every concept, which shifts all
    vectors identically and discriminates nothing. Measured in Social_Acceptance,
    `theoretical_significance` is `0` in all 1388 concepts.
    """
    declared = ontology_field_types(template_data)
    if not declared:
        return EmbeddingFieldError(
            message="Template declares no SCOPE ONTOLOGY fields",
            stage="embeddings",
            details="Vector embeddings need at least one ontology field to describe a concept.",
        )

    unknown = [f for f in requested if f not in declared]
    if unknown:
        available = ", ".join(sorted(declared))
        return EmbeddingFieldError(
            message=f"Unknown ontology field(s): {', '.join(unknown)}",
            stage="embeddings",
            details=f"Available SCOPE ONTOLOGY fields: {available}",
        )

    warnings: list[str] = []
    fields: list[str] = []

    for field_name in requested:
        field_type = declared[field_name]

        # Constancy is checked first and wins: a constant field is dropped, so
        # warning that it is also a closed vocabulary would only contradict the
        # decision the user is about to read.
        distinct = _distinct_values(payload.concepts, field_name)
        if distinct <= 1:
            warnings.append(
                f"'{field_name}' has {distinct} distinct value(s) across "
                f"{len(payload.concepts)} concepts — constant, so it cannot "
                f"discriminate. Skipping it."
            )
            continue

        if field_type in CLOSED_VOCABULARY_TYPES:
            warnings.append(
                f"'{field_name}' is {field_type}, a closed vocabulary — every concept "
                f"sharing a value contributes identical text. Including it anyway."
            )
        elif field_type not in EMBEDDABLE_FIELD_TYPES:
            warnings.append(
                f"'{field_name}' has type {field_type}, which is not a text type. "
                f"Including it anyway."
            )

        fields.append(field_name)

    if not fields:
        return EmbeddingFieldError(
            message="No usable fields left after validation",
            stage="embeddings",
            details="Every requested field was constant across the corpus.",
        )

    return fields, warnings


def _distinct_values(concepts: list[dict[str, Any]], field_name: str) -> int:
    """Counts distinct non-empty values of a field across the corpus."""
    seen = {_normalize(_concept_field_value(c, field_name)) for c in concepts}
    seen.discard("")
    return len(seen)


def build_sidecar(
    payload: GraphPayload,
    fields: list[str],
) -> EmbeddingsSidecar:
    """Builds the sidecar contents for a payload, without vectors.

    Concepts whose assembled text is empty are omitted: an empty string embeds to
    a meaningless point that would still be returned as a neighbour.
    """
    sidecar = EmbeddingsSidecar(fields=list(fields))
    for concept in payload.concepts:
        name = concept.get("props", {}).get("name")
        if not name:
            continue
        text = build_concept_text(concept, fields)
        if not text:
            continue
        sidecar.concepts[name] = ConceptText(name=name, text=text)
    return sidecar


def load_sidecar(path: Path) -> EmbeddingsSidecar | None:
    """Reads an existing sidecar, or None when absent or unreadable.

    A malformed or version-mismatched file is treated as absent rather than
    fatal: the sidecar is a cache, and regenerating it is always correct.
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if raw.get("schema_version") != SCHEMA_VERSION:
        return None

    sidecar = EmbeddingsSidecar(
        fields=list(raw.get("fields", [])),
        model=raw.get("model"),
        dimensions=raw.get("dimensions"),
    )
    for name, entry in raw.get("concepts", {}).items():
        if not isinstance(entry, dict):
            continue
        sidecar.concepts[name] = ConceptText(name=name, text=entry.get("text", ""))
        vector = entry.get("vector")
        if isinstance(vector, list):
            sidecar.vectors[name] = vector
    return sidecar
