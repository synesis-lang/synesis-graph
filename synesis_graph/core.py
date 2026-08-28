"""Core types, template analysis, and project compilation for synesis-graph."""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

with contextlib.suppress(ModuleNotFoundError):
    import tomllib  # type: ignore  # noqa: F401

from synesis_graph.sanitize import sanitize_cypher_label

# Hard exit if synesis is not installed — preserved exactly from original.
try:
    from synesis import SynesisCompiler
except ImportError:
    print("ERRO CRÍTICO: Biblioteca 'synesis' não encontrada.")
    print("Instale via: pip install synesis")
    sys.exit(1)


# ============================================================================
# LAZY BACKEND DEPENDENCY LOADERS
# ============================================================================


def get_neo4j_driver_factory() -> Any:
    """Loads Neo4j driver factory lazily to isolate backend dependencies."""
    try:
        from neo4j import GraphDatabase as neo4j_graph_database

        return neo4j_graph_database
    except ImportError:
        return None


# ============================================================================
# RESULT TYPES
# ============================================================================


@dataclass
class PipelineError:
    """Base pipeline error with context."""

    message: str
    stage: str
    details: str | None = None


@dataclass
class CompilationError(PipelineError):
    """Error in Synesis project compilation."""

    diagnostics: list[str] = field(default_factory=list)


@dataclass
class ConnectionError(PipelineError):
    """Error connecting to database backends."""

    pass


@dataclass
class SyncError(PipelineError):
    """Error synchronizing with the database."""

    pass


@dataclass
class DependencyError(PipelineError):
    """Error for missing runtime dependencies."""

    pass


@dataclass
class ChainFieldSpec:
    """Specification of a CHAIN field from the template."""

    field_name: str
    relations: dict[str, str] | None  # {type: description}; None when CHAIN has no RELATIONS block


@dataclass
class CodeFieldSpec:
    """Specification of a CODE field from the template."""

    field_name: str
    description: str


# Template field types whose values are free text, and therefore worth
# full-text indexing. TOPIC/ENUMERATED/ORDERED are excluded on purpose: they
# carry a closed vocabulary (and ORDERED/ENUMERATED are numeric indices mapped
# to labels), so indexing them as prose pollutes the index with category names.
FULLTEXT_FIELD_TYPES: frozenset[str] = frozenset({"TEXT", "QUOTATION", "MEMO"})


@dataclass
class SourceFieldSpec:
    """Specification of a SCOPE SOURCE field from the template.

    Carries the declared type alongside the name so downstream consumers can
    tell prose (TEXT) from a closed vocabulary (ENUMERATED) — the distinction
    that decides whether a field belongs in a full-text index.
    """

    field_name: str
    field_type: str

    @property
    def is_text(self) -> bool:
        """True when the field holds free text worth full-text indexing."""
        return self.field_type.upper() in FULLTEXT_FIELD_TYPES


# Lucene analyzer for the full-text indexes. Neo4j's own default; does no stemming
# and no accent folding — safe for any language, optimal for none. Overridable per
# corpus via `fulltext_analyzer` in config.toml (see DEFAULT_FULLTEXT_ANALYZER usage
# in config.py), because the right value follows the corpus language, not the code.
DEFAULT_FULLTEXT_ANALYZER = "standard-no-stop-words"

# Default HTTP endpoint of an ArcadeDB server. Port 2480 is the server's own default;
# unlike Neo4j's BOLT port it serves the Studio UI too, so it is the address a user
# already has open in the browser.
DEFAULT_ARCADEDB_URI = "http://localhost:2480"

# Lucene analyzer for ArcadeDB's full-text indexes. ArcadeDB names analyzers by their
# Lucene class rather than by a short label, and its own default is the standard
# analyzer — no stemming, no accent folding: safe for any language, optimal for none.
# Override per corpus in config.toml; a Portuguese corpus gains both under
# `org.apache.lucene.analysis.br.BrazilianAnalyzer`.
DEFAULT_ARCADEDB_ANALYZER = "org.apache.lucene.analysis.standard.StandardAnalyzer"

# Short analyzer names (the vocabulary Neo4j uses) mapped to the Lucene classes
# ArcadeDB expects. Lets one config.toml serve both backends: `fulltext_analyzer =
# "brazilian"` means the same thing to each. A value already shaped like a class name
# is passed through untouched, so any analyzer the server has stays reachable.
ARCADEDB_ANALYZER_ALIASES: dict[str, str] = {
    "brazilian": "org.apache.lucene.analysis.br.BrazilianAnalyzer",
    "portuguese": "org.apache.lucene.analysis.pt.PortugueseAnalyzer",
    "english": "org.apache.lucene.analysis.en.EnglishAnalyzer",
    "spanish": "org.apache.lucene.analysis.es.SpanishAnalyzer",
    "french": "org.apache.lucene.analysis.fr.FrenchAnalyzer",
    "german": "org.apache.lucene.analysis.de.GermanAnalyzer",
    "italian": "org.apache.lucene.analysis.it.ItalianAnalyzer",
    "standard": "org.apache.lucene.analysis.standard.StandardAnalyzer",
    # Neo4j's default has no ArcadeDB equivalent; map it to the closest analyzer so a
    # config written for Neo4j does not fail against a server that never heard of it.
    "standard-no-stop-words": "org.apache.lucene.analysis.core.SimpleAnalyzer",
    "simple": "org.apache.lucene.analysis.core.SimpleAnalyzer",
    "whitespace": "org.apache.lucene.analysis.core.WhitespaceAnalyzer",
}


def resolve_arcadedb_analyzer(name: str) -> str:
    """Lucene class for an analyzer name, accepting short labels and class names.

    Keeps `fulltext_analyzer` portable between the Neo4j and ArcadeDB backends: the
    short names Neo4j understands resolve to the classes ArcadeDB requires, while a
    fully-qualified class (or any name the alias table does not know) is passed
    through so the server remains the authority on what exists.
    """
    if not name:
        return DEFAULT_ARCADEDB_ANALYZER
    return ARCADEDB_ANALYZER_ALIASES.get(name.strip().lower(), name)


# Default embedding model. Multilingual is a requirement rather than a
# preference here: measured against the 210 real face85 concepts, the
# English-only `all-MiniLM-L6-v2` reproduced BM25's lexical error on the question
# most dependent on Portuguese semantics, while this model answered it correctly.
# Lives in core (not in the provider) so config.py can name it without importing
# anything that pulls in torch.
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def humanize_concept_name(name: str) -> str:
    """Concept name as words, for the full-text index.

    Lucene's tokenizer follows UAX#29, where the underscore is a word character
    rather than a boundary — so `governança_corporativa` is indexed as ONE token
    and no natural-language question can ever match it. Measured against face85:
    "governança corporativa", "governança" and "corporativa" all failed to reach a
    node that plainly exists. Swapping the analyzer does not help (`brazilian` over
    the raw name returned 0 hits for every variation); the tokenization has to be
    fixed in the indexed text itself.

    Synesis guarantees the snake_case — SYNESIS_E015 rejects spaces in a concept,
    since the parser needs `_` where the chain separator `->` does not reach — so
    this derivation is mechanical and template-agnostic.

    `name` is untouched: it remains the MERGE key, the uniqueness constraint and
    the identity every edge resolves against.
    """
    return name.replace("_", " ")


