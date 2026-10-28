# tokendiet — plan

A local code graph exposed to coding agents (Claude Code, Cursor, VS Code agents) over MCP, so that the
agent asks the graph instead of grepping and reading whole files — and doesn't redo that exploration
every session.

## 1. The problem, in measurable terms

What agentic-coding users actually complain about (research, Oct 2026):

| Concern | What happens | What we can measure |
|---|---|---|
| Exploration burn | Agent greps, then `Read`s whole files to find one function | tokens in Read/Grep/Glob/Bash results |
| Context carry cost | Every tool result is re-sent (cached) on every later turn until compaction | result tokens × turns carried |
| Re-exploration every session | New session = re-read the same files, re-explain the same repo | same file read in ≥2 sessions |
| Fixed session overhead | System prompt + tool schemas + CLAUDE.md before any work (reports of ~50% of 200k) | first-turn cache write |
| MCP bloat | Servers with 14–40 tools cost thousands of tokens of schema per turn | schema tokens of *our* tools |
| Stale indexes | Indexer answers from old code after edits / branch switch | freshness checks per call |
| Agent ignores the tool | Model keeps grepping | CLAUDE.md snippet + tool descriptions |
| Inflated benchmarks | Vendor "120x" claims; independent tests show ~45% | publish raw per-task numbers + baseline model |

## 2. Design choices (each answers a row above)

1. **Few tools, terse schemas** — 5 tools; schema cost measured and reported.
2. **Budgeted outputs** — every tool takes/obeys a token budget; nothing returns a whole file unless asked.
3. **Symbol-level answers** — `path:start-end` + signature, body only on request, so the agent can do a ranged `Read`.
4. **Always fresh** — each call stats indexed files (throttled) and re-parses only changed hashes; new/deleted files picked up. Works on uncommitted edits and branch switches.
5. **Cross-session notes** — `notes` tool pins what the agent learned to a file/symbol + content hash; notes auto-flag as stale when the code changes. Replaces "re-explain the repo every session".
6. **Deterministic, cache-friendly output** — stable ordering so a repo map placed early in context stays a prompt-cache hit.
7. **Local only** — tree-sitter + SQLite + PageRank. No embeddings, no network, no code leaves the machine.
8. **Honest about gaps** — dynamic dispatch, reflection, string imports are invisible to static parsing; answers say "name-resolved, verify" when ambiguous.

## 3. Components

- `tokendiet/indexer.py` — file discovery (`git ls-files` or walk with ignores), tree-sitter parse → symbols, calls, identifier refs, imports; incremental by content hash.
- `tokendiet/store.py` — SQLite (`.tokendiet/graph.db`, WAL).
- `tokendiet/graph.py` — file graph from imports + name references, pure-Python PageRank (personalised by focus), queries.
- `tokendiet/server.py` — MCP stdio server: `repo_map`, `find_symbol`, `symbol_context`, `impact`, `notes`.
- `tokendiet/cli.py` — `index`, `map`, `find`, `context`, `impact`, `stats`, `serve`.
- `bench/transcripts.py` — real numbers from Claude Code session logs (`~/.claude/projects/*/*.jsonl`).
- `bench/offline.py` — per-repo task suite: baseline exploration tokens vs tokendiet tokens, + recall check.
- `bench/live_ab.py` — (needs CLI/API access) same prompts with and without the MCP server, real `usage`.

VS Code extension: not needed for v1 — Claude Code's VS Code extension, Cursor and Copilot agent mode all
load MCP servers. A thin extension (status bar + "index now") can come later.

## 4. Edge cases

- Monorepos / huge repos: size cap per file (512 KB), skip minified/generated/lockfiles/vendored dirs; index time + DB size reported.
- Non-UTF-8 / binary files: decoded with replacement; binary skipped.
- Windows paths: stored as forward-slash relative paths.
- Unsupported languages: still listed at file level in the map.
- Same-name symbols across files: callers resolved by import evidence first, otherwise flagged ambiguous.
- Dynamic imports / DI / reflection / decorators-as-routing: not visible; stated in output.
- Deleted / renamed files: removed on refresh; notes attached to them marked orphaned.
- Concurrent sessions on one repo: SQLite WAL; indexing is idempotent.
- First index on a big repo: progress printed; tools work on partial index.
- Symlink loops: not followed.

## 5. Benchmark protocol (no vendor-style claims)

1. **Problem size (real)** — from your transcripts: tokens per session, % of context from exploration tools,
   carry cost, re-reads within and across sessions, session-start overhead.
2. **Offline A/B per repo** — auto-generated tasks (locate, understand-with-callers, impact, orient).
   Baseline = the grep → read-files pattern observed in the transcripts. Report median and p90, not best case,
   plus caller recall vs grep ground truth so savings aren't bought with wrong answers.
3. **Live A/B** (when CLI/API key is available) — same prompts, with/without MCP, report `usage` from the API.

Token counts use tiktoken `cl100k_base` as a proxy for Claude's tokenizer (Claude typically counts somewhat more on code);
ratios between baseline and tokendiet are unaffected. Live A/B uses real API counts.
