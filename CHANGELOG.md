# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Language:** [English](CHANGELOG.md) | [Português](CHANGELOG.pt.md)

**Documentation:** [Synesis Language Docs](https://synesis-lang.github.io/synesis-docs)

---

## [Unreleased]

## [0.11.0] - 2026-08-29

### Changed — a full rebuild writes with `CREATE` instead of `MERGE`

Where the payload already guarantees unique keys — `Item`, `Source` and the
ontology's concepts, measured at 0 duplicates across 272,154 rows of the real
corpus — a rebuild now writes with `CREATE`. `MERGE` costs an index lookup per
row against an index that grows as the load proceeds, which degrades
quadratically: 10,000 nodes in 22.2s, 40,000 in 472.8s. The same load with
`CREATE` measured 12x faster.

This is safe only because a rebuild clears the graph first, and it is applied
only there. Chains, taxonomies and every edge keep `MERGE` in both modes: their
duplication is in the payload rather than in the destination — 302,392 chain
endpoints resolve to 22,553 distinct concepts — so the deduplication is doing
real work.

### Added — `--mode update` writes only what changed

`synesis-graph neo4j`, `arcadedb` and `arcadedb-embedded` accept
`--mode rebuild|update`. `rebuild` stays the default and is unchanged: the graph
is wiped and written again, which is always correct.

`update` merges the payload into the existing graph without clearing it. On a
large corpus that is the difference between rewriting everything and touching
the fraction that actually moved — the 246,588-item Quinto Andar corpus
extrapolated to hours against a small server, and most re-runs change very
little of it.

- **It does not delete.** The payload describes what exists, not what was
  removed, so material deleted from the project stays in the graph until the
  next rebuild. This is stated in `--help` and announced at the start of every
  update run, because a gap nobody mentions is a gap nobody notices.
- **Settings that need an index rebuild are refused, not silently ignored.**
  `update` does not drop indexes, so it cannot apply a changed
  `fulltext_analyzer`, embeddings model or embedded field list. It compares what
  the graph says it was built with — read from the `ProjectContext` vertex, the
  only thing that knows — against what the run asks for, and stops with a message
  naming the setting and telling you to rebuild. Letting it through would leave
  the graph advertising a capability its indexes do not have.
- **Exactly one `ProjectContext`, in both modes.** In update the vertex is
  replaced rather than appended, so a client never reads two conflicting
  descriptions of the same graph.

### Changed — a local rebuild bulk-loads its vertices

On the local (embedded) engine, a rebuild now writes `Item`, `Source` and the
ontology's concepts through ArcadeDB's GraphBatch API instead of Cypher. On a
20,000-item project the whole sync went from 4.62s to 1.64s — 2.8x, on top of
the `CREATE` change below.

The resulting graph is identical: same vertices, same edges, same properties.
Only the write path differs, and the test suite compares both paths over the
same payload to keep it that way.

- **Rebuild only, and only where keys are unique.** Bulk loading never
  deduplicates, so it is used only where the compiler guarantees unique keys and
  only when the graph was just cleared. Chains and taxonomies keep `MERGE` — the
  duplication in their rows is meaningful — and `--mode update` never bulk-loads
  at all.
- **Edges are untouched.** They still resolve their endpoints through the
  existing lookup, so there is nothing new to go wrong in the part of the graph
  that carries the relationships.
- **Older engines and the HTTP backend are unaffected.** Where the API is not
  available the previous path runs, producing the same graph a little slower.

### Fixed — a step that failed no longer reports `[OK]`

A sync that failed printed both `[OK]` and `[ERROR]` for the same step, one line
apart. The step marker only watched for raised exceptions, and this codebase
reports failure by *returning* a typed error — so an early return left nothing
for it to see.

Six steps were affected, including both database syncs. Two contradictory lines
about one step make every log from this tool untrustworthy, which is worse than
any single failure: a reader who has learned that `[OK]` can mean failure cannot
rely on the rest of it either.

### Fixed — a dropped connection no longer throws away a finished export

Against a stock server behind a reverse proxy, a large upload could fail with
`Bad Gateway (HTTP 502)` after minutes of work, leaving nothing behind. The
database was healthy throughout — answering its readiness probe in 74ms while
returning 502s — so the proxy in front of it was cutting connections under load.
The same statement succeeded repeatedly minutes later, which is what makes this
a transient failure rather than a limit worth tuning around.

- **Connection failures are retried immediately.** When the connection never
  opened, the database cannot have seen the statement, so repeating it is safe.
  The real server stopped accepting connections under load and recovered on its
  own within a minute; that minute used to cost a whole export.
- **Interrupted uploads restart, rather than resuming.** When a proxy cuts a
  request in flight, whether the database applied it is unknowable — and since a
  rebuild writes with `CREATE`, resuming could duplicate. A rebuild clears the
  graph before writing, so starting over is always safe; `--mode update` does
  not clear, so it reports the failure instead of guessing.
- **The message names the right culprit.** "Bad Gateway" reads as a database
  error and sends you looking in the wrong place; it now says the server's proxy
  closed the connection, and that nothing was left half-written.
- **A failed cleanup no longer hides the failure.** When the connection drops
  mid-upload the server discards the transaction with it, so the rollback that
  follows answers "transaction not found" — and that answer used to *replace*
  the original error. Since "not found" is not a transient failure, the upload
  was abandoned after one attempt instead of three.

### Fixed — uploads to a server are sent in smaller pieces

Each statement was sending up to 50,000 rows, which for a real corpus is a
single 32.6MB HTTP request — large enough that a stock reverse proxy drops it
under load. That was the actual cause of the `Bad Gateway` failures above.

The limit was inherited from the Neo4j backend, whose driver streams over a
long-lived connection instead of making one request per statement. This backend
now sends 5,000 rows at a time: 3.3MB, and measurably *faster* as well as
smaller (6,625 rows/s against 4,917), because the server can work on one request
while the next is still arriving.

### Fixed — every concept gets a centrality score

Scores were written for only the first 20,000 concepts. On a 22,585-concept
ontology that left 2,585 of them — 11% — with no PageRank and no betweenness,
while the run reported `[OK] PageRank calculated (20000 nodes)`. Asking the
graph for "the most central concepts" returned a ranking that silently omitted
a ninth of the ontology.

The cause: the server returns at most 20,000 rows per response and flags the
rest as truncated, and that flag was never read. It is now, and a partial
response is refused rather than mistaken for a complete one.

- **The metrics themselves got faster, not slower.** The row cap is a
  per-request default rather than a server limit, so the whole result is now
  requested at once. Betweenness over the measured corpus went from about 34
  minutes — reading it in pages re-runs the algorithm once per page — to 1m36s.
- **Long calculations say how long they will take**, and name `--metrics fast`
  as the way to skip them.

### Added — `--metrics all|fast|none`

Chooses how much measuring to do. `all` (the default) computes centrality and
communities; `fast` keeps PageRank and skips the two slow ones; `none` skips
them entirely, leaving the graph itself complete.

The default computes everything on purpose: these scores are research findings,
not diagnostics, so they are never lost unless someone chooses to trade them for
time.

### Changed — a long upload says how long it will take

A sync runs in one transaction, so the database stays empty until the very end.
Combined with a terminal that printed nothing for minutes, that was
indistinguishable from a freeze — and interrupting throws away all the work.
Uploads of 50,000 excerpts or more now state the rough duration up front and say
plainly that silence is expected.

### Fixed — `pip install synesis-graph` works on Intel Macs

The local graph engine publishes builds for Linux, Windows and Apple Silicon,
but not for Intel Macs — and it ships no source to build from. Declared as an
ordinary requirement, that did not mean "the local engine is unavailable": pip
found nothing to install and aborted the **entire** installation, so the
researcher lost the Neo4j and HTML backends too, over an engine they may never
have used.

It is now requested only where a build exists. On an Intel Mac the other
backends install and work normally, and asking for the local engine explains
that no build exists for that machine rather than suggesting a reinstall that
cannot help.

## [0.10.0] - 2026-08-27

### Added — the ArcadeDB sync layer is typed against a transport contract

New `ArcadeDBTransport` Protocol names what the sync layer actually needs from a
connection: `command`, `query`, `begin`, `commit`, `rollback`, and the `database`
attribute. `backends/arcadedb.py` and `metrics_arcadedb.py` now annotate against
it instead of the concrete `ArcadeDBClient`.

Nothing changes at runtime — this is a type-level move. What it buys is room for
a second transport: the in-process embedded engine speaks the same Cypher and the
same SQL, and with the sync layer typed against the contract, adding it touches
no query, no schema statement, and none of the eight `_sync_*` functions.

- **Structural typing, so the cost falls on the newcomer.** `ArcadeDBClient`
  satisfies the Protocol without inheriting from it, without registering, and
  without importing it. A shared abstract base would have meant changing the HTTP
  client — in production — to accommodate a backend that does not exist yet.
- **Deliberately smaller than the client.** `is_ready`, `list_databases`,
  `create_database` and `close` stay out: they are preflight, setup and teardown,
  they live in the adapter, and they are exactly where two transports differ (an
  in-process database has no server to probe and no credential to exercise).
- `language` stays keyword-with-default rather than following the embedded
  engine's positional-first signature, so all 20 call sites keep working
  unchanged.
- Conformance is pinned by tests that compare the two surfaces mechanically
  (`inspect.Signature`), not by restating the signatures — a hand-copied
  expectation drifts as easily as the code. Verified to fail on real drift.

### Added — an in-process ArcadeDB transport (`ArcadeDBEmbeddedClient`)

`arcadedb-embedded` ships the real engine plus a bundled JRE, so a graph can be
built and queried with nothing installed but `pip` — no Java, no server, no port.
This class adapts it to `ArcadeDBTransport`, which means the sync layer runs over
it unchanged: no query rewritten, no schema statement touched, none of the eight
`_sync_*` functions modified.

It exists to absorb five ways the binding differs from the HTTP client. Each was
measured against `arcadedb-embedded` 26.8.1, and each is a **silent** failure if
left to the caller — the code runs, raises nothing, and returns a wrong answer:

- **`language` is the binding's first positional argument.** This class keeps the
  client's keyword-with-default signature; following the binding would make every
  unqualified call in the sync layer execute as the wrong language.
- **A write returns `None`, not an empty result.** The sync layer iterates results
  directly, so `None` becomes `[]` here rather than a `TypeError` at a call site.
- **`ResultSet` is single-pass.** Reading it twice yields `[]` the second time,
  with no error. Results are materialised at this boundary — the only place that
  knows the cursor is still unread.
- **Passing `None` as parameters raises `Ambiguous overloads`** from JPype, which
  cannot resolve the Java overload from a null. The argument is omitted entirely
  when there are none, so the failure cannot reach a caller. This one would have
  looked intermittent: it fires only on statements that take no parameters.
- **The binding raises its own same-named `ArcadeDBError`,** from a different
  module. Nine `except ArcadeDBError` sites expect this package's class; without
  translation each would let an engine error escape unhandled, past the code
  written to report it.

The optional dependency is imported lazily and reported as an actionable
`ArcadeDBError`, the same pattern the embeddings provider already uses, so the
other backends keep working when the extra is absent.

### Added — `[arcadedb-embedded]` configuration and the backend name

`ArcadeDBEmbeddedConfig` and the `arcadedb-embedded` backend constant. The
adapter that consumes them lands next; this is the configuration surface.

- **Its own section, not a reuse of `[arcadedb]`.** Sharing that block would
  leave `uri`, `user` and `password` sitting in the file, read by nobody — a
  field that looks honoured but is not is the defect shape this project keeps
  paying for. A distinct section makes the mode visible in the file itself.
- **Every field is optional, and so is the section — and the file.** There is no
  credential to supply and no host to reach, so the defaults already describe a
  working setup; demanding a file whose every value repeats a default is friction
  with nothing behind it. Malformed sections are still reported, because those
  are typos rather than omissions.
- **`db_path` is the server root, not the database directory.** The database is
  created at `<db_path>/databases/<project>`, which is where ArcadeDB's server
  looks for it. Pointing it one level deeper produces the worst failure available
  here: the server starts, registers its MCP endpoint, reports success, and finds
  no databases at all — nothing errors, the corpus is simply invisible. The
  layout is derived by a single method, so whatever writes the graph and whatever
  serves it cannot drift apart.
- The default root is `.`, not `./databases`: the root already gains a
  `databases/` child, and `databases/databases/face85` reads like a bug — which
  invites the "fix" that lands exactly on the silent failure above.
- `[arcadedb_embedded]` (underscore) is accepted too, for readers who know
  `[tool.ruff]`.

### Added — the embedded backend adapter, over a base shared with the HTTP one

`ArcadeDBEmbeddedBackendAdapter` completes the local path: a project now exports
to a directory, with no server, no port and no Java installed.

- **`_ArcadeDBAdapterBase` holds everything past the connection.** The engine is
  the same whichever way it is reached, so clearing, syncing, metrics and close
  are written once. Only `preflight`, `connect` and `prepare_destination` differ,
  and each is simpler here for the same reason: there is no server in the picture.
- **The pipeline's two "is this ArcadeDB?" questions now have one answer.** It
  asks in order to attach the embeddings sidecar and to record the metrics scope
  caveat, and both apply to either transport — same engine, same whole-graph
  `algo.*` limitation. Naming both adapters at each site would have put that rule
  in two places that must be kept in step; no third check was added.
- **`connect` deliberately does nothing.** The HTTP adapter opens a client there
  because its target exists independently of the project; an embedded database
  *is* a directory named after the project, so opening waits for the payload.
- **`preflight` checks what can actually fail** — that the optional package is
  installed, and that the root is writable. Both are reported before compilation,
  which on a 41k-item corpus is the difference between a wasted minute and a
  wasted hour.
- `prepare_destination` derives the directory from `ArcadeDBEmbeddedConfig`
  rather than joining paths itself, so the export and the future serving side
  cannot disagree about the layout.
- The HTTP adapter now declares its `client` as the concrete `ArcadeDBClient`:
  `create_database` is a server operation, absent from the transport contract by
  design. Narrowing in the subclass keeps that call type-checked without widening
  the contract for a transport that has no server to send it to.

### Added — `synesis-graph arcadedb-embedded`

The local path is now a command. A project exports to a directory with no server
running, no port to manage and no Java installed:

```
synesis-graph arcadedb-embedded --project project.synp
```

- **`--config` is optional here.** There is no credential to supply and no host
  to reach, so the defaults describe a working setup; the command runs in a
  directory with no `config.toml` at all.
- **`--db-path` is the server root**, and its help says so — the database is
  created in `<DIR>/databases/<project_name>`. Pointing it straight at a database
  directory is the mistake that makes the serving side start, report success and
  find nothing, so the flag documents the layout rather than assuming it is known.
- **It sits under "Graph Backends" in `--help`**, with the other three: it
  exports and exits. A long-running command would not belong there, which is
  exactly why the grouping exists.
- `--vector-embeddings` and `--rebuild-embeddings` work as on the server backend,
  and the guard against rebuilding with no fields names
  `[arcadedb-embedded.embeddings]` — not `[arcadedb]`, which is a different mode.

Backend-agnostic CLI overrides (`cli_overrides`) replace what was an HTML-only
mechanism. A flag the user did not pass stays `None` and never displaces a
configured value, so an unused flag cannot silently reset the file.

### Added — `synesis-graph serve`, and the local graph reaches the chat clients

Phase B. The export commands write a graph and exit; this one opens a graph
already built and keeps it reachable, so Claude Desktop, Claude Code or the
VSCode extension can ask questions of it:

```
synesis-graph serve
```

The engine does most of this itself — `arcadedb-embedded` bundles the real
ArcadeDB server and auto-discovers its MCP plugin. Three things it does not do
are why this is a command rather than a snippet to paste:

- **MCP starts disabled.** The embedded distribution ships without the
  `config/mcp-config.json` the standalone server reads, so the plugin registers
  and then refuses every call. The setting lives in the running server, not on
  disk, so enabling it has to happen on **every** start.
- **Read-only is not the default.** Writes are off unless `--allow-writes` says
  otherwise: a corpus is months of coding work, and reading it is the use case.
  Every permission flag is stated rather than inherited, so a future engine
  default cannot quietly widen what a chat client may do. `allowAdmin` stays off
  even with `--allow-writes` — administrative calls reach past the corpus to the
  server itself.
- **A password must exist and must not be written down.** One is generated per
  session and printed with the `mcpServers` entry to paste; `SYNESIS_DB_PASSWORD`
  keeps one across restarts so a client config stays valid. Nothing is written to
  a project file.

Serving a root with no database under `databases/` is refused, with the export
command that fixes it. That check is the layout contract seen from the other
side: a server started over the wrong directory runs happily, registers MCP, and
answers every query with no rows.

`--help` now groups the commands. Backends export and exit; `serve` publishes and
stays — a distinction worth showing, since it is what keeps the module's role
legible as it grows.

### Added — the local backend validated on a real corpus

**Validated against the Quinto_Andar corpus** — 41,474 items, 7,293 concepts, 661
sources, exported in 113s with no server running. All **16** node and
relationship counts match the Neo4j Aura export exactly: `Item` 41,474,
`FROM_SOURCE` 41,474, `MENTIONS` 61,796, `RELATES_TO` 19,126, `GROUPED_BY` 7,293,
and every taxonomy label and edge besides. `ProjectContext` carries the same
counts, and the embedded backend additionally declares its full-text capability,
which the Neo4j one cannot.

On the face85 corpus the two transports were compared directly, and the vectors
are the deciding detail: the same 210 concepts, the same 49 communities, PageRank
agreeing to the eighth decimal, and `vectorNeighbors` returning the same
neighbours in the same order. Stored vectors are byte-identical.

This is the criterion the whole embedded series was written against: the same
graph, from the same project, with no Java and no database server at any point.

READMEs (EN and PT) document the third backend, when to prefer it over the
server one, and the `serve` command.

### Changed — the local graph engine now installs with the package

`pip install synesis-graph` brings the in-process ArcadeDB engine. There is no
extra to remember, and no Java to install: the wheel carries its own JVM.

The audience decided this. "Install the right extra" is one more step to get
wrong before seeing any result, and the person most likely to get it wrong is the
one with the fewest tools to diagnose it. ~67 MB is a smaller price than that
friction. Someone using only Neo4j or HTML now carries weight they do not use —
a deliberate trade, made in favour of the researcher.

`synesis-graph[arcadedb-embedded]` still resolves, so older instructions keep
working.

### Changed — the terminal speaks to the researcher, not to the database

Exporting a corpus used to print sixty lines of engine internals — every index
build, every sub-index split, every page write — with the three lines that
mattered buried among them. Worse, `WARNI` and `Building index 'Item_0_406270...'`
read like something had gone wrong.

The engine's logging is now configured down to warnings, and the two unactionable
JVM startup lines are filtered. Anything else Java says still reaches the
terminal: a real failure is never swallowed, and that is pinned by a test.

The step labels changed with it, from implementation to intent:

| Before | Now |
|---|---|
| `Compiling Project (In-Memory)` | `Reading your project` |
| `41474 items compiled` | `41.474 coded excerpts read` |
| `Synchronizing Graph (Transactional)` | `Building the graph` |
| `Calculating Native Metrics` | `Measuring the graph` |
| `Calculating Graph Algorithms` | `Finding central concepts and communities` |
| `Target database: …` | `Graph location: …` |

- The metrics caveat now has **two wordings**: `ProjectContext` keeps the precise
  one naming `algo.*`, because its reader is a program about to rank concepts by
  those scores; the terminal gets the plain version, because its reader needs to
  know only that these numbers are not comparable with a Neo4j export's.
- A finished export now says **where the graph is and what to do next** — it is a
  means, not an end, and a researcher left with a directory has no way to guess
  that `serve` exists.
- `SUCCESS em 3s` had a stray Portuguese word in an otherwise English interface.

### Fixed — a second `serve` on the same graph failed with HTTP 403

The engine honours `root_password` only while it is creating
`config/server-users.jsonl`. Every later start reads the stored hash and ignores
what it is handed — silently. `serve` generated a fresh password per session, so
the first run worked and every one after it authenticated against a credential
nobody held: the server started, reported success, and answered
`User/Password not valid`.

- The password is now **generated once and remembered** beside the server's own
  state, so restarts keep working with no variable to set. `SYNESIS_DB_PASSWORD`
  remains for choosing your own.
- A credential already stored but **unknown to everyone** — the state left by any
  earlier version — is **reset rather than reported**. It guards nothing: a local
  server, reachable only from this machine, whose password exists because the
  engine demands one. Refusing would hand over a chore whose only answer is yes.
  The old file is set aside as `.superseded`, never deleted, and `databases/` is
  not touched.
- A password under the engine's 8-character minimum is now refused **before**
  starting, naming the variable that set it.
- A failure to start no longer appends "is the port in use?" to an unrelated
  engine message. The guess is offered only when the error mentions the port.

### Changed — `--install` names its client, and VS Code is supported properly

VS Code reads a different format: `servers` rather than `mcpServers`, HTTP
directly with a `headers` object, and no `npx`/`mcp-remote` bridge. The previous
snippet claimed to serve "Claude Desktop, Claude Code or the VSCode extension"
while emitting a shape VS Code ignores in silence — no error, the server simply
never appears.

- `--install claude-desktop` and `--install vscode` each write their own format.
  VS Code's goes to `.vscode/mcp.json` in the working directory: an entry naming
  a port and a password belongs to the project it was started for, not to every
  window the editor opens.
- **Installing is now opt-in.** It was briefly the default, which was wrong:
  editing another application's configuration is the researcher's decision, and
  file permissions vary by platform in ways no default can assume.
- The location is claimed only for the two platforms with an official Claude
  Desktop build. Elsewhere the entry is printed instead of written to a guessed
  path, and `SYNESIS_MCP_CONFIG` overrides everywhere.
- Both installers preserve every existing entry and every top-level key, back the
  file up first, and refuse rather than overwrite a file they cannot parse. A
  missing config directory is reported, never created — that would configure an
  application that is not installed.

### Fixed — the help stopped describing the command

Two examples had drifted into being wrong, one of them broken: `--install`
became a choice option and the epilog kept illustrating it bare, which now fails
with "Option '--install' requires an argument". The text most likely to be copied
was the text that no longer worked. The password example still told researchers
to set `SYNESIS_DB_PASSWORD` for restarts to survive, which the fix above made
unnecessary.

Both are corrected, and **every example in every epilog is now checked against
the real parsers** by a test. This is the second time a `serve` example was
wrong; a one-off correction would not have prevented the third.

### Fixed — the `serve` example pointed at the wrong directory

`synesis-graph serve --help` illustrated `--db-path ./databases`. Both the export
and `serve` add the `databases/` level themselves, so following the example makes
the server look in `databases/databases` — and a server over the wrong root does
not complain: it starts, registers its MCP endpoint, and answers every query with
no rows.

The example now passes the same root the export used, and shows the two commands
side by side so the symmetry is visible. A test pins it, because an example is
the likeliest text to be copied verbatim.

`synesis-graph arcadedb-embedded --help` now also points at `serve` as the next
step — exporting a graph and querying it from a chat client are two halves of the
same task.

### Fixed — a template may declare a field named `title` without breaking the sync

Full-text index property lists are now deduplicated, keeping first-seen order.

Both backends build the `Source` index by prepending the structural
bibliographic props (`title`, `abstract`) to the template's own TEXT SOURCE
fields. Nothing stops a template from declaring one of those names, and doing so
is legitimate — Quinto_Andar declares `FIELD title TYPE TEXT SCOPE SOURCE` for
the candidate's name, the same slot filled from a different source. The result
was a composite index naming `title` twice.

- **Neo4j rejected it outright** with `RepeatedPropertyInCompositeSchema` — and
  did so *after* compiling 41,474 items and synchronising the graph, since index
  creation comes last. The whole run failed at the end with nothing written.
- **ArcadeDB failed silently**: it accepts the composite and indexes the same
  column twice. Worse, `_declare_fulltext` derives from the same list, so the
  duplicate reached `ProjectContext.fulltext_source_fields` and would teach a
  consumer a `SEARCH_INDEX` name that does not match the index actually created.
- The same collision existed on the concept index, where `search_name` is
  prepended to `scalar_fields`.
- Order is preserved rather than sorted: the structural prop is the first
  occurrence, and `SEARCH_INDEX` addresses a composite index by its full ordered
  name.

Deduplication lives in one helper, `core.dedupe_index_props()`, used by all three
derivation points — a second implementation of the rule is free to disagree with
the first.

## [0.9.0] - 2026-08-25

### Added — network metrics declare their backend and scope

`ProjectContext` now carries `metrics_backend` and `metrics_scope`.

This is not bookkeeping: it changes how the number must be read. ArcadeDB's
`algo.*` procedures accept no scope filter and run over the **whole graph**, so
a concept's PageRank incorporates its edges to Items, Sources and taxonomy
nodes; Neo4j's GDS projects only the concept subgraph. The two are not
comparable, and a consumer ranking "most central concepts" had no way to know
that from the score alone.

The caveat already existed in `metrics_arcadedb.SCOPE_NOTE` — it reached the CLI
output and never the graph.

- Declared **before the sync writes the context**, because the metrics
  themselves run after it; which backend will compute them is known earlier.
- The prose block also states that centrality is a **methodological choice** —
  degree, PageRank and betweenness answer different questions — and that the
  consumer must say which one it used.

### Added — the graph declares its full-text capability

`ProjectContext` now carries `fulltext_concept_fields`, `fulltext_item_fields`,
`fulltext_source_fields` and `fulltext_analyzer`.

The indexes already existed — this backend builds them over the humanised
concept name and the template's text fields, with a configurable Lucene
analyzer. What was missing is that **a consumer had no way to find out**:
`get_schema` lists properties, not indexes. So the chat assistant was working
around, in natural language, a problem this layer already solved — it instructed
the model to cut a search term before its first accented character, because
`CONTAINS 'psicologicos'` misses `psicológicos`.

- The **exact field list** is declared, not a flag: `SEARCH_INDEX` addresses a
  composite index by its full name — `Concept[search_name, ontology_description]`
  — so a consumer that only knew "there is an index" could not form the call.
- The **analyzer is part of the contract.** `StandardAnalyzer` does no stemming
  and no accent folding; `brazilian` does both. The prose block says which,
  rather than letting a consumer present full-text as accent-insensitive when it
  is not.
- Declared from the **same field lists that fed `CREATE INDEX`**, so the
  declaration cannot drift from what exists.
- **ArcadeDB only.** Neo4j has full-text too, but it is queried through
  `db.index.fulltext.queryNodes`; announcing one backend's syntax for the other's
  graph would teach a query that always fails.

### Changed — traceability paths: explicit root, and omission over leakage

`relative_source_file()` now **omits** `source_file` when relativisation fails,
instead of falling back to the absolute path.

Keeping it was documented as "a wart, never a crash". It is worse than a wart:
the absolute path leaks the exporting machine's directory layout to everyone the
graph is shared with, and it does not resolve on the reader's machine — so the
anchor it produces is a link that cannot open. An absent `source_file` is honest;
a wrong one promises verification and fails.

- The root is now **passed explicitly** by `compile_project()` and by each member
  of a linked study, which know it: the `.synp` sits in it. Inference from the
  `project.includes[]` / `traceability.file` redundancy remains the fallback for
  `load_json_project()`, where the original directory may no longer exist.
- In a linked study each member gets **its own root** before `merge_payloads` —
  the projects live in different directories, and no single root is correct for
  all of them.
- The containment check compares **path components**, not the raw prefix:
  `D:/proj-evil` starts with `D:/proj` as a string but is not inside it.

### Added — `Item.annotation_id`: the counting unit becomes expressible

Every `Item` now carries the identity of the **annotated block** it came from,
shared by all the items that block produced.

One `ITEM` block with four chains yields four `Item` vertices — four analytical
units over one annotated excerpt. Both counts are legitimate answers to
different questions; reading one as the other is not, and that is what made an
audit turn contradict a correct answer (it reported 11 where the bank said 20).

Without this property the distinction was not expressible in a query: counting
excerpts meant guessing by grouping on file and line.

| Unit | Expression |
|---|---|
| sources | `count(DISTINCT s.bibtex)` |
| annotated excerpts | `count(DISTINCT i.annotation_id)` |
| analytical items | `count(DISTINCT i.item_id)` |
| mentions | `MENTIONS` edges |
| concepts | `count(DISTINCT c.name)` |

- Built from `corpus_id`, which was already available where the `item_id`
  suffixes (`_c0001`/`_n0001`) are appended — no inference from text or line.
- Joins the **protected set**: a template free to name a field `annotation_id`
  must not be able to rewrite which block an excerpt belongs to.
- **Omitted, not null**, when absent — the rule the traceability pair follows.

### Changed — `Item.source_line` is declared `INTEGER` in the ArcadeDB schema

It was left schema-less because `_declare_property` only wrote `STRING`, which
ArcadeDB rejects for an integer. The typed helper removed that limitation, and
an undeclared property is **invisible to `get_schema`** — which is how the chat
discovers what the graph offers.

### Added — the audit trail reaches the graph (`Item.source_file`, `Item.source_line`)

Every `Item` now carries the `.syn` file and line it was annotated on. A consumer
can go from a concept to the excerpt, to the reference, and finally **to the line
the researcher wrote** — the chat assistant turns it into a clickable link.

The data already existed and was being thrown away. The compiler emits
`traceability: {file, line}` in the canonical JSON, and CSV/XLS export it as
`source_file`/`source_line`; the graph was the **only export dropping it**, which
is why a graph-backed answer could not say where it came from.

- The path is stored **relative to the project root**, inferred from the
  redundancy between `project.includes[].path` (relative) and
  `traceability.file` (absolute). An absolute path would leak the exporting
  machine's directory layout and would not resolve for whoever reads the graph.
- The pair is **omitted, not null**, when a corpus item has no location, so
  `WHERE i.source_file IS NOT NULL` stays honest.
- `source_file` and `source_line` join the structural keys that a template field
  of the same name cannot overwrite — the same protection `citation` already had.
- Neo4j needed no change (`SET i = row`); ArcadeDB declares `source_file` so the
  property is visible to schema introspection.

### Added — the graph declares its semantic search capability

`ProjectContext` now records which ontology fields were embedded, with which
model and how many dimensions, whenever a sync runs with `--vector-embeddings`.

A client could already see from `get_schema` that a vector index exists, but not
**which field produced the vectors** — and that changes what proximity means:
over `ontology_description` it is conceptual similarity, over `topic` it is
thematic co-occurrence.

- Declared from the embeddings sidecar, **never inferred from the index**: an
  index survives a re-sync without vectors, and reading the capability off it
  would announce something the data no longer has.
- A partial declaration is refused — a consumer would query by a field
  composition that never existed.
- The prose summary also states that **vector proximity is approximate**: a
  neighbour is a reading suggestion, not something the researcher asserted.

### Added — network metrics are declared in the ArcadeDB schema

The eight metrics the sync already computes (`pagerank`, `betweenness`,
`community`, `degree`, `in_degree`, `out_degree`, `mention_count`,
`source_count`) are now declared, and therefore visible to MCP introspection.

They were being written and stayed invisible: asked for the most central
concepts, an MCP client counted edges by hand and produced a *degree* ranking,
because it had no way to learn that `pagerank` was already there. The two
rankings differ — on a real corpus the top two by degree are not in the top five
by PageRank.

- `_declare_property()` accepts a declared type, whitelisted. `pagerank` and
  `betweenness` are DOUBLE, the rest INTEGER; declaring them STRING would make
  the server reject the value at sync time.
- Accepted types verified against ArcadeDB 26.7.3.

## [0.8.0] - 2026-08-23

### Added — the exported graph now describes itself (`ProjectContext`)

Every sync to a database backend writes a single `ProjectContext` vertex holding
the project's own context. **The context travels with the data, not with the
tool**: any consumer of the graph gets it — Claude Desktop, any MCP client, the
database's own studio, or a colleague who receives a copy.

The problem it solves: an exported graph was *syntax without semantics*. A
consumer introspecting the schema learned that a vertex `Aspect` has a property
`name`, but not that `Aspect` is Dooyeweerd's modal scale, that its values are
**ordered**, or what `[15] Fiducial` means. All of that is declared in the
template and used to be discarded at export time.

Properties written:

| Property | Content |
|---|---|
| `description` | the `.synp` `DESCRIPTION` block, verbatim |
| `project_summary` | metadata, corpus size and provenance, as prose |
| `template_doc` | the template as a readable document: every field with type, scope, description, value scale and **GUIDELINES**, plus the validation rules and a **`## Como navegar o grafo`** section naming every edge with its direction |
| `concept_label`, `template_name`, `project_name` | identifiers |
| `source_count`, `item_count`, `concept_count` | integers, queryable without parsing |
| `compiler_version`, `synesis_graph_version`, `compiled_at`, `generated_at` | provenance |

Nothing new is extracted from the compiler: the canonical JSON already carried
all of it. `prepare_payload` read the `project` object only to take its name.

**Written as Markdown, not JSON.** Measured through the real MCP path against a
210-concept corpus: as JSON the field specs reached the model as ~7.3k tokens in
which **53% of the keys were `null`**, with the GUIDELINES — written by the
researcher with headings and line breaks — escaped inside a string. The same
content as prose is smaller, needs no parsing, and keeps the shape the
researcher gave it.

`GUIDELINES` are the highest-value part: they are the **coding protocol**, with
explicit decision rules and examples ("do not include proper names", "1–3
sentences"). They answer what no schema can — *why* a datum looks the way it
does, and what counts as a valid instance of a field. Until now they lived only
in the `.synt` and left no trace in the graph.

- **Backends:** ArcadeDB (TCP/HTTP) and Neo4j. The HTML backend is deliberately
  out of scope — it is a visualisation artifact with no programmatic consumer,
  and the context is already implicit on screen for a human reader.
- **Counts are measured from what reaches the graph**, never copied from the
  compiler's `export_metadata`: its counters answer a different question (its
  `item_count` counts SOURCE blocks, not the `Item` vertices the sync writes),
  so storing them would produce a property that looks checkable against the
  graph and silently disagrees with it.
- **`location` keys are stripped recursively.** They are absolute paths from the
  machine that compiled the project, useless to any consumer and unwelcome in a
  shared graph. They appear at two levels — on the field spec and inside each
  `values[]` entry — so a shallow pass would leave most of them behind.
- **A single instance is guaranteed** by the clearing both backends already do
  before syncing; no upsert logic was needed.
- **ArcadeDB declares the context's text properties** even though none is
  indexed. Every other type here declares only what an index needs, but a type
  with no declared properties shows up in schema introspection as an empty
  vertex — so an MCP client had no way to learn the context was there.

### Fixed — a missing `[neo4j]` section reported a nonsensical error

- Running the Neo4j backend against a config written for another backend failed
  with `Required field missing in [neo4j]: 'neo4j'` — a field inside a section
  that does not exist. The whole-section case is now handled apart from the
  missing-field one, and the message names the sections the file does have.
- A missing `uri` was rendered with doubled quotes (`"'uri'"`).


### Added — contract tests for `ORDERED` values from the compiler

- **No code change was needed**, but the guarantee is now pinned by tests. Since
  synesis canonicalised `ORDERED` (the stored datum is always the **index**, an
  `int`; writing the label is error `E088`), `_index_to_label` resolves every
  value to the declared label instead of only those that happened to arrive as
  integers.

  Under the previous mixed contract a label reached the graph untouched, so
  `Econômico` and `ECONÔMICO` became **two distinct taxonomy nodes** — silent
  fragmentation of the same aspect. With indices that is unreachable: the datum
  is `11` and exactly one canonical label exists.

  Verified end to end against a real 210-concept project: 13 distinct aspects,
  no numeric value reaching the graph.

  The tests live in `tests/test_ordered_contract.py` because the end-to-end
  check in `test_linkage.py` that would also cover this is skipped whenever the
  Davi corpus is absent (field data, not versioned).

- **`value_maps` remains necessary.** Backends never read it directly — they
  receive concepts with labels already resolved by `_extract_concepts` — so the
  channel is what carries the index→label map to the point of resolution.
  `_index_to_label` changed role, not necessity: it is no longer repairing an
  ambiguous datum, it is presentation.

---

## [0.7.0] - 2026-08-17

### Added — vector embeddings for semantic search (ArcadeDB)

- **`--vector-embeddings FIELD,FIELD`** on the `arcadedb` command. Names the
  ontology fields whose text becomes a vector, generates the vectors locally and
  indexes them as `LSM_VECTOR` next to the existing full-text index. No API key,
  no data leaving the machine. Also configurable as
  `[arcadedb.embeddings].fields`; the flag wins, as `--database` does.
- **Why it earns its place.** Measured on the FACE/UFMG corpus (210 concepts),
  with five natural-language questions whose vocabulary is deliberately disjoint
  from the descriptions: the vector search returns the exact concept in **4 of 5**,
  BM25 in none. "quem manda nas decisões da empresa" reaches
  `decisões_estratégicas`, which full-text search cannot find — the two share no
  word. The goal is not to replace keyword search but to complement it.
- **Field selection is template-driven, not a fixed list.** Each requested field
  is checked against the template: an unknown name is an error listing the
  available ones, a closed vocabulary (ORDERED/ENUMERATED/SCALE) is a warning,
  and a field holding a single distinct value across the corpus is dropped —
  it would add identical text to every concept and discriminate nothing.
  (Measured: `theoretical_significance` is `0` in all 1388 concepts of the
  Social_Acceptance corpus.)
- **The model is per project**, in `[arcadedb.embeddings].model`. A Portuguese
  corpus and an English one have different requirements, and that is a research
  decision rather than a code one. The default is multilingual for a measured
  reason: on the question most dependent on Portuguese semantics, the
  English-only `all-MiniLM-L6-v2` reproduced exactly the lexical error BM25
  makes.
- **Vectors are cached** in `<project>.embeddings.json` (git-ignored). Only the
  concepts whose text changed are recomputed; a fully cached run does not load
  the model at all. Measured on face85: 11s cold, 1s warm. `--rebuild-embeddings`
  forces a full recompute.
- The sidecar records the model, the dimensions and a hash of the field
  composition. Changing any of them invalidates every vector, because vectors
  from different models — or different field compositions — are individually
  valid and mutually meaningless: the distance between them measures the
  composition, not the meaning, and search degrades with nothing to show for it.
- Requires the optional extra: `pip install "synesis-graph[embeddings]"`.
  Absent, the module behaves exactly as before and the flag reports an
  actionable `DependencyError` rather than an `ImportError`.
- **Validated end to end on two real corpora**, both embedded and queryable in
  their ArcadeDB databases: FACE/UFMG (210 concepts, Portuguese, multilingual
  MiniLM) and Social_Acceptance (1388 concepts, English, `all-mpnet-base-v2`).
  See the README's "Case studies" section for the measured generation times and
  live query results.

### Changed

- `GraphPayload` carries the template's `field_specs`. `analyze_template` splits
  fields by destination and drops the declared type — TOPIC and ORDERED both land
  in `graph_fields` — so embedding selection had no way to tell a text field from
  a closed vocabulary. Additive and defaulted; no backend is affected.

---

## [0.6.0] - 2026-08-15

### Added — ArcadeDB backend

- **New backend: `synesis-graph arcadedb`.** ArcadeDB implements OpenCypher natively,
  so the graph-writing statements are the *same ones* the Neo4j backend uses — they are
  imported and called, not copied, which keeps one definition of the MERGE semantics.
  Validated against the FACE/UFMG corpus: every structural count matches the Neo4j
  export (210 concepts, 20 sources, 174 items, 168 `RELATES_TO`, 348 `MENTIONS`,
  78 `IS_LINKED_TO`, 99 `MAPPED_TO_ASPECT`), and the top-10 by degree is identical.
- **No new dependency.** The backend talks HTTP/JSON over `urllib` (standard library),
  so it adds nothing to install — not even an optional extra. ArcadeDB also speaks the
  BOLT protocol, which would have allowed reusing the Neo4j driver, but that plugin is
  not loaded by default and has no persistent configuration file: enabling it means
  passing a flag on *every* server start. The HTTP API works on a stock installation.
- Configure it with an `[arcadedb]` block in `config.toml`. Only `password` is
  required; `uri` defaults to `http://localhost:2480` and `user` to `root`. **The URI is
  the HTTP endpoint — the one that also serves ArcadeDB Studio — not a `bolt://` URL.**
- `fulltext_analyzer` is portable between the two backends. Neo4j names analyzers by a
  short label (`brazilian`), ArcadeDB by the Lucene class
  (`org.apache.lucene.analysis.br.BrazilianAnalyzer`); short names are expanded
  automatically and anything else is passed through, so the server stays the authority.

### Changed — `--database` applies to every database backend

- The flag was gated on `backend == neo4j`, so `--database` was silently ignored when
  another database backend was selected. It now applies to any backend that has a
  database to name, which is what the help text already promised.

### Added — schema declaration for ArcadeDB

- **Cypher writes properties without declaring them, and ArcadeDB refuses to index an
  undeclared property**: `Cannot create the index on type 'Chain.search_name' because
  the property does not exist`. This affects every index, not only the full-text ones.
- The backend therefore declares the types and the indexed properties before writing.
  Only *indexed* properties are declared — everything else stays schema-less, so a
  project remains free to carry any field the template defines without the backend
  knowing its name.
- Two further ArcadeDB specifics, both found against a live server: index names come
  back from introspection as `Item[item_id]`, which is a syntax error in `DROP INDEX`
  unless backticked; and re-creating an index needs `IF NOT EXISTS`, otherwise a
  re-export fails with `Index '...' already exists`.

### Added — graph metrics for ArcadeDB, and a corrected persistence path

- `pagerank`, `betweenness` and `community` are computed with ArcadeDB's built-in
  `algo.*` library. No plugin is involved, so unlike the Neo4j path there is no
  "GDS not installed" degradation.
- **Two plausible ways of persisting those results fail silently, and the second is
  worse than the first.** `CALL algo.pagerank() YIELD node, score SET node.pagerank =
  score` writes nothing and reports `stats: null` — `YIELD node` is a serialized RID
  string, not a bindable vertex. The apparent fix,
  `MATCH (c:Label) WHERE id(c) = id(node)`, *corrupts data*: `id()` of a string is not
  comparable to `id()` of a vertex, the predicate degenerates, and the MATCH becomes a
  cartesian product — measured writing concept scores onto `Item` nodes.
- The RID is now resolved client-side (`@rid` → the concept's unique `name`) and the
  values are written back with a single `UNWIND`. Rows whose RID is not a concept are
  dropped there, which is also the scope filter the algorithms do not provide.
- **Scores are not directly comparable to Neo4j's.** GDS projects only the concept
  subgraph; `algo.*` runs over the whole graph and accepts no scope filter —
  `edgeTypes`, `relationship` and friends are accepted and ignored, and `weightProperty`
  set to zero yields a uniform score rather than isolating a subgraph. On FACE/UFMG the
  two top-10 PageRank lists overlap by 6 of 10. The pipeline states this on every
  export.

### Fixed — concept search was unreachable by natural language

- **A full-text index over a snake_case `name` matched nothing a person would
  type.** Lucene's tokenizer follows UAX#29, where the underscore is a word
  character rather than a boundary, so `governança_corporativa` was indexed as a
  single token. Measured against face85: `"governança corporativa"`,
  `"governança"` and `"corporativa"` all failed to retrieve a node that plainly
  existed, while the exact string with the underscore worked. The index reported
  `populationPercent: 100` throughout — it was built correctly and answered
  nothing.
- Concepts now carry `search_name`, the same words separated by spaces
  (`humanize_concept_name`), and `concept_search` indexes that instead of `name`.
  Synesis guarantees the snake_case — `SYNESIS_E015` rejects spaces in a concept,
  since the parser needs `_` where the chain separator `->` does not reach — so
  the derivation is mechanical and template-agnostic.
- `name` is untouched: it remains the MERGE key, the uniqueness constraint and the
  identity every edge resolves against. Exact-identifier lookups keep using
  `MATCH` on `name`, served by the constraint's RANGE index.
- After the fix, all five phrasings retrieve the node in first place (score 4.74
  for the full phrase).

### Added — configurable full-text analyzer (`fulltext_analyzer`)

- New optional key in the `[neo4j]` config block. Defaults to Neo4j's own
  `standard-no-stop-words`, which does no stemming and no accent folding — safe
  for any language, optimal for none.
- Setting it to the corpus language measurably improves recall: under `brazilian`,
  face85 also matches `governanca` (no cedilla) and `governancas` (plural), which
  the default misses entirely.
- It belongs in the config rather than the code because the right value follows
  the corpus: `brazilian` suits face85 and would degrade the English-language
  factors corpus. Nothing in the template declares a language today.
- **Indexes are now dropped before being recreated.** Neo4j refuses a second index
  over the same (label, properties) pair, so `CREATE ... IF NOT EXISTS` succeeded
  silently while leaving the *old* index in place — a changed analyzer would never
  have taken effect and nothing would have said so.

### Changed — pipeline output is written for researchers, not DBAs

- The Neo4j driver logged raw server notifications during a normal export, e.g.
  `Neo.ClientNotification.Schema.IndexOrConstraintDoesNotExist` repeated once per
  index. Nothing was wrong: the sync wipes the database first, so the subsequent
  `DROP INDEX ... IF EXISTS` legitimately finds nothing. The notices were
  addressed to database engineers and buried the readable `[STEP]`/`[OK]` output.
- Server notifications are now filtered at the source (the driver requests
  `WARNING` and above), and the `neo4j.*` loggers are capped so nothing slips
  through a different path. Real problems still surface as warnings.
- `-v` lifts the filter and restores the full notification stream for debugging.

### Added — full-text indexes derived from the template

- **The graph had no search index at all.** Constraints guaranteed integrity, but
  nothing served retrieval: the only way in was text2cypher with exact string
  matching, so a question that missed a concept's literal name retrieved nothing.
- Added `_create_search_indexes`, run right after the constraints, creating three
  full-text indexes: `concept_search`, `item_search` and `source_search`.
- **Every indexed property comes from the template — none is hardcoded.** Concept
  prose lives in `scalar_fields`, which is `ontology_description` in one project
  and `factor_description` in another; Source prose is whichever SCOPE SOURCE
  field was declared `TEXT`. Naming a fixed property would have created the index
  successfully and indexed nothing.
- `graph_fields` are deliberately excluded: `TOPIC`/`ENUMERATED`/`ORDERED` become
  taxonomy nodes of their own, and indexing a closed vocabulary as prose only
  dilutes the index. `Item` is indexed on `citation`/`description`, the structural
  names the payload normalises from the template's `QUOTATION` and `MEMO` fields.
- Validated against face85 with 40 corpus-derived terms (Portuguese and English,
  from 1 to 80 occurrences): `item_search` and `source_search` both retrieve every
  node containing the term — 100% recall.
- Field names are interpolated into the Cypher, so each one is checked with
  `validate_cypher_label` — the same guard `_create_constraints` already applied
  to labels.

### Changed — `analyze_template` reports the declared type of each SOURCE field

- `analyze_template` now returns `list[SourceFieldSpec]` instead of `list[str]` in
  the `source_fields` position, mirroring the existing `ChainFieldSpec` and
  `CodeFieldSpec`. Each spec carries `field_name` and `field_type`.
- This is what lets the Source index include prose while leaving a closed
  vocabulary out: in the FACE/UFMG template, `description` and `method` (`TEXT`)
  are indexed, `knowledge_area` (`ENUMERATED`) stays a node property but never
  enters the index.
- **The returned tuple still has 8 positions** — the type rides inside the spec
  rather than as a ninth element, so the five call sites that unpack it
  positionally are unaffected.
- Added `source_field_names()` and `text_source_field_names()`. Both accept plain
  strings as well as specs, so hand-built payloads (tests, the `synesis2graph`
  shim) keep working.
- Covered by `tests/test_search_indexes.py`.

### Fixed — ITEM template fields now reach the Neo4j node

- **The `Item` node carried only `item_id`, `citation` and `description`.** Every
  other ITEM field declared in the template (`zone`, `confidence`, `score`, ...)
  was diverted to the HTML-only `item_fields` map and never reached the graph.
  The preview was therefore richer than the database serving the GraphRAG: a
  rhetorical filter such as "only evidence from `Result` sections" was
  unexpressible in Cypher even though the value existed in the `.syn` and was
  visible on screen.
- The detour was justified by Neo4j rejecting nested-map properties, but
  `_extract_item_extra` already returns `dict[str, str]` — flat scalars. Only the
  nested key stood in the way, so flattening the fields onto the row is enough;
  `SET i = row` in `_sync_items` already writes every key it receives.
- Added `_build_item_row`, used by both the CHAIN and the CODE branch of
  `_extract_corpus_data`. Structural keys always win: a template free to name a
  field `citation` cannot overwrite the quotation the node is built around.
- `item_fields` is unchanged, so the HTML evidence view keeps working as before.
- Covered by `tests/test_item_fields.py` — the first tests over the payload sent
  to Neo4j, which had no coverage until now.

**Existing databases need a re-export to pick the fields up**; the sync clears the
database before writing, so no migration is required.

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

### Added — contrato de empacotamento (pre-PyPI)

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
    `{text = "..."}`, os testes falham. `twine check` **passa** nesse cenário —
    é por isso que ele não basta, e é a causa provável do `license: None` que
    o PyPI hoje mostra para `synesis` e `synesis-lsp`.
- `pyyaml` acrescentado ao extra `dev` (o teste lê o `CITATION.cff`).
- **`pypa/gh-action-pypi-publish` pinada por SHA** — era a única action ainda em
  ref mutável (`@release/v1`) neste repositório, justamente a que tem permissão
  de publicar.

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
