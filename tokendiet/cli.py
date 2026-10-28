"""Command line: python -m tokendiet <command> [repo] ..."""

import argparse
import json
import os
import sys

from . import tokens
from .graph import Graph

CLAUDE_MD = """## Code navigation (tokendiet)
This repo is indexed by the `tokendiet` MCP server. For navigation, prefer it over grep/glob/whole-file reads:
- Start of a session: `repo_map` (add `focus` with the feature/area you are working on). Read its notes.
- Where is X / what does X look like: `find_symbol` → then Read only the returned line range.
- Before editing a function: `symbol_context` (source + callees + call sites).
- Before changing shared code: `impact` (dependents + tests to run).
- When you learn something non-obvious about the code, save it with `notes` action=add.
Grep is still right for string literals, config keys, and text that is not a code symbol.
"""


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tokendiet")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("index", "stats", "serve"):
        p = sub.add_parser(name)
        p.add_argument("repo", nargs="?", default=".")
    p = sub.add_parser("map"); p.add_argument("repo"); p.add_argument("--focus", default=""); p.add_argument("--budget", type=int, default=2000)
    p = sub.add_parser("find"); p.add_argument("repo"); p.add_argument("query"); p.add_argument("--body", action="store_true")
    p = sub.add_parser("context"); p.add_argument("repo"); p.add_argument("name"); p.add_argument("--path", default="")
    p = sub.add_parser("impact"); p.add_argument("repo"); p.add_argument("target"); p.add_argument("--depth", type=int, default=2)
    p = sub.add_parser("note"); p.add_argument("repo"); p.add_argument("action", choices=["get", "add", "delete"]); p.add_argument("target", nargs="?", default=""); p.add_argument("text", nargs="?", default="")
    sub.add_parser("claude-md")
    p = sub.add_parser("init", help="enable tokendiet in a repo for Claude Code and Codex")
    p.add_argument("repo", nargs="?", default=".")
    p.add_argument("--agents", default="", help="comma list of claude,codex,cursor,copilot (default: auto-detect)")
    p.add_argument("--vscode", action="store_true", help="also configure VS Code Copilot agent mode")
    p.add_argument("--no-hook", action="store_true", help="skip the SessionStart hook that injects the map each session")
    p.add_argument("--budget", type=int, default=1500, help="token budget of the map injected at session start")
    p = sub.add_parser("uninit", help="remove what init added"); p.add_argument("repo", nargs="?", default=".")
    p = sub.add_parser("report", help="how much tokendiet saved in this repo")
    p.add_argument("repo", nargs="?", default="."); p.add_argument("--days", type=int)
    p.add_argument("--format", choices=["text", "json"], default="text")
    p.add_argument("--quick", action="store_true", help="skip the Claude Code session comparison")
    p = sub.add_parser("session-start", help="SessionStart hook entry point"); p.add_argument("--budget", type=int, default=1500)
    p = sub.add_parser("mcp-config"); p.add_argument("repo")
    p = sub.add_parser("update", help="upgrade tokendiet from PyPI and refresh this repo's setup")
    p.add_argument("repo", nargs="?", default="."); p.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)
    p = sub.add_parser("sync", help="rewrite the files init added if they came from another version")
    p.add_argument("repo", nargs="?", default=".")
    a = ap.parse_args(argv)
    if a.cmd != "serve":
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252

    if a.cmd == "update":
        from . import update
        if a.auto:
            update.auto()
            return
        from . import __version__
        if update.update() != __version__:
            # the new code is on disk but not loaded here; let it rewrite the repo files
            import subprocess
            subprocess.run([sys.executable, "-m", "tokendiet", "sync", a.repo])
            print("updated. Restart the agent session to load the new version.")
        return
    if a.cmd == "sync":
        from . import setup_agents
        print("setup refreshed" if setup_agents.sync(a.repo) else "setup already current")
        return
    if a.cmd == "claude-md":
        print(CLAUDE_MD)
        return
    if a.cmd == "mcp-config":
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        py = os.path.join(root, ".venv", "Scripts" if os.name == "nt" else "bin", "python.exe" if os.name == "nt" else "python")
        cfg = {"mcpServers": {"tokendiet": {"command": py, "args": ["-m", "tokendiet", "serve", os.path.abspath(a.repo)],
                                           "env": {"PYTHONPATH": root}}}}
        print(json.dumps(cfg, indent=2))
        return
    if a.cmd in ("init", "uninit", "session-start"):
        from . import setup_agents
        if a.cmd == "init":
            agents = [x.strip() for x in a.agents.split(",") if x.strip()] or None
            bad = [x for x in agents or [] if x not in setup_agents.AGENTS]
            if bad:
                ap.error(f"unknown agent(s): {', '.join(bad)}; choose from {', '.join(setup_agents.AGENTS)}")
            setup_agents.init(a.repo, agents=agents, hook=not a.no_hook, budget=a.budget, vscode=a.vscode)
        elif a.cmd == "uninit":
            setup_agents.uninit(a.repo)
        else:
            setup_agents.session_start(a.budget)
        return
    if a.cmd == "serve":
        from .server import serve
        repo = a.repo
        if repo == "." and os.environ.get("CLAUDE_PROJECT_DIR"):
            repo = os.environ["CLAUDE_PROJECT_DIR"]
        serve(repo)
        return

    if a.cmd == "report":
        from . import meter
        g = Graph(a.repo)
        g.refresh(force=True)
        print(meter.render(meter.report(g, a.days, sessions=not a.quick), a.format))
        return

    g = Graph(a.repo)
    r = g.ix.refresh(force=True, verbose=True)
    if a.cmd == "index":
        print(json.dumps(r))
    elif a.cmd == "stats":
        con = g.con
        q = lambda s: con.execute(s).fetchone()[0]
        db = g.ix.repo / ".tokendiet" / "graph.db"
        print(json.dumps({
            "files": q("SELECT COUNT(*) FROM files"), "symbols": q("SELECT COUNT(*) FROM symbols"),
            "calls": q("SELECT COUNT(*) FROM calls"), "imports": q("SELECT COUNT(*) FROM imports"),
            "imports_resolved": q("SELECT COUNT(*) FROM imports WHERE target_id IS NOT NULL"),
            "source_tokens": q("SELECT COALESCE(SUM(tokens),0) FROM files"),
            "db_mb": round(db.stat().st_size / 1e6, 2),
            "langs": dict(con.execute("SELECT lang, COUNT(*) FROM files GROUP BY lang").fetchall()),
        }, indent=2))
    else:
        if a.cmd == "map":
            out = g.repo_map(a.focus, a.budget)
        elif a.cmd == "find":
            out = g.find_symbol(a.query, a.body)
        elif a.cmd == "context":
            out = g.symbol_context(a.name, a.path)
        elif a.cmd == "impact":
            out = g.impact(a.target, a.depth)
        else:
            out = g.notes(a.action, a.target, a.text)
        sys.stdout.reconfigure(encoding="utf-8")
        print(out)
        print(f"\n[{tokens.count(out)} tokens]", file=sys.stderr)


if __name__ == "__main__":
    main()
