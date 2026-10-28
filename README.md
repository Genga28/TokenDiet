# tokendiet

A local code graph for coding agents, served over MCP. The agent asks the graph ("where is X, who uses it, what
breaks if I change this file") instead of grepping and reading whole files, and keeps what it learns in notes
that survive across sessions and flag themselves stale when the code changes.

- Local only: tree-sitter + SQLite. No embeddings, no network, no code leaves the machine.
- 5 tools, ~500 tokens of schema (re-sent every turn, so it is kept small).
- Index stays fresh on its own: every call re-checks file mtimes and re-parses only changed files (~0.1 s on
  100 files, ~0.2 s on 500), so uncommitted edits and branch switches are picked up.
- Languages: Python, JS/TS/TSX, Go, Java, Rust, C/C++, C#, Ruby, PHP, Kotlin, Swift, Scala (best-effort for the last few).

## Install

**VS Code (one click):** install the TokenDiet extension (until it's on the Marketplace:
`code --install-extension dist/tokendiet-0.1.0.vsix`), then *TokenDiet: Enable for this workspace* or click
*TokenDiet: off* in the status bar. It sets up its own Python environment (needs Python 3.10+), runs `init`, and
shows tokens saved in the status bar; click it for the report. Build the .vsix with `python vscode/build_vsix.py`.

**Command line:** see Setup below.

## Measuring it

```
tokendiet report            # in the repo; --format json, --days N, --quick
```

From the tool's own call log: tokens served vs what a careful agent (grep + ranged reads) and a typical one
(grep + whole-file reads) would have pulled in, billed-token estimate including context carry, notes carried,
and Claude Code session medians before vs after enabling (from `~/.claude/projects`). The per-call estimates
were checked against measured grep/read output and run low (median 0.84x), so the report under-counts.

## Setup

```
pipx install tokendiet          # or: pip install tokendiet   (from this folder: pip install -e .)
cd your-repo
tokendiet init                  # enable for Claude Code + Codex, build the index
tokendiet uninit                # undo; removes only what init added
```

`init` detects the agents in use (on this machine or in the repo) and merges into existing files, marking what it adds:

| agent | files | what |
|---|---|---|
| Claude Code | `.mcp.json`, `.claude/skills/tokendiet/SKILL.md`, `CLAUDE.md` section, `.claude/settings.local.json` | tools; usage guide; SessionStart hook that injects the map + notes at startup, `/clear` and compaction (local settings: holds a machine path) |
| Codex | `AGENTS.md` section | call `repo_map` first, use the tools, save notes |
| Copilot (VS Code) | `.vscode/mcp.json`, `.github/copilot-instructions.md` section | tools + instructions (`--vscode`, or detected) |
| Cursor | `.cursor/mcp.json`, `.cursor/rules/tokendiet.mdc` | tools + always-on rule |

Codex has no SessionStart hook; `init` prints the `~/.codex/config.toml` entry to add (it never edits global
config). Options: `--agents claude,codex,cursor,copilot`, `--vscode`, `--no-hook`, `--budget N` (injected map size, default 1500).

The index and notes live in `<repo>/.tokendiet/` (self-gitignored). Notes survive re-indexing and upgrades.

## Tools

| tool | use it for | typical size |
|---|---|---|
| `repo_map(focus, budget)` | session start: ranked outline + notes from earlier sessions | ≤ budget (2k) |
| `find_symbol(query, include_body)` | where is X; `Class.method` works; large classes come back as an outline | 150–550 |
| `symbol_context(name, path)` | source + resolved callees + call sites (with confidence) + non-call references | 200–600 |
| `impact(target, depth)` | dependents of a file/symbol, depth 2, and tests touching it | 600–1,800 |
| `notes(action, target, text)` | durable findings pinned to a file/symbol + content hash | small |

CLI mirrors: `map`, `find`, `context`, `impact`, `note`, `stats`.

## Benchmarks

See [RESULTS.md](https://github.com/Genga28/TokenDiet/blob/main/RESULTS.md). Reproduce:

```
python -m bench.transcripts            # where tokens go in your own Claude Code sessions
python -m bench.session_sim            # replay those sessions under an early-reset policy
python -m bench.offline <repo> ...     # navigation tasks: grep+read vs tokendiet, with recall checks
python -m bench.live_ab <repo>         # real agent A/B via `claude -p` (spends tokens; capped per run)
```

## Known limits

- Callers are resolved by name, preferring import evidence. Dynamic dispatch, DI containers, reflection,
  string-based routing and lowercase property access (`res.header`) that isn't a call are not seen.
  `symbol_context` labels each call site direct / likely / name-match.
- Token counts in offline benchmarks use tiktoken cl100k as a proxy for Claude's tokenizer; ratios hold, absolute
  numbers run somewhat low on code. Live A/B uses the API's own counts.
- Files > 512 KB, minified and generated files are skipped.

## License

MIT © 2026 [Genga28](https://github.com/Genga28). See [LICENSE](https://github.com/Genga28/TokenDiet/blob/main/LICENSE).
