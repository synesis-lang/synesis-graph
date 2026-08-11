# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Language:** [English](CHANGELOG.md) | [Português](CHANGELOG.pt.md)

**Documentation:** [Synesis Language Docs](https://synesis-lang.github.io/synesis-docs)

---

## [0.5.0] - 2026-08-11

### Removed — GraphQLite backend

- **The `graphqlite` backend was removed.** It never worked and will not be
  implemented. Keeping it meant advertising a third export target in `--help`,
  in the config file, and in the public API that silently failed the moment
  anyone selected it — worse than not offering it at all.
- Removed: the `graphqlite` subcommand, `GraphQLiteBackendAdapter`,
  `GraphQLiteConfig`, `sync_to_graphqlite`, `compute_metrics_graphqlite`,
  `_GraphQLiteQueryRunner`, `get_graphqlite_connect_factory`,
  `_resolve_graphqlite_db_path`, the `[graphqlite]` config section, the
  `graphqlite` optional dependency, and `GraphQLite_Reference.md`.
- **Public API contract changed:** `SUPPORTED_BACKENDS` is now
  `("neo4j", "html")` and `BACKEND_GRAPHQLITE` no longer exists. Guarded by
  `tests/test_public_api.py`. Since the package has never been published to
  PyPI, no released version is affected.
- Support for further graph databases remains on the roadmap — Google Vertex
  is a candidate, not a commitment. Any new backend implements the existing
  `BackendAdapter` contract, which stays untouched.
- Test suite: 257 → 245 (the 12 removed exercised GraphQLite only). The
  cross-backend consistency test now compares Neo4j against HTML, preserving
  its original intent.

### Security

- **All GitHub Actions are now pinned by commit SHA** (`.github/workflows/ci.yml`).
  Twelve `uses:` entries referenced mutable tags (`@v4`, `@v5`), so a compromised
  or retagged release would run in CI without any change to this repository.
  `synesis` and `synesis-lsp` already pinned by SHA; this brings the third repo
  in line. Each SHA was verified against the GitHub API before being applied.

- **New `security` CI job**, matching the other Python packages — the only one
  of the four that lacked it:
  - `pip-audit` over the runtime dependencies declared in `pyproject.toml`
    (`synesis`, `click`). Verified locally: no known vulnerabilities.
  - Gitleaks secret scan over the full history (`fetch-depth: 0`).
  - The audit runner is pinned to Python 3.11 because the step reads
    `pyproject.toml` with `tomllib` (stdlib only from 3.11). This is the runner
    version, not the supported floor: `requires-python = ">=3.10"` still holds
    and the test matrix keeps covering 3.10.

- **`graphs/Davi.db` removed from version control** — a 164 KB GraphQLite
  database, an artifact of the backend removed in this release. `graphs/` was
  already in `.gitignore`, but the file predated the rule and `.gitignore` does
  not untrack what is already tracked. Removed with `git rm --cached`; the local
  file is untouched.

### Documentation

- **Project identity corrected across the documentation.** Both READMEs were
  still titled *"Synesis to Neo4j: Universal Graph Pipeline"* and described the
  repository as "the ingestion pipeline to **Neo4j**". Neo4j is one backend
  among others — the title is now `synesis-graph`, the intro states the two
  shipping backends (Neo4j and HTML) over the shared `BackendAdapter` contract,
  and the badge reads `Backends: Neo4j | HTML` instead of `Neo4j: Graph DB`.
- **Usage section pointed at a file that does not exist** — both READMEs
  instructed `python synesis2neo4j.py --project ...`. That name predates two
  renames; the shim is `synesis2graph.py` and the supported entry point is the
  `synesis-graph` CLI. Replaced with real, verified commands for both backends.
- **`CITATION.cff` was citing the wrong work** — `title` read *"Synesis: A DSL
  compiler for knowledge engineering"*, the compiler's title, so anyone citing
  synesis-graph would credit the wrong package. Now titled after this package,
  with the abstract describing both backends instead of Neo4j alone. (The same
  copied title is present in `synesis-lsp` and `synesis-coder` — worth fixing
  there too.)
- **Compatibility matrix corrected in both READMEs.** Every row was stale:
  `synesis 0.5.5` (now 0.11.0), `synesis-coder 0.4.1` (0.8.0), `synesis-lsp
  0.15.4` (0.22.0), `synesis-graph 0.2.0` (0.5.0), and the `synesis>=`
  constraint listed as `0.5.5` when all three consumers require `>=0.10.0`.
- **`README.pt.md` install section fixed** — it required *Python 3.11+* against
  a package that declares `>=3.10`, cloned `synesis2neo4j` (the repository's
  former name), and installed dependencies (`rich`, `tomli`) that are not the
  package's. It also had no compatibility matrix; one was added, mirroring the
  English README.

### License — MIT → AGPL-3.0-only + Synesis Data-Output Exception

- Follows the compiler migration (`synesis` 0.10.0) — synesis-graph imports
  `SynesisCompiler` in-process (`core.py`), which triggers AGPL copyleft.
  Full study: `synesis-planning/synesis/new_licence_policy.md`.
  - `LICENSE` (full AGPL-3.0 text) and `LICENSE.exception` replicated from
    the core. The exception matters specifically here: the generated HTML
    graph visualization embeds Synesis's own JavaScript/CSS (*Synesis
    Runtime Material*) and remains freely licensable under the exception.
  - `pyproject.toml`: legacy `license = {text = "MIT"}` replaced with
    `license = "AGPL-3.0-only AND LicenseRef-Synesis-data-output-exception"`
    (PEP 639 string form — the legacy table syntax builds without error but
    silently emits the obsolete `License:` metadata field instead of
    `License-Expression:`) + `license-files = ["LICENSE", "LICENSE.exception"]`;
    `setuptools>=77`.
  - `CITATION.cff`, `README.md`/`README.pt.md`, and the license badge
    updated in both languages.
  - Releases published before this change (≤ 0.3.x) remain available under
    MIT.

