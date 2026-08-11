# synesis-graph: Pipeline Universal de Grafos

[![Synesis](https://img.shields.io/badge/Synesis-Language-blue?style=for-the-badge)](https://synesis-lang.github.io/synesis-docs) ![Python](https://img.shields.io/badge/Python-3.10%2B-yellow?style=for-the-badge) ![Backends](https://img.shields.io/badge/Backends-Neo4j%20%7C%20HTML-blueviolet?style=for-the-badge) ![License](https://img.shields.io/badge/License-AGPL%20v3%20%2B%20exception-green?style=for-the-badge)

> **Transforme sua pesquisa qualitativa em um Grafo de Conhecimento vivo, navegável e pronto para IA (GraphRAG).**

**Idioma:** [English](README.md) | [Português](README.pt.md)

**Documentação:** [Synesis Language Docs](https://synesis-lang.github.io/synesis-docs)

Este repositório contém o pipeline oficial da linguagem **Synesis** para representações em grafo. Ele atua como uma ponte entre a análise humana estruturada (arquivos `.syn`) e a inteligência computacional, permitindo que Agentes MCP e algoritmos de Data Science interajam com sua pesquisa.

Dois backends acompanham esta versão — **Neo4j** (property graph, com métricas GDS) e **HTML** (visualização interativa autocontida). Ambos são construídos sobre o mesmo contrato `BackendAdapter`, então o suporte a outros bancos de grafos pode ser acrescentado sem tocar no pipeline.

---

## Destaques

* **Zero-IO / Direct Link:** Utiliza a API `synesis.load()` para compilar o projeto em memória e sincronizar diretamente com o banco. Sem arquivos JSON/CSV intermediários.
* **Universal & Agnóstico:** Não depende de regras fixas (como "Fatores" ou "Dimensões"). O script lê o seu **Template (.synt)** e cria a estrutura do grafo dinamicamente.
* **Métricas Automáticas:** Calcula métricas de rede em dois níveis:
    * **Métricas Nativas:** Sempre disponíveis via Cypher puro.
    * **Métricas GDS:** Algoritmos avançados quando o plugin Graph Data Science está instalado.
* **Rastreabilidade Total:** Cada nó e aresta mantém metadados de origem (`source_file`, `line`, `column`), garantindo auditabilidade científica.

---

## Arquitetura

O pipeline segue o fluxo **"Research as Code"**:

1.  **Entrada:** Arquivos de texto plano (`.syn`, `.syno`, `.synt`, `.synp`) definidos pela linguagem Synesis.
2.  **Compilação:** O compilador valida a sintaxe e a semântica em tempo real.
3.  **Modelagem Dinâmica:** O script traduz definições do Template em estruturas de grafo.
4.  **Persistência:** Os dados são injetados no Neo4j via transações atômicas.
5.  **Analytics:** Métricas de grafo são calculadas e armazenadas nos nós.

---

## Modelagem de Dados: Template → Grafo

O pipeline traduz automaticamente os tipos de campo definidos no seu **Template (.synt)** para estruturas no grafo Neo4j. Esta tradução é dinâmica e não depende de nomes específicos de campos.

### Tabela de Mapeamento

| Tipo no Template | Elemento no Grafo | Relação Criada | Descrição |
|------------------|-------------------|----------------|-----------|
| `CODE` | **Nó Conceito** | `MENTIONS` (Item → Conceito) | Unidade central de análise. Nome do nó derivado do campo (ex: `ordem_2a` → label `Ordem_2a`). |
| `TOPIC` | **Nó Taxonomia** | `GROUPED_BY` | Agrupamento temático de conceitos. Cria hierarquia navegável. |
| `ASPECT` | **Nó Taxonomia** | `QUALIFIED_BY` | Dimensão qualitativa. Permite classificação multidimensional. |
| `DIMENSION` | **Nó Taxonomia** | `BELONGS_TO` | Dimensão agregada de alto nível. |
| `ENUMERATED` | **Propriedade** | — | Valores discretos armazenados como propriedade no nó. |
| `CHAIN` | **Relação Explícita** | `RELATES_TO` | Conexão direta entre conceitos com tipo e descrição. |
| `TEXT` / `MEMO` | **Propriedade** | — | Texto livre armazenado como propriedade. |
| `SCOPE SOURCE` | **Propriedade de Source** | — | Campos com `SCOPE SOURCE` são transferidos dinamicamente como propriedades do nó `Source`. |

### Nós Base (Sempre Criados)

| Nó | Descrição | Propriedades |
|----|-----------|--------------|
| `Source` | Fonte de dados (entrevista, artigo, documento) | `bibtex`, `title`, `author`, `year` + todos os campos `SCOPE SOURCE` do Template |
| `Item` | Unidade de citação extraída da fonte | `item_id`, `citation`, `description` |

### Exemplo de Tradução

**Template:**
```
FIELD categoria TYPE CODE
    SCOPE ITEM
END FIELD

FIELD tema TYPE TOPIC
    SCOPE ONTOLOGY
END FIELD
```

**Grafo Resultante:**
```
(:Item)-[:MENTIONS]->(:Categoria)-[:GROUPED_BY]->(:Tema)
```

---

## Métricas de Grafo

O pipeline calcula automaticamente métricas de rede que enriquecem a análise. As métricas são divididas em dois níveis:

### Métricas Nativas (Sempre Disponíveis)

Calculadas via Cypher puro, sem dependências externas.

#### Nós de Conceito (CODE)

| Métrica | Descrição | Uso Analítico |
|---------|-----------|---------------|
| `degree` | Grau total (in + out) | Conectividade geral do conceito |
| `in_degree` | Relações recebidas | Conceitos que referenciam este |
| `out_degree` | Relações emitidas | Conceitos referenciados por este |
| `mention_count` | Citações que mencionam o conceito | Frequência nos dados primários |
| `source_count` | Fontes distintas onde aparece | Dispersão/generalização do conceito |

#### Nós de Taxonomia (TOPIC, ASPECT, DIMENSION)

| Métrica | Descrição | Uso Analítico |
|---------|-----------|---------------|
| `concept_count` | Conceitos classificados nesta categoria | Abrangência da categoria |
| `weighted_degree` | Soma dos pesos das conexões IS_LINKED_TO | Força das relações inter-taxonomia |
| `aspect_diversity` | Aspectos distintos dos conceitos filhos | Diversidade qualitativa |
| `dimension_diversity` | Dimensões distintas dos conceitos filhos | Dispersão dimensional |

#### Nós de Source

| Métrica | Descrição | Uso Analítico |
|---------|-----------|---------------|
| `item_count` | Citações extraídas da fonte | Volume de dados da fonte |
| `concept_count` | Conceitos distintos mencionados | Riqueza conceitual da fonte |

### Métricas GDS (Requer Plugin)

Quando o plugin **Neo4j Graph Data Science** está instalado, o pipeline calcula métricas avançadas de rede:

| Métrica | Algoritmo | Descrição |
|---------|-----------|-----------|
| `pagerank` | PageRank | Relevância/centralidade baseada em conexões |
| `betweenness` | Betweenness Centrality | Papel de "ponte" entre clusters |
| `community` | Louvain | Detecção de comunidades temáticas |

#### Estratégias de Projeção do Grafo

O cálculo das métricas GDS adapta-se automaticamente ao tipo de template:

| Estratégia | Quando Usada | Descrição |
|------------|--------------|-----------|
| **RELATES_TO** | Templates com `CHAIN` | Usa relações explícitas entre conceitos |
| **CO_TAXONOMY** | Templates com `CODE` + `TOPIC` | Conecta conceitos que compartilham taxonomia |
| **CO_CITATION** | Fallback | Conecta conceitos que co-ocorrem nas mesmas fontes |

> **Nota:** Se o GDS não estiver instalado, o pipeline exibe um aviso e continua normalmente com as métricas nativas.

---

## Instalação

Requer **Python 3.10+** e [synesis](https://github.com/synesis-lang/synesis) ≥ 0.10.0.

```bash
# Clone o repositório
git clone https://github.com/synesis-lang/synesis-graph.git
cd synesis-graph

# Instale (editável) com os backends de grafo que precisar
pip install -e ".[neo4j]"
```

### Matriz de compatibilidade

| Pacote | Esta versão | Requer `synesis` | Python |
|---|---|---|---|
| synesis | 0.11.0 | — | ≥3.10 |
| synesis-coder | 0.8.0 | ≥0.10.0 | ≥3.10 |
| synesis-lsp | 0.22.0 | ≥0.10.0 | ≥3.10 |
| synesis-graph | 0.5.0 | ≥0.10.0 | ≥3.10 |

### Plugin GDS (Opcional)

Para métricas avançadas, instale o [Neo4j Graph Data Science](https://neo4j.com/docs/graph-data-science/current/installation/):

```bash
# Neo4j Desktop: Plugins → Install Graph Data Science Library
# Neo4j Server: Baixe o JAR e adicione ao diretório plugins/
```

---

## Configuração

Crie um arquivo `config.toml` na raiz com as credenciais do seu banco Neo4j:

```toml
[neo4j]
uri = "bolt://localhost:7687"  # Ou seu URI do Neo4j Aura
user = "neo4j"
password = "sua_senha_secreta"
database = "neo4j"             # Opcional, default é 'neo4j'
```

---

## Uso

Escolha um backend e aponte para o arquivo de projeto Synesis (`.synp`):

```bash
# Sincroniza com o Neo4j
synesis-graph neo4j --project ./meu-projeto/analise.synp

# Gera um grafo HTML interativo e autocontido
synesis-graph html --project ./meu-projeto/analise.synp --output graph.html

# Lista todos os backends e suas opções
synesis-graph --help
```

### O que acontece durante a execução?

1. **Compilação:** O compilador Synesis valida seu código. Erros de sintaxe são exibidos e o processo para (o destino não é tocado).
2. **Conexão:** Se a compilação for bem-sucedida, o pipeline conecta ao backend escolhido.
3. **Constraints:** Regras de unicidade são aplicadas baseadas no Template.
4. **Sincronização:** Dados são injetados (Conceitos, Citações, Fontes, Relações).
5. **Métricas Nativas:** Calculadas via Cypher puro.
6. **Métricas GDS:** Calculadas se o plugin estiver disponível (com aviso se não estiver).

---

## Estrutura do Grafo Resultante

### Relações Principais

| Relação | Origem | Destino | Descrição |
|---------|--------|---------|-----------|
| `FROM_SOURCE` | Item | Source | Rastreabilidade da citação |
| `MENTIONS` | Item | Conceito | Citação menciona conceito |
| `GROUPED_BY` | Conceito | Topic | Classificação temática |
| `QUALIFIED_BY` | Conceito | Aspect | Qualificação dimensional |
| `BELONGS_TO` | Conceito | Dimension | Agregação de alto nível |
| `RELATES_TO` | Conceito | Conceito | Relação explícita (CHAIN) |
| `IS_LINKED_TO` | Topic | Topic | Co-taxonomia ponderada |

### Exemplo de Consulta Cypher

```cypher
// Encontrar os 10 conceitos mais centrais
MATCH (c:Conceito)
WHERE c.pagerank IS NOT NULL
RETURN c.name, c.pagerank, c.mention_count, c.community
ORDER BY c.pagerank DESC
LIMIT 10

// Explorar comunidades temáticas
MATCH (c:Conceito)
WHERE c.community = 42
RETURN c.name, c.pagerank
ORDER BY c.pagerank DESC
```

---

## Integração com Agentes MCP (IA)

Uma vez que seus dados estão no Neo4j, você pode utilizar o **Servidor MCP Neo4j** para permitir que LLMs (como Claude Desktop ou Cursor) conversem com sua pesquisa.

### Instalação Rápida

```bash
# Instalar uv (se necessário)
pip install uv
```

### Configuração do Claude Desktop

Adicione ao arquivo `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "synesis-neo4j": {
      "command": "uvx",
      "args": ["mcp-neo4j-cypher@0.5.2", "--read-only"],
      "env": {
        "NEO4J_URI": "bolt://localhost:7687",
        "NEO4J_USERNAME": "neo4j",
        "NEO4J_PASSWORD": "sua_senha",
        "NEO4J_DATABASE": "nome_do_banco"
      }
    }
  }
}
```

**Localização do arquivo:**
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

### Exemplos de Perguntas

| Pergunta | O que retorna |
|----------|---------------|
| "Quais conceitos têm maior PageRank?" | Top conceitos por relevância |
| "Mostre as fontes que mencionam 'Acceptance'" | Rastreabilidade Item → Source |
| "Quais conceitos pertencem à comunidade 1?" | Análise de clusters |
| "Compare as métricas dos principais conceitos" | Tabela comparativa |

### Documentação Completa

Consulte a pasta `mcp/` para:
- [SETUP.md](mcp/SETUP.md) - Guia completo de configuração
- [QUERIES_REFERENCE.md](mcp/QUERIES_REFERENCE.md) - Referência de queries Cypher
- Templates de configuração para banco único e múltiplo

---

## Diagrama de Fluxo

```mermaid
graph TD
    %% Estilos
    classDef files fill:#e1f5fe,stroke:#01579b,stroke-width:2px;
    classDef engine fill:#fff3e0,stroke:#ff6f00,stroke-width:2px;
    classDef data fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px;
    classDef graphDb fill:#f3e5f5,stroke:#7b1fa2,stroke-width:2px;
    classDef agent fill:#212121,stroke:#000,stroke-width:2px,color:#fff;

    subgraph "1. Entrada: Pesquisa como Código"
        SYN[Corpus Anotado<br/>.syn]:::files
        SYNO[Ontologia<br/>.syno]:::files
        SYNP[Projeto<br/>.synp]:::files
        SYNT[Template<br/>.synt]:::files
    end

    subgraph "2. Synesis Engine"
        COMPILER{{synesis.load}}:::engine
        VALIDATOR(Validação Semântica):::engine

        SYNP --> COMPILER
        SYN --> COMPILER
        SYNO --> COMPILER
        SYNT --> COMPILER
        COMPILER --> VALIDATOR
    end

    subgraph "3. Dados Estruturados"
        JSON[Objeto Canônico<br/>Rastreável]:::data
        SCHEMA[Schema Dinâmico<br/>do Template]:::data

        VALIDATOR -->|Sucesso| JSON
        SYNT -.->|Define| SCHEMA
    end

    subgraph "4. Grafo de Conhecimento"
        NEO4J[(Neo4j)]:::graphDb
        NATIVE[Métricas Nativas<br/>Cypher]:::graphDb
        GDS[Métricas GDS<br/>Opcional]:::graphDb

        JSON & SCHEMA -->|Sync| NEO4J
        NEO4J --> NATIVE
        NATIVE --> GDS

        subgraph "Métricas"
            DEG[Degree<br/>Centralidade]
            PR[PageRank<br/>Relevância]
            BC[Betweenness<br/>Pontes]
            COM[Louvain<br/>Comunidades]
        end
        NATIVE -.-> DEG
        GDS -.-> PR & BC & COM
    end

    subgraph "5. Consumo Inteligente"
        MCP[Agente MCP]:::agent
        LLM[LLMs / Claude]:::agent

        NEO4J <-->|GraphRAG| MCP
        MCP <-->|Consultas| LLM
    end
```

---

## Licença

Este programa é distribuído sob a **GNU Affero General Public License, versão 3
apenas (AGPL-3.0-only), com a Synesis Data-Output Exception** — veja
[LICENSE](LICENSE) e [LICENSE.exception](LICENSE.exception).

Identificador SPDX: `AGPL-3.0-only AND LicenseRef-Synesis-data-output-exception`

**Os grafos que você gera são seus.** Os artefatos exportados — scripts Cypher,
bases Neo4j e a visualização HTML autocontida — **não** são cobertos pela AGPL
e não carregam nenhuma obrigação de copyleft em relação ao Synesis.

Isso importa especialmente aqui: o HTML gerado **embute JavaScript e CSS do
próprio Synesis** para que a visualização funcione de forma autocontida. Esse
material embutido é *Synesis Runtime Material* nos termos da Exceção e **não**
coloca o arquivo gerado sob a AGPL. Você pode publicar, vender ou licenciar o
HTML que gerar sob os termos que quiser.

A AGPL se aplica ao synesis-graph em si: se você modificá-lo e distribuí-lo, ou
executá-lo como serviço de rede, deve compartilhar suas alterações sob a AGPL.

As versões publicadas antes desta mudança permanecem disponíveis sob a licença
MIT sob a qual foram lançadas.

Esta licença não concede direitos sobre o nome ou logotipo "Synesis".

---

Parte do ecossistema **[Synesis Language](https://synesis-lang.github.io/synesis-docs)**.
