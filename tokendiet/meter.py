"""Usage meter: what each tool call served vs what the agent would otherwise have pulled into context.

Two counterfactuals per call, estimated from the index (no extra file reads):
  lean  a careful agent: grep with line numbers (~25 tokens per hit, max 100 hits) + ranged reads (±15 lines)
  full  the common pattern in real transcripts: grep for files, then Read whole files (capped at 25k tokens each)
Report the lean number as the claim; full is the typical case. Both were validated against measured grep/read
output in bench/offline.py.
"""

import json
import os
import statistics
import time
from collections import defaultdict
from pathlib import Path

from . import tokens

GREP_LINE, GREP_MAX, WINDOW, READ_CAP = 25, 100, 31, 25000


def _tpl(g, fid):
    f = g.files.get(fid)
    return (f["tokens"] / max(1, f["lines"])) if f else 10


def _full(g, fid):
    f = g.files.get(fid)
    return min(f["tokens"], READ_CAP) if f else 0


def _grep(g, names):
    if not names:
        return 0
    q = ",".join("?" * len(names))
    hits = g.con.execute(f"SELECT COALESCE(SUM(n),0) FROM refs WHERE name IN ({q})", list(names)).fetchone()[0]
    hits += g.con.execute(f"SELECT COUNT(*) FROM symbols WHERE name IN ({q})", list(names)).fetchone()[0]
    return min(GREP_MAX, hits) * GREP_LINE


def _users(g, name, fid, k):
    rows = g.con.execute("SELECT file_id FROM refs WHERE name=? AND file_id!=? ORDER BY n DESC LIMIT ?", (name, fid, k))
    return [r[0] for r in rows]


def _orient(g):
    listing = len(g.files) * 8
    readme = next((p for p in os.listdir(g.ix.repo) if p.lower().startswith("readme")), None)
    rt = tokens.count((g.ix.repo / readme).read_text(encoding="utf-8", errors="replace")) if readme else 0
    central = [fid for fid, _ in g.rank("")[:3]]
    return listing + rt, listing + rt + sum(_full(g, f) for f in central)


def estimate(g, tool, a):
    g._build()
    if tool in ("repo_map", "session_start"):
        return _orient(g)
    if tool in ("find_symbol", "symbol_context"):
        rows = g._symbols_named(a.get("query") or a.get("name") or "")
        if not rows:
            return 0, 0
        s = rows[0]
        fid, name = s["file_id"], s["name"]
        grep = _grep(g, [name])
        nfiles = len(g.ref_files.get(name, ())) + 1
        body = int(_tpl(g, fid) * (s["end_line"] - s["start_line"] + 11))
        lean, full = grep + body, nfiles * 10 + _full(g, fid)
        if tool == "symbol_context":
            users = _users(g, name, fid, 3)
            lean += sum(int(_tpl(g, u) * WINDOW) for u in users)
            full += sum(_full(g, u) for u in users)
        return lean, full
    if tool == "impact":
        rf = g._resolve_target_file(a.get("target", ""))
        if not isinstance(rf, int):
            rows = g._symbols_named(a.get("target", ""))
            if not rows:
                return 0, 0
            rf = rows[0]["file_id"]
        names = [r[0] for r in g.con.execute("SELECT DISTINCT name FROM symbols WHERE file_id=? AND depth=0", (rf,))][:30]
        deps = sorted(g._rev.get(rf, {}), key=lambda d: -g._rev[rf][d])[:5]
        lean = _grep(g, names) + sum(int(_tpl(g, d) * WINDOW) for d in deps)
        full = (len(g._rev.get(rf, {})) + 1) * 10 + sum(_full(g, d) for d in deps)
        return lean, full
    return 0, 0


def log(g, tool, args, output, session="", client=""):
    """Record one call. Never raises: metering must not break the tool."""
    try:
        lean, full = estimate(g, tool, args)
        lean = min(lean, full)  # a careful agent takes the cheaper route, so this stays a lower bound
        g.con.execute("INSERT INTO usage VALUES(?,?,?,?,?,?,?)",
                      (time.strftime("%Y-%m-%dT%H:%M:%S"), session, client, tool, tokens.count(output), lean, full))
        g.con.commit()
    except Exception:
        pass


# ---------------- report ----------------

def _claude_project_dir(repo: Path):
    import re
    want = re.sub(r"[^A-Za-z0-9]", "-", str(repo)).lower()
    base = Path.home() / ".claude" / "projects"
    if base.exists():
        for d in base.iterdir():
            if d.name.lower() == want:
                return d
    return None


def _session_compare(repo: Path, enabled_at: str):
    from .sessions import analyze_session
    d = _claude_project_dir(repo)
    if d is None:
        return {"available": False, "reason": "no Claude Code sessions found for this folder"}
    rows = [s for s in (analyze_session(p) for p in sorted(d.glob("*.jsonl"))) if s and s["api_calls"] >= 3]

    def stats(group):
        if not group:
            return None
        def med(f):
            return round(statistics.median(f(s) for s in group))
        return {
            "sessions": len(group),
            "median_cost_input_equiv": med(lambda s: s["input_equiv"]),
            "median_api_calls": med(lambda s: s["api_calls"]),
            "median_peak_context": med(lambda s: s["peak_context"]),
            "median_exploration_tokens": med(lambda s: s["by_category"].get("explore", {}).get("tokens", 0)),
            "median_exploration_calls": med(lambda s: s["by_category"].get("explore", {}).get("calls", 0)),
            "median_tokendiet_calls": med(lambda s: sum(v["calls"] for k, v in s["by_tool"].items() if "tokendiet" in k)),
        }
    before = [s for s in rows if (s["started"] or "") < (enabled_at or "")]
    after = [s for s in rows if (s["started"] or "") >= (enabled_at or "9999")]
    return {"available": True, "project_dir": str(d), "before": stats(before), "after": stats(after)}