### Added

- **Dataset TOML fields (`ON DATASET`) represented in the graph** — `synesis`
  0.10.0 introduces a `dataset` JSON section, separate from `bibliography`,
  holding SCOPE SOURCE field values resolved from `ON DATASET`. This release
  makes those values reach the Neo4j graph as Source node
  properties and as reified entities in multi-project linkage, without
  touching `_build_source_props` (the shared, critical function both backends
  depend on).
  - `linkage.py`: new `_merge_source_origins(data)` unions `bibliography` and
    `dataset` by bibref before `resolve_linkage` resolves `IDENTIFIES`/
    `REFERS TO` — cross-project edges over `ON DATASET` fields now work.
  - `core.py`: new `_merge_source_origins_payload(json_data)` applied at the
    `_build_graph_payload` boundary — `bibliography` becomes the union, so
    `_extract_corpus_data → _build_source_props` picks up dataset-origin
    fields as node properties. No-op for
    projects without a `dataset` section. On field-name collision,
    `bibliography` wins (historical precedence).

### Testing

- 4 new tests in `test_linkage.py` covering the union in both linkage and
  payload paths, no-op behavior, and collision precedence. Full suite after the
  GraphQLite removal: 245 passed, 1 skipped (case-study fixture not present).

### Fixed — CI (found during the pending test pass, 2026-08-04)

- **CI reported green with zero tests executed.** The test step tolerated
  pytest exit code 5 (`no tests collected`) as success:

  ```
  python -c "... sys.exit(0 if code == 5 else code)"   # before
  pytest --cov=synesis_graph --cov=synesis2graph ...   # now
  ```

  Not hypothetical: the same pattern masked a real breakage in `synesis-lsp`,
  where an outdated `jsonschema` made `test_contract.py` fail at import,
  aborting collection of the whole suite while CI stayed green. Fixed across
  `synesis`, `synesis-lsp`, `synesis-graph` and `synesis-coder`.

- **`ruff check synesis_graph/` failed** on a 103-character line in `core.py`
  (limit 100). Ruff is blocking in CI, so this alone broke the pipeline.
  Suite re-verified after the fix: 257 passed, 1 skipped.

---

## [0.3.1] - 2026-07-15

### Fixed

- **Critério (CODE) em ITEM híbrido (chain + code) ficava desconectado do grafo** (`synesis_graph/core.py`, `_extract_corpus_data`)
  - O extrator tratava cada ITEM como CHAIN-pattern **ou** CODE-pattern (`if has_chain: ... elif has_code:`). Num ITEM que declara **os dois** — comum no corpus real, onde o artigo tem tanto o critério de avaliação (`criterio: conhecimento_ia_real`, com `score`) quanto o grafo de conceitos técnicos (`chain`) — só as chains eram processadas; o critério virava propriedade e **nunca gerava `MENTIONS`**. Um conceito que só aparecesse ao lado de chains (como `conhecimento_ia_real`) ficava flutuando, ligado apenas à sua categoria (`Topic`), sem nenhuma evidência de Item.
  - Agora, no ramo de chain, cada Item node gerado pelo bloco também menciona o(s) critério(s) do ITEM (o critério avalia o bloco inteiro). Verificado no banco real: `conhecimento_ia_real` passou de `MENTIONS=0` para `MENTIONS=8`, e o grafo `quinto-andar` deixou de ter qualquer conceito sem evidência (0 nós isolados, 0 nós de grau 1, 1 único componente conexo). ITEMs só-chain e só-code permanecem inalterados.
