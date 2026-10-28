"""Replay real Claude Code sessions under a "reset the context at T tokens" policy.

Each API call re-sends the whole conversation (cache read at 0.1x). Long sessions are expensive because of
that carry, not because of any one tool result. Resetting earlier cuts the carry but forces re-orientation.

Cases (input-equivalent tokens; weights as in bench.transcripts):
  actual          the session as logged
  reset_plain     reset at T; afterwards the agent re-reads its whole working set (files read or written so far)
  reset_tokendiet  reset at T; handoff = repo_map + notes + summary (HANDOFF tokens) and targeted re-reads
                  (REREAD_FRACTION of the working set, from the offline benchmark's symbol-vs-file ratio)

The new tokens each call adds (tool results, user text, outputs) are replayed unchanged.

  python -m bench.session_sim [project_dir ...] [--thresholds 100000 150000 200000]
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokendiet import tokens  # noqa: E402

W_READ, W_OUT = 0.1, 5.0
HANDOFF = 5000
REREAD_FRACTION = 0.25


def load(path):
    calls, order, file_sizes_at = {}, [], []
    tool_uses = {}
    touched = {}  # file -> latest token size
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = r.get("message") or {}
        if r.get("type") == "assistant" and isinstance(m, dict):
            rid = r.get("requestId") or m.get("id")
            if m.get("usage") and rid:
                if rid not in calls:
                    order.append(rid)
                    file_sizes_at.append(None)
                calls[rid] = m["usage"]
            for c in m.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    tool_uses[c["id"]] = (c.get("name"), inp)
                    if c.get("name") == "Write" and inp.get("file_path"):
                        touched[os.path.normcase(inp["file_path"])] = tokens.count(str(inp.get("content", "")))
        elif r.get("type") == "user" and isinstance(m.get("content"), list):
            for c in m["content"]:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    name, inp = tool_uses.get(c.get("tool_use_id"), (None, {}))
                    if name == "Read" and inp.get("file_path"):
                        content = c.get("content")
                        text = content if isinstance(content, str) else "\n".join(
                            x.get("text", "") for x in content or [] if isinstance(x, dict))
                        touched[os.path.normcase(inp["file_path"])] = tokens.count(text)
        if order:
            file_sizes_at[-1] = dict(touched)
    rows = []
    for rid, ws in zip(order, file_sizes_at):
        u = calls[rid]
        cw = u.get("cache_creation_input_tokens", 0) or 0
        cc = u.get("cache_creation") or {}
        rows.append({"read": u.get("cache_read_input_tokens", 0) or 0, "new": (u.get("input_tokens", 0) or 0) + cw,
                     "w_write": 2.0 if (cc.get("ephemeral_1h_input_tokens", 0) or 0) >= cw / 2 else 1.25,
                     "out": u.get("output_tokens", 0) or 0, "ws": sum((ws or {}).values())})
    return rows


def simulate(rows, threshold=None, reread_fraction=1.0, handoff=0):
    cost = 0.0
    if not rows:
        return 0.0, 0
    base = rows[0]["read"] + rows[0]["new"]  # system prompt, tools, CLAUDE.md, first message
    ctx = 0  # context carried into the next call (what gets cache-read)
    resets = 0
    for i, r in enumerate(rows):
        if threshold is None:
            ctx = r["read"]
        elif i > 0 and ctx > threshold:
            resets += 1
            extra = handoff + int(r["ws"] * reread_fraction)
            cost += extra * r["w_write"]
            ctx = base + extra
        elif i == 0:
            ctx = r["read"]
        cost += ctx * W_READ + r["new"] * r["w_write"] + r["out"] * W_OUT
        ctx = ctx + r["new"] + r["out"]
    return cost, resets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dirs", nargs="*")
    ap.add_argument("--thresholds", nargs="+", type=int, default=[100_000, 150_000, 200_000])
    ap.add_argument("--reread", type=float, default=REREAD_FRACTION, help="fraction of working set re-read with tokendiet")
    ap.add_argument("--handoff", type=int, default=HANDOFF)
    ap.add_argument("--json")
    a = ap.parse_args()
    dirs = a.dirs or [str(p) for p in (Path.home() / ".claude" / "projects").iterdir() if p.is_dir()]
    out = []
    print(f"{'project':38} {'actual':>12} " + " ".join(f"{'plain@' + str(t // 1000) + 'k':>13} {'graph@' + str(t // 1000) + 'k':>13}" for t in a.thresholds))
    for d in dirs:
        sessions = [load(p) for p in sorted(Path(d).rglob("*.jsonl"))]
        sessions = [s for s in sessions if len(s) >= 5]
        if not sessions:
            continue
        actual = sum(simulate(s)[0] for s in sessions)
        row = {"project": Path(d).name, "actual": round(actual), "policies": {}}
        cells = []
        for t in a.thresholds:
            plain = sum(simulate(s, t, 1.0, 0)[0] for s in sessions)
            graph_res = [simulate(s, t, a.reread, a.handoff) for s in sessions]
            graph = sum(c for c, _ in graph_res)
            resets = sum(n for _, n in graph_res)
            row["policies"][t] = {"plain": round(plain), "tokendiet": round(graph), "resets": resets}
            cells.append(f"{(plain / actual - 1) * 100:>+12.1f}% {(graph / actual - 1) * 100:>+12.1f}%")
        out.append(row)
        print(f"{row['project'][:38]:38} {actual:>12,.0f} " + " ".join(cells))
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
