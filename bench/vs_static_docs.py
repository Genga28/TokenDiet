"""TokenDiet vs static agent docs (CLAUDE.md / AGENTS.md / SKILL.md).

Part 1, real docs (--doc-repo): size, per-session carry cost, how many code references in the doc still exist,
and how many of the repo's important symbols the doc mentions at all.
Part 2, doc rot (--rot-repo): snapshot the repo's structure N months ago (git archive, read-only) and check how
much of a structure doc written then would be wrong today.

  python -m bench.vs_static_docs --doc-repo <repo> ... --rot-repo <repo> ... [--json out.json]
"""

import argparse
import io
import json
import random
import re
import statistics
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tokendiet import tokens  # noqa: E402
from tokendiet.graph import Graph, _is_test  # noqa: E402

DOC_GLOBS = ["CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md", ".agents/skills/*/SKILL.md", ".claude/skills/*/SKILL.md"]
CALLS_PER_SESSION = 102  # median-ish from the user's 18 real sessions (1,835 calls); overridden by --calls
SPAN_RE = re.compile(r"`([^`\n]{2,120})`")
PATH_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.\w{1,5}|[\w-]+\.(?:py|ts|tsx|js|go|rs|java|md|toml|yaml|yml|json))(?![\w/])")
IDENT_RE = re.compile(r"^[A-Za-z_][\w.]*(\(\))?$")


def session_cost(doc_tokens, calls):
    """Input-equivalent tokens a doc costs per session: written to cache once (1h, 2x), re-read on every call (0.1x)."""
    return round(doc_tokens * (2.0 + 0.1 * calls))


