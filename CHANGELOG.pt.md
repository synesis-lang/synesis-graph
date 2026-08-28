# Changelog

Todas as mudanças notáveis neste projeto serão documentadas neste arquivo.

O formato segue [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/),
e este projeto adere ao [Versionamento Semântico](https://semver.org/lang/pt-BR/).

**Idioma:** [English](CHANGELOG.md) | [Português](CHANGELOG.pt.md)

**Documentação:** [Synesis Language Docs](https://synesis-lang.github.io/synesis-docs)

---

## [Não lançado]

## [0.10.0] - 2026-08-27

### Added — a camada de sync do ArcadeDB passa a ser tipada por um contrato de transporte

O novo Protocol `ArcadeDBTransport` nomeia o que a camada de sync realmente exige
de uma conexão: `command`, `query`, `begin`, `commit`, `rollback` e o atributo
`database`. `backends/arcadedb.py` e `metrics_arcadedb.py` agora anotam contra ele,
não mais contra a classe concreta `ArcadeDBClient`.

Nada muda em tempo de execução — é uma mudança de tipos. O que ela compra é espaço
para um segundo transporte: o motor embedded, in-process, fala o mesmo Cypher e o
mesmo SQL, e com a camada de sync tipada pelo contrato, acrescentá-lo não toca
nenhuma query, nenhuma instrução de schema, nenhuma das oito funções `_sync_*`.

- **Tipagem estrutural, para o custo recair sobre quem chega.** `ArcadeDBClient`
  satisfaz o Protocol sem herdar dele, sem registro e sem importá-lo. Uma classe-base
  abstrata obrigaria a mexer no cliente HTTP — em produção — para beneficiar um
  backend que ainda não existe.
- **Deliberadamente menor que o cliente.** `is_ready`, `list_databases`,
  `create_database` e `close` ficam de fora: são preflight, setup e teardown, vivem
  no adapter, e são justamente onde os dois transportes divergem (um banco
  in-process não tem servidor a sondar nem credencial a exercitar).
- `language` continua keyword-com-default, em vez de seguir a assinatura
  posicional-primeiro do motor embedded, para que os 20 call sites sigam
  funcionando sem alteração.
- A conformidade é trancada por testes que comparam as duas superfícies de forma
  mecânica (`inspect.Signature`), e não por reescrever as assinaturas à mão — uma
  expectativa copiada à mão desanda tão fácil quanto o código. Verificado que
  falha diante de divergência real.

### Added — um transporte ArcadeDB in-process (`ArcadeDBEmbeddedClient`)

O `arcadedb-embedded` traz o motor real mais uma JRE embutida, de modo que um grafo
pode ser gerado e consultado sem nada instalado além do `pip` — sem Java, sem
servidor, sem porta. Esta classe o adapta ao `ArcadeDBTransport`, o que faz a camada
de sync rodar sobre ele sem alteração: nenhuma query reescrita, nenhuma instrução de
schema tocada, nenhuma das oito funções `_sync_*` modificada.

Ela existe para absorver cinco divergências entre o binding e o cliente HTTP. Todas
foram medidas contra o `arcadedb-embedded` 26.8.1, e todas são falhas **silenciosas**
se deixadas a cargo de quem chama — o código roda, não levanta erro, e devolve
resultado errado:

- **`language` é o primeiro argumento posicional do binding.** Esta classe mantém a
  assinatura keyword-com-default do cliente; seguir o binding faria toda chamada não
  qualificada da camada de sync executar na linguagem errada.
- **Uma escrita devolve `None`, não um resultado vazio.** A camada de sync itera o
  resultado direto, então `None` vira `[]` aqui, e não um `TypeError` num call site.
- **`ResultSet` é de passe único.** Lê-lo duas vezes devolve `[]` na segunda, sem
  erro. Os resultados são materializados nesta fronteira — o único lugar que sabe
  que o cursor ainda não foi lido.
- **Passar `None` como parâmetros levanta `Ambiguous overloads`** no JPype, que não
  resolve a sobrecarga Java a partir de um nulo. O argumento é omitido por completo
  quando não há parâmetros. Esta pareceria intermitente: só dispara em instruções
  sem parâmetros.
- **O binding levanta seu próprio `ArcadeDBError`, de mesmo nome,** em outro módulo.
  Nove pontos `except ArcadeDBError` esperam a classe deste pacote; sem tradução,
  cada um deixaria um erro do motor escapar sem tratamento, passando ao largo do
  código escrito para reportá-lo.

A dependência opcional é importada preguiçosamente e reportada como `ArcadeDBError`
acionável — o mesmo padrão que o provedor de embeddings já usa — para que os demais
backends sigam funcionando quando o extra estiver ausente.

### Added — configuração `[arcadedb-embedded]` e o nome do backend

`ArcadeDBEmbeddedConfig` e a constante de backend `arcadedb-embedded`. O adapter
que os consome vem em seguida; aqui está a superfície de configuração.

- **Seção própria, não reaproveitamento de `[arcadedb]`.** Compartilhar aquele
  bloco deixaria `uri`, `user` e `password` no arquivo, lidos por ninguém — um
  campo que parece respeitado e não é constitui o formato de defeito que este
  projeto insiste em pagar. Uma seção distinta torna o modo visível no arquivo.
- **Todo campo é opcional, e a seção também — e o arquivo.** Não há credencial a
  fornecer nem host a alcançar, então os defaults já descrevem uma configuração
  funcional; exigir um arquivo cujo cada valor repete um default é atrito sem
  nada por trás. Seções malformadas continuam sendo reportadas, porque são erros
  de digitação, não omissões.
- **`db_path` é a raiz do servidor, não o diretório do banco.** O banco é criado
  em `<db_path>/databases/<projeto>`, que é onde o servidor do ArcadeDB procura.
  Apontá-lo um nível abaixo produz a pior falha disponível aqui: o servidor sobe,
  registra o endpoint MCP, reporta sucesso e não encontra banco nenhum — nada dá
  erro, o corpus fica simplesmente invisível. O layout é derivado por um único
  método, de modo que quem grava o grafo e quem o serve não podem divergir.
- A raiz padrão é `.`, não `./databases`: a raiz já ganha um filho `databases/`, e
  `databases/databases/face85` parece um bug — o que convida à "correção" que cai
  exatamente na falha silenciosa acima.
- `[arcadedb_embedded]` (sublinhado) também é aceito, para quem conhece
  `[tool.ruff]`.

### Added — o adapter do backend embedded, sobre uma base compartilhada com o HTTP

`ArcadeDBEmbeddedBackendAdapter` fecha o caminho local: um projeto agora exporta
para um diretório, sem servidor, sem porta e sem Java instalado.

- **`_ArcadeDBAdapterBase` concentra tudo depois da conexão.** O motor é o mesmo
  seja qual for a via de acesso, então limpeza, sync, métricas e close são
  escritos uma vez. Só `preflight`, `connect` e `prepare_destination` divergem, e
  cada um é mais simples aqui pelo mesmo motivo: não há servidor no caminho.
- **As duas perguntas "isto é ArcadeDB?" do pipeline passam a ter uma resposta
  só.** Ele pergunta para pendurar o sidecar de embeddings e para registrar a
  ressalva de escopo das métricas, e ambas valem para qualquer transporte — mesmo
  motor, mesma limitação de grafo inteiro do `algo.*`. Nomear os dois adapters em
  cada ponto colocaria essa regra em dois lugares a manter em sincronia; nenhuma
  terceira verificação foi acrescentada.
- **`connect` deliberadamente não faz nada.** O adapter HTTP abre um cliente ali
  porque seu alvo existe independentemente do projeto; um banco embedded *é* um
  diretório com o nome do projeto, então a abertura espera o payload.
- **`preflight` verifica o que pode de fato falhar** — se o pacote opcional está
  instalado e se a raiz é gravável. Ambos reportados antes da compilação, o que
  num corpus de 41 mil itens é a diferença entre perder um minuto e perder uma
  hora.
- `prepare_destination` deriva o diretório do `ArcadeDBEmbeddedConfig` em vez de
  montar o caminho por conta própria, para que a exportação e o futuro lado
  servidor não possam divergir quanto ao layout.
- O adapter HTTP passa a declarar seu `client` como o `ArcadeDBClient` concreto:
  `create_database` é operação de servidor, ausente do contrato de transporte por
  desenho. Estreitar na subclasse mantém essa chamada verificada sem alargar o
  contrato para um transporte que não tem servidor a quem enviá-la.

### Added — `synesis-graph arcadedb-embedded`

O caminho local vira um comando. Um projeto exporta para um diretório sem
servidor no ar, sem porta a administrar e sem Java instalado:

```
synesis-graph arcadedb-embedded --project project.synp
```

- **`--config` é opcional aqui.** Não há credencial a fornecer nem host a
  alcançar, então os defaults descrevem uma configuração funcional; o comando
  roda num diretório sem `config.toml` nenhum.
- **`--db-path` é a raiz do servidor**, e a ajuda diz isso — o banco é criado em
  `<DIR>/databases/<nome_do_projeto>`. Apontá-lo direto para um diretório de banco
  é o engano que faz o lado servidor subir, reportar sucesso e não achar nada;
  por isso a flag documenta o layout em vez de supor que ele seja conhecido.
- **Fica em "Graph Backends" no `--help`**, junto dos outros três: exporta e sai.
  Um comando de longa duração não caberia ali, que é precisamente o motivo de o
  agrupamento existir.
- `--vector-embeddings` e `--rebuild-embeddings` funcionam como no backend de
  servidor, e a trava contra reconstruir sem campos cita
  `[arcadedb-embedded.embeddings]` — não `[arcadedb]`, que é outro modo.

Overrides de CLI agnósticos de backend (`cli_overrides`) substituem o que era um
mecanismo exclusivo do HTML. Uma flag que o usuário não passou continua `None` e
nunca desloca um valor configurado, de modo que uma flag não usada não pode
zerar o arquivo em silêncio.

### Added — `synesis-graph serve`, e o grafo local alcança os clientes de chat

Fase B. Os comandos de exportação gravam um grafo e encerram; este abre um grafo
já construído e o mantém acessível, para que Claude Desktop, Claude Code ou a
extensão do VSCode possam perguntar a ele:

```
synesis-graph serve
```

O motor faz quase tudo sozinho — o `arcadedb-embedded` traz o servidor ArcadeDB
real e descobre seu plugin MCP automaticamente. Três coisas que ele **não** faz
são o motivo de isto ser um comando, e não um trecho a colar:

- **O MCP nasce desabilitado.** A distribuição embedded não traz o
  `config/mcp-config.json` que o servidor standalone lê, então o plugin registra
  e em seguida recusa toda chamada. A configuração vive no servidor em execução,
  não em disco, de modo que habilitá-la precisa acontecer a **cada** start.
- **Somente-leitura não é o padrão.** Escritas ficam desligadas a menos que
  `--allow-writes` diga o contrário: um corpus são meses de trabalho de
  codificação, e lê-lo é o caso de uso. Cada flag de permissão é declarada em vez
  de herdada, para que um default futuro do motor não possa alargar em silêncio o
  que um cliente de chat pode fazer. `allowAdmin` permanece desligado mesmo com
  `--allow-writes` — chamadas administrativas alcançam além do corpus, o próprio
  servidor.
- **Uma senha precisa existir e não pode ser anotada.** Uma é gerada por sessão e
  impressa junto da entrada `mcpServers` a colar; `SYNESIS_DB_PASSWORD` mantém uma
  entre reinícios, para a configuração do cliente seguir válida. Nada é gravado em
  arquivo do projeto.

Servir uma raiz sem banco algum sob `databases/` é recusado, com o comando de
exportação que resolve. Essa checagem é o contrato de layout visto do outro lado:
um servidor iniciado sobre o diretório errado sobe tranquilamente, registra o MCP
e responde toda query sem nenhuma linha.

O `--help` agora agrupa os comandos. Backends exportam e saem; `serve` publica e
permanece — distinção que vale mostrar, porque é o que mantém o papel do módulo
legível conforme ele cresce.

### Added — o backend local validado num corpus real

**Validado contra o corpus Quinto_Andar** — 41.474 itens, 7.293 conceitos, 661
fontes, exportados em 113s sem servidor algum no ar. As **16** contagens de nós e
relações batem exatamente com a exportação do Neo4j Aura: `Item` 41.474,
`FROM_SOURCE` 41.474, `MENTIONS` 61.796, `RELATES_TO` 19.126, `GROUPED_BY` 7.293,
e todos os demais rótulos e arestas de taxonomia. O `ProjectContext` carrega as
mesmas contagens, e o backend embedded ainda declara sua capacidade full-text, o
que o do Neo4j não faz.

No corpus face85 os dois transportes foram comparados diretamente, e os vetores
são o detalhe decisivo: os mesmos 210 conceitos, as mesmas 49 comunidades,
PageRank coincidindo até a oitava casa decimal, e o `vectorNeighbors` devolvendo
os mesmos vizinhos na mesma ordem. Os vetores gravados são idênticos byte a byte.

Este é o critério contra o qual toda a série embedded foi escrita: o mesmo grafo,
a partir do mesmo projeto, sem Java e sem servidor de banco em momento algum.

Os READMEs (EN e PT) documentam o terceiro backend, quando preferi-lo ao de
servidor, e o comando `serve`.

### Changed — o motor de grafo local passa a vir junto com o pacote

`pip install synesis-graph` já traz o motor ArcadeDB in-process. Não há extra a
lembrar, nem Java a instalar: o wheel carrega a própria JVM.

Quem decidiu isso foi o público. "Instale o extra certo" é mais uma etapa para
errar antes de ver qualquer resultado, e quem tem mais chance de errar é quem tem
menos ferramentas para diagnosticar. ~67 MB é um preço menor do que essa fricção.
Quem usa só Neo4j ou HTML passa a carregar peso que não usa — troca deliberada,
decidida a favor do pesquisador.

`synesis-graph[arcadedb-embedded]` continua resolvendo, então instruções antigas
seguem funcionando.

### Changed — o terminal fala com o pesquisador, não com o banco

Exportar um corpus imprimia sessenta linhas de detalhes internos do motor — cada
índice construído, cada sub-índice dividido, cada página escrita — com as três
linhas que importavam soterradas no meio. Pior: `WARNI` e `Building index
'Item_0_406270...'` pareciam indicar que algo deu errado.

O logging do motor agora é configurado para avisos, e as duas linhas de
inicialização da JVM sobre as quais não há nada a fazer são filtradas. Qualquer
outra coisa que o Java disser continua chegando ao terminal: uma falha real nunca
é engolida, e isso está trancado por teste.

Os rótulos das etapas mudaram junto, de implementação para intenção:

| Antes | Agora |
|---|---|
| `Compiling Project (In-Memory)` | `Reading your project` |
| `41474 items compiled` | `41.474 coded excerpts read` |
| `Synchronizing Graph (Transactional)` | `Building the graph` |
| `Calculating Native Metrics` | `Measuring the graph` |
| `Calculating Graph Algorithms` | `Finding central concepts and communities` |
| `Target database: …` | `Graph location: …` |

- A ressalva das métricas passa a ter **duas redações**: o `ProjectContext` mantém
  a precisa, que nomeia `algo.*`, porque quem a lê é um programa prestes a ranquear
  conceitos por esses números; o terminal recebe a versão simples, porque quem a lê
  precisa saber apenas que esses números não se comparam aos de uma exportação Neo4j.
- Uma exportação concluída agora diz **onde o grafo está e qual é o passo seguinte**
  — ela é meio, não fim, e um pesquisador deixado com um diretório não tem como
  adivinhar que o `serve` existe.
- `SUCCESS em 3s` tinha uma palavra solta em português numa interface em inglês.

### Fixed — um segundo `serve` no mesmo grafo falhava com HTTP 403

O motor honra `root_password` apenas enquanto cria o
`config/server-users.jsonl`. Todo start posterior lê o hash gravado e ignora o
que recebe — em silêncio. O `serve` gerava senha nova a cada sessão, então a
primeira execução funcionava e todas as seguintes autenticavam contra uma
credencial que ninguém tinha: o servidor subia, reportava sucesso e respondia
`User/Password not valid`.

- A senha agora é **gerada uma vez e lembrada** junto do estado do próprio
  servidor, então reinícios seguem funcionando sem variável a definir. O
  `SYNESIS_DB_PASSWORD` continua servindo para escolher a sua.
- Uma credencial já gravada mas **desconhecida por todos** — o estado deixado por
  qualquer versão anterior — é **resetada em vez de reportada**. Ela não protege
  nada: um servidor local, alcançável só desta máquina, cuja senha existe porque
  o motor exige uma. Recusar seria entregar uma tarefa cuja única resposta
  possível é sim. O arquivo antigo é posto de lado como `.superseded`, nunca
  apagado, e `databases/` não é tocado.
- Uma senha abaixo do mínimo de 8 caracteres do motor passa a ser recusada
  **antes** de subir, nomeando a variável que a definiu.
- Uma falha ao iniciar não acrescenta mais "a porta está em uso?" a uma mensagem
  do motor que nada tem a ver. O palpite só aparece quando o erro cita a porta.

### Changed — o `--install` nomeia o cliente, e o VS Code passa a ter suporte real

O VS Code lê outro formato: `servers` em vez de `mcpServers`, HTTP direto com um
objeto `headers`, e nenhuma ponte `npx`/`mcp-remote`. O snippet anterior dizia
servir "Claude Desktop, Claude Code ou a extensão do VSCode" enquanto emitia um
formato que o VS Code ignora em silêncio — sem erro, o servidor simplesmente
nunca aparece.

- `--install claude-desktop` e `--install vscode` gravam cada um o seu formato. O
  do VS Code vai para `.vscode/mcp.json` no diretório atual: uma entrada que
  nomeia porta e senha pertence ao projeto para o qual o servidor foi iniciado,
  não a toda janela que o editor abrir.
- **Instalar passou a ser opt-in.** Foi brevemente o padrão, e era errado: editar
  a configuração de outro aplicativo é decisão do pesquisador, e permissões de
  arquivo variam por plataforma de um jeito que nenhum padrão pode presumir.
- O local só é afirmado para as duas plataformas com build oficial do Claude
  Desktop. No resto, a entrada é impressa em vez de escrita num caminho chutado, e
  o `SYNESIS_MCP_CONFIG` sobrepõe em qualquer lugar.
- Os dois instaladores preservam cada entrada existente e cada chave de topo,
  fazem backup antes, e recusam em vez de sobrescrever um arquivo que não
  conseguem interpretar. Um diretório de configuração ausente é reportado, nunca
  criado — isso configuraria um aplicativo que não está instalado.

### Fixed — a ajuda deixou de descrever o comando

Dois exemplos tinham desandado, um deles quebrado: o `--install` virou opção de
escolha e o epílogo seguia ilustrando-o sem valor, o que agora falha com "Option
'--install' requires an argument". O texto com maior chance de ser copiado era
justamente o que não funcionava mais. O exemplo da senha ainda mandava definir
`SYNESIS_DB_PASSWORD` para os reinícios sobreviverem, o que a correção acima
tornou desnecessário.

Os dois estão corrigidos, e **todo exemplo de todo epílogo passa a ser conferido
contra os parsers reais** por um teste. Esta é a segunda vez que um exemplo do
`serve` estava errado; uma correção pontual não teria impedido a terceira.

### Fixed — o exemplo do `serve` apontava para o diretório errado

O `synesis-graph serve --help` ilustrava `--db-path ./databases`. Tanto a
exportação quanto o `serve` acrescentam o nível `databases/` por conta própria,
então seguir o exemplo faz o servidor procurar em `databases/databases` — e um
servidor sobre a raiz errada não reclama: ele sobe, registra o endpoint MCP e
responde toda query sem nenhuma linha.

O exemplo agora passa a mesma raiz usada na exportação, e mostra os dois comandos
lado a lado para a simetria ficar visível. Um teste tranca isso, porque um exemplo
é o texto com maior chance de ser copiado literalmente.

O `synesis-graph arcadedb-embedded --help` também passa a apontar o `serve` como
passo seguinte — exportar um grafo e consultá-lo de um cliente de chat são duas
metades da mesma tarefa.

### Fixed — um template pode declarar um campo chamado `title` sem quebrar o sync

As listas de propriedades dos índices full-text passam a ser deduplicadas,
preservando a ordem de primeira ocorrência.

Os dois backends montam o índice de `Source` prefixando as propriedades
bibliográficas estruturais (`title`, `abstract`) aos campos TEXT SOURCE do
próprio template. Nada impede um template de declarar um desses nomes, e fazê-lo
é legítimo — o Quinto_Andar declara `FIELD title TYPE TEXT SCOPE SOURCE` para o
nome do candidato, o mesmo lugar preenchido a partir de outra origem. O
resultado era um índice composto nomeando `title` duas vezes.

- **O Neo4j recusava de saída**, com `RepeatedPropertyInCompositeSchema` — e o
  fazia *depois* de compilar 41.474 itens e sincronizar o grafo, já que a
  criação dos índices vem por último. A execução inteira falhava no fim, sem
  nada gravado.
- **No ArcadeDB a falha era silenciosa**: ele aceita o composto e indexa a mesma
  coluna duas vezes. Pior, `_declare_fulltext` deriva da mesma lista, de modo
  que a duplicata chegava a `ProjectContext.fulltext_source_fields` e ensinaria
  ao consumidor um nome de `SEARCH_INDEX` que não corresponde ao índice
  realmente criado.
- A mesma colisão existia no índice de conceitos, onde `search_name` é
  prefixado a `scalar_fields`.
- A ordem é preservada, não ordenada: a propriedade estrutural é a primeira
  ocorrência, e `SEARCH_INDEX` endereça o índice composto pelo nome ordenado
  completo.

A deduplicação vive num único helper, `core.dedupe_index_props()`, usado pelos
três pontos de derivação — uma segunda implementação da mesma regra é livre para
discordar da primeira.

## [0.9.0] - 2026-08-25

### Added — as métricas de rede declaram backend e escopo

`ProjectContext` passa a carregar `metrics_backend` e `metrics_scope`.

Não é burocracia: muda como o número deve ser lido. As procedures `algo.*` do
ArcadeDB não aceitam filtro de escopo e rodam sobre o **grafo inteiro**, então o
PageRank de um conceito incorpora suas arestas para Items, Sources e taxonomias;
o GDS do Neo4j projeta apenas o subgrafo de conceitos. Os dois não são
comparáveis, e o consumidor que ordena "conceitos mais centrais" não tinha como
saber disso pelo escore.

A ressalva já existia em `metrics_arcadedb.SCOPE_NOTE` — chegava à saída da CLI
e nunca ao grafo.

- Declarado **antes de o sync gravar o contexto**, porque as métricas em si
  rodam depois dele; qual backend vai calculá-las já se sabe antes.
- O bloco em prosa também diz que centralidade é uma **escolha metodológica** —
  grau, PageRank e betweenness respondem perguntas diferentes — e que o
  consumidor precisa dizer qual usou.

### Added — o grafo declara sua capacidade de busca lexical

`ProjectContext` passa a carregar `fulltext_concept_fields`,
`fulltext_item_fields`, `fulltext_source_fields` e `fulltext_analyzer`.

Os índices já existiam — este backend os cria sobre o nome humanizado do
conceito e os campos de texto do template, com analyzer Lucene configurável. O
que faltava é que **o consumidor não tinha como descobrir**: `get_schema` lista
propriedades, não índices. Por isso o chat contornava em linguagem natural um
problema que esta camada já resolvia — mandava o modelo cortar o termo antes do
primeiro caractere acentuado, porque `CONTAINS 'psicologicos'` não encontra
`psicológicos`.

- A **lista exata de campos** é declarada, não um booleano: `SEARCH_INDEX`
  endereça um índice composto pelo nome inteiro —
  `Concept[search_name, ontology_description]` —, então saber apenas que "existe
  um índice" não permite formar a chamada.
- O **analyzer faz parte do contrato.** `StandardAnalyzer` não faz stemming nem
  dobra acento; `brazilian` faz os dois. O bloco em prosa diz qual é, em vez de
  deixar o consumidor apresentar a busca como insensível a acento quando não é.
- Declarada a partir das **mesmas listas que alimentaram o `CREATE INDEX`**, de
  modo que a declaração não pode divergir do que existe.
- **Só no ArcadeDB.** O Neo4j também tem full-text, mas consultado por
  `db.index.fulltext.queryNodes`; anunciar a sintaxe de um backend para o grafo
  do outro ensinaria uma consulta que sempre falha.

### Changed — rastreabilidade: raiz explícita, e omissão em vez de vazamento

`relative_source_file()` passa a **omitir** o `source_file` quando a
relativização falha, em vez de cair no caminho absoluto.

Preservá-lo estava documentado como "uma verruga, nunca uma quebra". É pior que
uma verruga: o caminho absoluto vaza a estrutura de diretórios de quem exportou
para todos com quem o grafo é compartilhado, e não resolve na máquina de quem lê
— ou seja, a âncora que ele produz é um link que não abre. Um `source_file`
ausente é honesto; um errado promete verificação e falha.

- A raiz agora é **passada explicitamente** por `compile_project()` e por cada
  membro de um estudo ligado, que a conhecem: o `.synp` está nela. A inferência
  pela redundância `project.includes[]` / `traceability.file` continua como
  fallback do `load_json_project()`, onde o diretório original pode não existir
  mais.
- Num estudo ligado cada membro recebe **sua própria raiz** antes do
  `merge_payloads` — os projetos vivem em diretórios diferentes, e não há raiz
  única correta para todos.
- A checagem de contenção compara **componentes de caminho**, não o prefixo cru:
  `D:/proj-evil` começa com `D:/proj` como string mas não está dentro dele.

### Added — `Item.annotation_id`: a unidade de contagem vira consultável

Cada `Item` passa a carregar a identidade do **bloco anotado** de onde veio,
compartilhada por todos os itens que aquele bloco produziu.

Um bloco `ITEM` com quatro chains gera quatro vértices `Item` — quatro unidades
analíticas sobre um trecho anotado. As duas contagens respondem perguntas
diferentes e ambas são legítimas; ler uma como se fosse a outra não é, e foi
isso que fez uma auditoria contradizer uma resposta correta (relatou 11 onde o
banco tinha 20).

Sem esta propriedade a distinção não era expressável numa consulta: contar
trechos exigia adivinhar por agrupamento de arquivo e linha.

| Unidade | Expressão |
|---|---|
| fontes | `count(DISTINCT s.bibtex)` |
| trechos anotados | `count(DISTINCT i.annotation_id)` |
| itens analíticos | `count(DISTINCT i.item_id)` |
| menções | arestas `MENTIONS` |
| conceitos | `count(DISTINCT c.name)` |

- Construída a partir de `corpus_id`, que já estava disponível onde os sufixos
  do `item_id` (`_c0001`/`_n0001`) são acrescentados — nenhuma inferência por
  texto ou linha.
- Entra no **conjunto protegido**: um template livre para nomear um campo
  `annotation_id` não pode reescrever a que bloco um trecho pertence.
- **Omitida, não nula**, quando ausente — a regra que o par de rastreabilidade
  já segue.

### Changed — `Item.source_line` declarada `INTEGER` no schema do ArcadeDB

Ficava sem declaração porque `_declare_property` só escrevia `STRING`, que o
ArcadeDB recusa para um inteiro. O helper tipado removeu essa limitação, e uma
propriedade não declarada é **invisível ao `get_schema`** — que é como o chat
descobre o que o grafo oferece.

### Adicionado — a trilha de auditoria chega ao grafo (`Item.source_file`, `Item.source_line`)

Todo `Item` passa a carregar o arquivo `.syn` e a linha em que foi anotado. Um
consumidor vai do conceito ao trecho, à referência, e finalmente **à linha que o
pesquisador escreveu** — o assistente de chat transforma isso em link clicável.

O dado já existia e era jogado fora. O compilador emite `traceability: {file,
line}` no JSON canônico, e CSV/XLS o exportam como `source_file`/`source_line`;
o grafo era o **único exportador que o descartava**, e por isso uma resposta
apoiada no grafo não conseguia dizer de onde veio.

- O caminho é gravado **relativo à raiz do projeto**, inferido da redundância
  entre `project.includes[].path` (relativo) e `traceability.file` (absoluto).
  Um caminho absoluto vazaria a estrutura de diretórios de quem exportou e não
  resolveria para quem lê o grafo.
- O par é **omitido, não nulo**, quando o item do corpus não tem localização —
  assim `WHERE i.source_file IS NOT NULL` continua honesto.
- `source_file` e `source_line` entram no conjunto de chaves estruturais que um
  campo de template homônimo não pode sobrescrever — a mesma proteção que
  `citation` já tinha.
- Neo4j não precisou de mudança (`SET i = row`); o ArcadeDB declara
  `source_file` para que a propriedade apareça na introspecção do schema.

### Adicionado — o grafo declara sua capacidade de busca semântica

`ProjectContext` passa a registrar quais campos da ontologia foram embedados, com
qual modelo e quantas dimensões, sempre que o sync roda com
`--vector-embeddings`.

Um cliente já via pelo `get_schema` que existe um índice vetorial, mas não **de
qual campo os vetores vieram** — e isso muda o que a proximidade significa: por
`ontology_description` é semelhança conceitual, por `topic` é coocorrência
temática.

- Declarada a partir do sidecar de embeddings, **nunca inferida do índice**: um
  índice sobrevive a um re-sync sem vetores, e lê-lo anunciaria algo que o dado
  já não tem.
- Declaração parcial é recusada — o consumidor consultaria por uma composição de
  campos que nunca existiu.
- O resumo em prosa também informa que **a proximidade vetorial é aproximada**:
  um vizinho é sugestão de leitura, não algo que o pesquisador afirmou.

### Adicionado — métricas de rede declaradas no schema do ArcadeDB

As oito métricas que o sync já calcula (`pagerank`, `betweenness`, `community`,
`degree`, `in_degree`, `out_degree`, `mention_count`, `source_count`) passam a
ser declaradas, e portanto visíveis à introspecção via MCP.

Estavam sendo gravadas e permaneciam invisíveis: perguntado pelos conceitos mais
centrais, um cliente MCP contou arestas à mão e produziu um ranking de *grau*,
porque não tinha como saber que `pagerank` já estava lá. Os dois rankings
divergem — num corpus real, os dois primeiros por grau não estão no top-5 por
PageRank.

- `_declare_property()` aceita um tipo declarado, com whitelist. `pagerank` e
  `betweenness` são DOUBLE, os demais INTEGER; declará-los STRING faria o
  servidor recusar o valor no sync.
- Tipos aceitos verificados contra o ArcadeDB 26.7.3.

## [0.8.0] - 2026-08-23

### Adicionado — o grafo exportado agora se descreve (`ProjectContext`)

Todo sync para um backend de banco grava um vértice `ProjectContext` com o
contexto do próprio projeto. **O contexto viaja com os dados, não com a
ferramenta**: qualquer consumidor do grafo o recebe — Claude Desktop, qualquer
cliente MCP, o studio do próprio banco, ou um colega que receba uma cópia.

O problema que resolve: o grafo exportado era *sintaxe sem semântica*. Um
consumidor que fizesse introspecção do schema descobria que o vértice `Aspect`
tem uma propriedade `name`, mas não que `Aspect` é a escala modal de Dooyeweerd,
que seus valores são **ordenados**, nem o que significa `[15] Fiducial`. Tudo
isso está declarado no template e era descartado na exportação.

Propriedades gravadas:

| Propriedade | Conteúdo |
|---|---|
| `description` | o bloco `DESCRIPTION` do `.synp`, literal |
| `project_summary` | metadados, tamanho do corpus e proveniência, em prosa |
| `template_doc` | o template como documento legível: cada campo com tipo, escopo, descrição, escala de valores e **GUIDELINES**, mais as regras de preenchimento e uma seção **`## Como navegar o grafo`** que nomeia cada aresta com sua direção |
| `concept_label`, `template_name`, `project_name` | identificadores |
| `source_count`, `item_count`, `concept_count` | inteiros, consultáveis sem parse |
| `compiler_version`, `synesis_graph_version`, `compiled_at`, `generated_at` | proveniência |

Nada de novo é extraído do compilador: o JSON canônico já trazia tudo. O
`prepare_payload` lia o objeto `project` apenas para pegar o nome.

**Gravado em Markdown, não JSON.** Medido pelo caminho MCP real contra um corpus
de 210 conceitos: como JSON, as especificações de campo chegavam ao modelo como
~7,3 mil tokens em que **53% das chaves valiam `null`**, com as GUIDELINES —
escritas pelo pesquisador com títulos e quebras de linha — escapadas dentro de
uma string. O mesmo conteúdo em prosa é menor, dispensa parse e preserva a forma
que o pesquisador lhe deu.

As `GUIDELINES` são a parte de maior valor: são o **protocolo de codificação**,
com regras de decisão explícitas e exemplos ("não inclua nome próprio", "1–3
frases"). Respondem o que nenhum schema responde — *por que* um dado está assim,
e o que conta como instância válida de um campo. Até agora viviam só no `.synt` e
não deixavam rastro no grafo.

- **Backends:** ArcadeDB (TCP/HTTP) e Neo4j. O backend HTML fica de fora de
  propósito — é artefato de visualização, sem consumidor programático, e para
  quem lê na tela o contexto já está implícito.
- **As contagens são medidas do que chega ao grafo**, nunca copiadas do
  `export_metadata` do compilador: os contadores dele respondem outra pergunta
  (seu `item_count` conta blocos SOURCE, não os vértices `Item` que o sync
  grava), então guardá-los produziria uma propriedade que *parece* verificável
  contra o grafo e discorda dele em silêncio.
- **Chaves `location` são removidas recursivamente.** São caminhos absolutos da
  máquina que compilou o projeto, inúteis a qualquer consumidor e indesejáveis
  num grafo compartilhado. Aparecem em dois níveis — no campo e dentro de cada
  entrada de `values[]` —, então uma limpeza superficial deixaria a maior parte.
- **A instância única é garantida** pela limpeza que ambos os backends já fazem
  antes de sincronizar; nenhuma lógica de upsert foi necessária.
- **O ArcadeDB declara as propriedades de texto do contexto** mesmo sem indexar
  nenhuma. Todo o resto ali declara só o que um índice exige, mas um tipo sem
  propriedades declaradas aparece na introspecção como vértice vazio — e um
  cliente MCP não tinha como saber que havia contexto ali.

### Corrigido — seção `[neo4j]` ausente reportava um erro sem sentido

- Rodar o backend Neo4j contra um config escrito para outro backend falhava com
  `Required field missing in [neo4j]: 'neo4j'` — um campo dentro de uma seção que
  não existe. O caso de seção ausente passa a ser tratado à parte do de campo
  ausente, e a mensagem nomeia as seções que o arquivo realmente tem.
- Um `uri` ausente era exibido com aspas duplicadas (`"'uri'"`).


### Adicionado — testes de contrato para valores `ORDERED` vindos do compilador

- **Nenhuma mudança de código foi necessária**, mas a garantia passa a ser fixada
  por testes. Desde que o synesis canonizou `ORDERED` (o dado gravado é sempre o
  **índice**, um `int`; escrever o rótulo é erro `E088`), `_index_to_label`
  resolve **todos** os valores para o rótulo declarado, e não apenas os que por
  acaso chegavam como inteiros.

  No contrato misto anterior, um rótulo chegava ao grafo intacto, de modo que
  `Econômico` e `ECONÔMICO` viravam **dois nós de taxonomia distintos** —
  fragmentação silenciosa do mesmo aspecto. Com índices isso é inalcançável: o
  dado é `11` e existe exatamente um rótulo canônico.

  Verificado de ponta a ponta contra um projeto real de 210 conceitos: 13
  aspectos distintos, nenhum valor numérico chegando ao grafo.

  Os testes estão em `tests/test_ordered_contract.py` porque a verificação
  ponta a ponta de `test_linkage.py`, que também cobriria isso, é pulada sempre
  que o corpus Davi está ausente (dados de campo, não versionados).

- **O canal `value_maps` continua necessário.** Os backends nunca o leem
  diretamente — recebem conceitos com os rótulos já resolvidos por
  `_extract_concepts` —, então é ele que leva o mapa índice→rótulo até o ponto de
  resolução. `_index_to_label` mudou de papel, não de necessidade: deixou de
  reparar um dado ambíguo e passou a ser apresentação.

---

## [0.7.0] - 2026-08-17

### Adicionado — embeddings vetoriais para busca semântica (ArcadeDB)

- **`--vector-embeddings CAMPO,CAMPO`** no comando `arcadedb`. Nomeia os campos da
  ontologia cujo texto vira vetor, gera os vetores localmente e os indexa como
  `LSM_VECTOR` ao lado do índice full-text já existente. Sem chave de API e sem
  enviar dado nenhum para fora da máquina. Também configurável em
  `[arcadedb.embeddings].fields`; a opção de linha de comando prevalece, como já
  acontece com `--database`.
- **Por que vale a pena.** Medido no corpus FACE/UFMG (210 conceitos), com cinco
  perguntas em linguagem natural cujo vocabulário é deliberadamente disjunto das
  descrições: a busca vetorial devolve o conceito exato em **4 de 5**; o BM25, em
  nenhuma. "quem manda nas decisões da empresa" alcança `decisões_estratégicas`,
  que a busca textual não encontra — as duas não compartilham palavra alguma. O
  objetivo não é substituir a busca por palavra-chave, e sim complementá-la.
- **A escolha de campos vem do template, não de uma lista fixa.** Cada campo
  pedido é conferido: nome inexistente é erro listando os disponíveis;
  vocabulário fechado (ORDERED/ENUMERATED/SCALE) gera aviso; e campo com um único
  valor distinto no corpus é descartado — acrescentaria texto idêntico a todos os
  conceitos, sem discriminar nada. (Medido: `theoretical_significance` é `0` nos
  1388 conceitos do corpus Social_Acceptance.)
- **O modelo é por projeto**, em `[arcadedb.embeddings].model`. Um corpus em
  português e um em inglês têm exigências diferentes, e isso é decisão de
  pesquisa, não de código. O padrão é multilíngue por um motivo medido: na
  pergunta mais dependente de semântica portuguesa, o modelo só-inglês
  `all-MiniLM-L6-v2` reproduziu exatamente o erro lexical do BM25.
- **Os vetores são cacheados** em `<projeto>.embeddings.json` (no `.gitignore`).
  Só os conceitos cujo texto mudou são recalculados; execução totalmente
  cacheada nem carrega o modelo. Medido no face85: 11s a frio, 1s a quente.
  `--rebuild-embeddings` força o recálculo completo.
- O sidecar registra o modelo, as dimensões e um hash da composição de campos.
  Mudar qualquer um deles invalida todos os vetores, porque vetores de modelos
  diferentes — ou de composições diferentes — são individualmente válidos e
  mutuamente incomparáveis: a distância entre eles mede a composição, não o
  significado, e a busca degrada sem dar sinal de erro.
- Exige o extra opcional: `pip install "synesis-graph[embeddings]"`. Sem ele, o
  módulo se comporta exatamente como antes e a opção reporta um `DependencyError`
  acionável, nunca um `ImportError`.
- **Validado ponta a ponta em dois corpora reais**, ambos embedados e consultáveis
  em seus bancos ArcadeDB: FACE/UFMG (210 conceitos, português, MiniLM
  multilíngue) e Social_Acceptance (1388 conceitos, inglês, `all-mpnet-base-v2`).
  Ver a seção "Estudos de caso" do README para os tempos de geração medidos e os
  resultados de busca reais.

### Alterado

- `GraphPayload` passa a carregar o `field_specs` do template. `analyze_template`
  separa os campos por destino e descarta o tipo declarado — TOPIC e ORDERED caem
  ambos em `graph_fields` —, então a seleção de embeddings não tinha como
  distinguir um campo de texto de um vocabulário fechado. Mudança aditiva e com
  padrão; nenhum backend é afetado.

---

## [0.6.0] - 2026-08-15

### Adicionado — backend ArcadeDB

- **Novo backend: `synesis-graph arcadedb`.** O ArcadeDB implementa OpenCypher
  nativamente, então os statements de escrita do grafo são *os mesmos* do backend Neo4j
  — importados e chamados, não copiados, o que mantém uma única definição da semântica
  de MERGE. Validado contra o corpus FACE/UFMG: todas as contagens estruturais batem com
  a exportação para Neo4j (210 conceitos, 20 fontes, 174 itens, 168 `RELATES_TO`, 348
  `MENTIONS`, 78 `IS_LINKED_TO`, 99 `MAPPED_TO_ASPECT`), e o top-10 por grau é idêntico.
- **Sem dependência nova.** O backend fala HTTP/JSON via `urllib` (biblioteca padrão),
  então não acrescenta nada para instalar — nem um extra opcional. O ArcadeDB também
  fala o protocolo BOLT, o que permitiria reusar o driver do Neo4j, mas esse plugin não
  é carregado por padrão e não tem arquivo de configuração persistente: habilitá-lo
  exige passar uma flag em *toda* inicialização do servidor. A API HTTP funciona numa
  instalação padrão.
- Configure com um bloco `[arcadedb]` no `config.toml`. Só `password` é obrigatório;
  `uri` assume `http://localhost:2480` e `user` assume `root`. **A URI é o endpoint
  HTTP — o mesmo que serve o ArcadeDB Studio — e não uma URL `bolt://`.**
- `fulltext_analyzer` é portátil entre os dois backends. O Neo4j nomeia analyzers por
  rótulo curto (`brazilian`), o ArcadeDB pela classe Lucene
  (`org.apache.lucene.analysis.br.BrazilianAnalyzer`); nomes curtos são expandidos
  automaticamente e qualquer outro valor passa intacto, então o servidor segue sendo a
  autoridade.

### Alterado — `--database` vale para todos os backends de banco

- A flag estava condicionada a `backend == neo4j`, então `--database` era ignorado em
  silêncio quando outro backend de banco era selecionado. Agora vale para qualquer
  backend que tenha um banco a nomear, que é o que o texto de ajuda já prometia.

### Adicionado — declaração de schema para o ArcadeDB

- **O Cypher grava propriedades sem declará-las, e o ArcadeDB recusa indexar uma
  propriedade não declarada**: `Cannot create the index on type 'Chain.search_name'
  because the property does not exist`. Isso afeta todos os índices, não só os
  full-text.
- O backend passa então a declarar os tipos e as propriedades indexadas antes de
  escrever. Só as propriedades *indexadas* são declaradas — o resto segue schema-less,
  então um projeto continua livre para carregar qualquer campo definido no template sem
  que o backend conheça o nome dele.
- Mais duas especificidades do ArcadeDB, ambas encontradas contra um servidor real: os
  nomes de índice voltam da introspecção como `Item[item_id]`, que é erro de sintaxe em
  `DROP INDEX` sem crases; e recriar um índice exige `IF NOT EXISTS`, senão uma nova
  exportação falha com `Index '...' already exists`.

### Adicionado — métricas de grafo no ArcadeDB, e um caminho de persistência corrigido

- `pagerank`, `betweenness` e `community` são calculados com a biblioteca nativa
  `algo.*` do ArcadeDB. Não há plugin envolvido, então — diferente do caminho Neo4j —
  não existe a degradação "GDS não instalado".
- **Duas formas plausíveis de persistir esses resultados falham em silêncio, e a segunda
  é pior que a primeira.** `CALL algo.pagerank() YIELD node, score SET node.pagerank =
  score` não grava nada e reporta `stats: null` — `YIELD node` é um RID serializado como
  string, não um vértice vinculável. A correção aparente,
  `MATCH (c:Label) WHERE id(c) = id(node)`, *corrompe dados*: `id()` de uma string não é
  comparável a `id()` de um vértice, o predicado degenera e o MATCH vira produto
  cartesiano — medido gravando scores de conceito em nós `Item`.
- O RID passa a ser resolvido no cliente (`@rid` → o `name` único do conceito) e os
  valores são gravados de volta com um único `UNWIND`. As linhas cujo RID não é conceito
  são descartadas aí, o que também é o filtro de escopo que os algoritmos não oferecem.
- **Os scores não são diretamente comparáveis aos do Neo4j.** O GDS projeta apenas o
  subgrafo de conceitos; o `algo.*` roda sobre o grafo inteiro e não aceita filtro de
  escopo — `edgeTypes`, `relationship` e afins são aceitos e ignorados, e
  `weightProperty` em zero produz score uniforme em vez de isolar um subgrafo. No
  FACE/UFMG os dois top-10 de PageRank coincidem em 6 de 10. O pipeline declara isso a
  cada exportação.

### Corrigido — busca de conceitos era inalcançável por linguagem natural

- **Um índice full-text sobre `name` em snake_case não casava com nada que uma
  pessoa digitaria.** O tokenizador do Lucene segue a UAX#29, na qual o underscore
  é caractere de palavra e não separador, então `governança_corporativa` era
  indexado como um único token. Medido no face85: `"governança corporativa"`,
  `"governança"` e `"corporativa"` falhavam em recuperar um nó que existia, ao
  passo que a string exata com underscore funcionava. O índice reportava
  `populationPercent: 100` o tempo todo — estava construído corretamente e não
  respondia nada.
- Conceitos passam a carregar `search_name`, as mesmas palavras separadas por
  espaço (`humanize_concept_name`), e o `concept_search` indexa esse campo no lugar
  de `name`. O Synesis garante o snake_case — o `SYNESIS_E015` rejeita espaços em
  conceitos, já que o parser precisa do `_` onde o separador de chain `->` não
  alcança —, portanto a derivação é mecânica e agnóstica ao template.
- `name` permanece intocado: continua sendo a chave de MERGE, a constraint de
  unicidade e a identidade contra a qual toda aresta resolve. Buscas pelo
  identificador exato seguem usando `MATCH` em `name`, servidas pelo índice RANGE
  da constraint.
- Após a correção, todas as cinco formulações recuperam o nó em primeiro lugar
  (score 4.74 para a frase completa).

### Adicionado — analyzer full-text configurável (`fulltext_analyzer`)

- Nova chave opcional no bloco `[neo4j]` do config. O padrão é o próprio
  `standard-no-stop-words` do Neo4j, que não faz stemming nem folding de acentos —
  seguro para qualquer idioma, ótimo para nenhum.
- Defini-lo conforme o idioma do corpus melhora o recall de forma mensurável: sob
  `brazilian`, o face85 também casa `governanca` (sem cedilha) e `governancas`
  (plural), que o padrão não encontra.
- Fica no config, e não no código, porque o valor correto acompanha o corpus:
  `brazilian` serve ao face85 e degradaria o corpus factors, em inglês. Nada no
  template declara idioma hoje.
- **Os índices agora são removidos antes de recriados.** O Neo4j recusa um segundo
  índice sobre o mesmo par (label, propriedades), de modo que
  `CREATE ... IF NOT EXISTS` tinha sucesso em silêncio deixando o índice *antigo*
  no lugar — um analyzer alterado nunca entraria em vigor e nada avisaria.

### Alterado — a saída do pipeline é escrita para pesquisadores, não para DBAs

- O driver Neo4j registrava notificações cruas do servidor durante uma exportação
  normal, por exemplo `Neo.ClientNotification.Schema.IndexOrConstraintDoesNotExist`
  repetida uma vez por índice. Nada estava errado: o sync limpa o banco antes, e o
  `DROP INDEX ... IF EXISTS` seguinte legitimamente não encontra nada. Os avisos
  eram dirigidos a engenheiros de banco e soterravam a saída legível
  `[STEP]`/`[OK]`.
- As notificações do servidor passam a ser filtradas na origem (o driver solicita
  `WARNING` para cima), e os loggers `neo4j.*` ficam limitados para que nada escape
  por outro caminho. Problemas reais continuam aparecendo como avisos.
- O `-v` suspende o filtro e restaura o fluxo completo de notificações para
  depuração.

### Adicionado — índices full-text derivados do template

- **O grafo não tinha nenhum índice de busca.** As constraints garantiam
  integridade, mas nada servia à recuperação: a única porta de entrada era
  text2cypher com casamento exato de string, de modo que uma pergunta que não
  acertasse o nome literal do conceito não recuperava nada.
- Adicionado `_create_search_indexes`, executado logo após as constraints,
  criando três índices full-text: `concept_search`, `item_search` e
  `source_search`.
- **Toda propriedade indexada vem do template — nenhuma é hardcoded.** A prosa do
  conceito está em `scalar_fields`, que é `ontology_description` em um projeto e
  `factor_description` em outro; a prosa da fonte é o campo SCOPE SOURCE declarado
  como `TEXT`. Nomear uma propriedade fixa criaria o índice com sucesso e não
  indexaria nada.
- `graph_fields` ficam de fora deliberadamente: `TOPIC`/`ENUMERATED`/`ORDERED`
  viram nós de taxonomia próprios, e indexar vocabulário fechado como prosa apenas
  dilui o índice. `Item` é indexado por `citation`/`description`, os nomes
  estruturais que o payload normaliza a partir dos campos `QUOTATION` e `MEMO`.
- Validado no face85 com 40 termos extraídos do corpus (português e inglês, de 1 a
  80 ocorrências): `item_search` e `source_search` recuperam todos os nós que
  contêm o termo — recall de 100%.
- Os nomes de campo são interpolados no Cypher, então cada um passa por
  `validate_cypher_label` — a mesma proteção que `_create_constraints` já aplicava
  aos labels.

### Alterado — `analyze_template` informa o tipo declarado de cada campo SOURCE

- `analyze_template` passa a devolver `list[SourceFieldSpec]` em vez de
  `list[str]` na posição `source_fields`, espelhando os já existentes
  `ChainFieldSpec` e `CodeFieldSpec`. Cada spec carrega `field_name` e
  `field_type`.
- É isso que permite ao índice de Source incluir prosa e deixar de fora o
  vocabulário fechado: no template FACE/UFMG, `description` e `method` (`TEXT`)
  são indexados, enquanto `knowledge_area` (`ENUMERATED`) continua propriedade do
  nó mas nunca entra no índice.
- **A tupla devolvida continua com 8 posições** — o tipo viaja dentro do spec, e
  não como nono elemento, então os cinco call sites que a desempacotam
  posicionalmente seguem intactos.
- Adicionados `source_field_names()` e `text_source_field_names()`. Ambos aceitam
  strings simples além de specs, para que payloads montados à mão (testes, o shim
  `synesis2graph`) continuem funcionando.
- Coberto por `tests/test_search_indexes.py`.

### Corrigido — campos ITEM do template agora chegam ao nó Neo4j

- **O nó `Item` carregava apenas `item_id`, `citation` e `description`.** Todos os
  demais campos ITEM declarados no template (`zone`, `confidence`, `score`, ...)
  eram desviados para o mapa `item_fields`, exclusivo do HTML, e nunca chegavam
  ao grafo. A prévia ficava assim mais rica que o banco que alimenta o GraphRAG:
  um filtro retórico como "apenas evidências de trechos `Result`" era
  inexprimível em Cypher, embora o valor existisse no `.syn` e estivesse visível
  na tela.
- O desvio se justificava pela recusa do Neo4j a propriedades de mapa aninhado,
  mas `_extract_item_extra` já devolve `dict[str, str]` — escalares achatados.
  Só a chave aninhada atrapalhava, então achatar os campos na linha basta;
  o `SET i = row` em `_sync_items` já grava todas as chaves que recebe.
- Adicionado `_build_item_row`, usado pelos ramos CHAIN e CODE de
  `_extract_corpus_data`. Chaves estruturais sempre vencem: um template livre
  para nomear um campo `citation` não pode sobrescrever a citação em torno da
  qual o nó é construído.
- `item_fields` permanece inalterado, então a visão de evidências do HTML
  continua funcionando como antes.
- Coberto por `tests/test_item_fields.py` — os primeiros testes sobre o payload
  enviado ao Neo4j, que não tinha cobertura até agora.

**Bancos existentes precisam de re-exportação para receber os campos**; o sync
limpa o banco antes de escrever, portanto nenhuma migração é necessária.

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