- **Campos `SCOPE SOURCE` sumiam do nó `Source` quando o bibref tinha letra maiúscula** (`synesis_graph/core.py`, `_build_source_props`)
  - `_build_source_props` buscava a entrada com `bibliography.get(source_ref)`, mas `source_ref` preserva a caixa do bloco SOURCE (ex. `Vitor_Mourao_Hanriot_...`) enquanto as chaves de `bibliography` vêm normalizadas em minúsculas pelo `bib_loader`. O `get()` exato falhava e retornava `{}`, então o nó `Source` era gravado **sem nenhum** campo `SCOPE SOURCE` (no corpus real do Quinto_Andar, o Source do currículo perdeu `lattes_id`, `nome` e `cargo_institucional`). Os projetos com bibrefs já minúsculos (abstracts) não eram afetados.
  - A busca agora tolera caixa (`bibliography.get(source_ref) or bibliography.get(source_ref.lower())`). Verificado no banco real: o Source do lattes voltou a carregar todos os campos declarados.
- **Aresta `IDENTIFIED_AS` não era criada quando o bibref do dono tinha letra maiúscula** (`synesis_graph/linkage.py`)
  - A reificação ligava o `Source` dono ao nó de identidade (`(:Source)-[:IDENTIFIED_AS]->(:Researcher)`) por um `MATCH` case-sensitive em `bibtex`. Mas o `source_bibtex` do nó reificado era montado a partir das **chaves de `bibliography`** (normalizadas em minúsculas pelo `bib_loader`), enquanto o nó `Source` grava o `bibtex` na **caixa original** do bloco SOURCE (`corpus[].source_ref`). Com um bibref de nome próprio (ex. `@Vitor_Mourao_Hanriot_...`), as duas formas divergiam só na caixa e a aresta nunca era criada — silenciosamente. As arestas `REFERS_TO` escapavam por coincidência (bibrefs já minúsculos no `.bib` e no SOURCE).
  - `resolve_linkage` agora ancora os bibrefs em `corpus[].source_ref` (a mesma fonte que o nó `Source` usa), resolvendo os valores dos campos via `bibliography` com tolerância de caixa. `Researcher.source_bibtex` passa a casar exatamente com `Source.bibtex`. Verificado no banco real `quinto-andar`: `IDENTIFIED_AS` = 1 (era 0), dono `lattes:@Vitor_Mourao_Hanriot_...` → `Researcher`, 7 arestas `REFERS_TO` intactas.
- **`--database` agora nomeia o grafo unificado de projetos linkados** (`synesis_graph/pipeline.py`, `synesis_graph/backends/base.py` via `payload.project_name`)
  - `--database` era aceito mas efetivamente ignorado: era gravado em `config.database` (que nada lê) enquanto o nome do banco Neo4j vinha sempre de `payload.project_name`. No caminho multiprojeto, isso deixava o agregado com o nome derivado dos membros (`lattes_abstracts`), sem como renomeá-lo.
  - Agora, quando `--database` é passado, ele sobrescreve `payload.project_name` logo após a compilação/linkagem — o que passa a nomear o banco Neo4j (sanitizado) e o título do grafo HTML. É a forma pretendida de nomear o grafo unificado de vários projetos: `synesis-graph neo4j --project lattes.synp --project abstracts.synp --database Quinto_Andar` grava no banco `quinto-andar`. Sem o flag, o nome continua derivado (nome do PROJECT para um único `.synp`, ou os membros unidos por `_` para uma linkagem). Verificado no corpus real Quinto_Andar (7 arestas resolvidas, `Target database: quinto-andar`).

---

## [0.3.0] - 2026-07-15

### Added