def doc_repo(repo, calls, seed=7):
    repo = Path(repo).resolve()
    g = Graph(repo)
    g.refresh(force=True)
    g._build()
    docs = sorted({p for pat in DOC_GLOBS for p in repo.glob(pat) if p.is_file()})
    always = [p for p in docs if p.name in ("CLAUDE.md", "AGENTS.md") and p.parent == repo or p.parent.name == ".claude" and p.name == "CLAUDE.md"]
    text = {p: p.read_text(encoding="utf-8", errors="replace") for p in docs}
    all_text = "\n".join(text.values())

    # code references in the docs, checked against the repo as it is now
    all_files = {str(p.relative_to(repo)).replace("\\", "/") for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts}
    basenames = {f.rsplit("/", 1)[-1] for f in all_files}
    sym_names = {r[0] for r in g.con.execute("SELECT DISTINCT name FROM symbols")}
    paths, idents = set(), set()
    for m in PATH_RE.finditer(all_text):
        p = m.group(1).strip("./")
        if "/" in p and "path/to" not in p:  # bare "Node.js"-style words and placeholders are not file references
            paths.add(p)
    for m in SPAN_RE.finditer(all_text):
        s = m.group(1).strip()
        if IDENT_RE.match(s) and ("_" in s or any(c.isupper() for c in s[1:]) or s.endswith("()")) and "/" not in s:
            idents.add(s.rstrip("()").split(".")[-1])
    path_ok = {p for p in paths if p in all_files or any(f.endswith("/" + p) for f in all_files)}
    # an identifier is valid if it is a parsed symbol or appears anywhere in the repo's text (config keys, flags, env vars)
    repo_text = []
    for f in all_files:
        fp = repo / f
        if fp.suffix.lower() in (".md",) or fp.stat().st_size > 2_000_000:
            continue
        try:
            repo_text.append(fp.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            pass
    blob = "\n".join(repo_text)
    ident_ok = {i for i in idents if i in sym_names or re.search(rf"(?<![\w$]){re.escape(i)}(?![\w$])", blob)}

    # coverage: important symbols (used from 2+ files, not tests) the docs mention at all
    cands = sorted({r["name"] for r in g.con.execute(
        "SELECT s.name, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.depth<=1 AND length(s.name)>3")
        if len(g.ref_files.get(r["name"], ())) >= 2 and not _is_test(r["path"])})
    random.Random(seed).shuffle(cands)
    sample = cands[:200]
    mentioned = [n for n in sample if re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", all_text)]

    full_map = g.repo_map("", 10 ** 8)
    always_tk = sum(tokens.count(text[p]) for p in always)
    skills_tk = {str(p.relative_to(repo)): tokens.count(text[p]) for p in docs if p.name == "SKILL.md"}
    return {
        "repo": repo.name, "files_indexed": len(g.files), "symbols": len(sym_names),
        "docs": {str(p.relative_to(repo)): tokens.count(text[p]) for p in docs},
        "always_loaded_tokens": always_tk, "skill_tokens": skills_tk,
        "always_loaded_cost_per_session": session_cost(always_tk, calls),
        "paths_mentioned": len(paths), "paths_valid": len(path_ok),
        "stale_paths": sorted(paths - path_ok)[:15],
        "identifiers_mentioned": len(idents), "identifiers_valid": len(ident_ok),
        "important_symbols_sampled": len(sample), "important_symbols_mentioned": len(mentioned),
        "full_structure_doc_tokens": tokens.count(full_map),
        "full_structure_cost_per_session": session_cost(tokens.count(full_map), calls),
        "tokendiet_map_cost_per_session": session_cost(1500, calls),
    }


def _snapshot(repo, rev, dest):
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", rev], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        t.extractall(dest)


def _structure(path):
    g = Graph(path)
    g.refresh(force=True)
    rows = g.con.execute("SELECT f.path, s.name, s.kind, s.start_line, s.signature FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.depth<=1")
    out = {}
    for r in rows:
        out.setdefault((r["path"], r["name"]), (r["start_line"], r["signature"]))
    files = {r[0] for r in g.con.execute("SELECT path FROM files")}
    g.con.close()
    return out, files


def rot_repo(repo, months=(3, 6, 12)):
    repo = Path(repo).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        now_dir = Path(tmp) / "now"
        _snapshot(repo, "HEAD", now_dir)
        now, now_files = _structure(now_dir)
        names_now = {n for _, n in now}
        head_date = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%cI"], capture_output=True, text=True).stdout.strip()
        res = {"repo": repo.name, "head": head_date[:10], "ages": []}
        head = datetime.fromisoformat(head_date)
        for m in months:
            cutoff = (head - timedelta(days=round(30.4 * m))).strftime("%Y-%m-%dT%H:%M:%S")
            before = subprocess.run(["git", "-C", str(repo), "rev-list", "-1", f"--before={cutoff}", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
            if not before:
                continue
            d = Path(tmp) / f"m{m}"
            _snapshot(repo, before, d)
            old, old_files = _structure(d)
            if not old:
                continue
            gone = [k for k in old if k not in now]
            renamed_or_deleted = [k for k in gone if k[1] not in names_now]
            moved_file = [k for k in gone if k[1] in names_now]
            line_off = [k for k in old if k in now and abs(old[k][0] - now[k][0]) > 10]
            sig_changed = [k for k in old if k in now and old[k][1] != now[k][1]]
            new_syms = [k for k in now if k not in old]
            res["ages"].append({
                "months": m, "entries_then": len(old),
                "pct_entries_wrong_path_or_gone": round(100 * len(gone) / len(old), 1),
                "pct_symbols_deleted_or_renamed": round(100 * len(renamed_or_deleted) / len(old), 1),
                "pct_moved_to_other_file": round(100 * len(moved_file) / len(old), 1),
                "pct_signature_changed": round(100 * len(sig_changed) / len(old), 1),
                "pct_line_numbers_off_by_10plus": round(100 * len(line_off) / len(old), 1),
                "pct_of_today_missing_from_doc": round(100 * len(new_syms) / len(now), 1),
                "commit_then": before[:8], "files_then": len(old_files), "files_now": len(now_files),
                "files_gone": len(old_files - now_files), "files_new": len(now_files - old_files),
            })
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-repo", nargs="*", default=[])
    ap.add_argument("--rot-repo", nargs="*", default=[])
    ap.add_argument("--calls", type=int, default=CALLS_PER_SESSION)
    ap.add_argument("--json")
    a = ap.parse_args()
    out = {"docs": [], "rot": []}
    for r in a.doc_repo:
        d = doc_repo(r, a.calls)
        out["docs"].append(d)
        print(f"\n== {d['repo']} ({d['files_indexed']} files, {d['symbols']} symbols)")
        for k, v in d["docs"].items():
            print(f"   {k}: {v:,} tokens")
        print(f"   always-loaded: {d['always_loaded_tokens']:,} tok -> {d['always_loaded_cost_per_session']:,} input-equiv per {a.calls}-call session")
        print(f"   paths in docs still valid: {d['paths_valid']}/{d['paths_mentioned']}   code identifiers still valid: {d['identifiers_valid']}/{d['identifiers_mentioned']}")
        if d["stale_paths"]:
            print(f"   stale paths: {', '.join(d['stale_paths'][:8])}")
        print(f"   important symbols mentioned by docs: {d['important_symbols_mentioned']}/{d['important_symbols_sampled']}")
        print(f"   full structure as a static doc: {d['full_structure_doc_tokens']:,} tok -> {d['full_structure_cost_per_session']:,} per session"
              f"  (tokendiet 1.5k map: {d['tokendiet_map_cost_per_session']:,})")
    for r in a.rot_repo:
        x = rot_repo(r)
        out["rot"].append(x)
        print(f"\n== doc rot: {x['repo']} (HEAD {x['head']})")
        for s in x["ages"]:
            print(f"   doc written {s['months']:>2} months ago ({s['entries_then']} entries): wrong path/gone {s['pct_entries_wrong_path_or_gone']}% "
                  f"(deleted/renamed {s['pct_symbols_deleted_or_renamed']}%, moved file {s['pct_moved_to_other_file']}%), "
                  f"signature changed {s['pct_signature_changed']}%, line no. off >10: {s['pct_line_numbers_off_by_10plus']}%, "
                  f"today's symbols missing: {s['pct_of_today_missing_from_doc']}%  files {s['files_then']}->{s['files_now']}")
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
