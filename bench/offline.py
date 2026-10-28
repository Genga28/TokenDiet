"""Offline A/B: tokens an agent pulls into context to answer navigation tasks, grep+read vs tokendiet.

Baseline models the pattern seen in Claude Code transcripts: Grep (files_with_matches) → Read whole files
(Read caps at 2000 lines / 25k tokens). Tasks are generated from the repo with a fixed seed. Quality is
checked against text-search ground truth so savings are not bought with missing answers.

  python -m bench.offline <repo> [<repo> ...] [--json out.json]
"""

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokendiet import tokens  # noqa: E402
from tokendiet.graph import Graph, _is_test  # noqa: E402
from tokendiet.indexer import _parser  # noqa: E402
from tokendiet.server import TOOLS  # noqa: E402

READ_MAX_LINES, READ_MAX_TOKENS = 2000, 25000


def wb(name):
    """Word boundary that also treats $ as a word char (JS identifiers like $ZodType)."""
    return rf"(?<![\w$]){re.escape(name)}(?![\w$])"


def listed_paths(out, paths):
    return {p for p in paths if re.search(rf"(?<![\w./-]){re.escape(p)}(?![\w./-])", out)}


def code_only(src: bytes, lang: str) -> str:
    """Source with comments and string literals blanked, for ground truth that ignores prose mentions."""
    parser = _parser(lang)
    if parser is None:
        return src.decode("utf-8", "replace")
    buf = bytearray(src)
    stack = [parser.parse(src).root_node]
    while stack:
        n = stack.pop()
        if "comment" in n.type or n.type in ("string", "string_literal", "template_string", "interpreted_string_literal",
                                             "raw_string_literal", "string_fragment", "string_content"):
            for i in range(n.start_byte, n.end_byte):
                if buf[i] not in (10, 13):
                    buf[i] = 32
            continue
        stack.extend(n.children)
    return buf.decode("utf-8", "replace")


class Repo:
    def __init__(self, g: Graph):
        self.g = g
        self.root = g.ix.repo
        self.text, self.code = {}, {}
        for f in g.files.values():
            try:
                raw = (self.root / f["path"]).read_bytes()
            except OSError:
                continue
            self.text[f["path"]] = raw.decode("utf-8", "replace")
            self.code[f["path"]] = code_only(raw, f["lang"])

    def code_hits(self, pattern):
        rx = re.compile(pattern)
        return {p for p, t in self.code.items() if rx.search(t)}

    def grep_files(self, pattern):
        rx = re.compile(pattern)
        return sorted(p for p, t in self.text.items() if rx.search(t))

    def grep_output(self, pattern):
        hits = self.grep_files(pattern)
        return f"Found {len(hits)} files\n" + "\n".join(hits[:100]), hits

    def grep_content(self, pattern, limit=100):
        """Grep in content mode (path:line:text), as a careful agent would use it."""
        rx = re.compile(pattern)
        out, locs = [], []
        for p in sorted(self.text):
            for i, ln in enumerate(self.text[p].splitlines()):
                if rx.search(ln):
                    locs.append((p, i + 1))
                    if len(out) < limit:
                        out.append(f"{p}:{i + 1}:{ln.strip()[:200]}")
        return tokens.count("\n".join(out)), locs

    def read_window(self, path, start, end):
        lines = self.text.get(path, "").splitlines()
        lo, hi = max(1, start), min(len(lines), end)
        return tokens.count("\n".join(f"{i:>6}→{lines[i - 1]}" for i in range(lo, hi + 1)))

    def read_output(self, path):
        lines = self.text.get(path, "").splitlines()[:READ_MAX_LINES]
        out = "\n".join(f"{i + 1:>6}→{ln}" for i, ln in enumerate(lines))
        n = tokens.count(out)
        return n if n <= READ_MAX_TOKENS else READ_MAX_TOKENS  # Read errors past the cap; the agent re-reads a 2000-line window