- **Identity reification across projects — `IDENTIFIES` / `REFERS TO`** (`synesis_graph/linkage.py`, `synesis_graph/core.py`, `synesis_graph/backends/neo4j.py`, `synesis_graph/cli.py`, `synesis_graph/pipeline.py`)
  - `--project` is now repeatable: `synesis-graph neo4j --project lattes.synp --project abstracts.synp` compiles each member in isolation and materializes the identities they declare. Each member stays an independent compilation unit — the aggregate exists only in this command, never in the LSP.
  - **Reified nodes:** one node per distinct value of a field declared `IDENTIFIES <entity>`, labelled from the entity name (`researcher` → `:Researcher {entity_id}`), plus a `(:Source)-[:IDENTIFIED_AS]->(:Entity)` edge to its owning Source. The node is born **only** from `IDENTIFIES` — an orphan `REFERS TO` creates no stub node, so a typo cannot invent an entity.
  - **Edges:** `(:Source)-[:REFERS_TO {entity, member}]->(:Entity)` for each `REFERS TO` value matched against the primary key. The entity name rides as a property rather than becoming part of the relationship type, so one type serves every entity. Many edges may target one node (n:1); a multi-valued field yields one edge per value (n:n).
  - **Matching is exact** after trimming — no case-folding, no silent normalization, no fuzzy. A value differing only in case stays an orphan rather than being merged into an entity the source considers distinct.
  - **Bibrefs and item ids are namespaced by member alias** (`abstracts:@artigo_a`). Two corpora sharing a bibref — as `linkedin.bib` and `posts.bib` do in the real corpus — would otherwise collapse into one node, asserting an identity the data never claimed. Identity joins pass exclusively through `IDENTIFIES`/`REFERS TO`.
  - Uniqueness constraint `REQUIRE e.entity_id IS UNIQUE` created per entity label. Entity labels are validated before interpolation into Cypher (they originate from user templates and cannot be parameterized by the driver).
  - `GraphPayload` gains `entities` and `refers_to_edges` (both defaulting to empty): a project without linkage modifiers reifies nothing and syncs exactly as before.
  - The `html` backend rejects multiple `--project` with a clear error: its view is a **concept** graph, and rendering identity nodes there requires a layer design that is not yet decided.

---

## [0.2.5] - 2026-06-16

### Added

- **HTML: "Informações / Filtros" sidebar tab bar** (`templates/graph.html.tmpl`)
  - New second tab row below the search bar splits the sidebar into two panels: **Informações** (info panel — default) and **Filtros** (degree filter + legend). This frees the full sidebar height for the info/evidence panel when browsing nodes, while filters remain one click away.
  - `setSidebarTab('info'|'filters')` toggles visibility between `#info-panel` and the new `#filters-tab` container; `.active` state tracks the selected tab.
  - Degree accordion, legend accordion, and grouping tabs moved inside `#filters-tab` — DOM unchanged so all JS initialization (slider, renderLegend, toggleAccordion) works without modification.

### Removed

- **HTML: "Compact mode" HUD button** (`templates/graph.html.tmpl`)
  - `#btn-compact`, `toggleCompact()`, and the `body.compact` CSS rule removed. Superseded by the Filtros tab, which provides a cleaner and more permanent solution to the vertical space problem.

---

## [0.2.4] - 2026-06-16

### Added

- **HTML: "Compact mode" HUD button** (`templates/graph.html.tmpl`)
  - New `#btn-compact` button in the HUD toggles `body.compact`, which hides the degree filter (`#degree-accordion`) and the legend (`#legend-wrap`), giving the info panel — and the evidence table — all the freed vertical space.
  - `toggleCompact()` mirrors the `toggleLock()` pattern: pure class toggle, `.active` state on the button. No backend or data-contract change.

---

## [0.2.3] - 2026-06-16

### Added

- **HTML: dynamic evidence table with project-specific extra columns** (`synesis_graph/backends/html.py`, `templates/graph.html.tmpl`)
  - `EV_ITEM_FIELDS` JS constant injected from backend: ordered list of extra item field names (e.g. `zona`, `criterio_5a`, `score_sugerido`, `area_tematica`, `metodo`) that appear as additional columns in the evidence table. Adapts automatically to each project's field schema.
  - `SOURCE_PROPS` JS constant injected from backend: map of `ref → SOURCE block properties` (nome, lattes_id, etc.); used in `showInfo()` to display source metadata in the info panel.
  - `anchor` and `analysis` fields parsed from `ChainNode(...)` repr strings via `_parse_note_fields()` and rendered as sub-rows inside the annotation cell (no extra column needed).
  - Source labels in evidence records now show human-readable `nome`/`title`/`author` instead of raw ref keys; raw ref stored in `_src_ref` for internal edge matching.

- **HTML: list-valued item fields now reach the evidence table** (`synesis_graph/core.py`)
  - Fields like `area_tematica` and `metodo` (stored as lists in the corpus) were previously discarded. Now joined with `", "` and included as evidence record fields.
  - `criterio_5a` and `score_sugerido` removed from `_skip` set; analytical fields now flow through to the HTML.

