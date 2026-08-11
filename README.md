# synesis-graph: Universal Graph Pipeline

[![Synesis](https://img.shields.io/badge/Synesis-Language-blue?style=for-the-badge)](https://synesis-lang.github.io/synesis-docs) ![Python](https://img.shields.io/badge/Python-3.10%2B-yellow?style=for-the-badge) ![Backends](https://img.shields.io/badge/Backends-Neo4j%20%7C%20HTML-blueviolet?style=for-the-badge) ![License](https://img.shields.io/badge/License-AGPL%20v3%20%2B%20exception-green?style=for-the-badge)

> **Transform your qualitative research into a living, navigable Knowledge Graph ready for AI (GraphRAG).**

**Language:** [English](README.md) | [Portugues](README.pt.md)

**Documentation:** [Synesis Language Docs](https://synesis-lang.github.io/synesis-docs)

This repository contains the official pipeline from the **Synesis** language to graph representations. It acts as a bridge between structured human analysis (`.syn` files) and computational intelligence, enabling MCP Agents and Data Science algorithms to interact with your research.

Two backends ship today — **Neo4j** (property graph, with GDS metrics) and **HTML** (self-contained interactive visualization). Both are built on the same `BackendAdapter` contract, so support for further graph databases can be added without touching the pipeline.

---

## Highlights

* **Zero-IO / Direct Link:** Uses the `synesis.load()` API to compile the project in memory and sync directly to the database. No intermediate JSON/CSV files.
* **Universal & Agnostic:** Does not depend on fixed rules (like "Factors" or "Dimensions"). The script reads your **Template (.synt)** and creates the graph structure dynamically.
* **Automatic Metrics:** Calculates network metrics at two levels:
    * **Native Metrics:** Always available via pure Cypher.
    * **GDS Metrics:** Advanced algorithms when the Graph Data Science plugin is installed.
* **Full Traceability:** Every node and edge maintains origin metadata (`source_file`, `line`, `column`), ensuring scientific auditability.

---

## Architecture

The pipeline follows the **"Research as Code"** flow:

1. **Input:** Plain text files (`.syn`, `.syno`, `.synt`, `.synp`) defined by the Synesis language.
2. **Compilation:** The compiler validates syntax and semantics in real-time.
3. **Dynamic Modeling:** The script translates Template definitions into graph structures.
4. **Persistence:** Data is injected into Neo4j via atomic transactions.
5. **Analytics:** Graph metrics are calculated and stored in nodes.

---

## Data Modeling: Template → Graph

The pipeline automatically translates field types defined in your **Template (.synt)** to structures in the Neo4j graph. This translation is dynamic and does not depend on specific field names.

### Mapping Table

| Template Type | Graph Element | Relationship Created | Description |
|---------------|---------------|---------------------|-------------|
| `CODE` | **Concept Node** | `MENTIONS` (Item → Concept) | Central unit of analysis. Node name derived from field (e.g., `ordem_2a` → label `Ordem_2a`). |
| `TOPIC` | **Taxonomy Node** | `GROUPED_BY` | Thematic grouping of concepts. Creates navigable hierarchy. |
| `ASPECT` | **Taxonomy Node** | `QUALIFIED_BY` | Qualitative dimension. Enables multidimensional classification. |
| `DIMENSION` | **Taxonomy Node** | `BELONGS_TO` | High-level aggregate dimension. |
| `ENUMERATED` | **Property** | — | Discrete values stored as node property. |
| `CHAIN` | **Explicit Relationship** | `RELATES_TO` | Direct connection between concepts with type and description. |
| `TEXT` / `MEMO` | **Property** | — | Free text stored as property. |
| `SCOPE SOURCE` | **Source Property** | — | Fields with `SCOPE SOURCE` are dynamically transferred as properties of the `Source` node. |

### Base Nodes (Always Created)

| Node | Description | Properties |
|------|-------------|------------|
| `Source` | Data source (interview, article, document) | `bibtex`, `title`, `author`, `year` + all `SCOPE SOURCE` fields from Template |
| `Item` | Citation unit extracted from source | `item_id`, `citation`, `description` |

### Translation Example

**Template:**
```
FIELD category TYPE CODE
    SCOPE ITEM
END FIELD

FIELD theme TYPE TOPIC
    SCOPE ONTOLOGY
END FIELD
```

**Resulting Graph:**
```
(:Item)-[:MENTIONS]->(:Category)-[:GROUPED_BY]->(:Theme)
```

---

## Graph Metrics

The pipeline automatically calculates network metrics that enrich the analysis. Metrics are divided into two levels:

### Native Metrics (Always Available)

Calculated via pure Cypher, without external dependencies.

#### Concept Nodes (CODE)

| Metric | Description | Analytical Use |
|--------|-------------|----------------|
| `degree` | Total degree (in + out) | Overall concept connectivity |
| `in_degree` | Incoming relationships | Concepts referencing this one |
| `out_degree` | Outgoing relationships | Concepts referenced by this one |
| `mention_count` | Citations mentioning the concept | Frequency in primary data |
| `source_count` | Distinct sources where it appears | Concept dispersion/generalization |

#### Taxonomy Nodes (TOPIC, ASPECT, DIMENSION)

| Metric | Description | Analytical Use |
|--------|-------------|----------------|
| `concept_count` | Concepts classified in this category | Category coverage |
| `weighted_degree` | Sum of IS_LINKED_TO connection weights | Inter-taxonomy relationship strength |
| `aspect_diversity` | Distinct aspects of child concepts | Qualitative diversity |
| `dimension_diversity` | Distinct dimensions of child concepts | Dimensional dispersion |

#### Source Nodes

| Metric | Description | Analytical Use |
|--------|-------------|----------------|
| `item_count` | Citations extracted from source | Source data volume |
| `concept_count` | Distinct concepts mentioned | Source conceptual richness |

### GDS Metrics (Requires Plugin)

When the **Neo4j Graph Data Science** plugin is installed, the pipeline calculates advanced network metrics:

| Metric | Algorithm | Description |
|--------|-----------|-------------|
| `pagerank` | PageRank | Connection-based relevance/centrality |
| `betweenness` | Betweenness Centrality | "Bridge" role between clusters |
| `community` | Louvain | Thematic community detection |

#### Graph Projection Strategies

GDS metrics calculation automatically adapts to template type:

| Strategy | When Used | Description |
|----------|-----------|-------------|
| **RELATES_TO** | Templates with `CHAIN` | Uses explicit relationships between concepts |
| **CO_TAXONOMY** | Templates with `CODE` + `TOPIC` | Connects concepts sharing taxonomy |
| **CO_CITATION** | Fallback | Connects concepts co-occurring in same sources |

> **Note:** If GDS is not installed, the pipeline displays a warning and continues normally with native metrics.

---

## Installation

Requires **Python 3.10+** and [synesis](https://github.com/synesis-lang/synesis) ≥ 0.10.0.

```bash
# Clone the repository
git clone https://github.com/synesis-lang/synesis-graph.git
cd synesis-graph

# Install (editable) with the graph backends you need
pip install -e ".[neo4j]"
```

### Compatibility matrix

| Package | This version | Requires `synesis` | Python |
|---|---|---|---|
| synesis | 0.11.0 | — | ≥3.10 |
| synesis-coder | 0.8.0 | ≥0.10.0 | ≥3.10 |
| synesis-lsp | 0.22.0 | ≥0.10.0 | ≥3.10 |
| synesis-graph | 0.5.0 | ≥0.10.0 | ≥3.10 |

### GDS Plugin (Optional)

For advanced metrics, install the [Neo4j Graph Data Science](https://neo4j.com/docs/graph-data-science/current/installation/):

```bash
# Neo4j Desktop: Plugins → Install Graph Data Science Library
# Neo4j Server: Download JAR and add to plugins/ directory
```

---

## Configuration

Create a `config.toml` file in the root with your Neo4j credentials:

```toml
[neo4j]
uri = "bolt://localhost:7687"  # Or your Neo4j Aura URI
user = "neo4j"
password = "your_secret_password"
database = "neo4j"             # Optional, default is 'neo4j'
```

---

## Usage

Pick a backend and point it at the Synesis project file (`.synp`):

```bash
# Sync to Neo4j
synesis-graph neo4j --project ./my-project/analysis.synp

# Render a self-contained interactive HTML graph
synesis-graph html --project ./my-project/analysis.synp --output graph.html

# See every backend and its options
synesis-graph --help
```

### What happens during execution?

1. **Compilation:** The Synesis compiler validates your code. Syntax errors are displayed and the process stops (the destination is not touched).
2. **Connection:** If compilation succeeds, the pipeline connects to the selected backend.
3. **Constraints:** Uniqueness rules are applied based on the Template.
4. **Synchronization:** Data is injected (Concepts, Citations, Sources, Relationships).
5. **Native Metrics:** Calculated via pure Cypher.
6. **GDS Metrics:** Calculated if plugin is available (with warning if not).

---

## Resulting Graph Structure

### Main Relationships

| Relationship | Source | Target | Description |
|--------------|--------|--------|-------------|
| `FROM_SOURCE` | Item | Source | Citation traceability |
| `MENTIONS` | Item | Concept | Citation mentions concept |
| `GROUPED_BY` | Concept | Topic | Thematic classification |
| `QUALIFIED_BY` | Concept | Aspect | Dimensional qualification |
| `BELONGS_TO` | Concept | Dimension | High-level aggregation |
| `RELATES_TO` | Concept | Concept | Explicit relationship (CHAIN) |
| `IS_LINKED_TO` | Topic | Topic | Weighted co-taxonomy |

### Cypher Query Example

```cypher
// Find the 10 most central concepts
MATCH (c:Concept)
WHERE c.pagerank IS NOT NULL
RETURN c.name, c.pagerank, c.mention_count, c.community
ORDER BY c.pagerank DESC
LIMIT 10

// Explore thematic communities
MATCH (c:Concept)
WHERE c.community = 42
RETURN c.name, c.pagerank
ORDER BY c.pagerank DESC
```

---

## MCP Agent Integration (AI)

Once your data is in Neo4j, you can use the **Neo4j MCP Server** to allow LLMs (like Claude Desktop or Cursor) to converse with your research.

### Quick Installation

```bash
# Install uv (if needed)
pip install uv
```

### Claude Desktop Configuration

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "synesis-neo4j": {
      "command": "uvx",
      "args": ["mcp-neo4j-cypher@0.5.2", "--read-only"],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "your_password",
        "NEO4J_DATABASE": "database_name"
      }
    }
  }
}
```

**File location:**
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

### Question Examples

| Question | Returns |
|----------|---------|
| "Which concepts have the highest PageRank?" | Top concepts by relevance |
| "Show sources mentioning 'Acceptance'" | Item → Source traceability |
| "Which concepts belong to community 1?" | Cluster analysis |
| "Compare metrics of main concepts" | Comparative table |

### Complete Documentation

See the `mcp/` folder for:
- [SETUP.en.md](mcp/SETUP.en.md) - Complete configuration guide
- [QUERIES_REFERENCE.en.md](mcp/QUERIES_REFERENCE.en.md) - Cypher query reference
- Configuration templates for single and multiple databases

---

## Flow Diagram

```mermaid
graph TD
    %% Styles
    classDef files fill:#e1f5fe,stroke:#01579b,stroke-width:2px;
    classDef engine fill:#fff3e0,stroke:#ff6f00,stroke-width:2px;
    classDef data fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px;
    classDef graphDb fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px;
    classDef agent fill:#212121,stroke:#000,stroke-width:2px,color:#fff;

    subgraph "1. Input: Research as Code"
        SYN[Annotated Corpus<br/>.syn]:::files
        SYNO[Ontology<br/>.syno]:::files
        SYNP[Project<br/>.synp]:::files
        SYNT[Template<br/>.synt]:::files
    end

    subgraph "2. Synesis Engine"
        COMPILER{{synesis.load}}:::engine
        VALIDATOR(Semantic Validation):::engine

        SYNP --> COMPILER
        SYN --> COMPILER
        SYNO --> COMPILER
        SYNT --> COMPILER
        COMPILER --> VALIDATOR
    end

    subgraph "3. Structured Data"
        JSON[Canonical Object<br/>Traceable]:::data
        SCHEMA[Dynamic Schema<br/>from Template]:::data

        VALIDATOR -->|Success| JSON
        SYNT -.->|Defines| SCHEMA
    end

    subgraph "4. Knowledge Graph"
        NEO4J[(Neo4j)]:::graphDb
        NATIVE[Native Metrics<br/>Cypher]:::graphDb
        GDS[GDS Metrics<br/>Optional]:::graphDb

        JSON & SCHEMA -->|Sync| NEO4J
        NEO4J --> NATIVE
        NATIVE --> GDS

        subgraph "Metrics"
            DEG[Degree<br/>Centrality]
            PR[PageRank<br/>Relevance]
            BC[Betweenness<br/>Bridges]
            COM[Louvain<br/>Communities]
        end
        NATIVE -.-> DEG
        GDS -.-> PR & BC & COM
    end

    subgraph "5. Intelligent Consumption"
        MCP[MCP Agent]:::agent
        LLM[LLMs / Claude]:::agent

        NEO4J <-->|GraphRAG| MCP
        MCP <-->|Queries| LLM
    end
```

---

## License

This program is distributed under the **GNU Affero General Public License,
version 3 only (AGPL-3.0-only), with the Synesis Data-Output Exception** — see
[LICENSE](LICENSE) and [LICENSE.exception](LICENSE.exception).

SPDX identifier: `AGPL-3.0-only AND LicenseRef-Synesis-data-output-exception`

**The graphs you generate are yours.** Exported artifacts — Cypher scripts,
Neo4j databases, and the standalone HTML visualization — are **not** covered by
the AGPL and carry no copyleft obligation toward Synesis.

This matters especially here: the generated HTML **embeds Synesis's own
JavaScript and CSS** so the visualization can render standalone. That embedded
material is *Synesis Runtime Material* under the Exception, and it does **not**
place the generated file under the AGPL. You may publish, sell, or license the
HTML you generate under any terms you choose.

The AGPL applies to synesis-graph itself: if you modify it and distribute it,
or run it as a network service, you must share your changes under the AGPL.

Releases published before this change remain available under the MIT license
they were issued under.

This license grants no rights to the "Synesis" name or logo.

---

Part of the **[Synesis Language](https://synesis-lang.github.io/synesis-docs)** ecosystem.
