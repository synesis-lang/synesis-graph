# Changelog

Todas as mudanças notáveis neste projeto serão documentadas neste arquivo.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/),
e este projeto adere ao [Versionamento Semântico](https://semver.org/lang/pt-BR/).

**Idioma:** [English](CHANGELOG.md) | [Português](CHANGELOG.pt.md)

**Documentação:** [Synesis Language Docs](https://synesis-lang.github.io/synesis-docs)

---

## [0.5.0] - 2026-08-11

### Removido — backend GraphQLite

- **O backend `graphqlite` foi removido.** Nunca funcionou e não será
  implementado. Mantê-lo significava anunciar um terceiro destino de exportação
  no `--help`, no arquivo de configuração e na API pública que falhava em
  silêncio assim que alguém o selecionasse — pior que não oferecer.
- Removidos: o subcomando `graphqlite`, `GraphQLiteBackendAdapter`,
  `GraphQLiteConfig`, `sync_to_graphqlite`, `compute_metrics_graphqlite`,
  `_GraphQLiteQueryRunner`, `get_graphqlite_connect_factory`,
  `_resolve_graphqlite_db_path`, a seção `[graphqlite]` do config, a dependência
  opcional `graphqlite` e o `GraphQLite_Reference.md`.
- **Contrato da API pública mudou:** `SUPPORTED_BACKENDS` passa a ser
  `("neo4j", "html")` e `BACKEND_GRAPHQLITE` deixa de existir. Travado por
  `tests/test_public_api.py`. Como o pacote nunca foi publicado no PyPI,
  nenhuma versão lançada é afetada.
- O suporte a outros bancos de grafos segue no roteiro — Google Vertex é
  candidato, não compromisso. Qualquer backend novo implementa o contrato
  `BackendAdapter` existente, que permanece intacto.
- Suíte de testes: 257 → 245 (os 12 removidos exercitavam só o GraphQLite). O
  teste de consistência entre backends passa a comparar Neo4j com HTML,
  preservando sua intenção original.

### Adicionado — contrato de empacotamento (pré-PyPI)

- **`tests/test_packaging.py`** (10 testes) — constrói o sdist de verdade e
  inspeciona o `PKG-INFO` gerado, em vez de confiar no que o `pyproject.toml`
  declara. Publicar no PyPI é irreversível: o nome fica reservado para sempre e
  uma versão enviada nunca pode ser sobrescrita, então um erro de embalagem
  custa queimar o número da versão.
  - Licença: `License-Expression` PEP 639 correta, **ausência** do campo
    obsoleto `License:`, e ambos os arquivos (`LICENSE`, `LICENSE.exception`)
    declarados **e** empacotados — a exceção só vale se o arquivo dela viajar
    junto.
  - Conteúdo: template HTML e shim legado presentes; nenhum `config.toml`
    (carrega senha real), `.db`, `.html` ou `.env` no artefato.
  - Consistência: versão do sdist, do `CITATION.cff` e do `CHANGELOG.md`
    conferidas contra o `pyproject.toml` — o CFF defasado já aconteceu duas
    vezes no ecossistema.
  - Verificado por mutação: trocando a licença pela sintaxe legada
    `{text = "..."}`, os testes falham. O `twine check` **passa** nesse cenário —
    por isso ele não basta, e é a causa provável do `license: None` que o PyPI
    hoje mostra para `synesis` e `synesis-lsp`.
- `pyyaml` acrescentado ao extra `dev` (o teste lê o `CITATION.cff`).
- **`pypa/gh-action-pypi-publish` pinada por SHA** — era a única action ainda em
  ref mutável (`@release/v1`) neste repositório, justamente a que tem permissão
  de publicar.

### Segurança

- **Todas as GitHub Actions passam a ser pinadas por SHA de commit**
  (`.github/workflows/ci.yml`). Doze entradas `uses:` apontavam para tags
  mutáveis (`@v4`, `@v5`) — um release comprometido ou re-tagueado rodaria no CI
  sem nenhuma mudança neste repositório. `synesis` e `synesis-lsp` já pinavam por
  SHA; isto alinha o terceiro repo. Cada SHA foi verificado contra a API do
  GitHub antes de ser aplicado.

- **Novo job `security` no CI**, igual ao dos outros pacotes Python — este era o
  único dos quatro sem ele:
  - `pip-audit` sobre as dependências de runtime declaradas no `pyproject.toml`
    (`synesis`, `click`). Verificado localmente: nenhuma vulnerabilidade
    conhecida.
  - Varredura de segredos com Gitleaks sobre o histórico completo
    (`fetch-depth: 0`).
  - O runner da auditoria é fixado em Python 3.11 porque o passo lê o
    `pyproject.toml` com `tomllib` (stdlib só a partir do 3.11). É a versão do
    RUNNER, não o piso suportado: `requires-python = ">=3.10"` continua valendo e
    a matriz de testes segue cobrindo 3.10.

- **`graphs/Davi.db` removido do controle de versão** — banco GraphQLite de
  164 KB, artefato do backend removido nesta versão. `graphs/` já constava do
  `.gitignore`, mas o arquivo é anterior à regra e o `.gitignore` não desrastreia
  o que já está rastreado. Removido com `git rm --cached`; o arquivo local
  permanece intacto.

### Documentação

- **Identidade do projeto corrigida na documentação.** Os dois READMEs ainda se
  intitulavam *"Synesis to Neo4j: Pipeline Universal de Grafos"* e descreviam o
  repositório como "o pipeline de ingestão para o **Neo4j**". Neo4j é um backend
  entre outros — o título agora é `synesis-graph`, a introdução informa os dois
  backends desta versão (Neo4j e HTML) sobre o contrato comum `BackendAdapter`,
  e o badge passa a ser `Backends: Neo4j | HTML` em vez de `Neo4j: Graph DB`.
- **A seção de uso apontava para um arquivo inexistente** — os dois READMEs
  mandavam executar `python synesis2neo4j.py --project ...`. O nome é anterior a
  duas renomeações; o shim é `synesis2graph.py` e o ponto de entrada suportado é
  a CLI `synesis-graph`. Substituído por comandos reais e verificados dos dois
  backends.
- **O `CITATION.cff` citava a obra errada** — o `title` era *"Synesis: A DSL
  compiler for knowledge engineering"*, título do compilador, de modo que quem
  citasse o synesis-graph creditaria o pacote errado. Agora leva o título deste
  pacote, com o abstract descrevendo os dois backends em vez de só o Neo4j. (O
  mesmo título copiado está no `synesis-lsp` e no `synesis-coder` — vale
  corrigir lá também.)
- **Matriz de compatibilidade corrigida nos dois READMEs.** Todas as linhas
  estavam defasadas: `synesis 0.5.5` (hoje 0.11.0), `synesis-coder 0.4.1`
  (0.8.0), `synesis-lsp 0.15.4` (0.22.0), `synesis-graph 0.2.0` (0.5.0), e a
  constraint `synesis>=` listada como `0.5.5` quando os três consumidores exigem
  `>=0.10.0`.
- **Seção de instalação do `README.pt.md` corrigida** — exigia *Python 3.11+*
  contra um pacote que declara `>=3.10`, clonava `synesis2neo4j` (nome anterior
  do repositório) e instalava dependências (`rich`, `tomli`) que não são as do
  pacote. Também não tinha matriz de compatibilidade; foi acrescentada,
  espelhando o README em inglês.

### Licença — MIT → AGPL-3.0-only + Synesis Data-Output Exception

- Acompanha a migração do compilador (`synesis` 0.10.0) — o synesis-graph
  importa `SynesisCompiler` no mesmo processo (`core.py`), o que aciona o
  copyleft da AGPL. Estudo completo:
  `synesis-planning/synesis/new_licence_policy.md`.
  - `LICENSE` (texto integral da AGPL-3.0) e `LICENSE.exception` replicados
    do core. A exceção importa especialmente aqui: o HTML de grafo gerado
    embute JavaScript/CSS autorais do Synesis (*Synesis Runtime Material*) e
    permanece livremente licenciável pela exceção.
  - `pyproject.toml`: sintaxe legada `license = {text = "MIT"}` substituída
    por `license = "AGPL-3.0-only AND LicenseRef-Synesis-data-output-exception"`
    (forma string do PEP 639 — a sintaxe de tabela legada compila sem erro
    mas emite silenciosamente o campo obsoleto `License:` em vez de
    `License-Expression:`) + `license-files = ["LICENSE", "LICENSE.exception"]`;
    `setuptools>=77`.
  - `CITATION.cff`, `README.md`/`README.pt.md` e o badge de licença
    atualizados nos dois idiomas.
  - Versões publicadas antes desta mudança (≤ 0.3.x) permanecem sob MIT.

### Adicionado

- **Campos de dataset TOML (`ON DATASET`) representados no grafo** — o
  `synesis` 0.10.0 introduz uma seção `dataset` no JSON, separada de
  `bibliography`, com os valores de campos SCOPE SOURCE resolvidos via
  `ON DATASET`. Esta versão faz esses valores chegarem ao grafo Neo4j
  como propriedades do nó Source e como entidades reificadas na linkagem
  multiprojeto, sem tocar em `_build_source_props` (função compartilhada e
  crítica de que os dois backends dependem).
  - `linkage.py`: novo `_merge_source_origins(data)` une `bibliography` e
    `dataset` por bibref antes de `resolve_linkage` resolver
    `IDENTIFIES`/`REFERS TO` — arestas entre projetos sobre campos
    `ON DATASET` agora funcionam.
  - `core.py`: novo `_merge_source_origins_payload(json_data)` aplicado na
    fronteira de `_build_graph_payload` — `bibliography` passa a ser a união,
    então `_extract_corpus_data → _build_source_props` capta campos de origem
    dataset como propriedades de nó. No-op para
    projetos sem seção `dataset`. Em colisão de nome de campo, `bibliography`
    prevalece (precedência histórica).

### Testado

- 4 testes novos em `test_linkage.py` cobrindo a união nos dois caminhos
  (linkagem e payload), comportamento no-op e precedência de colisão. Suíte
  completa após a remoção do GraphQLite: 245 aprovados, 1 pulado (fixture de
  estudo de caso ausente).

---

## [0.3.1] - 2026-07-15

### Corrigido

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

### Adicionado

- **Reificação de identidades entre projetos — `IDENTIFIES` / `REFERS TO`** (`synesis_graph/linkage.py`, `synesis_graph/core.py`, `synesis_graph/backends/neo4j.py`, `synesis_graph/cli.py`, `synesis_graph/pipeline.py`)
  - `--project` passa a ser repetível: `synesis-graph neo4j --project lattes.synp --project abstracts.synp` compila cada membro isoladamente e materializa as identidades declaradas. Cada membro continua sendo uma unidade de compilação independente — o agregado existe só neste comando, nunca no LSP.
  - **Nós reificados:** um nó por valor distinto de campo declarado `IDENTIFIES <entidade>`, com label derivado do rótulo (`researcher` → `:Researcher {entity_id}`), mais a aresta `(:Source)-[:IDENTIFIED_AS]->(:Entidade)` para o Source dono. O nó nasce **só** de `IDENTIFIES` — um `REFERS TO` órfão não cria nó stub, então um valor digitado errado não inventa uma entidade.
  - **Arestas:** `(:Source)-[:REFERS_TO {entity, member}]->(:Entidade)` para cada valor de `REFERS TO` casado com a chave primária. O rótulo da entidade vai como propriedade em vez de virar parte do tipo, então um único tipo serve a todas as entidades. Muitas arestas podem apontar ao mesmo nó (n:1); campo multi-valorado gera uma aresta por valor (n:n).
  - **Casamento por igualdade exata** pós-`trim` — sem *case-folding*, sem normalização silenciosa, sem fuzzy. Um valor que difere só em caixa permanece órfão em vez de ser fundido numa entidade que a fonte considera distinta.
  - **Bibrefs e ids de item são qualificados pelo alias do membro** (`abstracts:@artigo_a`). Dois corpora que compartilham um bibref — como `linkedin.bib` e `posts.bib` no corpus real — colapsariam num único nó, afirmando uma identidade que o dado nunca declarou. A junção de identidade passa exclusivamente por `IDENTIFIES`/`REFERS TO`.
  - Constraint de unicidade `REQUIRE e.entity_id IS UNIQUE` criada por label de entidade. Labels são validados antes de serem interpolados no Cypher (vêm do template do usuário e não podem ser parametrizados pelo driver).
  - `GraphPayload` ganha `entities` e `refers_to_edges` (ambos vazios por padrão): um projeto sem os modificadores não reifica nada e sincroniza exatamente como antes.
  - O backend `html` recusa múltiplos `--project` com erro claro: sua visão é um grafo de **conceitos**, e exibir nós de identidade ali exige um design de camada ainda não decidido.

---

## [0.2.5] - 2026-06-16

### Adicionado

- **HTML: barra de abas "Informações / Filtros" no sidebar** (`templates/graph.html.tmpl`)
  - Nova segunda fileira de abas abaixo da busca divide o sidebar em dois painéis: **Informações** (painel de info — padrão) e **Filtros** (filtro de degree + legenda). Libera toda a altura do sidebar para o painel de informações/evidências ao navegar pelos nós, mantendo os filtros a um clique de distância.
  - `setSidebarTab('info'|'filters')` alterna a visibilidade entre `#info-panel` e o novo container `#filters-tab`; estado `.active` rastreia a aba selecionada.
  - Accordion de degree, accordion de legenda e abas de agrupamento movidos para dentro de `#filters-tab` — DOM preservado, toda a inicialização JS (slider, renderLegend, toggleAccordion) funciona sem alteração.

### Removido

- **HTML: botão "Modo compacto" no HUD** (`templates/graph.html.tmpl`)
  - `#btn-compact`, `toggleCompact()` e a regra CSS `body.compact` removidos. Substituídos pela aba Filtros, que oferece solução mais limpa e permanente para o problema de espaço vertical.

---

## [0.2.4] - 2026-06-16

### Adicionado

- **HTML: botão "Modo compacto" no HUD** (`templates/graph.html.tmpl`)
  - Novo botão `#btn-compact` no HUD alterna `body.compact`, que oculta o filtro de degree (`#degree-accordion`) e a legenda (`#legend-wrap`), liberando todo o espaço vertical para o painel de informações — e para a tabela de evidências.
  - `toggleCompact()` segue o padrão de `toggleLock()`: toggle puro de classe, estado `.active` no botão. Sem alteração no backend nem no contrato de dados.

---

## [0.2.3] - 2026-06-16

### Adicionado

- **HTML: tabela de evidências dinâmica com colunas extras por projeto** (`synesis_graph/backends/html.py`, `templates/graph.html.tmpl`)
  - Constante JS `EV_ITEM_FIELDS` injetada pelo backend: lista ordenada dos nomes de campos extras do item (ex.: `zona`, `criterio_5a`, `score_sugerido`, `area_tematica`, `metodo`) que aparecem como colunas adicionais na tabela de evidências. Adapta-se automaticamente ao esquema de campos de cada projeto.
  - Constante JS `SOURCE_PROPS` injetada pelo backend: mapa de `ref → propriedades do bloco SOURCE` (nome, lattes_id, etc.); usado em `showInfo()` para exibir metadados da fonte no painel de informações.
  - Campos `anchor` e `analysis` extraídos de representações `ChainNode(...)` via `_parse_note_fields()` e renderizados como sub-linhas dentro da célula de anotação (sem coluna extra).
  - Labels de fonte nos registros de evidência agora exibem `nome`/`title`/`author` legíveis em vez de chaves de ref brutas; ref bruta armazenada em `_src_ref` para correspondência interna de arestas.

- **HTML: campos de item com valor de lista agora chegam à tabela de evidências** (`synesis_graph/core.py`)
  - Campos como `area_tematica` e `metodo` (armazenados como listas no corpus) eram descartados. Agora são unidos com `", "` e incluídos como campos dos registros de evidência.
  - `criterio_5a` e `score_sugerido` removidos do conjunto `_skip`; campos analíticos agora fluem até o HTML.

- **HTML: painel de informações reestruturado em zonas meta + tabela** (`templates/graph.html.tmpl`)
  - `#info-panel` é agora uma coluna flex com dois filhos: `#info-meta` (título e campos de metadados, encolhe até o conteúdo, `overflow-y: auto`) e `#info-table` (tabela de evidências, `flex: 1`, `overflow-x: scroll` sempre visível na parte inferior do painel).
  - Barra de rolagem horizontal da tabela de evidências agora é permanentemente visível, independente da posição na rolagem vertical — antes ficava soterrada ao final do conteúdo da tabela.
  - Helper JS `_setInfoTable(html)` escreve em `#info-table`; `showInfo`, `showEdgeInfo`, clique para desselecionar e reset de modo chamam `_setInfoTable('')` para limpar a zona da tabela quando não está no modo de evidência.

### Alterado

- **HTML: layout do painel de informações compacto e eficiente** (`templates/graph.html.tmpl`)
  - Seções acordeão para degree/legenda (`toggleAccordion()`): recolhidas por padrão, expandem ao clicar com transição suave de `max-height`.
  - Descrições longas de nós (>120 caracteres) limitadas a 3 linhas com toggle "ver mais / ver menos" (`toggleDesc()`).
  - Campos curtos de metadados renderizados em grade de duas colunas (`.field-grid`) para reduzir espaço vertical.
  - Tabela de evidências compacta: `table-layout: auto` com `min-width` por classe de coluna; `anchor`/`analysis` como sub-linhas dentro da célula de anotação em vez de colunas separadas.
  - Rodapé (`#stats`) limitado a uma única linha com `white-space: nowrap; text-overflow: ellipsis` — informações de versão na mesma linha que as estatísticas do grafo.

- **HTML: velocidade de zoom por scroll do mouse reduzida** (`templates/graph.html.tmpl`)
  - `interaction.zoomSpeed` definido para `0.3` (padrão era `1.0`) para zoom mais controlado, aproximando-se da sensação do trackpad.

---

## [0.2.2] - 2026-06-16

### Corrigido

- **Neo4j: perda de arestas RELATES_TO quando o mesmo par de conceitos tem múltiplos tipos de relação** (`synesis_graph/backends/neo4j.py`)
  - `MERGE (s)-[r:RELATES_TO]->(t)` sem `type` na chave fazia o segundo MERGE sobrescrever o primeiro quando dois CHAINs entre o mesmo par tinham tipos distintos (ex.: `APPLICATION` e `METHODOLOGICAL`).
  - Correção: chave de MERGE alterada para `MERGE (s)-[r:RELATES_TO {type: row.type}]->(t)` de modo que cada tipo produza uma aresta independente.
  - Resultado: a projeção GDS agora conta todas as arestas tipadas distintas entre pares de conceitos (ex.: 23 → 25 relacionamentos no corpus Lattes).

- **HTML: overlay "Loading graph…" permanente quando todos os conceitos são filtrados** (`templates/graph.html.tmpl`)
  - `stabilizationIterationsDone` nunca dispara quando `RAW_NODES = []`; o overlay de carregamento ficava visível indefinidamente.
  - Correção: adicionada guarda explícita — quando `RAW_NODES.length === 0` o overlay é removido imediatamente, sem aguardar o evento de rede.

- **HTML: RAW_NODES e EV_SOURCE_NODES usavam esquemas de campos inconsistentes** (`synesis_graph/backends/html.py`)
  - RAW_NODES emitia nomes sem prefixo (`community`, `degree`, `extra`) enquanto EV_SOURCE_NODES usava nomes com underscore (`_community`, `_degree`, `_extra`), exigindo remapeamento JS apenas para RAW_NODES.
  - Correção: ambos os tipos de nó unificados para usar campos com prefixo underscore (`_community`, `_community_name`, `_source_file`, `_file_type`, `_degree`, `_extra`). Inicialização do DataSet JS simplificada para `{ ...n, _onto: true }`.

- **HTML: troca de modo vazava estado `hidden` entre conjuntos de nós** (`templates/graph.html.tmpl`)
  - `setMode('ONTOLOGY')` restaurava nós ocultos com `filter: n => !!n.hidden`, reexibindo incorretamente EV_SOURCE_NODES (sempre ocultos no modo ONTOLOGY).
  - Correção: introduzida flag `_onto: bool` como identificador de modo ortogonal ao campo `hidden`. `setMode` e `switchGrouping` agora filtram exclusivamente por `_onto`.

- **HTML: `switchGrouping()` recolorava nós de evidência** (`templates/graph.html.tmpl`)
  - `nodesDS.getIds()` incluía todos os nós (ontologia + evidência); atualizações de cor de comunidade eram aplicadas aos EV_SOURCE_NODES.
  - Correção: `switchGrouping` agora filtra `nodesDS.get({ filter: n => !!n._onto })` antes de atualizar cores.

- **HTML: busca consultava o pool errado no modo EVIDENCE** (`templates/graph.html.tmpl`)
  - No modo EVIDENCE, a busca consultava `RAW_NODES` (nós de ontologia, possivelmente ocultos).
  - Correção: a busca agora usa `nodesDS.get({ filter: n => !n._onto && !n.hidden })` no modo EVIDENCE.

- **`_load_html_config` com defaults antigos hardcoded como fallback TOML** (`synesis_graph/config.py`)
  - Quando chaves estavam ausentes da seção `[html]`, `_load_html_config` usava `min_frequency=3`, `min_source_count=2`, `max_nodes=200`, `include_isolated=False` como fallback em vez de ler do `HTMLConfig()`.
  - Correção: valores de fallback agora derivam de `HTMLConfig()`, tornando os defaults da dataclass a única fonte da verdade.

### Alterado

- **Defaults do `HTMLConfig` alterados para exibir todos os dados por padrão** (`synesis_graph/config.py`)
  - `min_frequency`: 3 → 0 (sem filtro de frequência)
  - `min_source_count`: 2 → 0 (sem filtro de contagem de fontes)
  - `max_nodes`: 200 → 0 (ilimitado)
  - `include_isolated`: `False` → `True`
  - Justificativa: filtros são ferramentas de análise para aplicação interativa pelo usuário; o grafo deve exibir todos os dados disponíveis na primeira carga.

- **Paleta HTML substituída pelas cores do cheatsheet Synesis** (`synesis_graph/backends/html.py`, `templates/graph.html.tmpl`)
  - Paleta Tableau-10 antiga (`#4E79A7`, `#F28E2B`, etc.) substituída pela paleta do cheatsheet: navy `#1A3A5C`, slate `#3D5A7A`, sage `#4A6741`, terracotta `#8B4A3C`, gold `#A8905A`, amber `#C8963A`.
  - `_HTML_RELATION_COLORS` estendido com tipos Synesis-específicos: `ASSOCIATION=#A8905A`, `APPLICATION=#8B4A3C`, `METHODOLOGICAL=#3D5A7A`.
  - Objeto JS `RELATION_COLORS` no template atualizado para corresponder.

- **Modo claro agora é o padrão no HTML; modo escuro é opt-in** (`templates/graph.html.tmpl`)
  - CSS `:root` agora contém as variáveis do modo claro (fundo paper `#F7F4EF`, texto ink `#1C1C1E`, accent navy `#1A3A5C`).
  - Classe `body.dark` ativa o tema escuro. `body.light` não existe — o claro é a linha de base.
  - Flag `_isDark` inicia `false`; botão de tema inicializa com 🌙 (oferecendo o modo escuro).
  - Todos os valores hex escuros hardcoded no CSS substituídos por `var(--bg)`, `var(--track)`, `var(--accent)`, `var(--muted)`, etc.
  - Exportação PNG usa `_isDark ? '#0f0f1a' : '#F7F4EF'` para preenchimento de fundo.
  - Cor da fonte dos nós inicializada como `'#1C1C1E'` (ink); alternada para `'#e0e0e0'` no modo escuro.

### Adicionado

- **Bateria abrangente de testes HTML** (`tests/test_html_v2.py` — 56 novos testes em 12 classes)
  - `TestUnifiedNodeSchema`: consistência de nomenclatura de campos entre RAW_NODES e EV_SOURCE_NODES, ausência de campos legados sem prefixo, formato slug, exclusão mútua de IDs entre conjuntos de nós.
  - `TestOntoFlag`: injeção de `_onto: true/false` na inicialização do DataSet, `setMode` e `switchGrouping` filtrando por `_onto`.
  - `TestLightModeDefault`: fundo paper em `:root`, presença da classe `body.dark`, flag `_isDark`, botão inicial 🌙, fonte ink nos nós, preenchimento condicional no `exportPNG`.
  - `TestCheatsheetPalette`: todas as 6 cores do cheatsheet presentes, cores Tableau ausentes de `RELATION_COLORS`, tipos Synesis-específicos cobertos, cores dos nós vindas da paleta.
  - `TestHTMLConfigDefaults`: todos os quatro novos defaults abertos; corpus de fonte única exibe todos os conceitos; filtros restritos ainda funcionam quando definidos explicitamente.
  - `TestEmptyRawNodesGuard`: guarda de loading presente, `stabilizationIterationsDone` usado quando não vazio, arestas de evidência ainda populadas quando ontologia está vazia.
  - `TestEvidenceDedup`: mesmo item+tipo contado uma vez por conceito; tipos distintos no mesmo par produzem registros separados.
  - `TestEvMentionEdges`: campos obrigatórios, precisão tipo-cor, contagem de arestas bate com chains do payload, RAW_EDGES colapsa chains paralelos.
  - `TestAllGroupings`: chaves obrigatórias, cores da legenda vindas da paleta cheatsheet, resiliência com payload vazio.
  - `TestStatsText`: div de stats presente, "hidden by filter" exibido apenas quando filtragem está ativa.
  - `TestPlaceholderCompleteness`: todos os 10 placeholders do template substituídos, inclusive com payload vazio.
  - `TestDegreeSliderCSS`: CSS do slider usa `var(--track)`, `var(--accent)`, `var(--muted)` — sem valores hex hardcoded.

---

## [0.2.1] - 2026-06-12

### Adicionado

- **Flags de verbosidade `-v`/`-q` na CLI `synesis-graph`** (`synesis_graph/cli.py`, `synesis2graph.py`)
  - `-v` / `--verbose` (contagem): eleva o nível de log do logger `synesis2graph` para DEBUG. Repetível.
  - `-q` / `--quiet` (contagem): reduz para WARNING (`-q`) ou ERROR (`-qq`). Repetível.
  - Implementado via `_configure_logging(verbose, quiet)` em `synesis_graph/cli.py`; o shim delega para ele.
  - Seção `Global Options:` adicionada ao help de `cli.py` e `synesis2graph.py`.
  - `logger.setLevel(logging.INFO)` hardcoded removido de `synesis2graph.py` — nível agora controlado pela flag CLI.

### Alterado

- **`synesis2graph.py` refatorado em shim fino** (Fase 6)
  - Reduzido de 3.165 linhas para ~480 linhas. Toda implementação extraída para o subpacote `synesis_graph/`.
  - Novos módulos: `synesis_graph/sanitize.py`, `synesis_graph/ui.py`, `synesis_graph/core.py`, `synesis_graph/config.py`, `synesis_graph/metrics.py`, `synesis_graph/pipeline.py`, `synesis_graph/backends/neo4j.py`, `synesis_graph/backends/base.py`, `synesis_graph/backends/html.py`.
  - `synesis2graph.py` agora re-exporta todos os nomes públicos dos submódulos; `python synesis2graph.py --help/--version` e `from synesis2graph import run_pipeline` continuam funcionando sem alteração.

---

## [0.2.0] - 2026-06-12

### Adicionado

- **Estrutura de pacote instalável** (`synesis_graph/`, `pyproject.toml`)
  - `pyproject.toml` define o pacote `synesis-graph` com `click>=8.0` e `synesis>=0.5.5` como dependências principais; `neo4j>=5.0` e `graphqlite` como extras opcionais (`pip install synesis-graph[neo4j]`).

- **Toolchain de qualidade e CI** (`pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`)
  - `ruff==0.15.17` e `mypy==1.15.0` adicionados aos extras `dev`.
  - Workflow CI: `test` (pytest + cobertura), `lint`, `build` (wheel), `integration`.

- **Suíte de testes versionada** (`tests/`)
  - `tests/` removido do `.gitignore`; `conftest.py` e testes de fase rastreados.
  - Testes contrato: `test_cli.py` (CLI estrutural), `test_public_api.py` (API pública).

- **CLI baseada em Click** (`synesis2graph.py`, `synesis_graph/cli.py`)
  - Entry point `synesis-graph` registrado via `pyproject.toml`.
  - Três subcomandos: `neo4j`, `graphqlite`, `html` — cada um com `--help` e epílogo colorido.
  - Flags HTML (`--output`, `--group-by`, `--min-frequency`, etc.) movidas para o subcomando `html`.

### Alterado

- Repositório e pacote renomeados de `synesis2neo4j` para `synesis-graph`.

---

## [0.1.2] - 2025-02-01

### Adicionado

#### Campos Dinâmicos de Source
- **Suporte a SCOPE SOURCE:** Campos com `SCOPE SOURCE` definidos no Template (.synt) agora são transferidos dinamicamente como propriedades do nó `Source` no Neo4j
- **Extração Guiada pelo Template:** `analyze_template()` agora identifica e cataloga campos SOURCE junto com campos ONTOLOGY e ITEM
- **Propriedades Dinâmicas de Source:** `_build_source_props()` substituiu extração hardcoded por iteração dinâmica sobre campos SOURCE definidos no template
- **Fluxo de Dados Completo:** Nomes dos campos SOURCE são propagados por todo o pipeline: `analyze_template()` → `GraphPayload` → `_extract_corpus_data()` → `_build_source_props()`
- **Retrocompatibilidade:** Campos bibliográficos padrão (`title`, `author`, `year`, `doi`, `journal`, `abstract`) permanecem como fallback das entradas bibliográficas

---

## [0.1.1] - 2025-01-25

### Corrigido

#### Compatibilidade GDS (Neo4j GDS 2.x+)
- **gds.graph.drop:** Adicionado `YIELD graphName` para evitar warning do campo `schema` depreciado
- **gds.graph.project.cypher:** Substituída procedure depreciada pela nova API de função de agregação
  - Estratégia CO_TAXONOMY agora usa `gds.graph.project()` inline como agregação
  - Estratégia CO_CITATION agora usa `gds.graph.project()` inline como agregação
  - Execução mais eficiente dentro do fluxo Cypher

---

## [0.1.0] - 2025-01-24

### Adicionado

#### Pipeline Universal
- **Modelagem Dinâmica:** Labels de nós derivados automaticamente do Template (.synt)
- **Suporte a CODE:** Campos CODE criam nós de conceito com label dinâmico
- **Suporte a CHAIN:** Campos CHAIN criam relações RELATES_TO entre conceitos
- **Suporte a Taxonomias:** TOPIC, ASPECT, DIMENSION criam hierarquias navegáveis
- **Rastreabilidade:** Metadados de origem (source_file, line, column) em todos os nós

#### Métricas de Grafo
- **Métricas Nativas (Cypher puro):**
  - `degree`, `in_degree`, `out_degree` para conceitos
  - `mention_count`, `source_count` para conceitos
  - `concept_count` para taxonomias
  - `weighted_degree`, `aspect_diversity`, `dimension_diversity` para Topics
  - `item_count`, `concept_count` para Sources

- **Métricas GDS (opcional):**
  - `pagerank` - PageRank para relevância/centralidade
  - `betweenness` - Betweenness Centrality para nós "ponte"
  - `community` - Louvain para detecção de comunidades

- **Estratégias de Projeção:**
  - `RELATES_TO` - usa relações explícitas (templates CHAIN)
  - `CO_TAXONOMY` - conecta conceitos via taxonomia compartilhada
  - `CO_CITATION` - conecta conceitos via co-citação em Sources

#### Infraestrutura
- **Controle de Versão:** `--version` flag no CLI
- **Fallback Gracioso:** Métricas nativas sempre calculadas; GDS opcional com aviso
- **Sanitização:** Labels e nomes de banco validados contra Cypher injection
- **Transações Atômicas:** Sincronização via transação única

#### Integração MCP (Claude Desktop)
- **Configuração Universal:** Templates para Claude Desktop (`mcp/`)
- **Suporte Multi-Banco:** Namespaces para múltiplos projetos simultâneos
- **Documentação GraphRAG:** Guia de queries com rastreabilidade total
- **Estudo de Viabilidade:** Análise completa em `docs/MCP_VIABILITY_STUDY.md`

### Documentação
- README.md com tabela de mapeamento Template → Grafo
- Documentação completa de métricas (nativas e GDS)
- Diagrama Mermaid do fluxo de dados
- Exemplos de consultas Cypher
- Guia de configuração MCP (`mcp/SETUP.md`)
- Referência de queries Cypher (`mcp/QUERIES_REFERENCE.md`)
- Documentação bilíngue (EN/PT)

---

## Roadmap

### [0.2.0] - Planejado
- [ ] Servidor MCP customizado Synesis-específico
- [ ] Prompts otimizados para pesquisa qualitativa
- [ ] Interface de configuração interativa

### [0.3.0] - Futuro
- [ ] Interface web para visualização do grafo
- [ ] Exportação para formatos externos (GraphML, GEXF)
- [ ] Integração com Jupyter Notebooks

---

## Links

- **Repositório:** [github.com/synesis-lang/synesis2neo4j](https://github.com/synesis-lang/synesis2neo4j)
- **Documentação:** [synesis-lang.github.io/synesis-docs](https://synesis-lang.github.io/synesis-docs)
- **Issues:** [GitHub Issues](https://github.com/synesis-lang/synesis2neo4j/issues)