- **HTML: info panel restructured into meta + table zones** (`templates/graph.html.tmpl`)
  - `#info-panel` is now a flex column with two children: `#info-meta` (title and metadata fields, shrinks to content, `overflow-y: auto`) and `#info-table` (evidence table only, `flex: 1`, `overflow-x: scroll` always visible at bottom of panel).
  - Horizontal scrollbar for the evidence table is now permanently visible regardless of vertical scroll position — previously it was buried at the end of the table content.
  - `_setInfoTable(html)` JS helper writes to `#info-table`; `showInfo`, `showEdgeInfo`, click-to-deselect, and mode-reset all call `_setInfoTable('')` to clear the table zone when not in evidence mode.

### Changed

- **HTML: space-efficient info panel layout** (`templates/graph.html.tmpl`)
  - Accordion sections for degree/legend (`toggleAccordion()`): collapsed by default, expand on click with smooth `max-height` transition.
  - Long node descriptions (>120 chars) are clamped to 3 lines with a "ver mais / ver menos" toggle (`toggleDesc()`).
  - Short metadata fields rendered in a two-column grid (`.field-grid`) to reduce vertical space.
  - Dense evidence table: `table-layout: auto` with `min-width` per column class; `anchor`/`analysis` as sub-rows inside the annotation cell instead of separate columns.
  - Footer (`#stats`) constrained to a single line with `white-space: nowrap; text-overflow: ellipsis` — version info on the same line as graph stats.

- **HTML: mouse-wheel zoom speed reduced** (`templates/graph.html.tmpl`)
  - `interaction.zoomSpeed` set to `0.3` (down from default `1.0`) for more controlled zooming, matching trackpad feel.

---

## [0.2.2] - 2026-06-16

### Fixed

- **Neo4j: RELATES_TO edge loss when same concept pair has multiple relation types** (`synesis_graph/backends/neo4j.py`)
  - `MERGE (s)-[r:RELATES_TO]->(t)` without `type` in the key caused the second MERGE to overwrite the first when two chains between the same pair had different types (e.g. `APPLICATION` and `METHODOLOGICAL`).
  - Fix: changed MERGE key to `MERGE (s)-[r:RELATES_TO {type: row.type}]->(t)` so each type produces an independent edge.
  - Result: GDS projection now counts all distinct typed edges between concept pairs (e.g. 23 → 25 relationships in the Lattes corpus).

- **HTML: permanent "Loading graph…" overlay when all concepts are filtered out** (`templates/graph.html.tmpl`)
  - `stabilizationIterationsDone` never fires when `RAW_NODES = []`; the loading overlay remained forever.
  - Fix: added an explicit guard — when `RAW_NODES.length === 0` the overlay is removed immediately without waiting for the network event.

- **HTML: RAW_NODES and EV_SOURCE_NODES used inconsistent field schemas** (`synesis_graph/backends/html.py`)
  - RAW_NODES emitted bare field names (`community`, `degree`, `extra`) while EV_SOURCE_NODES used underscore-prefixed names (`_community`, `_degree`, `_extra`), requiring JS-side remapping only for RAW_NODES.
  - Fix: unified both node types to use underscore-prefixed fields (`_community`, `_community_name`, `_source_file`, `_file_type`, `_degree`, `_extra`). JS DataSet initialization simplified to `{ ...n, _onto: true }` spread.

- **HTML: mode switching leaked `hidden` state across node sets** (`templates/graph.html.tmpl`)
  - `setMode('ONTOLOGY')` restored hidden nodes with `filter: n => !!n.hidden`, which incorrectly re-showed EV_SOURCE_NODES (always hidden in ONTOLOGY mode).
  - Fix: introduced `_onto: bool` flag as a mode identifier orthogonal to the `hidden` field. `setMode` and `switchGrouping` now filter by `_onto` exclusively.

- **HTML: `switchGrouping()` recolored evidence nodes** (`templates/graph.html.tmpl`)
  - `nodesDS.getIds()` included all nodes (ontology + evidence); community color updates were applied to EV_SOURCE_NODES.
  - Fix: `switchGrouping` now filters `nodesDS.get({ filter: n => !!n._onto })` before updating colors.

- **HTML: search queried wrong pool in EVIDENCE mode** (`templates/graph.html.tmpl`)
  - In EVIDENCE mode, search was querying `RAW_NODES` (ontology nodes, possibly hidden).
  - Fix: search now uses `nodesDS.get({ filter: n => !n._onto && !n.hidden })` when in EVIDENCE mode.