def dedupe_index_props(props: Iterable[str]) -> list[str]:
    """Removes repeated property names from an index list, keeping first order.

    The structural bibliographic props (`title`, `abstract`) are prepended by
    both backends before the template's own TEXT SOURCE fields, and nothing stops
    a template from declaring a field with one of those names — Quinto_Andar
    declares `FIELD title TYPE TEXT SCOPE SOURCE` for the candidate's name, which
    is legitimate: the same slot, filled from a different source.

    Neo4j rejects the repetition outright (`RepeatedPropertyInCompositeSchema`),
    which failed the whole sync after a 41k-item compile; ArcadeDB accepts the
    composite and indexes the column twice. Neither is what the caller meant, and
    the intent is the same in both: index each property once.

    Order is preserved because the first occurrence is the structural one, and
    `SEARCH_INDEX` addresses a composite index by its full ordered name.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for prop in props:
        if prop not in seen:
            seen.add(prop)
            unique.append(prop)
    return unique


def source_field_names(source_fields: Sequence[SourceFieldSpec | str]) -> list[str]:
    """Field names from a SOURCE spec list.

    Tolerates plain strings so payloads built by hand (tests, the synesis2graph
    shim) keep working after `source_fields` grew from list[str] into specs.
    """
    return [f if isinstance(f, str) else f.field_name for f in source_fields]


def text_source_field_names(source_fields: Sequence[SourceFieldSpec | str]) -> list[str]:
    """Names of the SOURCE fields holding free text, for full-text indexing.

    A bare string carries no type, so it is assumed to be text — that is the
    pre-spec behaviour, and over-indexing is the safer failure here.
    """
    return [f for f in source_fields if isinstance(f, str)] + [
        f.field_name for f in source_fields if isinstance(f, SourceFieldSpec) and f.is_text
    ]


@dataclass
class ProjectContextSpec:
    """The project's own context, to be written into the graph as a vertex.

    An exported graph is syntax without semantics: a consumer introspecting the
    schema learns that a vertex `Aspect` has a property `name`, but not that
    `Aspect` is Dooyeweerd's modal scale, that its values are *ordered*, or what
    `[15] Fiducial` means. All of that is declared in the template and thrown
    away at export time.

    The point of this type is that **the context travels with the data, not with
    the tool**. A client reading the template locally would solve it for itself
    only; written to the graph, every consumer gets it — Claude Desktop, any MCP
    client, or a human in the database's own studio.

    The context is not stored as a vertex-per-field either. Exploding the template
    into `Field`/`Value` vertices would pollute the research graph with metadata:
    analytical queries would start bumping into nodes that are not corpus data.
    This context is meant to be *read*, not traversed.

    **It is written as Markdown, not JSON.** Measured against the face85 corpus
    through the real MCP path: the serialized `field_specs` reached the model as
    29k characters (~7.3k tokens) of raw JSON in which 53% of the keys were
    `null` — a shape the model has to parse before it can read anything. The same
    content as Markdown is 27% smaller and immediately legible, and it is the
    template's own idiom: GUIDELINES are already written with headings and line
    breaks, which JSON escaping destroys.
    """

    project_name: str
    description: str
    concept_label: str
    template_name: str
    # The template's semantics as a readable document: every field with its type,
    # scope, description, value scale and GUIDELINES. This is the property a
    # consumer injects into a prompt.
    template_doc: str
    # Provenance and shape of this snapshot, also as prose.
    project_summary: str
    compiler_version: str
    synesis_graph_version: str
    compiled_at: str
    generated_at: str
    source_count: int
    item_count: int
    concept_count: int

    # What the graph can answer semantically, declared rather than guessed.
    #
    # A client can see from `get_schema` that a vector index exists, but not
    # WHICH FIELD produced the vectors — and that changes what proximity means:
    # over `ontology_description` it is conceptual similarity, over `topic` it is
    # thematic co-occurrence. A consumer that cannot tell them apart is
    # interpreting distances it does not understand.
    #
    # Empty means no semantic search. Never inferred from the index: a stale
    # index over a graph that was re-synced without vectors would announce a
    # capability the data no longer has.
    embedding_fields: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 0

    # Lexical search capability — the full-text indexes this backend actually
    # built, and the analyzer they were built with.
    #
    # The chat was working around, in natural language, a problem this layer
    # already solves: it instructed the model to cut a search term before its
    # first accented character, because `CONTAINS 'psicologicos'` misses
    # `psicológicos`. `SEARCH_INDEX` with the `brazilian` analyzer finds it
    # deterministically — but the consumer cannot use it without knowing that the
    # index exists, over WHICH fields, and under which name: a composite index
    # (`Concept[search_name, ontology_description]`) has to be addressed by its
    # exact field list, so guessing is not an option.
    #
    # Declared, not inferred, for the same reason as the vector capability — and
    # only by the backend that built the indexes. Neo4j has full-text too, but a
    # different query form; announcing ArcadeDB's syntax for a Neo4j graph would
    # teach a query that always fails.
    fulltext_concept_fields: str = ""
    fulltext_item_fields: str = ""
    fulltext_source_fields: str = ""
    fulltext_analyzer: str = ""

    # Which backend computed the network metrics, and over which projection.
    #
    # This is not bookkeeping: it changes how the number must be read. ArcadeDB's
    # `algo.*` procedures accept no scope filter and run over the WHOLE graph, so a
    # concept's PageRank incorporates its edges to Items, Sources and taxonomy
    # nodes; Neo4j's GDS projects only the concept subgraph. The two are not
    # comparable, and a consumer ranking "most central concepts" has no way to know
    # that from the score alone.
    metrics_backend: str = ""
    metrics_scope: str = ""


def _utc_now_iso() -> str:
    """Sync timestamp, UTC. Lets a consumer tell how old the snapshot is."""
    return datetime.now(timezone.utc).isoformat()


def _synesis_graph_version() -> str:
    """This package's version, for provenance.

    Imported lazily: `synesis_graph/__init__.py` is the package root and importing
    it from here at module level would cycle.
    """
    try:
        from synesis_graph import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001 - provenance is best-effort, never fatal
        return ""


def strip_locations(obj: Any) -> Any:
    """Removes every `location` key, at any depth.

    Locations are absolute paths from the machine that compiled the project
    (`D:\\GitHub\\...`). They are useless to any consumer of the database and
    unwelcome in a graph shared with a co-author.

    **The recursion is the point.** `location` shows up at two levels — on the
    field spec itself and inside each entry of its `values[]` — so a shallow
    pass would leave most of the noise behind. Dropping them cuts the payload
    by 17% to 35%, measured over two real projects.
    """
    if isinstance(obj, dict):
        return {k: strip_locations(v) for k, v in obj.items() if k != "location"}
    if isinstance(obj, list):
        return [strip_locations(item) for item in obj]
    return obj


def project_root_from_includes(project: dict[str, Any], corpus: list[dict[str, Any]]) -> str:
    """Infers the project root, to turn absolute traceability paths into relative ones.

    The canonical JSON carries the same file twice, in two shapes: `project.includes[]`
    lists it **relative** to the project (`annotations.syn`), while each corpus item's
    `traceability.file` is **absolute** on the machine that compiled
    (`D:\\GitHub\\...\\annotations.syn`). Verified against a real export, not assumed.

    That redundancy is the whole trick: the root is the prefix that remains when the
    relative include is stripped off the end of the absolute path. No extra field, no
    guessing from the filesystem — and it works even when the graph is built on a
    machine that no longer has the project.

    Returns "" when the two shapes cannot be matched, and callers then keep the path
    as it came. Degrading to an absolute path is a privacy wart (see
    `strip_locations`), never a failure.
    """
    includes = project.get("includes") or []
    relative_paths = [
        str(entry.get("path") or "").replace("\\", "/")
        for entry in includes
        if isinstance(entry, dict) and entry.get("path")
    ]
    if not relative_paths:
        return ""

    for item in corpus:
        absolute = str((item.get("traceability") or {}).get("file") or "").replace("\\", "/")
        if not absolute:
            continue
        for relative in relative_paths:
            # `endswith` and not a path join: the include may itself be nested
            # (`data/annotations.syn`), and the suffix is what both shapes share.
            if relative and absolute.endswith(relative):
                return absolute[: -len(relative)].rstrip("/")
    return ""


def relative_source_file(absolute_path: str, project_root: str) -> str:
    """The traceability path as stored on the Item node.

    Relative to the project root, with forward slashes. Two reasons, and the second
    is the one that decides the format:

    - An absolute path leaks the directory layout of whoever exported the graph, to
      everyone the graph is shared with — the same objection `strip_locations` raises.
    - The VSCode chat resolves the anchor against the open workspace, so a path
      relative to the project root is what actually clicks through to the `.syn`.

    Normalising separators is not cosmetic: a graph exported on Windows is read by a
    chat that may run anywhere, and `D:\\a\\b.syn` would not resolve there.
    """
    normalised = str(absolute_path or "").replace("\\", "/")
    if not normalised:
        return ""

    root = str(project_root or "").replace("\\", "/").rstrip("/")
    if root:
        # Boundary check on path COMPONENTS, not on the raw prefix: `/proj-evil`
        # starts with `/proj` as a string but is not inside it, and returning
        # `-evil/x.syn` from that would be worse than returning nothing.
        if normalised == root:
            return ""
        if normalised.startswith(root + "/"):
            return normalised[len(root) + 1 :]

    # Relativisation failed. **Omit rather than keep the absolute path.** Keeping
    # it leaks the exporting machine's directory layout to everyone the graph is
    # shared with, and the path does not resolve on the reader's machine anyway —
    # so the anchor it produces is a link that cannot open. An absent
    # `source_file` is honest; a wrong one promises verification and fails.
    return ""


def _render_field_doc(name: str, spec: dict[str, Any]) -> list[str]:
    """One field as Markdown: heading, prose, value scale, guidelines.

    Keys whose value is `None` are skipped rather than rendered — over half the
    keys in a real `field_specs` are null, and printing them would bury the
    content that matters in placeholders.
    """
    kind = spec.get("type") or "?"
    scope = spec.get("scope") or "?"
    lines = [f"### `{name}` — {kind}, escopo {scope}"]

    if spec.get("description"):
        lines.append(str(spec["description"]).strip())

    if spec.get("arity"):
        lines.append(f"Multiplicidade: {spec['arity']}.")
    if spec.get("format"):
        lines.append(f"Formato: {spec['format']}.")

    values = spec.get("values") or []
    if values:
        lines.append("Valores possíveis:")
        for value in values:
            index = value.get("index")
            label = value.get("label") or ""
            desc = (value.get("description") or "").strip()
            prefix = f"- [{index}] {label}" if index is not None else f"- {label}"
            lines.append(f"{prefix}: {desc}" if desc else prefix)

    relations = spec.get("relations") or {}
    if relations:
        lines.append("Relações admitidas:")
        lines.extend(f"- {rel}: {desc}" for rel, desc in relations.items())

    if spec.get("identifies"):
        lines.append(f"IDENTIFIES: {spec['identifies']}.")
    if spec.get("refers_to"):
        lines.append(f"REFERS TO: {spec['refers_to']}.")

    if spec.get("guidelines"):
        # The researcher's coding protocol, verbatim. It is already written as
        # prose with headings and line breaks — JSON escaping used to destroy
        # exactly that shape.
        lines.append("Orientações de codificação:")
        lines.append(str(spec["guidelines"]).strip())

    lines.append("")
    return lines


def _render_navigation_doc(concept_label: str, graph_fields: list[str]) -> list[str]:
    """The edges, with their direction, so a consumer need not guess the topology.

    Without this the document says `aspect` is a taxonomy but never says which
    edge reaches it. Verified through the real MCP path against face85: guessing
    `MAPPED_TO_ASPECT` (a stale type still present in the schema, with zero
    edges) instead of `QUALIFIED_BY` returned **zero rows and no error** — the
    silent failure mode that makes a model keep probing, or worse, conclude the
    data is absent.

    Derived from `TAXONOMY_RELATION_MAP`, the same table the backends use when
    writing, so the document cannot drift from the graph.
    """
    from synesis_graph.backends.neo4j import _get_taxonomy_relation

    lines = [
        "## Como navegar o grafo",
        "",
        "Arestas, com a direção que importa — a seta invertida devolve zero linhas sem erro:",
        "- `(Item)-[:FROM_SOURCE]->(Source)` — de qual referência o trecho veio",
        f"- `(Item)-[:MENTIONS]->({concept_label})` — quais conceitos o trecho menciona",
        f"- `({concept_label})-[:RELATES_TO]->({concept_label})` — relação entre conceitos",
    ]
    # Labels come from `get_taxonomy_labels`, the same function the backends call
    # when declaring the types — deriving them again here would be a second
    # source of truth waiting to disagree.
    for field_name, label in zip(graph_fields, get_taxonomy_labels(graph_fields), strict=True):
        relation = _get_taxonomy_relation(field_name)
        lines.append(f"- `({concept_label})-[:{relation}]->({label})`")

    lines += [
        "",
        "Das referências de um conceito:",
        f"`MATCH (c:{concept_label})<-[:MENTIONS]-(i:Item)-[:FROM_SOURCE]->(s:Source)`",
        "",
        "Nomes de conceito são snake_case e minúsculos; para busca aproximada use "
        "`CONTAINS`, não igualdade.",
        "",
    ]
    return lines


def _render_template_doc(
    template: dict[str, Any], concept_label: str, graph_fields: list[str]
) -> str:
    """The template as a document a model can read straight into a prompt."""
    specs = strip_locations(template.get("field_specs") or {})
    rules = {
        "obrigatórios": template.get("required_fields") or {},
        "opcionais": template.get("optional_fields") or {},
        "proibidos": template.get("forbidden_fields") or {},
    }
    bundles = template.get("bundled_fields") or {}

    lines: list[str] = [f"# Template `{template.get('name') or '(sem nome)'}`", ""]
    lines.append(f"Os conceitos deste projeto são vértices `{concept_label}`.")
    taxonomies = get_taxonomy_labels(graph_fields)
    if taxonomies:
        lines.append("Taxonomias: " + ", ".join(f"`{t}`" for t in taxonomies) + ".")
    lines.append("")

    lines.append("## Regras de preenchimento")
    for scope in ("SOURCE", "ITEM", "ONTOLOGY"):
        parts = [
            f"{kind} {', '.join(f'`{f}`' for f in (by_scope.get(scope) or []))}"
            for kind, by_scope in rules.items()
            if by_scope.get(scope)
        ]
        for bundle in bundles.get(scope) or []:
            parts.append("em conjunto " + ", ".join(f"`{f}`" for f in bundle))
        lines.append(f"- **{scope}**: " + ("; ".join(parts) if parts else "sem restrições"))
    lines.append("")

    lines.extend(_render_navigation_doc(concept_label, graph_fields))

    lines.append("## Campos")
    lines.append("")
    for name, spec in specs.items():
        if isinstance(spec, dict):
            lines.extend(_render_field_doc(name, spec))

    return "\n".join(lines).strip()


def _render_project_summary(
    project: dict[str, Any],
    export_metadata: dict[str, Any],
    counts: dict[str, int],
    generated_at: str,
) -> str:
    """Provenance and shape of the snapshot, as prose rather than a JSON blob."""
    lines = [f"# Projeto `{project.get('name') or '(sem nome)'}`", ""]

    metadata = project.get("metadata") or {}
    if metadata:
        lines.append("## Metadados")
        lines.extend(f"- {key}: {value}" for key, value in metadata.items())
        lines.append("")

    lines.append("## Conteúdo deste grafo")
    lines.append(f"- {counts['source_count']} referências (`Source`)")
    lines.append(f"- {counts['item_count']} trechos analisados (`Item`)")
    lines.append(f"- {counts['concept_count']} conceitos")
    lines.append("")

    lines.append("## Proveniência")
    lines.append(f"- compilado em {export_metadata.get('timestamp') or '(desconhecido)'}")
    lines.append(f"- gravado no grafo em {generated_at}")
    lines.append(
        "Se o projeto mudou desde então, este contexto descreve o snapshot, não o estado atual."
    )
    return "\n".join(lines).strip()


def build_project_context(
    json_data: dict[str, Any],
    concept_label: str,
    graph_fields: list[str],
    synesis_graph_version: str,
    sources: Sequence[Any] = (),
    items: Sequence[Any] = (),
    concepts: Sequence[Any] = (),
) -> ProjectContextSpec:
    """Assembles the project context from the compiler's canonical JSON.

    Nothing is extracted here that the compiler does not already emit: the JSON
    carries `project.description` (the `.synp` DESCRIPTION block), the full
    `template.field_specs` — with each field's GUIDELINES, VALUES, RELATIONS and
    ARITY — the validation rules per scope, and an `export_metadata` block with
    provenance.

    The output is **Markdown**, not JSON. Verified through the real MCP path
    against face85: as JSON the field specs reached the model as ~7.3k tokens
    with 53% null keys; the same content rendered as prose is 27% smaller and
    needs no parsing before it can be read.

    `sources`/`items`/`concepts` are the payload's own rows, used only to count
    what the sync will write — never the compiler's own counters, which count
    different things under the same words (its `item_count` counts SOURCE blocks,
    not the `Item` vertices the sync writes). They default to empty so a caller
    that only wants the template semantics can omit them.
    """
    project = json_data.get("project") or {}
    template = json_data.get("template") or {}
    export_metadata = json_data.get("export_metadata") or {}

    counts = {
        "source_count": len(sources),
        "item_count": len(items),
        "concept_count": len(concepts),
    }
    generated_at = _utc_now_iso()

    return ProjectContextSpec(
        project_name=project.get("name") or "",
        # A project without a DESCRIPTION block yields None here; an empty string
        # keeps the property a string for every consumer.
        description=(project.get("description") or "").strip(),
        concept_label=concept_label,
        template_name=template.get("name") or "",
        template_doc=_render_template_doc(template, concept_label, graph_fields),
        project_summary=_render_project_summary(project, export_metadata, counts, generated_at),
        compiler_version=str(export_metadata.get("compiler_version") or ""),
        synesis_graph_version=synesis_graph_version,
        compiled_at=str(export_metadata.get("timestamp") or ""),
        generated_at=generated_at,
        source_count=counts["source_count"],
        item_count=counts["item_count"],
        concept_count=counts["concept_count"],
    )


def declare_semantic_capability(
    context: ProjectContextSpec | None,
    fields: Sequence[str],
    model: str,
    dimensions: int,
) -> None:
    """Records on the context which fields the graph can be searched by meaning.

    Called from the pipeline rather than from `build_project_context` because the
    payload is assembled before the vectors are known — the sidecar is only
    resolved at sync time, and only for ArcadeDB.

    **Declared, not inferred.** Reading the capability back off the vector index
    would announce semantic search for a graph re-synced without vectors: the
    index survives, the data does not. Only an actual sidecar sets this.

    Writes nothing when any part is missing. A half-declared capability is worse
    than none — a consumer would query by a field composition that never existed.
    """
    if context is None or not fields or not model or dimensions <= 0:
        return
    context.embedding_fields = ",".join(str(f) for f in fields)
    context.embedding_model = str(model)
    context.embedding_dimensions = int(dimensions)

    # Also stated in the prose block, because that is what an MCP client actually
    # reads: `project_summary` reaches the model as text, while the scalar
    # properties are only seen by a client that goes looking for them.
    context.project_summary = (
        f"{context.project_summary}\n\n"
        "## Busca semântica\n"
        f"- conceitos (`{context.concept_label}`) indexados por vetor a partir de: "
        f"{', '.join(f'`{f}`' for f in fields)}\n"
        f"- modelo `{model}`, {dimensions} dimensões\n"
        "- proximidade vetorial é **aproximada**: aproxima por significado, não por "
        "menção literal. Um resultado por proximidade é sugestão de leitura, não "
        "afirmação do pesquisador — diga qual dos dois está apresentando."
    ).strip()


def declare_fulltext_capability(
    context: ProjectContextSpec | None,
    concept_fields: Sequence[str],
    item_fields: Sequence[str],
    source_fields: Sequence[str],
    analyzer: str,
) -> None:
    """Records which full-text indexes this graph carries, and under which analyzer.

    Called from the ArcadeDB backend with the very field lists it passed to
    `CREATE INDEX`, so the declaration cannot drift from what was built. Deriving
    it again here would be a second implementation of the same rule, free to
    disagree with the first.

    **Why a consumer needs the field list, not just a flag.** ArcadeDB's
    `SEARCH_INDEX` takes the index name, and a composite index is named by its
    exact field list — `Concept[search_name, ontology_description]`. A client that
    knows only "there is a full-text index" cannot form the call.

    **Why the analyzer is part of the contract.** `StandardAnalyzer` does no
    stemming and no accent folding; `brazilian` does both. The same query returns
    different results, so a consumer that presents full-text as accent-insensitive
    without checking would be wrong for the default configuration. Saying which
    analyzer is in use lets it be honest instead.

    Writes nothing when there is no index at all — a half-declared capability is
    worse than none.
    """
    if context is None:
        return
    concept = [str(f) for f in concept_fields or []]
    item = [str(f) for f in item_fields or []]
    source = [str(f) for f in source_fields or []]
    if not (concept or item or source):
        return

    context.fulltext_concept_fields = ",".join(concept)
    context.fulltext_item_fields = ",".join(item)
    context.fulltext_source_fields = ",".join(source)
    context.fulltext_analyzer = str(analyzer or "")

    # The prose block is what an MCP client actually reads; the scalar properties
    # are only seen by a consumer that goes looking for them.
    folds = _analyzer_folds_accents(analyzer)
    lines = ["## Busca lexical (full-text)"]
    if concept:
        label = context.concept_label
        lines.append(f"- conceitos (`{label}`): {_as_index(label, concept)}")
    if item:
        lines.append(f"- trechos (`Item`): {_as_index('Item', item)}")
    if source:
        lines.append(f"- referências (`Source`): {_as_index('Source', source)}")
    lines.append(f"- analyzer `{analyzer}`")
    lines.append(
        "- **encontra sem acento e sem diferenciar caixa** (stemming do analyzer)"
        if folds
        else "- este analyzer **não** faz stemming nem dobra acento: `psicologicos` não "
        "encontra `psicológicos`. Para busca insensível a acento, configure um "
        "analyzer de idioma no projeto."
    )
    context.project_summary = f"{context.project_summary}\n\n" + "\n".join(lines)

def _as_index(type_name: str, fields: Sequence[str]) -> str:
    """The index identifier as `SEARCH_INDEX` expects it: `Type[field, field]`."""
    return f"`{type_name}[{', '.join(fields)}]`"


def _analyzer_folds_accents(analyzer: str) -> bool:
    """Does this analyzer stem and fold accents?

    Language analyzers do; `StandardAnalyzer` — the default — does not. The
    distinction decides whether the consumer may present full-text as
    accent-insensitive, so it is answered from the class name rather than assumed.
    """
    name = str(analyzer or "").lower()
    if not name or "standard" in name:
        return False
    return any(
        language in name
        for language in (
            "brazilian",
            "portuguese",
            "english",
            "spanish",
            "french",
            "german",
            "italian",
        )
    )


def declare_metrics_provenance(
    context: ProjectContextSpec | None,
    backend: str,
    scope: str,
    scope_note: str = "",
) -> None:
    """Records which backend computed the network metrics, and over what.

    Called by whoever ran them, with the scope that backend actually uses — not
    inferred here, because only the metrics module knows whether its procedures
    accept a projection filter.

    The note reaches the prose block because that is what an MCP client reads: a
    consumer ranking concepts by PageRank needs to know the score includes edges to
    Items and Sources, and the scalar property alone would not tell it.
    """
    if context is None or not backend or not scope:
        return
    context.metrics_backend = str(backend)
    context.metrics_scope = str(scope)

    lines = [
        "## Métricas de rede",
        f"- calculadas pelo backend `{backend}`, escopo `{scope}`",
    ]
    if scope_note:
        lines.append(f"- {scope_note}")
    lines.append(
        "- centralidade é uma **escolha metodológica**: grau, PageRank e betweenness "
        "respondem perguntas diferentes. Diga qual usou."
    )
    context.project_summary = f"{context.project_summary}\n\n" + "\n".join(lines)


@dataclass
class GraphPayload:
    """Payload prepared for Neo4j synchronization."""

    project_name: str
    concept_label: str  # Dynamic label for concept nodes (CHAIN/CODE field name)
    scalar_fields: list[str]
    graph_fields: list[str]
    chain_fields: list[ChainFieldSpec]
    code_fields: list[CodeFieldSpec]
    # Dynamic properties for Source nodes (SCOPE SOURCE). Specs carry the declared
    # type; plain strings are tolerated for hand-built payloads (see source_field_names).
    source_fields: list[SourceFieldSpec | str]
    value_maps: dict[str, list[dict[str, Any]]]  # Mapping of indices to labels
    concepts: list[dict[str, Any]]
    sources: list[dict[str, Any]]  # Previously "references"
    items: list[dict[str, Any]]
    chains: list[dict[str, Any]]
    mentions: list[dict[str, Any]]
    from_source: list[dict[str, Any]]
    # HTML-only metadata (never sent to the Neo4j sync):
    # item_id -> {field_name: value} for all non-structural ITEM fields from the template.
    item_fields: dict[str, dict[str, str]] = field(default_factory=dict)
    # Taxonomy relations derived from graph_fields, mirroring sync_to_neo4j's _sync_taxonomies.
    # Each entry: {source, target, type} (e.g. concept GROUPED_BY topic, topic IS_LINKED_TO topic).
    taxonomy_edges: list[dict[str, Any]] = field(default_factory=list)
    # Reified identity nodes from IDENTIFIES (multi-project linkage).
    # cypher_label -> [{entity_id, entity, member, source_bibtex}]. Empty for
    # single-project runs and for any project without linkage modifiers.
    entities: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # REFERS_TO edges: cypher_label -> [{entity_id, entity, from_bibtex, member}].
    refers_to_edges: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # The template's raw `field_specs`, kept because `analyze_template` splits
    # fields by destination and drops the declared type: TOPIC and ORDERED both
    # land in `graph_fields`, TEXT and SCALE both in `scalar_fields`. Embedding
    # selection needs the type itself (TEXT/TOPIC embed, closed vocabularies do
    # not), and no other consumer can reconstruct it from the split.
    # Empty for hand-built payloads, which simply have no fields to validate.
    field_specs: dict[str, Any] = field(default_factory=dict)
    # The project's own context (template semantics + .synp DESCRIPTION), written
    # to the graph as a `ProjectContext` vertex so consumers do not have to read
    # the project's source files. Defaults to None: hand-built payloads carry no
    # context, and the backends skip the write when it is absent.
    project_context: ProjectContextSpec | None = None


@dataclass
class PipelineResult:
    """Pipeline result with success or error."""

    success: bool
    error: PipelineError | None = None
    stats: dict[str, int] = field(default_factory=dict)


# ============================================================================
# TEMPLATE ANALYSIS
# ============================================================================


def analyze_template(
    template_data: dict[str, Any],
) -> tuple[
    list[str],
    list[str],
    list[ChainFieldSpec],
    list[CodeFieldSpec],
    dict[str, list[dict]],
    list[SourceFieldSpec],
    str,
    str,
]:
    """
    Analyzes Synesis template to identify scalar, relational, CHAIN, CODE and SOURCE fields.

    Returns:
        Tuple (scalar_fields, graph_fields, chain_fields, code_fields, value_maps,
               source_fields, memo_field_name, quotation_field_name).
        - graph_fields become taxonomy nodes
        - chain_fields define nodes with self-referential relations (triples)
        - code_fields define references to concepts (list of codes)
        - value_maps maps numeric indices to labels (for ORDERED/ENUMERATED)
        - source_fields become dynamic properties on Source nodes; each carries its
          declared type so consumers can tell prose from a closed vocabulary
        - memo_field_name is the ITEM-scoped MEMO field name (e.g. "note", "resumo")
    """
    field_specs = template_data.get("field_specs", {})

    scalar_fields: list[str] = []
    graph_fields: list[str] = []
    chain_fields: list[ChainFieldSpec] = []
    code_fields: list[CodeFieldSpec] = []
    value_maps: dict[str, list[dict]] = {}
    source_fields: list[SourceFieldSpec] = []
    memo_field_name: str = "note"  # default for backwards compatibility
    quotation_field_name: str = "citation"  # default for backwards compatibility

    for field_name, spec in field_specs.items():
        scope = spec.get("scope", "").upper()
        field_type = spec.get("type", "TEXT")

        if scope == "ONTOLOGY":
            if field_type in ("TOPIC", "ENUMERATED", "ORDERED"):
                graph_fields.append(field_name)
                if spec.get("values"):
                    value_maps[field_name] = spec["values"]
            else:
                scalar_fields.append(field_name)

        elif scope == "ITEM":
            if field_type == "CHAIN":
                relations = spec.get("relations", {})
                chain_fields.append(ChainFieldSpec(field_name=field_name, relations=relations))
            elif field_type == "CODE":
                code_fields.append(
                    CodeFieldSpec(field_name=field_name, description=spec.get("description", ""))
                )
            elif field_type == "MEMO":
                memo_field_name = field_name
            elif field_type == "QUOTATION":
                quotation_field_name = field_name

        elif scope == "SOURCE":
            source_fields.append(
                SourceFieldSpec(field_name=field_name, field_type=field_type.upper())
            )

    return (
        scalar_fields,
        graph_fields,
        chain_fields,
        code_fields,
        value_maps,
        source_fields,
        memo_field_name,
        quotation_field_name,
    )


def get_taxonomy_labels(graph_fields: list[str]) -> list[str]:
    """Converts field names to sanitized Neo4j labels."""
    return [sanitize_cypher_label(f.capitalize()) for f in graph_fields]


# ============================================================================
# COMPILATION AND PREPARATION
# ============================================================================


def load_json_project(
    json_path: Path,
    reporter: Any,
) -> GraphPayload | CompilationError:
    """
    Loads a pre-compiled Synesis JSON export (v3.0) and builds a GraphPayload.

    Args:
        json_path: Path to the exported .json file
        reporter: Reporter for visual feedback

    Returns:
        GraphPayload on success, CompilationError on failure.
    """
    try:
        with open(json_path, encoding="utf-8") as f:
            json_data = json.load(f)
    except Exception as e:
        return CompilationError(
            message="Failed to read JSON export",
            stage="load",
            diagnostics=[str(e)],
        )

    version = json_data.get("version", "")
    if not str(version).startswith("3"):
        reporter.warning(f"JSON version '{version}' may not be fully supported (expected 3.x)")

    corpus_count = len(json_data.get("corpus", []))
    reporter.info(f"{corpus_count} corpus items loaded from JSON export")

    (
        scalar_fields,
        graph_fields,
        chain_fields,
        code_fields,
        value_maps,
        source_fields,
        memo_field_name,
        quotation_field_name,
    ) = analyze_template(json_data["template"])

    payload = _build_graph_payload(
        json_data=json_data,
        scalar_fields=scalar_fields,
        graph_fields=graph_fields,
        chain_fields=chain_fields,
        code_fields=code_fields,
        value_maps=value_maps,
        source_fields=source_fields,
        memo_field_name=memo_field_name,
        quotation_field_name=quotation_field_name,
    )

    return payload


def compile_project_to_json(
    project_path: Path,
) -> dict[str, Any] | CompilationError:
    """Compiles a Synesis project and returns its v3.x JSON export as a dict.

    Extracted from compile_project so the multi-project path can obtain each
    member's json_data (needed to read the IDENTIFIES/REFERS TO declarations)
    without duplicating the compile+export dance.
    """
    compiler = SynesisCompiler(project_path)
    result = compiler.compile()

    if not result.success:
        return CompilationError(
            message="Falha na compilação do projeto Synesis",
            stage="compilation",
            diagnostics=[str(d) for d in result.get_diagnostics()],
        )

    # Export to temporary JSON and read back
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        tmp_path = Path(tmp.name)

    result.to_json(tmp_path)

    with open(tmp_path, encoding="utf-8") as f:
        json_data = json.load(f)

    tmp_path.unlink()  # Remove temporary file
    return json_data


def compile_project(
    project_path: Path,
    reporter: Any,
) -> GraphPayload | CompilationError:
    """
    Compiles Synesis project and transforms into payload for Neo4j.

    Args:
        project_path: Path to .synp file
        reporter: Reporter for visual feedback

    Returns:
        GraphPayload on success, CompilationError on failure.
    """
    json_data = compile_project_to_json(project_path)
    if isinstance(json_data, CompilationError):
        return json_data

    corpus_count = len(json_data.get("corpus", []))
    # "coded excerpts" rather than "items": the researcher wrote them one at a
    # time in the .syn file, and that is the unit they recognise.
    reporter.info(f"{corpus_count:,} coded excerpts read".replace(",", "."))

    (
        scalar_fields,
        graph_fields,
        chain_fields,
        code_fields,
        value_maps,
        source_fields,
        memo_field_name,
        quotation_field_name,
    ) = analyze_template(json_data["template"])

    payload = _build_graph_payload(
        json_data=json_data,
        scalar_fields=scalar_fields,
        graph_fields=graph_fields,
        chain_fields=chain_fields,
        code_fields=code_fields,
        value_maps=value_maps,
        source_fields=source_fields,
        memo_field_name=memo_field_name,
        quotation_field_name=quotation_field_name,
        # The root is KNOWN here: the `.synp` sits in it. Passing it beats
        # inferring from the includes/traceability redundancy, which is textual
        # and can fail — and a failed inference now omits the path entirely.
        project_root=str(project_path.parent.resolve()).replace("\\", "/"),
    )

    return payload


def _merge_source_origins_payload(json_data: dict[str, Any]) -> dict[str, Any]:
    """Une as secoes `bibliography` e `dataset` do JSON por bibref (§12.2).

    Retorna a bibliografia inalterada quando nao ha secao `dataset` (no-op para
    projetos sem ON DATASET). Em colisao de mesmo campo, bibliography vence.
    """
    bibliography = json_data.get("bibliography", {}) or {}
    dataset = json_data.get("dataset", {}) or {}
    if not dataset:
        return bibliography
    merged: dict[str, Any] = {}
    for key in set(bibliography) | set(dataset):
        entry: dict[str, Any] = {}
        entry.update(dataset.get(key) or {})
        entry.update(bibliography.get(key) or {})
        merged[key] = entry
    return merged


def _build_graph_payload(
    json_data: dict[str, Any],
    scalar_fields: list[str],
    graph_fields: list[str],
    chain_fields: list[ChainFieldSpec],
    code_fields: list[CodeFieldSpec],
    value_maps: dict[str, list[dict[str, Any]]],
    source_fields: Sequence[SourceFieldSpec | str],
    memo_field_name: str = "note",
    quotation_field_name: str = "citation",
    project_root: str = "",
) -> GraphPayload:
    """Transforms compiled JSON data into structured payload for Neo4j.

    `project_root` is the directory the traceability paths are made relative to.
    Pass it whenever the caller knows it — compiling a `.synp` does, since the
    project file is right there. Inferring it from the redundancy between
    `project.includes[]` and `traceability.file` is the fallback for callers that
    cannot know, and it is textual: it can fail, and a failed inference used to
    leave the absolute path in place.
    """
    project_name = json_data.get("project", {}).get("name", "synesis")
    ontology = json_data.get("ontology", {})
    corpus = json_data.get("corpus", [])
    # Une bibliography + dataset por bibref (§12.2): campos SCOPE SOURCE de origem
    # ON DATASET vivem numa secao `dataset` separada no JSON (para consumidores
    # distinguirem a origem), mas para o sync viram propriedades de no Source
    # exatamente como os de bibliografia. A uniao na fronteira do payload cobre
    # _build_source_props (Neo4j) sem alterar aquela funcao CRITICAL.
    bibliography = _merge_source_origins_payload(json_data)

    # Determine dynamic label based on first CHAIN or CODE field
    if chain_fields:
        concept_label = sanitize_cypher_label(chain_fields[0].field_name.capitalize())
    elif code_fields:
        concept_label = sanitize_cypher_label(code_fields[0].field_name.capitalize())
    else:
        concept_label = "Concept"  # Fallback

    # Build relations map for quick lookup
    relation_definitions: dict[str, str] = {}
    for cf in chain_fields:
        relation_definitions.update(cf.relations or {})

    # Extract CODE / CHAIN field names for corpus search
    code_field_names = [cf.field_name for cf in code_fields]
    chain_field_names = [cf.field_name for cf in chain_fields]

    # SCOPE ONTOLOGY field names (scalar + graph) so the corpus extractor can exclude
    # them from item evidence — the compiler inlines them into each ITEM's data block.
    ontology_field_names = [*scalar_fields, *graph_fields]

    concepts = _extract_concepts(ontology, scalar_fields, graph_fields, value_maps)
    sources, items, mentions, chains, from_source, item_fields = _extract_corpus_data(
        corpus,
        bibliography,
        relation_definitions,
        code_field_names,
        chain_field_names,
        source_fields,
        ontology_field_names,
        memo_field_name,
        quotation_field_name,
        project_root=project_root
        or project_root_from_includes(json_data.get("project") or {}, corpus),
    )

    # Reconcile chain/mention names against canonical ontology names (case-insensitive).
    # Annotators may write "CCS_Support" in a .syn while the .syno has "Ccs_Support" —
    # the compiler preserves both spellings, creating spurious duplicate nodes.
    _canonical_map: dict[str, str] = {
        c["props"]["name"].lower(): c["props"]["name"] for c in concepts
    }
    for ch in chains:
        ch["source"] = _canonical_map.get(ch["source"].lower(), ch["source"])
        ch["target"] = _canonical_map.get(ch["target"].lower(), ch["target"])
    for mn in mentions:
        mn["concept"] = _canonical_map.get(mn["concept"].lower(), mn["concept"])

    taxonomy_edges = _build_taxonomy_edges(concepts, graph_fields, chains)

    return GraphPayload(
        project_name=project_name,
        concept_label=concept_label,
        scalar_fields=scalar_fields,
        graph_fields=graph_fields,
        chain_fields=chain_fields,
        code_fields=code_fields,
        source_fields=source_fields,
        value_maps=value_maps,
        concepts=concepts,
        sources=sources,
        items=items,
        chains=chains,
        mentions=mentions,
        from_source=from_source,
        item_fields=item_fields,
        taxonomy_edges=taxonomy_edges,
        field_specs=json_data.get("template", {}).get("field_specs", {}),
        project_context=build_project_context(
            json_data,
            concept_label=concept_label,
            graph_fields=graph_fields,
            synesis_graph_version=_synesis_graph_version(),
            sources=sources,
            items=items,
            concepts=concepts,
        ),
    )


def _qualify_payload(payload: GraphPayload, alias: str) -> GraphPayload:
    """Namespaces a member's Source bibtex and Item ids by its alias.

    Bibrefs and item ids are local to a member. In the real corpus, linkedin.bib
    and posts.bib share the bibref @thiago-nogueira-60854758 — without this,
    the two Sources would collapse into a single node and the aggregate would
    assert an identity the data never claimed. Identity joins must pass
    exclusively through IDENTIFIES/REFERS TO, never through bibref equality.

    Mutates and returns the payload (it is freshly built per member).
    """

    def q_bib(ref: str) -> str:
        raw = ref if str(ref).startswith("@") else f"@{ref}"
        return f"{alias}:{raw}"

    def q_item(item_id: str) -> str:
        return f"{alias}:{item_id}"

    for s in payload.sources:
        s["bibtex"] = q_bib(s["bibtex"])
    for it in payload.items:
        it["item_id"] = q_item(it["item_id"])
    for fs in payload.from_source:
        fs["item_id"] = q_item(fs["item_id"])
        fs["ref"] = q_bib(fs["ref"])
    for mn in payload.mentions:
        mn["item_id"] = q_item(mn["item_id"])
    payload.item_fields = {q_item(k): v for k, v in payload.item_fields.items()}
    return payload


def merge_payloads(
    members: list[tuple[str, GraphPayload, dict[str, Any]]],
    reporter: Any,
    project_name: str | None = None,
) -> GraphPayload:
    """Merges N member payloads into one aggregate with reified identities.

    Concepts/taxonomies are merged by name: members of one study are expected to
    share the ontology vocabulary (that is what INCLUDE SHARED ONTOLOGY is for),
    so the same concept from two members is the same node.

    Args:
        members: [(alias, payload, json_data)] — json_data carries the template
            whose field_specs hold the IDENTIFIES/REFERS TO declarations.
        reporter: Reporter for visual feedback.
        project_name: Aggregate name; derived from the aliases when omitted.

    Returns:
        A single GraphPayload with entities/refers_to_edges populated.
    """
    from synesis_graph.linkage import edges_as_rows, entities_as_rows, resolve_linkage

    aliases = [alias for alias, _p, _d in members]
    merged_name = project_name or "_".join(aliases)

    base = GraphPayload(
        project_name=merged_name,
        concept_label=members[0][1].concept_label,
        scalar_fields=[],
        graph_fields=[],
        chain_fields=[],
        code_fields=[],
        source_fields=[],
        value_maps={},
        concepts=[],
        sources=[],
        items=[],
        chains=[],
        mentions=[],
        from_source=[],
    )

    seen_concepts: set[str] = set()
    for alias, payload, _data in members:
        _qualify_payload(payload, alias)

        base.sources.extend(payload.sources)
        base.items.extend(payload.items)
        base.from_source.extend(payload.from_source)
        base.mentions.extend(payload.mentions)
        base.chains.extend(payload.chains)
        base.taxonomy_edges.extend(payload.taxonomy_edges)
        base.item_fields.update(payload.item_fields)

        # Concepts merge by name (shared vocabulary across members).
        for c in payload.concepts:
            name = c.get("props", {}).get("name")
            if name and name in seen_concepts:
                continue
            if name:
                seen_concepts.add(name)
            base.concepts.append(c)

        # Field lists are the union across members, preserving order.
        for attr in ("scalar_fields", "graph_fields", "source_fields"):
            existing = getattr(base, attr)
            for f in getattr(payload, attr):
                if f not in existing:
                    existing.append(f)
        for spec in payload.chain_fields:
            if spec not in base.chain_fields:
                base.chain_fields.append(spec)
        for spec in payload.code_fields:
            if spec not in base.code_fields:
                base.code_fields.append(spec)
        for k, v in payload.value_maps.items():
            base.value_maps.setdefault(k, v)

    # Resolve the linkage over the members' JSON exports.
    link = resolve_linkage([{"alias": alias, "json_data": data} for alias, _p, data in members])

    for entity, first, dup in link.duplicate_owners:
        reporter.warning(
            f"Entity '{entity}' is declared with IDENTIFIES by both '{first}' and "
            f"'{dup}'. A label has a single owning corpus; ignoring '{dup}'."
        )

    base.entities = entities_as_rows(link.entities)
    base.refers_to_edges = edges_as_rows(link.edges)

    n_nodes = len(link.entities)
    n_edges = len(link.edges)
    if n_nodes or n_edges:
        entities_seen = ", ".join(sorted(link.owners)) or "none"
        reporter.info(
            f"{n_nodes} identity node(s) and {n_edges} REFERS_TO edge(s) resolved [{entities_seen}]"
        )
    if link.orphans:
        reporter.warning(
            f"{len(link.orphans)} REFERS TO value(s) without a matching IDENTIFIES "
            f"— no node created for them."
        )

    return base


def _index_to_label(value: Any, value_map: list[dict[str, Any]]) -> str:
    """Converts numeric index to label using the value mapping."""
    if isinstance(value, int):
        for entry in value_map:
            if entry.get("index") == value:
                return entry.get("label", str(value))
        return str(value)
    return str(value)


def _build_taxonomy_edges(
    concepts: list[dict[str, Any]],
    graph_fields: list[str],
    chains: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derives taxonomy relations from the ontology, mirroring neo4j.py _sync_taxonomies.

    Produces the SAME relation set that sync_to_neo4j writes, so the HTML can render a
    faithful preview of the Neo4j graph:
      - Concept -> taxonomy value (GROUPED_BY / QUALIFIED_BY / BELONGS_TO / HAS_*)
      - Topic -> Aspect (MAPPED_TO_ASPECT), Topic -> Dimension (MAPPED_TO_DIMENSION)
      - Topic -> Topic (IS_LINKED_TO, with strength) via RELATES_TO between their concepts

    Each edge: {source, target, type[, strength]}. source/target are the raw concept or
    taxonomy-value names (the HTML maps them to node ids). Relation type names are reused
    from neo4j.py's TAXONOMY_RELATION_MAP via a lazy import to avoid a circular dependency.
    """
    # Lazy import: neo4j.py imports from core, so importing at module top would cycle.
    from synesis_graph.backends.neo4j import _get_taxonomy_relation

    edges: list[dict[str, Any]] = []

    def _vals(relations: dict[str, Any], field_name: str) -> list[str]:
        raw = relations.get(field_name)
        if raw is None:
            return []
        seq = raw if isinstance(raw, list) else [raw]
        return [str(v) for v in seq if v is not None]

    # Concept -> taxonomy value
    for field_name in graph_fields:
        rel_type = _get_taxonomy_relation(field_name)
        for row in concepts:
            props = row.get("props", {})
            relations = row.get("relations", {})
            if not isinstance(props, dict) or not isinstance(relations, dict):
                continue
            concept_name = props.get("name")
            if not concept_name:
                continue
            for val in _vals(relations, field_name):
                edges.append({"source": concept_name, "target": val, "type": rel_type})

    # Topic -> Aspect / Topic -> Dimension co-occurrence mappings
    _tax_maps = (("aspect", "MAPPED_TO_ASPECT"), ("dimension", "MAPPED_TO_DIMENSION"))
    for tax_field, rel_type in _tax_maps:
        if "topic" in graph_fields and tax_field in graph_fields:
            seen_pairs: set[tuple[str, str]] = set()
            for row in concepts:
                relations = row.get("relations", {})
                if not isinstance(relations, dict):
                    continue
                topics = _vals(relations, "topic")
                others = _vals(relations, tax_field)
                for t in topics:
                    for o in others:
                        if (t, o) not in seen_pairs:
                            seen_pairs.add((t, o))
                            edges.append({"source": t, "target": o, "type": rel_type})

    # Topic -> Topic (IS_LINKED_TO): strength = count of RELATES_TO between concepts of
    # two distinct topics. Mirrors the Cypher in neo4j.py _sync_taxonomies.
    if "topic" in graph_fields and chains:
        concept_topics: dict[str, list[str]] = {}
        for row in concepts:
            props = row.get("props", {})
            relations = row.get("relations", {})
            if isinstance(props, dict) and isinstance(relations, dict) and props.get("name"):
                concept_topics[props["name"]] = _vals(relations, "topic")

        strength: dict[tuple[str, str], int] = {}
        for ch in chains:
            src_topics = concept_topics.get(ch.get("source", ""), [])
            tgt_topics = concept_topics.get(ch.get("target", ""), [])
            for t1 in src_topics:
                for t2 in tgt_topics:
                    if t1 != t2:
                        strength[(t1, t2)] = strength.get((t1, t2), 0) + 1

        for (t1, t2), s in strength.items():
            edges.append({"source": t1, "target": t2, "type": "IS_LINKED_TO", "strength": s})

    return edges


