# TokenDiet

Your AI coding agent spends most of its tokens re-reading your repo and carrying it around in context.
TokenDiet builds a local code graph of the workspace and gives Claude Code, Codex, Cursor and Copilot five
small tools to ask it instead: *where is X, who uses it, what breaks if I change this file*. It also
carries a repo map and the agent's own notes into every new session, so `/clear` stops meaning
"explain the repo again".

## Use

1. Install the extension.
2. Command palette → **TokenDiet: Enable for this workspace** (or click *TokenDiet: off* in the status bar).
3. Start a new agent chat. The status bar shows tokens saved; click it for the report.

First enable sets up a private Python environment (needs Python 3.10+ on the machine) — about a minute, once.

## What Enable writes in your repo

| file | for |
|---|---|
| `.mcp.json`, `.claude/skills/tokendiet/`, `CLAUDE.md` section, `.claude/settings.local.json` hook | Claude Code (map + notes injected at every session start, `/clear` and compaction) |
| `AGENTS.md` section | Codex and other AGENTS.md agents |
| `.vscode/mcp.json`, `.github/copilot-instructions.md` section | Copilot agent mode |
| `.cursor/mcp.json`, `.cursor/rules/tokendiet.mdc` | Cursor (if detected) |
| `.tokendiet/` | index, usage log, notes (self-gitignored) |

**TokenDiet: Disable** removes exactly these additions and leaves the rest of each file untouched.

## The numbers

The report shows, from the tool's own call log: tokens served vs what a careful agent (grep + ranged reads) and
a typical one (grep + whole-file reads) would have pulled in, plus Claude Code session medians before vs after
enabling. On 4 open-source repos the tools used 55–81% fewer tokens than the careful baseline per navigation
task, with 95–100% correct definitions and 0.70–0.97 usage recall. Estimates under-count by design.

Everything runs locally. No code leaves your machine.