- **`_load_html_config` hardcoded old filter defaults as TOML fallbacks** (`synesis_graph/config.py`)
  - When keys were absent from `[html]` section, `_load_html_config` fell back to `min_frequency=3`, `min_source_count=2`, `max_nodes=200`, `include_isolated=False` instead of reading from `HTMLConfig()` defaults.
  - Fix: fallback values now derive from `HTMLConfig()` so dataclass defaults are the single source of truth.

### Changed

- **`HTMLConfig` defaults changed to show all data by default** (`synesis_graph/config.py`)
  - `min_frequency`: 3 → 0 (no frequency filter)
  - `min_source_count`: 2 → 0 (no source-count filter)
  - `max_nodes`: 200 → 0 (unlimited)
  - `include_isolated`: `False` → `True`
  - Rationale: filters are analysis tools for the user to apply interactively; the graph should show all available data on first load.

- **HTML palette replaced with Synesis cheatsheet colors** (`synesis_graph/backends/html.py`, `templates/graph.html.tmpl`)
  - Old Tableau-10 palette (`#4E79A7`, `#F28E2B`, etc.) replaced with the cheatsheet palette: navy `#1A3A5C`, slate `#3D5A7A`, sage `#4A6741`, terracotta `#8B4A3C`, gold `#A8905A`, amber `#C8963A`.
  - `_HTML_RELATION_COLORS` extended with Synesis-specific types: `ASSOCIATION=#A8905A`, `APPLICATION=#8B4A3C`, `METHODOLOGICAL=#3D5A7A`.
  - `RELATION_COLORS` JS object in template updated to match.

- **HTML light mode is now the default; dark mode is opt-in** (`templates/graph.html.tmpl`)
  - CSS `:root` now holds light-mode variables (paper background `#F7F4EF`, ink text `#1C1C1E`, navy accent `#1A3A5C`).
  - `body.dark` class activates the dark theme. `body.light` does not exist — light is the baseline.
  - `_isDark` flag starts `false`; theme toggle button initializes with 🌙 (offering dark mode).
  - All previously hardcoded dark hex values in CSS replaced with `var(--bg)`, `var(--track)`, `var(--accent)`, `var(--muted)`, etc.
  - PNG export uses `_isDark ? '#0f0f1a' : '#F7F4EF'` for background fill.
  - Node font color initialized to `'#1C1C1E'` (ink); toggled to `'#e0e0e0'` in dark mode.

### Added

- **Comprehensive HTML test battery** (`tests/test_html_v2.py` — 56 new tests in 12 classes)
  - `TestUnifiedNodeSchema`: RAW_NODES and EV_SOURCE_NODES field naming consistency, no legacy bare-name fields, slug format, mutual exclusion of IDs between node sets.
  - `TestOntoFlag`: `_onto: true/false` injection in DataSet init, `setMode` and `switchGrouping` filter by `_onto`.
  - `TestLightModeDefault`: `:root` paper background, `body.dark` class presence, `_isDark` flag, 🌙 initial button, ink node font, `exportPNG` conditional fill.
  - `TestCheatsheetPalette`: all 6 cheatsheet colors present, Tableau colors absent from `RELATION_COLORS`, Synesis-specific types covered, node colors drawn from palette.
  - `TestHTMLConfigDefaults`: all four new open defaults; single-source corpus shows all concepts; strict filters still work when explicitly set.
  - `TestEmptyRawNodesGuard`: loading guard present, `stabilizationIterationsDone` used when non-empty, evidence edges still populated when ontology is empty.
  - `TestEvidenceDedup`: same item+type counted once per concept; different types on same pair produce separate records.
  - `TestEvMentionEdges`: required fields, type-to-color accuracy, edge count matches payload chains, RAW_EDGES collapses parallel chains.
  - `TestAllGroupings`: required keys, legend colors from cheatsheet palette, empty-payload resilience.
  - `TestStatsText`: stats div present, "hidden by filter" shown only when filtering is active.
  - `TestPlaceholderCompleteness`: all 10 template placeholders replaced, including on empty payload.
  - `TestDegreeSliderCSS`: slider CSS uses `var(--track)`, `var(--accent)`, `var(--muted)` — no hardcoded hex values.

---

## [0.2.1] - 2026-06-12

### Added

- **Verbosity flags `-v`/`-q` on `synesis-graph` CLI** (`synesis_graph/cli.py`, `synesis2graph.py`)
  - `-v` / `--verbose` (count): raises log level on `synesis2graph` logger to DEBUG. Repeatable.
  - `-q` / `--quiet` (count): lowers to WARNING (`-q`) or ERROR (`-qq`). Repeatable.
  - Implemented via `_configure_logging(verbose, quiet)` in `synesis_graph/cli.py`; shim delegates to it.
  - `Global Options:` section added to both `cli.py` and `synesis2graph.py` help output.
  - Removed hardcoded `logger.setLevel(logging.INFO)` from `synesis2graph.py` — level now controlled by CLI flag.