def _extract_concepts(
    ontology: dict[str, Any],
    scalar_fields: list[str],
    graph_fields: list[str],
    value_maps: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Extracts concepts from ontology with properties and relations."""
    concepts = []

    for name, entry in ontology.items():
        # v3.0: campos aplanados na raiz (sem sub-dict "fields")
        props: dict[str, Any] = {
            "name": name,
            "search_name": humanize_concept_name(name),
            "description": entry.get("description"),
            "created": int(time.time()),
        }

        for sf in scalar_fields:
            # A template field must never clobber a structural property: `name` is the
            # MERGE key and `search_name` is what the full-text index reads.
            if sf in entry and sf not in ("name", "search_name"):
                props[sf] = entry[sf]

        relations: dict[str, list[str]] = {}
        for gf in graph_fields:
            if gf in entry:
                raw_val = entry[gf]
                # Convert value to label if mapping exists
                if gf in value_maps:
                    if isinstance(raw_val, list):
                        relations[gf] = [_index_to_label(v, value_maps[gf]) for v in raw_val]
                    else:
                        relations[gf] = [_index_to_label(raw_val, value_maps[gf])]
                else:
                    # No mapping, use value directly
                    relations[gf] = raw_val if isinstance(raw_val, list) else [raw_val]

        concepts.append({"props": props, "relations": relations})

    return concepts


def _join_field_value(fv: Any) -> str:
    """Serializes an item field value to a display string (lists joined by ', ')."""
    if isinstance(fv, list):
        return ", ".join(str(v).strip() for v in fv if v and str(v).strip() != "None")
    return str(fv).strip()


def _extract_item_extra(data: dict[str, Any], skip: set[str]) -> dict[str, str]:
    """Collects all non-structural ITEM fields as display strings (for HTML evidence)."""
    extra: dict[str, str] = {}
    for fk, fv in data.items():
        if fk in skip or fv is None or fv == "None":
            continue
        sv = _join_field_value(fv)
        if sv and sv != "None":
            extra[fk] = sv
    return extra


def _build_item_row(
    item_id: str,
    citation: str,
    description: str,
    item_extra: dict[str, str],
    source_file: str = "",
    source_line: int | None = None,
    annotation_id: str = "",
) -> dict[str, Any]:
    """Builds the Item node row: structural properties plus the template's ITEM fields.

    The template fields (zone, confidence, score, ...) used to reach the HTML only,
    through the parallel `item_fields` map, leaving the Neo4j node with just three
    properties. That made the preview richer than the graph serving the GraphRAG:
    a rhetorical filter such as "only evidence from Result sections" was
    unexpressible in Cypher even though the value existed in the .syn.

    `item_extra` is already flat (`_extract_item_extra` returns dict[str, str] via
    `_join_field_value`), so no nested map ever reaches the driver — the Neo4j
    restriction that motivated the original detour does not apply here.

    Structural keys always win: a template free to name a field `citation` must not
    overwrite the quotation that the Item node is built around. `source_file` and
    `source_line` join that protected set, for the same reason: they are the audit
    trail back to the `.syn`, and a template field of the same name must not be able
    to rewrite where a quotation came from.

    The traceability pair is omitted when absent rather than written as null. A graph
    exported before this existed and one whose corpus item simply had no location must
    look the same to a consumer — `MATCH (i:Item) WHERE i.source_file IS NOT NULL` is
    the honest way to ask, and writing empty strings would silently break it.

    `annotation_id` is the identity of the annotated BLOCK, shared by every Item the
    block produces. One `ITEM` block with four chains yields four `Item` vertices —
    four analytical units over one excerpt — so `count(i)` and
    `count(DISTINCT i.annotation_id)` answer different questions: how many analytical
    items, and how many annotated excerpts. Both are legitimate; comparing one against
    the other is not, and that is exactly the mistake that made an audit contradict a
    correct answer (23 mentions read against 11 excerpts).

    It joins the protected set for the same reason as the traceability pair: a template
    field of this name must not be able to rewrite which block an excerpt belongs to.
    """
    row: dict[str, Any] = {
        "item_id": item_id,
        "citation": citation,
        "description": description,
    }
    if source_file:
        row["source_file"] = source_file
    if source_line is not None:
        row["source_line"] = source_line
    if annotation_id:
        row["annotation_id"] = annotation_id
    for key, value in item_extra.items():
        if key not in row:
            row[key] = value
    return row


def _extract_corpus_data(
    corpus: list[dict[str, Any]],
    bibliography: dict[str, Any],
    relation_definitions: dict[str, str],
    code_field_names: list[str],
    chain_field_names: list[str],
    source_fields: Sequence[SourceFieldSpec | str],
    ontology_field_names: list[str],
    memo_field_name: str = "note",
    quotation_field_name: str = "citation",
    project_root: str = "",
) -> tuple[
    list[dict[str, Any]],  # sources
    list[dict[str, Any]],  # items
    list[dict[str, Any]],  # mentions
    list[dict[str, Any]],  # chains
    list[dict[str, Any]],  # from_source
    dict[str, dict[str, str]],  # item_fields (HTML-only; item_id -> {field: value})
]:
    """
    Extracts sources, items and relationships from corpus.

    Supports two template patterns:
    - CHAIN: triples (source, relation, target) with per-triple description
    - CODE: list of codes referencing concepts

    The memo_field_name identifies the ITEM-scoped MEMO field (e.g. "note" for
    bibliometrics, "resumo" for causation coding). When the MEMO is a parallel list
    (one entry per chain triple), each triple gets its own description. When the MEMO
    is a single string shared across all triples of a chained sequence, that string is
    used as the description for every triple in the item.

    The quotation_field_name identifies the ITEM-scoped QUOTATION field (e.g. "citation",
    "trecho", "text") that carries the literal excerpt used as the Item's citation property.
    Falls back through common aliases when the template field is absent from item data.
    """
    sources: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    mentions: list[dict[str, Any]] = []
    chains: list[dict[str, Any]] = []
    from_source: list[dict[str, Any]] = []
    item_fields: dict[str, dict[str, str]] = {}
    seen_refs: set[str] = set()

    # Structural fields already materialized as nodes/edges/citation/description.
    # Everything else in an ITEM is template-derived metadata exposed to the HTML
    # evidence view (decision: fields derive exclusively from the template).
    #
    # The compiler inlines SCOPE ONTOLOGY fields (the linker aggregates them onto each
    # mentioned concept) into corpus_item["data"] alongside the SCOPE ITEM fields. Those
    # ontology fields must NOT surface as item evidence — they belong to the ontology view.
    # We skip them by name, derived from the template (scalar_fields + graph_fields), which
    # generalizes to any project instead of the previous hardcoded lattes-specific names.
    _structural_skip = {
        "chain",
        *chain_field_names,
        *code_field_names,
        memo_field_name,
        quotation_field_name,
        *ontology_field_names,
        # common citation aliases
        "text",
        "citation",
        "citação",
    }

    for corpus_item in corpus:
        source_ref = corpus_item["source_ref"].lstrip("@")
        corpus_id = corpus_item["id"]

        # Audit trail back to the `.syn`. The compiler already emits this in the
        # canonical JSON (and in CSV/XLS); the graph was the only export dropping
        # it, which left the chat unable to say where an answer came from.
        traceability = corpus_item.get("traceability") or {}
        item_source_file = relative_source_file(traceability.get("file") or "", project_root)
        item_source_line = traceability.get("line")

        # Extract source (SOURCE...END SOURCE block)
        if source_ref not in seen_refs:
            source_props = _build_source_props(source_ref, corpus_item, bibliography, source_fields)
            sources.append(source_props)
            seen_refs.add(source_ref)

        data = corpus_item["data"]

        # Detect template pattern
        has_chain = "chain" in data and data["chain"]
        has_code = any(cf in data and data[cf] for cf in code_field_names)

        if has_chain:
            chain_list = data.get("chain", [])
            raw_memo = data.get(memo_field_name, [])
            # Parallel list: one note per triple (bibliometrics format).
            # Single string or absent: shared description across all triples (causation format).
            notes: list[str] = raw_memo if isinstance(raw_memo, list) else []
            shared_note: str = raw_memo if isinstance(raw_memo, str) else ""
            base_text: str = (
                data.get(quotation_field_name)
                or data.get("text")
                or data.get("citação")
                or data.get("citation")
                or ""
            )

            # Collect all non-structural ITEM fields (zona, score, criterio, cot, ...).
            # Stored in the parallel item_fields map — never inside the item dict that
            # goes to Neo4j sync (which would reject a nested-map property).
            item_extra = _extract_item_extra(data, _structural_skip)

            # CODE values of a hybrid ITEM (chain + code): the criterion (e.g.
            # conhecimento_ia_real) evaluates the whole ITEM. Each Item node this
            # block produces also MENTIONS the criterion, so the concept is
            # connected to the evidence — without this, a criterion that only ever
            # appears alongside a chain would float unconnected.
            block_codes: list[str] = []
            for cf in code_field_names:
                cv = data.get(cf)
                if not cv:
                    continue
                block_codes.extend(cv if isinstance(cv, list) else [cv])

            for idx, chain in enumerate(chain_list, 1):
                note = notes[idx - 1] if idx - 1 < len(notes) else shared_note
                item_id = f"{corpus_id}_n{idx:04d}"

                items.append(
                    _build_item_row(
                        item_id,
                        base_text,
                        note,
                        item_extra,
                        item_source_file,
                        item_source_line,
                        corpus_id,
                    )
                )
                if item_extra:
                    item_fields[item_id] = dict(item_extra)
                from_source.append({"item_id": item_id, "ref": source_ref})

                # Criterion(s) of this ITEM are mentioned by every Item node it
                # generates (the criterion is a property of the whole block).
                for code in block_codes:
                    code = code.strip() if isinstance(code, str) else code
                    if code:
                        mentions.append({"item_id": item_id, "concept": code, "mention_order": 0})

                # v3.0: chains como {from, relation, to}
                src = chain.get("from", "").strip()
                rel = chain.get("relation", "").strip()
                tgt = chain.get("to", "").strip()
                if src and tgt:
                    mentions.append({"item_id": item_id, "concept": src, "mention_order": 1})
                    mentions.append({"item_id": item_id, "concept": tgt, "mention_order": 2})

                    # Normalize relation type and lookup description
                    rel_type = rel.upper().replace(" ", "_").replace("-", "_")
                    rel_description = relation_definitions.get(rel, "")

                    chains.append(
                        {
                            "source": src,
                            "target": tgt,
                            "type": rel_type,
                            "description": rel_description,
                            "item_id": item_id,
                        }
                    )

        elif has_code:
            # CODE pattern (gestao_fe): code field bundles
            # Find the first CODE field with data
            code_field = next((cf for cf in code_field_names if cf in data and data[cf]), None)
            if not code_field:
                continue

            code_list = data[code_field]
            if not isinstance(code_list, list):
                code_list = [code_list]

            # Extract descriptions if available (corresponding bundled field)
            descriptions = data.get("justificativa_interna", []) or data.get("descricao", [])
            if not isinstance(descriptions, list):
                descriptions = [descriptions] * len(code_list)

            # Extract base text — template QUOTATION field first, then common aliases
            base_text = ""
            for field_name in [quotation_field_name, "ordem_1a", "text", "citation"]:
                if field_name in data and data[field_name]:
                    val = data[field_name]
                    base_text = val[0] if isinstance(val, list) else val
                    break

            # Non-structural ITEM fields shared across this item's codes.
            item_extra = _extract_item_extra(data, _structural_skip)

            for idx, code in enumerate(code_list, 1):
                item_id = f"{corpus_id}_c{idx:04d}"
                description = descriptions[idx - 1] if idx <= len(descriptions) else ""

                items.append(
                    _build_item_row(
                        item_id,
                        base_text,
                        description,
                        item_extra,
                        item_source_file,
                        item_source_line,
                        corpus_id,
                    )
                )
                if item_extra:
                    item_fields[item_id] = dict(item_extra)
                from_source.append({"item_id": item_id, "ref": source_ref})
                mentions.append({"item_id": item_id, "concept": code, "mention_order": 1})

    return sources, items, mentions, chains, from_source, item_fields


def _build_source_props(
    source_ref: str,
    item: dict[str, Any],
    bibliography: dict[str, Any],
    source_fields: Sequence[SourceFieldSpec | str],
) -> dict[str, Any]:
    """Builds properties of a Source node (SOURCE...END SOURCE block).

    v3.0: source_metadata foi removido do corpus. Todos os campos de fonte
    (bibliograficos e sintetizados) estao em bibliography[source_ref].

    As chaves de bibliography vem normalizadas em minusculas pelo bib_loader,
    mas source_ref preserva a caixa do bloco SOURCE (ex. Vitor_Mourao_...). Um
    get() exato falharia e perderia TODOS os campos SCOPE SOURCE do node — por
    isso a busca tolera caixa (mesma correcao do casamento de linkagem).
    """
    bib_entry = bibliography.get(source_ref) or bibliography.get(source_ref.lower()) or {}

    props: dict[str, Any] = {"bibtex": source_ref}

    # Standard bibliographic fields
    for key in ("title", "author", "year", "doi", "journal", "abstract"):
        val = bib_entry.get(key)
        if val is not None:
            props[key] = val

    # Dynamic fields from template (SCOPE SOURCE) - agora em bibliography
    for field_name in source_field_names(source_fields):
        if field_name in bib_entry and bib_entry[field_name] is not None:
            props[field_name] = bib_entry[field_name]

    return props
