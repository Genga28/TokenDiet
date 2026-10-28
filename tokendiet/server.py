"""MCP server over stdio (newline-delimited JSON-RPC 2.0). No SDK dependency.

Five tools with short descriptions: tool schemas are re-sent on every turn, so they are part of the cost.
"""

import json
import sys
import traceback
import uuid

from . import __version__, meter
from .graph import Graph

PROTOCOL = "2025-06-18"

TOOLS = [
    {
        "name": "repo_map",
        "description": "Ranked outline of the repo: important files with their classes/functions and line numbers, plus notes "
                       "from earlier sessions. Call first in a session instead of listing/reading files. focus = words, paths "
                       "or symbol names to centre the map on.",
        "inputSchema": {"type": "object", "properties": {
            "focus": {"type": "string"}, "budget": {"type": "integer", "default": 2000}}},
    },
    {
        "name": "find_symbol",
        "description": "Locate a class/function/method by name (exact, then fuzzy; 'Class.method' works). Returns path:start-end "
                       "and signature; include_body returns the source. Use instead of grep + reading whole files.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string"}, "include_body": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 10}}, "required": ["query"]},
    },
    {
        "name": "symbol_context",
        "description": "One symbol's source, what it calls (resolved to locations) and every call site, with confidence. "
                       "Use to understand a function before editing it.",
        "inputSchema": {"type": "object", "properties": {
            "name": {"type": "string"}, "path": {"type": "string", "description": "disambiguate by file"},
            "budget": {"type": "integer", "default": 3000}}, "required": ["name"]},
    },
    {
        "name": "impact",
        "description": "What depends on a file or symbol (depth 1-2) and which tests touch it. Use before changing shared code.",
        "inputSchema": {"type": "object", "properties": {
            "target": {"type": "string"}, "depth": {"type": "integer", "default": 2}}, "required": ["target"]},
    },
    {
        "name": "notes",
        "description": "Memory across sessions. add: save a fact you learned about a file/symbol (flagged stale automatically "
                       "when that code changes). get: read notes (optionally filtered by target).",
        "inputSchema": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["get", "add", "delete"]}, "target": {"type": "string"},
            "text": {"type": "string"}}, "required": ["action"]},
    },
]


def call_tool(g: Graph, name: str, a: dict) -> str:
    g.refresh()
    if name == "repo_map":
        return g.repo_map(a.get("focus", ""), int(a.get("budget", 2000)))
    if name == "find_symbol":
        return g.find_symbol(a["query"], bool(a.get("include_body", False)), int(a.get("limit", 10)))
    if name == "symbol_context":
        return g.symbol_context(a["name"], a.get("path", ""), int(a.get("budget", 3000)))
    if name == "impact":
        return g.impact(a["target"], int(a.get("depth", 2)))
    if name == "notes":
        return g.notes(a.get("action", "get"), a.get("target", ""), a.get("text", ""))
    raise ValueError(f"unknown tool {name}")


def serve(repo: str):
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # no \r\n on Windows: clients split on \n
    g = Graph(repo)
    session = uuid.uuid4().hex[:12]  # one MCP server process per agent session
    client = ""
    print(f"[tokendiet] indexing {g.ix.repo} …", file=sys.stderr)
    print(f"[tokendiet] {g.ix.refresh(force=True, verbose=True)}", file=sys.stderr)
    out = sys.stdout

    def send(msg):
        out.write(json.dumps(msg) + "\n")
        out.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:  # notification
            continue
        try:
            if method == "initialize":
                client = (params.get("clientInfo") or {}).get("name", "")
                result = {"protocolVersion": params.get("protocolVersion", PROTOCOL),
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "tokendiet", "version": __version__},
                          "instructions": "Prefer repo_map / find_symbol / symbol_context / impact over grep and whole-file "
                                          "reads for code navigation. Save durable findings with notes(add)."}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                args = params.get("arguments") or {}
                text = call_tool(g, params["name"], args)
                meter.log(g, params["name"], args, text, session, client)
                result = {"content": [{"type": "text", "text": text}], "isError": False}
            elif method == "ping":
                result = {}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:
            traceback.print_exc(file=sys.stderr)
            if method == "tools/call":
                send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}})
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})