### Changed

- **`synesis2graph.py` refactored into a thin shim** (Phase 6)
  - Reduced from 3 165 lines to ~480 lines. All implementation extracted into `synesis_graph/` subpackage.
  - New modules: `synesis_graph/sanitize.py`, `synesis_graph/ui.py`, `synesis_graph/core.py`, `synesis_graph/config.py`, `synesis_graph/metrics.py`, `synesis_graph/pipeline.py`, `synesis_graph/backends/neo4j.py`, `synesis_graph/backends/base.py`, `synesis_graph/backends/html.py`.
  - `synesis2graph.py` now re-exports all public names from the submodules; `python synesis2graph.py --help/--version` and `from synesis2graph import run_pipeline` continue to work unchanged.
  - `synesis_graph/__init__.py` updated to import from submodules directly (eliminates a circular-import chain that arose from `synesis2graph` ↔ `synesis_graph` cross-imports).
  - `synesis_graph/cli.py` updated to import `TaskReporter` and `run_pipeline` from submodules instead of from the shim.
  - `tests/conftest.py` updated: `_purge_modules()` now removes all `synesis_graph.*` submodules from `sys.modules` on fixture setup/teardown, preventing cross-test contamination.
  - `tests/test_phase7_multidb.py` monkeypatches updated to target `synesis_graph.backends.base` and `synesis_graph.pipeline` (where implementations now live).
  - `synesis_graph/backends/base.py` fixed: missing `sanitize_database_name` import added.
  - Helper scripts `_analyze_structure.py`, `_remove_core_defs.py`, `_extract_modules.py`, `_create_modules.py`, `_create_modules2.py` deleted.

## [0.2.0] - 2026-06-12

### Added

- **Installable package structure** (`synesis_graph/`, `pyproject.toml`)
  - New `pyproject.toml` defines the `synesis-graph` package with `click>=8.0` and `synesis>=0.5.5` as core dependencies; `neo4j>=5.0` and `graphqlite` as optional extras (`pip install synesis-graph[neo4j]`).
  - `synesis_graph/__init__.py` re-exports the public API from `synesis2graph.py` (`run_pipeline`, `compile_project`, `load_json_project`, `GraphPayload`, `PipelineResult`, backend constants).

- **Quality toolchain and CI** (`pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`)
  - `ruff==0.15.17` and `mypy==1.15.0` added to `dev` extras (pinned, in sync with ecosystem).
  - `[tool.ruff]`: `line-length=100`, `target-version="py310"`; rules `["E","F","I","UP","B","SIM","C4"]`. `[tool.mypy]`: `ignore_missing_imports=true`.
  - `.pre-commit-config.yaml`: `ruff` (lint + `--fix`), `ruff-format`, `mypy`, standard file-hygiene hooks.
  - CI workflow (3 OS × 3 Python): `test` (pytest + coverage for `synesis_graph` and `synesis2graph`), `lint`, `build` (wheel includes `synesis2graph.py` via `py-modules`), `integration` (`synesis-graph --help/--version` + `python synesis2graph.py --version`).

- **Test suite added to version control** (`tests/`, `.gitignore`)
  - Removed `tests/` from `.gitignore`; `tests/conftest.py` and `tests/test_phase7_multidb.py` are now tracked.
  - Added `tests/Davi_Projeto_Completo/` to `.gitignore` (local fixture, not committed).

- **Contract tests** (`tests/test_cli.py`, `tests/test_public_api.py`)
  - `test_cli.py`: subprocess assertions on `synesis-graph --help` structural anchors and `--version` — regression guard for the CLI refactor.
  - `test_public_api.py`: verifies `import synesis_graph` exposes the exact public API set; verifies `from synesis2graph import run_pipeline, TaskReporter, ...` works (shim contract).

- **Click-based CLI in `synesis2graph.py`** — replaced the `argparse` `main()` directly in the script:
  - Entry point `synesis-graph` registered via `pyproject.toml` (i.e. `pip install -e .` → `synesis-graph` in PATH).
  - Same `_SynesisGroup` / `_SynesisCommand` / `_ex()` pattern used in `synesis` and `synesis-coder`: Unix-style output, ANSI colors (suppressed when stdout is not a TTY), `sys.stdout.buffer.write(UTF-8)` for encoding safety on Windows.
  - Three subcommands replacing the flat `--backend` flag: `neo4j`, `graphqlite`, `html` — each with their own `--help` and colored `Examples:` epilog.
  - `--project` and `--json` are shared source options on every subcommand (mutually exclusive, one required); `--config` defaults to `config.toml`.
  - HTML-specific flags (`--output`, `--group-by`, `--min-frequency`, `--min-source-count`, `--max-nodes`, `--max-hyperedges`, `--include-isolated`, `--all`) moved from a flat arg group to the `html` subcommand.
  - Graceful fallback to `argparse` when `click` is not installed (`python synesis2graph.py --backend ...` still works).

