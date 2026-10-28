"""Live A/B with the real agent: same prompts, with and without tokendiet. Uses Claude Code's own usage numbers.

Spends real tokens on your account. Each prompt runs in a fresh headless session (`claude -p`), read-only tools.

  python -m bench.live_ab <repo> --prompts prompts.json [--claude path/to/claude.exe] [--runs 2] [--model ...]

prompts.json: ["Where is request routing implemented and what calls it?", ...]
Without --prompts, a small default set of repo-agnostic navigation prompts is used.
"""

import argparse
import glob
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tokendiet.cli import CLAUDE_MD  # noqa: E402

DEFAULT_PROMPTS = [
    "Give me a concise overview of this codebase's architecture: the main modules and how they depend on each other.",
    "Find the most central class or function in this repo and explain what calls it and what it calls.",
    "If I change the most widely used module in this repo, which files and tests are affected?",
]
BASE_TOOLS = "Read,Grep,Glob"


def find_claude():
    hits = sorted(glob.glob(os.path.expanduser("~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude*")))
    return hits[-1] if hits else "claude"


def run_once(claude, repo, prompt, with_graph, model, max_usd):
    py = sys.executable
    if with_graph:
        cfg = {"mcpServers": {"tokendiet": {"command": py, "args": ["-m", "tokendiet", "serve", str(repo)],
                                           "env": {"PYTHONPATH": str(ROOT)}}}}
        tools = BASE_TOOLS + ",mcp__tokendiet__repo_map,mcp__tokendiet__find_symbol,mcp__tokendiet__symbol_context,mcp__tokendiet__impact,mcp__tokendiet__notes"
        extra = ["--append-system-prompt", CLAUDE_MD]
    else:
        cfg = {"mcpServers": {}}
        tools = BASE_TOOLS
        extra = []
    cmd = [claude, "-p", prompt, "--output-format", "json", "--strict-mcp-config", "--mcp-config", json.dumps(cfg),
           "--allowedTools", tools, "--max-budget-usd", str(max_usd)] + extra
    if model:
        cmd += ["--model", model]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8", timeout=900)
    try:
        res = json.loads(p.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": (p.stderr or p.stdout)[-500:]}
    u = res.get("usage") or {}
    return {"seconds": round(time.time() - t0, 1), "turns": res.get("num_turns"), "cost_usd": res.get("total_cost_usd"),
            "input": u.get("input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
            "cache_read": u.get("cache_read_input_tokens", 0), "output": u.get("output_tokens", 0),
            "answer": res.get("result", "")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--prompts")
    ap.add_argument("--claude", default=find_claude())
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--model", default="")
    ap.add_argument("--max-usd", type=float, default=1.0, help="per-run spending cap")
    ap.add_argument("--json", default="results/live_ab.json")
    a = ap.parse_args()
    repo = Path(a.repo).resolve()
    prompts = json.loads(Path(a.prompts).read_text()) if a.prompts else DEFAULT_PROMPTS
    subprocess.run([sys.executable, "-m", "tokendiet", "index", str(repo)], cwd=ROOT, check=True)
    rows = []
    for i, prompt in enumerate(prompts):
        for run in range(a.runs):
            for arm in (False, True):  # interleave arms so drift affects both
                r = run_once(a.claude, repo, prompt, arm, a.model, a.max_usd)
                r.update({"prompt": i, "run": run, "arm": "tokendiet" if arm else "baseline"})
                rows.append(r)
                print(f"prompt {i} run {run} {r['arm']:9} " + (f"error: {r['error'][:200]}" if "error" in r else
                      f"turns={r['turns']} in+cw+cr={r['input'] + r['cache_write'] + r['cache_read']:,} out={r['output']:,} ${r['cost_usd']}"))
    Path(a.json).parent.mkdir(exist_ok=True)
    Path(a.json).write_text(json.dumps(rows, indent=1))
    for arm in ("baseline", "tokendiet"):
        ok = [r for r in rows if r["arm"] == arm and "error" not in r]
        if ok:
            print(f"{arm:9} median cost ${statistics.median(r['cost_usd'] or 0 for r in ok):.4f}  "
                  f"median turns {statistics.median(r['turns'] or 0 for r in ok)}  "
                  f"median context tokens {statistics.median(r['input'] + r['cache_write'] + r['cache_read'] for r in ok):,.0f}")
    print(f"answers saved to {a.json} — compare them for quality, not just cost")


if __name__ == "__main__":
    main()
