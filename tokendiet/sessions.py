"""Measure where tokens go in real Claude Code sessions (~/.claude/projects/<project>/*.jsonl).

Outputs aggregates only (no transcript content). Usage numbers are the API's own; tool-result sizes are
tiktoken estimates. Cost is expressed in input-equivalent tokens using Anthropic's price ratios:
input 1, 5-min cache write 1.25, 1-hour cache write 2, cache read 0.1, output 5.

  python -m bench.transcripts   (library: tokendiet.sessions) [project_dir ...] [--json out.json]
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from . import tokens

EXPLORE_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
EXPLORE_CMD = re.compile(r"^\s*(cd\s+\S+\s*(&&|;)\s*)?(ls|cat|head|tail|find|grep|rg|tree|sed\s+-n|wc|Get-Content|Get-ChildItem|gci|dir|type|Select-String|git\s+(ls-files|grep|show|log))\b", re.I)
W = {"input": 1.0, "cw5": 1.25, "cw1h": 2.0, "cr": 0.1, "out": 5.0}


def _result_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


def classify(name, inp):
    if name in EXPLORE_TOOLS:
        return "explore"
    if name in SHELL_TOOLS:
        return "explore" if EXPLORE_CMD.match(str(inp.get("command", ""))) else "shell"
    if name in EDIT_TOOLS:
        return "edit"
    if name in ("Task", "Agent"):
        return "subagent"
    if name.startswith("mcp__"):
        return "mcp"
    return "other"


def analyze_session(path):
    calls = {}  # requestId -> usage (last wins; streaming writes several records per request)
    order = []
    tool_uses = {}
    results = []  # (call_index_after, tool_use_id, tokens)
    compact_at = []
    started = None
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = r.get("type")
        started = started or r.get("timestamp")
        m = r.get("message") or {}
        if t == "assistant" and isinstance(m, dict):
            rid = r.get("requestId") or m.get("id")
            if m.get("usage") and rid:
                if rid not in calls:
                    order.append(rid)
                calls[rid] = m["usage"]
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tool_uses[c["id"]] = (c.get("name", "?"), c.get("input") or {})
        elif t == "user" and isinstance(m, dict):
            if r.get("isCompactSummary"):
                compact_at.append(len(order))
            content = m.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result":
                        results.append((len(order), c.get("tool_use_id"), tokens.count(_result_text(c.get("content")))))
        elif t == "system" and r.get("subtype") == "compact_boundary":
            compact_at.append(len(order))

    n = len(order)
    if n == 0:
        return None
    u = [calls[k] for k in order]
    tot = defaultdict(int)
    ctx = []
    for x in u:
        cc = x.get("cache_creation") or {}
        cw1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
        cw = x.get("cache_creation_input_tokens", 0) or 0
        tot["input"] += x.get("input_tokens", 0) or 0
        tot["cw1h"] += cw1h
        tot["cw5"] += cw - cw1h
        tot["cr"] += x.get("cache_read_input_tokens", 0) or 0
        tot["out"] += x.get("output_tokens", 0) or 0
        ctx.append((x.get("input_tokens", 0) or 0) + cw + (x.get("cache_read_input_tokens", 0) or 0))
    cost = sum(tot[k] * W[k] for k in W)
    write_w = (W["cw1h"] if tot["cw1h"] >= tot["cw5"] else W["cw5"])

    by_cat = defaultdict(lambda: {"calls": 0, "tokens": 0, "carried": 0, "cost": 0.0})
    by_tool = defaultdict(lambda: {"calls": 0, "tokens": 0})
    reads = defaultdict(list)
    for idx, tid, tk in results:
        name, inp = tool_uses.get(tid, ("?", {}))
        cat = classify(name, inp)
        nxt_compact = min([c for c in compact_at if c >= idx] or [n])
        carried_calls = max(0, nxt_compact - idx - 1)  # re-sent as cache read on later calls
        b = by_cat[cat]
        b["calls"] += 1
        b["tokens"] += tk
        b["carried"] += tk * carried_calls
        b["cost"] += tk * write_w + tk * carried_calls * W["cr"]
        by_tool[name]["calls"] += 1
        by_tool[name]["tokens"] += tk
        if name == "Read" and inp.get("file_path"):
            reads[os.path.normcase(inp["file_path"])].append(tk)
    reread_tokens = sum(sum(v[1:]) for v in reads.values())
    first = u[0]
    return {
        "file": str(path), "started": started, "api_calls": n, "totals": dict(tot), "input_equiv": cost,
        "start_prefill": (first.get("input_tokens", 0) or 0) + (first.get("cache_creation_input_tokens", 0) or 0) + (first.get("cache_read_input_tokens", 0) or 0),
        "peak_context": max(ctx), "context_curve": ctx[:: max(1, n // 50)], "compactions": len(compact_at),
        "by_category": {k: dict(v) for k, v in by_cat.items()}, "by_tool": {k: dict(v) for k, v in by_tool.items()},
        "files_read": {k: len(v) for k, v in reads.items()}, "file_read_tokens": {k: sum(v) for k, v in reads.items()},
        "reread_tokens_in_session": reread_tokens,
    }


def analyze_project(d):
    sessions = [s for s in (analyze_session(p) for p in sorted(Path(d).rglob("*.jsonl"))) if s]
    if not sessions:
        return None
    agg = defaultdict(float)
    cats = defaultdict(lambda: defaultdict(float))
    tools = defaultdict(lambda: defaultdict(float))
    seen_sessions = defaultdict(int)
    cross_reread = 0
    for s in sessions:
        for k, v in s["totals"].items():
            agg[k] += v
        agg["input_equiv"] += s["input_equiv"]
        agg["api_calls"] += s["api_calls"]
        agg["reread_in_session"] += s["reread_tokens_in_session"]
        for c, v in s["by_category"].items():
            for k, x in v.items():
                cats[c][k] += x
        for c, v in s["by_tool"].items():
            for k, x in v.items():
                tools[c][k] += x
    for s in sorted(sessions, key=lambda s: s["file"]):
        for f, tk in s["file_read_tokens"].items():
            if seen_sessions[f] > 0:
                cross_reread += tk
            seen_sessions[f] += 1
    explore = cats.get("explore", {})
    return {
        "project": Path(d).name, "sessions": len(sessions), **{k: round(v) for k, v in agg.items()},
        "median_start_prefill": sorted(s["start_prefill"] for s in sessions)[len(sessions) // 2],
        "peak_context": max(s["peak_context"] for s in sessions),
        "compactions": sum(s["compactions"] for s in sessions),
        "explore_result_tokens": round(explore.get("tokens", 0)),
        "explore_carried_tokens": round(explore.get("carried", 0)),
        "explore_cost_share": round(explore.get("cost", 0) / agg["input_equiv"], 4) if agg["input_equiv"] else 0,
        "explore_calls": round(explore.get("calls", 0)),
        "files_read_in_multiple_sessions": sum(1 for v in seen_sessions.values() if v > 1),
        "cross_session_reread_tokens": cross_reread,
        "by_category": {c: {k: round(x) for k, x in v.items()} for c, v in cats.items()},
        "top_tools": sorted(((k, round(v["tokens"]), round(v["calls"])) for k, v in tools.items()), key=lambda x: -x[1])[:10],
        "context_curves": [s["context_curve"] for s in sessions if s["api_calls"] >= 10],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*")
    ap.add_argument("--json")
    a = ap.parse_args()
    dirs = a.dirs or [str(p) for p in (Path.home() / ".claude" / "projects").iterdir() if p.is_dir()]
    res = [r for r in (analyze_project(d) for d in dirs) if r]
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))
    hdr = f"{'project':42} {'sess':>4} {'calls':>6} {'in-equiv':>11} {'cache_rd':>12} {'prefill':>8} {'peak':>8} {'expl%':>6} {'x-sess reread':>13}"
    print(hdr)
    for r in res:
        print(f"{r['project'][:42]:42} {r['sessions']:>4} {r['api_calls']:>6} {r['input_equiv']:>11,} {r['cr']:>12,} "
              f"{r['median_start_prefill']:>8,} {r['peak_context']:>8,} {r['explore_cost_share'] * 100:>5.1f}% {r['cross_session_reread_tokens']:>13,}")


if __name__ == "__main__":
    main()