### Changed

- Repository and package renamed from `synesis2neo4j` to `synesis-graph`.

---

## [0.1.2] - 2025-02-01

### Added

#### Dynamic Source Fields
- **SOURCE Scope Support:** Fields with `SCOPE SOURCE` defined in the Template (.synt) are now dynamically transferred as properties of the `Source` node in Neo4j
- **Template-Driven Extraction:** `analyze_template()` now identifies and catalogs SOURCE-scoped fields alongside ONTOLOGY and ITEM fields
- **Dynamic Source Properties:** `_build_source_props()` replaced hardcoded field extraction with dynamic iteration over template-defined SOURCE fields
- **Full Data Flow:** SOURCE field names are now propagated through the entire pipeline: `analyze_template()` → `GraphPayload` → `_extract_corpus_data()` → `_build_source_props()`
- **Backward Compatibility:** Standard bibliographic fields (`title`, `author`, `year`, `doi`, `journal`, `abstract`) remain as fallback from bibliography entries

---

## [0.1.1] - 2025-01-25

### Fixed

#### GDS Compatibility (Neo4j GDS 2.x+)
- **gds.graph.drop:** Added `YIELD graphName` to avoid deprecated `schema` field warning
- **gds.graph.project.cypher:** Replaced deprecated procedure with new aggregation function API
  - CO_TAXONOMY strategy now uses inline `gds.graph.project()` aggregation
  - CO_CITATION strategy now uses inline `gds.graph.project()` aggregation
  - More efficient execution within Cypher flow

---

## [0.1.0] - 2025-01-24

### Added

#### Universal Pipeline
- **Dynamic Modeling:** Node labels automatically derived from Template (.synt)
- **CODE Support:** CODE fields create concept nodes with dynamic label
- **CHAIN Support:** CHAIN fields create RELATES_TO relationships between concepts
- **Taxonomy Support:** TOPIC, ASPECT, DIMENSION create navigable hierarchies
- **Traceability:** Origin metadata (source_file, line, column) on all nodes

#### Graph Metrics
- **Native Metrics (pure Cypher):**
  - `degree`, `in_degree`, `out_degree` for concepts
  - `mention_count`, `source_count` for concepts
  - `concept_count` for taxonomies
  - `weighted_degree`, `aspect_diversity`, `dimension_diversity` for Topics
  - `item_count`, `concept_count` for Sources

- **GDS Metrics (optional):**
  - `pagerank` - PageRank for relevance/centrality
  - `betweenness` - Betweenness Centrality for "bridge" nodes
  - `community` - Louvain for community detection

- **Projection Strategies:**
  - `RELATES_TO` - uses explicit relationships (CHAIN templates)
  - `CO_TAXONOMY` - connects concepts via shared taxonomy
  - `CO_CITATION` - connects concepts via co-citation in Sources

#### Infrastructure
- **Version Control:** `--version` flag in CLI
- **Graceful Fallback:** Native metrics always calculated; GDS optional with warning
- **Sanitization:** Labels and database names validated against Cypher injection
- **Atomic Transactions:** Synchronization via single transaction

#### MCP Integration (Claude Desktop)
- **Universal Configuration:** Templates for Claude Desktop (`mcp/`)
- **Multi-Database Support:** Namespaces for multiple simultaneous projects
- **GraphRAG Documentation:** Query guide with full traceability
- **Viability Study:** Complete analysis in `docs/MCP_VIABILITY_STUDY.md`

### Documentation
- README.md with Template → Graph mapping table
- Complete metrics documentation (native and GDS)
- Mermaid data flow diagram
- Cypher query examples
- MCP configuration guide (`mcp/SETUP.md`)
- Cypher query reference (`mcp/QUERIES_REFERENCE.md`)
- Bilingual documentation (EN/PT)

---

## Links

- **Repository:** [github.com/synesis-lang/synesis-graph](https://github.com/synesis-lang/synesis-graph)
- **Documentation:** [synesis-lang.github.io/synesis-docs](https://synesis-lang.github.io/synesis-docs)
- **Issues:** [GitHub Issues](https://github.com/synesis-lang/synesis-graph/issues)