def run(repo_path, seed=7, n_locate=20, n_understand=15, n_impact=10):
    g = Graph(repo_path)
    t0 = time.time()
    g.ix.reset()  # cold index for timing
    g.ix.refresh(force=True)
    cold = time.time() - t0
    t1 = time.time()
    g.ix.refresh(force=True)
    warm = time.time() - t1
    g._build()
    R = Repo(g)
    con = g.con
    rnd = random.Random(seed)

    cands = [r for r in con.execute(
        "SELECT s.name, s.kind, s.file_id, s.start_line, s.end_line, f.path FROM symbols s JOIN files f ON f.id=s.file_id "
        "WHERE s.depth<=1 AND s.kind IN ('function','method','class','struct','interface','type') AND length(s.name) > 3")]
    cands = [c for c in cands if len(g.ref_files.get(c["name"], ())) >= 2 and not _is_test(c["path"])
             and len(g.def_files.get(c["name"], ())) <= 3]
    rnd.shuffle(cands)
    seen, picks = set(), []
    for c in cands:
        if c["name"] not in seen:
            seen.add(c["name"])
            picks.append(c)
    tasks = []

    for c in picks[:n_locate]:
        name = c["name"]
        gout, hits = R.grep_output(wb(name))
        base = tokens.count(gout) + R.read_output(c["path"])
        gl, _ = R.grep_content(wb(name))
        lean = gl + R.read_window(c["path"], c["start_line"] - 5, c["end_line"] + 5)
        out = g.find_symbol(name, include_body=True, limit=3, budget=6000)
        ok = f"{c['path']}:{c['start_line']}-" in out
        tasks.append({"type": "locate", "target": name, "baseline": base, "lean": lean, "tokendiet": tokens.count(out), "correct": ok})

    # understand = the symbol's source plus everywhere it is used (calls and type/value references)
    for c in picks[n_locate:n_locate + n_understand]:
        name = c["name"]
        gout, hits = R.grep_output(wb(name))
        rx = re.compile(wb(name))
        truth = R.code_hits(wb(name)) - {c["path"]}
        users = sorted(set(hits) - {c["path"]}, key=lambda p: -len(rx.findall(R.text[p])))
        base = tokens.count(gout) + R.read_output(c["path"]) + sum(R.read_output(p) for p in users[:3])
        gl, locs = R.grep_content(wb(name))
        other = [(p, ln) for p, ln in locs if p != c["path"]][:3]
        lean = gl + R.read_window(c["path"], c["start_line"], c["end_line"]) + sum(R.read_window(p, ln - 15, ln + 15) for p, ln in other)
        out = g.symbol_context(name, c["path"], budget=4000)
        listed = listed_paths(out, R.text) - {c["path"]}
        recall = len(truth & listed) / len(truth) if truth else None
        precision = len(listed & truth) / len(listed) if listed else None
        tasks.append({"type": "understand", "target": name, "baseline": base, "lean": lean, "tokendiet": tokens.count(out),
                      "usage_recall": recall, "precision": precision, "true_users": len(truth)})

    # impact = which files use something this file defines. Truth: text hits for its distinctive symbol names.
    files_pick = [fid for fid in g.files if not _is_test(g.files[fid]["path"])]
    rnd.shuffle(files_pick)
    done = 0
    for fid in files_pick:
        if done >= n_impact:
            break
        path = g.files[fid]["path"]
        names = [r[0] for r in con.execute("SELECT DISTINCT name FROM symbols WHERE file_id=? AND depth=0", (fid,))
                 if len(r[0]) > 3 and len(g.def_files.get(r[0], ())) == 1]
        if not names:
            continue
        alt = "|".join(re.escape(n) for n in names[:30])
        gout, hits = R.grep_output(rf"(?<![\w$])(?:{alt})(?![\w$])")
        truth = R.code_hits(rf"(?<![\w$])(?:{alt})(?![\w$])") - {path}
        if len(truth) < 2:
            continue
        done += 1
        rx = re.compile(rf"(?<![\w$])(?:{alt})(?![\w$])")
        base = tokens.count(gout) + sum(R.read_output(p) for p in sorted(set(hits) - {path}, key=lambda p: -len(rx.findall(R.text[p])))[:5])
        gl, locs = R.grep_content(rf"(?<![\w$])(?:{alt})(?![\w$])")
        lean = gl + sum(R.read_window(p, ln - 15, ln + 15) for p, ln in [x for x in locs if x[0] != path][:5])
        out = g.impact(path, depth=2)
        listed = listed_paths(out, R.text) - {path}
        tasks.append({"type": "impact", "target": path, "baseline": base, "lean": lean, "tokendiet": tokens.count(out),
                      "dependent_recall": len(truth & listed) / len(truth), "true_dependents": len(truth)})

    # orientation at session start: list files, read README, read the 3 most central files
    listing = "\n".join(sorted(R.text))
    readme = next((p for p in os.listdir(R.root) if p.lower().startswith("readme")), None)
    readme_tk = tokens.count((R.root / readme).read_text(encoding="utf-8", errors="replace")) if readme else 0
    central = [g.files[fid]["path"] for fid, _ in g.rank("")[:3]]
    base = tokens.count(listing) + readme_tk + sum(R.read_output(p) for p in central)
    out = g.repo_map("", 2000)
    lean = tokens.count(listing) + readme_tk  # listing + README only, no source reads
    tasks.append({"type": "orient", "target": "repo", "baseline": base, "lean": lean, "tokendiet": tokens.count(out)})

    def summarize(kind):
        ts = [t for t in tasks if t["type"] == kind]
        if not ts:
            return None
        b = [t["baseline"] for t in ts]
        c = [t["tokendiet"] for t in ts]
        ln = [t["lean"] for t in ts]
        s = {"n": len(ts), "baseline_median": statistics.median(b), "lean_median": statistics.median(ln),
             "tokendiet_median": statistics.median(c),
             "baseline_total": sum(b), "lean_total": sum(ln), "tokendiet_total": sum(c),
             "reduction_pct": round(100 * (1 - sum(c) / sum(b)), 1) if sum(b) else 0,
             "reduction_vs_lean_pct": round(100 * (1 - sum(c) / sum(ln)), 1) if sum(ln) else 0,
             "worst_case_ratio": round(min([bb / cc for bb, cc in zip(b, c) if cc and bb >= 200] or [0]), 2)}
        for q in ("correct", "usage_recall", "dependent_recall", "precision"):
            vals = [t[q] for t in ts if t.get(q) is not None]
            if vals:
                s[q] = round(sum(vals) / len(vals), 3)
        return s

    q = lambda s: con.execute(s).fetchone()[0]
    return {
        "repo": Path(repo_path).name,
        "index": {"files": q("SELECT COUNT(*) FROM files"), "symbols": q("SELECT COUNT(*) FROM symbols"),
                  "source_tokens": q("SELECT SUM(tokens) FROM files"), "cold_seconds": round(cold, 2),
                  "warm_refresh_seconds": round(warm, 3),
                  "db_mb": round((g.ix.repo / ".tokendiet" / "graph.db").stat().st_size / 1e6, 2)},
        "tool_schema_tokens": tokens.count(json.dumps(TOOLS)),
        "summary": {k: summarize(k) for k in ("locate", "understand", "impact", "orient")},
        "tasks": tasks,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repos", nargs="+")
    ap.add_argument("--json")
    a = ap.parse_args()
    results = []
    for r in a.repos:
        res = run(r)
        results.append(res)
        print(f"\n== {res['repo']}  files={res['index']['files']} symbols={res['index']['symbols']} "
              f"src_tokens={res['index']['source_tokens']:,} cold={res['index']['cold_seconds']}s "
              f"warm={res['index']['warm_refresh_seconds']}s db={res['index']['db_mb']}MB schema={res['tool_schema_tokens']}tok")
        for k, s in res["summary"].items():
            if s:
                extra = " ".join(f"{q}={s[q]}" for q in ("correct", "usage_recall", "dependent_recall", "precision") if q in s)
                print(f"  {k:10} n={s['n']:>2} read-files={s['baseline_median']:>7,.0f} lean={s['lean_median']:>6,.0f} "
                      f"tokendiet={s['tokendiet_median']:>5,.0f} | saved vs read-files {s['reduction_pct']:>5}% vs lean "
                      f"{s['reduction_vs_lean_pct']:>5}% | {extra}")
    if a.json:
        Path(a.json).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