def report(g, days=None, carry_turns=20, sessions=True):
    con = g.con
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400)) if days else ""
    rows = con.execute("SELECT tool, COUNT(*) n, SUM(served) s, SUM(lean) l, SUM(full) f FROM usage WHERE ts >= ? "
                       "GROUP BY tool ORDER BY n DESC", (since,)).fetchall()
    by_tool = {r["tool"]: {"calls": r["n"], "served": r["s"] or 0, "lean": r["l"] or 0, "full": r["f"] or 0} for r in rows}
    tot = defaultdict(int)
    for v in by_tool.values():
        for k, x in v.items():
            tot[k] += x
    sessions = con.execute("SELECT COUNT(DISTINCT session) FROM usage WHERE ts >= ? AND session != ''", (since,)).fetchone()[0]
    g._build()
    notes = [g._note_line(n) for n in con.execute("SELECT * FROM notes").fetchall()]
    enabled = con.execute("SELECT v FROM meta WHERE k='enabled_at'").fetchone()
    saved_lean = tot["lean"] - tot["served"]
    saved_full = tot["full"] - tot["served"]
    # every token kept out of context is also not re-sent (cache read, 0.1x) on each later turn, and not cache-written (1.25x)
    billed_factor = 1.25 + 0.1 * carry_turns
    return {
        "repo": str(g.ix.repo), "period_days": days, "enabled_at": enabled[0] if enabled else None,
        "index": {"files": len(g.files), "source_tokens": sum(f["tokens"] for f in g.files.values())},
        "calls": tot["calls"], "sessions": sessions, "tokens_served": tot["served"],
        "context_tokens_avoided": {"conservative": saved_lean, "typical": saved_full},
        "billed_tokens_avoided_est": {"conservative": round(saved_lean * billed_factor), "typical": round(saved_full * billed_factor),
                                      "assumption": f"each avoided token: 1.25x cache write + 0.1x x {carry_turns} later turns"},
        "efficiency": {"conservative": round(tot["lean"] / tot["served"], 2) if tot["served"] else None,
                       "typical": round(tot["full"] / tot["served"], 2) if tot["served"] else None},
        "by_tool": by_tool,
        "notes": {"total": len(notes), "stale": sum("STALE" in n or "ORPHANED" in n for n in notes)},
        "claude_sessions": _session_compare(g.ix.repo, enabled[0] if enabled else "") if sessions
        else {"available": False, "reason": "skipped (quick report)"},
    }


def _k(n):
    n = n or 0
    return f"{n / 1e6:.2f}M" if abs(n) >= 1e6 else f"{n / 1e3:.1f}k" if abs(n) >= 1e3 else str(n)


def render(r, fmt="text"):
    if fmt == "json":
        return json.dumps(r, indent=2)
    c, t = r["context_tokens_avoided"], r["billed_tokens_avoided_est"]
    lines = [
        f"# tokendiet report: {Path(r['repo']).name}",
        "",
        f"Enabled since {r['enabled_at'] or '(not via init)'} · index: {r['index']['files']} files, {_k(r['index']['source_tokens'])} source tokens",
        "",
        "## Measured usage (this repo's tool log)",
        f"- Tool calls: **{r['calls']}** across {r['sessions']} agent sessions; tokens served: {_k(r['tokens_served'])}",
        f"- Context tokens kept out: **{_k(c['conservative'])}** conservative (vs careful grep + ranged reads), "
        f"{_k(c['typical'])} typical (vs grep + whole-file reads)",
        f"- Billed tokens avoided (est.): **{_k(t['conservative'])}** conservative, {_k(t['typical'])} typical "
        f"({t['assumption']})",
        f"- Efficiency: 1 token served replaces {r['efficiency']['conservative']} (conservative) / {r['efficiency']['typical']} (typical)",
        f"- Notes carried across sessions: {r['notes']['total']} ({r['notes']['stale']} stale)",
        "",
        "| tool | calls | served | conservative baseline | typical baseline |",
        "|---|---|---|---|---|",
    ]
    for k, v in r["by_tool"].items():
        lines.append(f"| {k} | {v['calls']} | {_k(v['served'])} | {_k(v['lean'])} | {_k(v['full'])} |")
    cs = r["claude_sessions"]
    lines += ["", "## Claude Code sessions in this folder, before vs after enabling"]
    if not cs["available"]:
        lines.append(f"- {cs['reason']}.")
    else:
        b, a = cs["before"], cs["after"]
        if not b or not a:
            lines.append(f"- Not enough sessions yet (before: {b['sessions'] if b else 0}, after: {a['sessions'] if a else 0}). "
                         "Keep working; this fills in automatically.")
        lines.append("")
        lines.append("| median per session | before | after | change |")
        lines.append("|---|---|---|---|")
        for key, label in [("median_cost_input_equiv", "cost (input-equivalent tokens)"), ("median_api_calls", "API calls"),
                           ("median_peak_context", "peak context"), ("median_exploration_tokens", "exploration result tokens"),
                           ("median_exploration_calls", "exploration calls (Read/Grep/Glob/ls…)"),
                           ("median_tokendiet_calls", "tokendiet calls")]:
            bv, av = (b or {}).get(key), (a or {}).get(key)
            ch = f"{(av / bv - 1) * 100:+.0f}%" if bv and av is not None else "–"
            lines.append(f"| {label} | {_k(bv) if bv is not None else '–'} | {_k(av) if av is not None else '–'} | {ch} |")
        lines.append("")
        lines.append("Session medians vary with the work done; compare over several sessions of similar tasks.")
    return "\n".join(lines)
