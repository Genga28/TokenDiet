"""`tokendiet init` / `tokendiet uninit`: wire a repo up for the coding agents in use.

Detects Claude Code, Codex, Cursor and VS Code Copilot (on this machine or in the repo) and writes, per agent,
its instruction file and MCP config. Everything is merged into existing files and marked, so uninit removes
only what init added.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __version__

BEGIN, END = "<!-- tokendiet:begin -->", "<!-- tokendiet:end -->"
HOOK_TAG = "tokendiet session-start"
AGENTS = ("claude", "codex", "cursor", "copilot")

GUIDE = """- At the start of a task call `repo_map` (set `focus` to the area you are working on). It includes notes from earlier sessions.
- Where is X / what does it look like: `find_symbol`, then read only the returned line range.
- Before editing a function: `symbol_context` (source, what it calls, every call site with confidence).
- Before changing shared code: `impact` (dependents to depth 2, tests to run).
- When you learn something non-obvious (an invariant, where a flow really starts, a trap), save it with `notes` action=add
  on that file or symbol. Notes appear in every later session and are flagged STALE when that code changes.
- Grep is still right for string literals, config keys and log messages. Callers are resolved by name; check `name-match` results."""

SKILL_MD = f"""---
name: tokendiet
description: Navigate this codebase with the tokendiet code graph (repo_map, find_symbol, symbol_context, impact, notes) instead of grep and whole-file reads. Use when locating code, understanding a function before editing it, checking what a change affects, or saving what you learned for later sessions.
---

# tokendiet

A repo map and notes from earlier sessions are injected at session start. Build on them; do not re-explore
what they already cover.

{GUIDE}
"""

SECTION = f"""{BEGIN}
## Code navigation (tokendiet)
This repo has a code graph available as the `tokendiet` MCP server. Prefer it over grep + whole-file reads.
{GUIDE}
{END}
"""

CURSOR_RULE = f"""---
description: Use the tokendiet code graph for code navigation
alwaysApply: true
---
{SECTION}"""


# ---------- file helpers ----------

def _read_json(p: Path):
    if p.exists() and p.read_text(encoding="utf-8").strip():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _write_json(p: Path, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _read_text(p: Path):
    """Text with \\n line endings, plus the file's own line ending so it is written back unchanged."""
    raw = p.read_bytes().decode("utf-8") if p.exists() else ""
    return raw.replace("\r\n", "\n"), ("\r\n" if "\r\n" in raw else "\n")


def _write_text(p: Path, text: str, eol: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.replace("\n", eol).encode("utf-8"))


def _upsert_section(p: Path, section: str):
    text, eol = _read_text(p)
    if BEGIN in text and END in text:
        pre, rest = text.split(BEGIN, 1)
        post = rest.split(END, 1)[1].lstrip("\n")
        text = pre + section + post
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + section
    _write_text(p, text, eol)


def _remove_section(p: Path):
    if not p.exists():
        return False
    text, eol = _read_text(p)
    if BEGIN not in text:
        return False
    pre, rest = text.split(BEGIN, 1)
    post = rest.split(END, 1)[1].lstrip("\n") if END in rest else ""
    text = pre.rstrip("\n") + ("\n\n" + post if post.strip() else "\n")
    if text.strip():
        _write_text(p, text, eol)
    else:
        p.unlink()
    return True


def _set_server(p: Path, key: str, entry: dict):
    data = _read_json(p)
    data.setdefault(key, {})["tokendiet"] = entry
    _write_json(p, data)


def _drop_server(p: Path, key: str):
    data = _read_json(p)
    if data.get(key, {}).pop("tokendiet", None) is None:
        return False
    if data[key] or len(data) > 1:
        _write_json(p, data)
    else:
        p.unlink()
    return True


def _rmdir_if_empty(*dirs):
    for d in dirs:
        if d.exists() and d.is_dir() and not any(d.iterdir()):
            d.rmdir()


# ---------- detection ----------

def detect(repo: Path, vscode=False):
    home = Path.home()
    found = []
    if (home / ".claude").exists() or (repo / ".claude").exists() or (repo / "CLAUDE.md").exists():
        found.append("claude")
    if (home / ".codex").exists() or (repo / "AGENTS.md").exists():
        found.append("codex")
    if (home / ".cursor").exists() or (repo / ".cursor").exists():
        found.append("cursor")
    if vscode or (repo / ".github" / "copilot-instructions.md").exists():
        found.append("copilot")
    return found or ["claude", "codex"]


def _command(repo_arg):
    if shutil.which("tokendiet"):
        return "tokendiet", ["serve", repo_arg]
    return sys.executable, ["-m", "tokendiet", "serve", repo_arg]


# ---------- init / uninit ----------

def init(repo, agents=None, hook=True, budget=1500, vscode=False):
    repo = Path(repo).resolve()
    agents = agents or detect(repo, vscode)
    done = []
    if "claude" in agents:
        cmd, args = _command(".")
        _set_server(repo / ".mcp.json", "mcpServers", {"command": cmd, "args": args})
        done.append(f".mcp.json  (Claude Code MCP server: {Path(cmd).name} {' '.join(args)})")
        skill = repo / ".claude" / "skills" / "tokendiet" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(SKILL_MD, encoding="utf-8")
        done.append(".claude/skills/tokendiet/SKILL.md")
        _upsert_section(repo / "CLAUDE.md", SECTION)
        done.append("CLAUDE.md  (tokendiet section)")
        # machine-specific interpreter path, so it goes in the uncommitted local settings
        settings = repo / ".claude" / "settings.local.json"
        data = _read_json(settings)
        # .mcp.json servers stay disabled until approved, and the VS Code panel may never show the prompt
        approved = data.setdefault("enabledMcpjsonServers", [])
        if "tokendiet" not in approved:
            approved.append("tokendiet")
        if hook:
            hooks = data.setdefault("hooks", {}).setdefault("SessionStart", [])
            hooks[:] = [h for h in hooks if not any(HOOK_TAG in x.get("command", "") for x in h.get("hooks", []))]
            hooks.append({"matcher": "startup|clear|compact", "hooks": [
                {"type": "command", "command": f'"{sys.executable}" -m tokendiet session-start --budget {budget}', "timeout": 30}]})
        _write_json(settings, data)
        done.append(".claude/settings.local.json  (MCP server approved" +
                    ("; SessionStart hook: map + notes injected at startup, /clear, compaction)" if hook else ")"))
    if "codex" in agents:
        _upsert_section(repo / "AGENTS.md", SECTION)
        done.append("AGENTS.md  (tokendiet section; also read by other AGENTS.md agents)")
    if "cursor" in agents:
        cmd, args = _command(str(repo))
        _set_server(repo / ".cursor" / "mcp.json", "mcpServers", {"command": cmd, "args": args})
        _write_text(repo / ".cursor" / "rules" / "tokendiet.mdc", CURSOR_RULE, "\n")
        done.append(".cursor/mcp.json + .cursor/rules/tokendiet.mdc")
    if "copilot" in agents:
        cmd, args = _command("${workspaceFolder}")
        _set_server(repo / ".vscode" / "mcp.json", "servers", {"type": "stdio", "command": cmd, "args": args})
        _upsert_section(repo / ".github" / "copilot-instructions.md", SECTION)
        done.append(".vscode/mcp.json + .github/copilot-instructions.md  (VS Code Copilot agent mode)")

    from .graph import Graph
    g = Graph(repo)
    r = g.ix.refresh(force=True, verbose=True)
    g.con.execute("INSERT OR IGNORE INTO meta VALUES('enabled_at', ?)", (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    g.con.execute("INSERT OR REPLACE INTO meta VALUES('agents', ?)", (",".join(agents),))
    # lets sync() rewrite these files with the same choices after an upgrade
    g.con.execute("INSERT OR REPLACE INTO meta VALUES('init_version', ?)", (__version__,))
    g.con.execute("INSERT OR REPLACE INTO meta VALUES('init_args', ?)",
                  (json.dumps({"agents": agents, "hook": hook, "budget": budget}),))
    g.con.commit()
    (repo / ".tokendiet" / "enabled").write_text(",".join(agents), encoding="utf-8")
    done.append(f"index: {r.get('files')} files in {r.get('seconds')}s -> .tokendiet/")

    print(f"tokendiet enabled in {repo}  (agents: {', '.join(agents)})")
    for d in done:
        print("  +", d)
    if "codex" in agents:
        cmd, args = _command(str(repo))
        print("\nCodex reads AGENTS.md automatically. To give it the tools too, add to ~/.codex/config.toml")
        print("(tokendiet does not edit global config):")
        print(f"  [mcp_servers.tokendiet]\n  command = {json.dumps(cmd)}\n  args = {json.dumps(args)}")
    print("\nRestart the agent session to pick it up. Measure with: tokendiet report   Undo with: tokendiet uninit")


def uninit(repo):
    repo = Path(repo).resolve()
    out = []
    if _drop_server(repo / ".mcp.json", "mcpServers"):
        out.append(".mcp.json entry")
    skill_dir = repo / ".claude" / "skills" / "tokendiet"
    if skill_dir.exists():
        shutil.rmtree(skill_dir)
        out.append(".claude/skills/tokendiet/")
    settings = repo / ".claude" / "settings.local.json"
    data = _read_json(settings)
    changed = False
    if "tokendiet" in data.get("enabledMcpjsonServers", []):
        data["enabledMcpjsonServers"].remove("tokendiet")
        if not data["enabledMcpjsonServers"]:
            del data["enabledMcpjsonServers"]
        changed = True
        out.append("MCP server approval")
    ss = data.get("hooks", {}).get("SessionStart")
    if ss:
        kept = [h for h in ss if not any(HOOK_TAG in x.get("command", "") for x in h.get("hooks", []))]
        if len(kept) != len(ss):
            if kept:
                data["hooks"]["SessionStart"] = kept
            else:
                del data["hooks"]["SessionStart"]
                if not data["hooks"]:
                    del data["hooks"]
            changed = True
            out.append("SessionStart hook")
    if changed:
        if data:
            _write_json(settings, data)
        else:
            settings.unlink()
    for f in ("CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md"):
        if _remove_section(repo / f):
            out.append(f"{f} section")
    if _drop_server(repo / ".cursor" / "mcp.json", "mcpServers"):
        out.append(".cursor/mcp.json entry")
    rule = repo / ".cursor" / "rules" / "tokendiet.mdc"
    if rule.exists():
        rule.unlink()
        out.append(".cursor/rules/tokendiet.mdc")
    if _drop_server(repo / ".vscode" / "mcp.json", "servers"):
        out.append(".vscode/mcp.json entry")
    _rmdir_if_empty(repo / ".claude" / "skills", repo / ".claude", repo / ".cursor" / "rules", repo / ".cursor",
                    repo / ".vscode", repo / ".github")
    marker = repo / ".tokendiet" / "enabled"
    if marker.exists():
        marker.unlink()
    for o in out:
        print("  -", o)
    print("tokendiet disabled. The index, usage log and notes remain in .tokendiet/ (delete it to remove them).")


def sync(repo):
    """Re-run init with the repo's original choices if it was set up by another tokendiet version.

    Returns True if the files were rewritten. Output is swallowed: this runs inside hooks and the extension.
    """
    import contextlib
    import io
    import sqlite3
    repo = Path(repo).resolve()
    db = repo / ".tokendiet" / "graph.db"
    if not (repo / ".tokendiet" / "enabled").exists() or not db.exists():
        return False
    con = sqlite3.connect(db)
    try:
        meta = dict(con.execute("SELECT k, v FROM meta WHERE k IN ('init_version','init_args','agents')"))
    finally:
        con.close()
    if meta.get("init_version") == __version__:
        return False
    args = json.loads(meta["init_args"]) if "init_args" in meta else {
        "agents": [a for a in meta.get("agents", "").split(",") if a] or None, "hook": True, "budget": 1500}
    with contextlib.redirect_stdout(io.StringIO()):
        init(repo, **args)
    return True


def session_start(budget=1500):
    """SessionStart hook: print the repo map + notes as additional context. Never blocks the session."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    repo = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        from . import update
        update.spawn_auto()
        sync(repo)
    except Exception:
        pass
    try:
        from . import meter
        from .graph import Graph
        g = Graph(repo)
        g.refresh(force=True)
        text = g.repo_map("", budget)
        meter.log(g, "session_start", {}, text, payload.get("session_id", ""), "claude-code-hook")
        ctx = ("tokendiet repo map and notes from earlier sessions (auto-injected; the index is kept current). "
               "Use the tokendiet tools for navigation and save durable findings with notes.\n\n" + text)
    except Exception as e:  # a broken index must not break the agent
        ctx = f"tokendiet: map unavailable ({e}). Tools may still work."
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}))
