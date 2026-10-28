"""Graph queries over the index. Every public method returns text that fits a token budget."""

import math
import re
import time
from collections import defaultdict

from . import tokens
from .indexer import Indexer

MAX_DEF_FILES = 20  # a name defined in more files than this carries no signal (get, init, run...)
TEST_RE = re.compile(r"(^|/)(tests?|spec|__tests__)(/|$)|(_test|\.test|\.spec|_spec)\.\w+$|(^|/)test_[^/]+$")


def _is_test(path):
    return bool(TEST_RE.search(path))


def pagerank(nodes, out_edges, personalization=None, alpha=0.85, iters=100, tol=1e-9):
    """Weighted personalised PageRank. out_edges: {src: {dst: weight}}. Dangling mass goes to the teleport vector."""
    n = len(nodes)
    if personalization:
        s = sum(personalization.values())
        tele = {k: personalization.get(k, 0.0) / s for k in nodes}
    else:
        tele = {k: 1.0 / n for k in nodes}
    totals = {k: sum(v.values()) for k, v in out_edges.items()}
    pr = dict(tele) if personalization else {k: 1.0 / n for k in nodes}
    for _ in range(iters):
        dangling = sum(pr[k] for k in nodes if not totals.get(k))
        nxt = {k: (1 - alpha) * tele[k] + alpha * dangling * tele[k] for k in nodes}
        for src, dsts in out_edges.items():
            if not totals[src]:
                continue
            share = alpha * pr[src] / totals[src]
            for dst, w in dsts.items():
                nxt[dst] += share * w
        if sum(abs(nxt[k] - pr[k]) for k in nodes) < tol:
            return nxt
        pr = nxt
    return pr


class Graph:
    def __init__(self, repo):
        self.ix = Indexer(repo)
        self.con = self.ix.con
        self._cache_key = None
        self._g = None
        self.last_refresh = {}

    # ---------- freshness ----------
    def refresh(self, force=False):
        self.last_refresh = self.ix.refresh(force=force)
        return self.last_refresh

    def _key(self):
        r = self.con.execute("SELECT COUNT(*), COALESCE(SUM(mtime),0) FROM files").fetchone()
        return (r[0], r[1])

    # ---------- file graph ----------
    def _build(self):
        key = self._key()
        if key == self._cache_key and self._g is not None:
            return
        con = self.con
        self.files = {r["id"]: dict(r) for r in con.execute("SELECT id, path, lang, lines, tokens, hash FROM files")}
        self.id_by_path = {f["path"]: i for i, f in self.files.items()}
        def_files = defaultdict(set)
        for r in con.execute("SELECT name, file_id FROM symbols WHERE depth <= 1"):
            def_files[r["name"]].add(r["file_id"])
        self.def_files = {n: fs for n, fs in def_files.items() if len(fs) <= MAX_DEF_FILES and len(n) > 2}
        self.imports = defaultdict(set)
        for r in con.execute("SELECT file_id, target_id FROM imports WHERE target_id IS NOT NULL"):
            if r["file_id"] != r["target_id"]:
                self.imports[r["file_id"]].add(r["target_id"])
        self.ref_files = defaultdict(set)  # name -> other files referencing it
        w = defaultdict(float)
        for src, tgts in self.imports.items():
            for t in tgts:
                w[(src, t)] += 1.0
        for r in con.execute("SELECT file_id, name, n FROM refs"):
            dsts = self.def_files.get(r["name"])
            src = r["file_id"]
            if not dsts or src in dsts:  # undefined here, or a local reference to the file's own definition
                continue
            # when the file imports one of the defining files, the reference goes there only
            dsts = (dsts & self.imports.get(src, set())) or dsts
            self.ref_files[r["name"]].add(src)
            share = math.sqrt(r["n"]) / len(dsts)
            for d in dsts:
                w[(src, d)] += share
        out_edges, in_edges = defaultdict(dict), defaultdict(dict)
        for (a, b), x in w.items():
            if a in self.files and b in self.files:
                out_edges[a][b] = x
                in_edges[b][a] = x
        self._g = out_edges
        self._rev = in_edges
        self._rank_cache = {}
        self._cache_key = key

    def rank(self, focus=""):
        self._build()
        if not self.files:
            return []
        if focus in self._rank_cache:
            return self._rank_cache[focus]
        pers = None
        terms = [t.lower() for t in re.split(r"[\s,;]+", focus or "") if len(t) >= 3]
        if terms:
            pers = {}
            for fid, f in self.files.items():
                p = f["path"].lower()
                if any(t in p for t in terms):
                    pers[fid] = pers.get(fid, 0) + 1.0
            for name, fs in self.def_files.items():
                ln = name.lower()
                if any(t == ln for t in terms):
                    for fid in fs:
                        pers[fid] = pers.get(fid, 0) + 3.0
            if not pers:
                pers = None
        pr = pagerank(list(self.files), self._g, pers)
        # personalisation alone leaks rank back to hub files; put the focused files themselves first
        top = max(pr.values()) or 1.0
        for fid, w in (pers or {}).items():
            pr[fid] += top * w
        for fid, f in self.files.items():
            if _is_test(f["path"]):
                pr[fid] *= 0.3
        ranked = sorted(pr.items(), key=lambda kv: (-kv[1], self.files[kv[0]]["path"]))
        self._rank_cache[focus] = ranked
        return ranked

    # ---------- helpers ----------
    def _header(self):
        r = self.last_refresh or {}
        n = len(self.files) if hasattr(self, "files") else 0
        fresh = f"reindexed {r.get('reparsed', 0)} changed file(s)" if r.get("checked") else "index current"
        return f"[tokendiet | {n} files | {fresh}]"

    def _read_lines(self, path, start, end):
        try:
            with open(self.ix.repo / path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return []
        return lines[start - 1:end]

    @staticmethod
    def _fit(lines, budget):
        out, used = [], 0
        for i, ln in enumerate(lines):
            c = tokens.count(ln) + 1
            if used + c > budget:
                out.append(f"… truncated ({len(lines) - i} more lines; raise budget or narrow the query)")
                break
            out.append(ln)
            used += c
        return "\n".join(out)

    def _symbols_named(self, query):
        con = self.con
        q = query.strip()
        parent = None
        if "." in q and not q.startswith(".") or "::" in q:
            parts = re.split(r"\.|::", q)
            parent, q = parts[-2], parts[-1]
        sql = ("SELECT s.*, f.path FROM symbols s JOIN files f ON f.id=s.file_id "
               "LEFT JOIN symbols p ON p.id=s.parent_id WHERE {} {} ORDER BY s.depth, f.path LIMIT 200")
        pclause = "AND p.name=?" if parent else ""
        args = (q, parent) if parent else (q,)
        rows = con.execute(sql.format("s.name=?", pclause), args).fetchall()
        if not rows:
            rows = con.execute(sql.format("s.name=? COLLATE NOCASE", pclause), args).fetchall()
        if not rows and len(q) >= 3:
            rows = con.execute(sql.format("s.name LIKE ?", pclause), ((f"%{q}%", parent) if parent else (f"%{q}%",))).fetchall()
        self._build()
        return sorted(rows, key=lambda r: (-len(self.ref_files.get(r["name"], ())), r["depth"], r["path"], r["start_line"]))

    def _resolve_target_file(self, target):
        self._build()
        t = target.strip().replace("\\", "/").lstrip("./")
        if t in self.id_by_path:
            return self.id_by_path[t]
        hits = [i for p, i in self.id_by_path.items() if p.endswith("/" + t) or p.endswith(t)]
        return hits[0] if len(hits) == 1 else (hits if hits else None)

    def _imports_of(self, fid):
        return {r[0] for r in self.con.execute("SELECT target_id FROM imports WHERE file_id=? AND target_id IS NOT NULL", (fid,))}

    def _notes_for(self, fid=None, symbol=None, limit=20):
        rows = self.con.execute("SELECT * FROM notes ORDER BY id DESC").fetchall()
        out = []
        for r in rows:
            if fid is not None and self.id_by_path.get(r["path"]) != fid:
                continue
            if symbol is not None and r["symbol"] != symbol:
                continue
            out.append(r)
        return out[:limit]

    def _note_line(self, r):
        cur = self.files.get(self.id_by_path.get(r["path"], -1))
        if cur is None:
            state = "ORPHANED (file gone)"
        elif cur["hash"] != r["file_hash"]:
            state = "STALE (file changed since)"
        else:
            state = "fresh"
        tgt = r["path"] + (f"::{r['symbol']}" if r["symbol"] else "")
        return f"- [{state}] {tgt}: {r['text']}"

    # ---------- tools ----------
    def repo_map(self, focus="", budget=2000):
        ranked = self.rank(focus)
        con = self.con
        syms_by_file = defaultdict(list)
        for r in con.execute("SELECT file_id, name, kind, depth, start_line, end_line, signature FROM symbols WHERE depth <= 1 ORDER BY file_id, start_line"):
            syms_by_file[r["file_id"]].append(r)
        lines = [self._header() + f" repo map, ranked by dependency centrality" + (f", focus: {focus}" if focus else "")]
        notes = self._notes_for(limit=8)
        if notes:
            lines.append("Notes from earlier sessions:")
            lines += [self._note_line(n) for n in notes]
        used = sum(tokens.count(x) for x in lines)
        shown = 0
        for rank_i, (fid, _score) in enumerate(ranked):
            f = self.files[fid]
            syms = syms_by_file.get(fid, [])
            cap = 25 if rank_i < 10 else 8
            if len(syms) > cap:
                keep = sorted(syms, key=lambda s: -len(self.ref_files.get(s["name"], ())))[:cap]
                keep_ids = {(s["name"], s["start_line"]) for s in keep}
                syms_shown = [s for s in syms if (s["name"], s["start_line"]) in keep_ids]
            else:
                syms_shown = syms
            block = [f"{f['path']} ({f['lines']} lines)"]
            block += [f"  {s['start_line']:>5} {'  ' * s['depth']}{s['signature']}" for s in syms_shown]
            if len(syms) > len(syms_shown):
                block.append(f"        … +{len(syms) - len(syms_shown)} more symbols")
            cost = sum(tokens.count(x) + 1 for x in block)
            if used + cost > budget:
                # degrade to one line: path + top symbol names
                top = sorted(syms, key=lambda s: -len(self.ref_files.get(s["name"], ())))[:4]
                block = [f"{f['path']}: " + ", ".join(s["name"] for s in top)]
                cost = tokens.count(block[0]) + 1
                if used + cost > budget:
                    break
            lines += block
            used += cost
            shown += 1
        rest = [self.files[fid]["path"] for fid, _ in ranked[shown:]]
        if rest:
            dirs = defaultdict(int)
            for p in rest:
                dirs[p.rsplit("/", 1)[0] if "/" in p else "."] += 1
            tail = "Not shown: " + ", ".join(f"{d}/ ({n})" for d, n in sorted(dirs.items(), key=lambda kv: -kv[1])[:15])
            if used + tokens.count(tail) <= budget * 1.05:
                lines.append(tail)
        return "\n".join(lines)

    def find_symbol(self, query, include_body=False, limit=10, budget=1500):
        rows = self._symbols_named(query)
        if not rows:
            return f"{self._header()} no symbol matching '{query}'. Try a shorter name, or grep for dynamic/string references."
        out = [f"{self._header()} {len(rows)} match(es) for '{query}'"]
        for i, r in enumerate(rows[:limit]):
            nref = len(self.ref_files.get(r["name"], ()))
            out.append(f"{r['path']}:{r['start_line']}-{r['end_line']}  {r['kind']}  {r['signature']}  (referenced in {nref} files)")
            if include_body and i == 0:  # body for the best match only; others are one line each
                members = self.con.execute("SELECT name, start_line, signature FROM symbols WHERE parent_id=? ORDER BY start_line",
                                           (r["id"],)).fetchall()
                if members and r["end_line"] - r["start_line"] > 120:
                    # a large class: outline its members; the agent can then ask for one method
                    out += [f"  {m['start_line']:>5}  {m['signature']}" for m in members]
                else:
                    body = self._read_lines(r["path"], r["start_line"], r["end_line"])
                    out += [f"  {r['start_line'] + i:>5}| {b}" for i, b in enumerate(body)]
        if len(rows) > limit:
            out.append(f"… {len(rows) - limit} more matches")
        if include_body and len(rows) > 1:
            out.append("Body shown for the first match; use symbol_context with path= for another.")
        return self._fit(out, budget)

    def symbol_context(self, name, path="", budget=3000):
        rows = self._symbols_named(name)
        if path:
            rows = [r for r in rows if r["path"].endswith(path.replace("\\", "/"))] or rows
        if not rows:
            return f"{self._header()} no symbol '{name}'."
        s = rows[0]
        con = self.con
        out = [self._header()]
        if len(rows) > 1:
            out.append(f"{len(rows)} definitions named '{s['name']}'; showing {s['path']}. Others: " +
                       ", ".join(f"{r['path']}:{r['start_line']}" for r in rows[1:6]))
        out.append(f"## {s['kind']} {s['name']}  {s['path']}:{s['start_line']}-{s['end_line']}")
        body = self._read_lines(s["path"], s["start_line"], s["end_line"])
        body_budget = int(budget * 0.5)
        out.append(self._fit([f"{s['start_line'] + i:>5}| {b}" for i, b in enumerate(body)], body_budget))

        members = con.execute("SELECT name, kind, start_line, signature FROM symbols WHERE parent_id=? ORDER BY start_line", (s["id"],)).fetchall()
        if members and len(body) > 0 and tokens.count("\n".join(body)) > body_budget:
            out.append("Members: " + ", ".join(f"{m['name']}@{m['start_line']}" for m in members))

        fid = s["file_id"]
        my_imports = self._imports_of(fid)
        callees = con.execute("SELECT DISTINCT callee FROM calls WHERE file_id=? AND line BETWEEN ? AND ?",
                              (fid, s["start_line"], s["end_line"])).fetchall()
        res = []
        for (cname,) in callees:
            if cname == s["name"]:
                continue
            defs = con.execute("SELECT s.file_id, s.start_line, f.path FROM symbols s JOIN files f ON f.id=s.file_id WHERE s.name=?", (cname,)).fetchall()
            pick = [d for d in defs if d["file_id"] == fid] or [d for d in defs if d["file_id"] in my_imports] or (defs if len(defs) == 1 else [])
            if pick:
                res.append(f"{cname} → {pick[0]['path']}:{pick[0]['start_line']}")
            elif defs:
                res.append(f"{cname} (ambiguous: {len(defs)} defs)")
        if res:
            out.append("Calls: " + "; ".join(sorted(res)[:40]))

        n_defs = len(self.def_files.get(s["name"], ())) or len(rows)
        callers = con.execute(
            "SELECT c.file_id, c.line, f.path, p.name AS caller FROM calls c JOIN files f ON f.id=c.file_id "
            "LEFT JOIN symbols p ON p.id=c.caller_id WHERE c.callee=? ORDER BY f.path, c.line", (s["name"],)).fetchall()
        groups = defaultdict(list)
        for c in callers:
            if c["file_id"] == fid and s["start_line"] <= c["line"] <= s["end_line"]:
                continue
            if c["file_id"] == fid or fid in self._imports_of(c["file_id"]):
                conf = "direct"
            elif n_defs == 1:
                conf = "likely"
            else:
                conf = f"name-match/{n_defs} defs"
            groups[(c["path"], conf)].append(f"{c['line']}" + (f"({c['caller']})" if c["caller"] else ""))
        if groups:
            out.append(f"Called from {len(groups)} file(s):")
            ordered = sorted(groups.items(), key=lambda kv: (kv[0][1] != "direct", kv[0][1] != "likely", _is_test(kv[0][0]), kv[0][0]))
            for (p, conf), locs in ordered[:40]:
                out.append(f"  {p} [{conf}] lines {', '.join(locs[:8])}{' …' if len(locs) > 8 else ''}")
            if len(groups) > 40:
                out.append(f"  … {len(groups) - 40} more files")
        # type annotations, instantiation via factories, re-exports, passing as a value
        call_files = {p for p, _ in groups}
        refs = sorted((self.files[f]["path"] for f in self.ref_files.get(s["name"], ()) if f != fid
                       and self.files[f]["path"] not in call_files), key=lambda p: (_is_test(p), p))
        if refs:
            out.append(f"Referenced without a call in {len(refs)} file(s): " + ", ".join(refs[:40]) + (" …" if len(refs) > 40 else ""))
        if not groups and not refs:
            out.append("No call sites or references found (may be invoked dynamically, via decorators/DI/reflection, or unused).")
        nts = self._notes_for(fid=fid, symbol=None)
        if nts:
            out.append("Notes:")
            out += [self._note_line(n) for n in nts[:5]]
        return self._fit("\n".join(out).split("\n"), budget)

    def impact(self, target, depth=2, budget=2000):
        self._build()
        rf = self._resolve_target_file(target)
        con = self.con
        out = [self._header()]
        if isinstance(rf, int):
            seeds = {rf}
            label = self.files[rf]["path"]
        elif isinstance(rf, list):
            return f"{self._header()} '{target}' matches several files: " + ", ".join(self.files[i]["path"] for i in rf[:10])
        else:
            rows = self._symbols_named(target)
            if not rows:
                return f"{self._header()} no file or symbol '{target}'."
            return self._symbol_impact(rows[0], depth, budget)
        level = {rf: 0}
        frontier = [rf]
        for d in range(1, depth + 1):
            nxt = []
            for n in frontier:
                for m in self._rev.get(n, ()):
                    if m not in level:
                        level[m] = d
                        nxt.append(m)
            frontier = nxt
        my_names = {r[0] for r in con.execute("SELECT name FROM symbols WHERE file_id=? AND depth<=1", (rf,))}
        direct = sorted((m for m, d in level.items() if d == 1), key=lambda m: self.files[m]["path"])
        second = sorted((m for m, d in level.items() if d >= 2), key=lambda m: self.files[m]["path"])
        out.append(f"## Impact of changing {label}: {len(direct)} direct dependents, {len(second)} at depth 2")
        for m in direct:
            used = sorted({r[0] for r in con.execute("SELECT name FROM refs WHERE file_id=?", (m,))} & my_names)
            tag = " [test]" if _is_test(self.files[m]["path"]) else ""
            out.append(f"  {self.files[m]['path']}{tag} uses: {', '.join(used[:8]) or '(import)'}")
        if second:
            out.append("Depth 2: " + ", ".join(self.files[m]["path"] for m in second[:60]) + (" …" if len(second) > 60 else ""))
        tests = [self.files[m]["path"] for m in level if m != rf and _is_test(self.files[m]["path"])]
        out.append(f"Tests to run ({len(tests)}): " + (", ".join(sorted(tests)[:20]) if tests else "none found by static analysis"))
        return self._fit(out, budget)

    def _symbol_impact(self, s, depth, budget):
        con = self.con
        out = [self._header(), f"## Impact of changing {s['kind']} {s['name']} ({s['path']}:{s['start_line']})"]
        seen = {s["name"]}
        frontier = [s["name"]]
        tests = set()
        for d in range(1, depth + 1):
            nxt = []
            rows = []
            for name in frontier:
                rows += con.execute("SELECT DISTINCT f.path, p.name AS caller FROM calls c JOIN files f ON f.id=c.file_id "
                                    "LEFT JOIN symbols p ON p.id=c.caller_id WHERE c.callee=?", (name,)).fetchall()
            files = defaultdict(set)
            for r in rows:
                files[r["path"]].add(r["caller"] or "<module>")
                if _is_test(r["path"]):
                    tests.add(r["path"])
                if r["caller"] and r["caller"] not in seen:
                    seen.add(r["caller"])
                    nxt.append(r["caller"])
            out.append(f"Depth {d}: {len(files)} file(s)")
            for p in sorted(files)[:40]:
                out.append(f"  {p}: {', '.join(sorted(files[p])[:6])}")
            frontier = nxt
            if not frontier:
                break
        out.append(f"Tests touching it ({len(tests)}): " + (", ".join(sorted(tests)[:20]) or "none found"))
        out.append("Caller resolution is by name; same-name methods on other types may appear.")
        return self._fit(out, budget)

    def notes(self, action="get", target="", text=""):
        self._build()
        if action == "add":
            if not text:
                return "notes add needs text."
            sym = None
            rf = self._resolve_target_file(target) if target else None
            if not isinstance(rf, int) and target:
                rows = self._symbols_named(target)
                if rows:
                    rf, sym = rows[0]["file_id"], rows[0]["name"]
            path = self.files[rf]["path"] if isinstance(rf, int) else (target or "*")
            fhash = self.files[rf]["hash"] if isinstance(rf, int) else None
            self.con.execute("INSERT INTO notes(path, symbol, text, file_hash, created) VALUES(?,?,?,?,?)",
                             (path, sym, text.strip()[:1000], fhash, time.strftime("%Y-%m-%d %H:%M")))
            self.con.commit()
            return f"saved note on {path}{'::' + sym if sym else ''}"
        if action == "delete":
            self.con.execute("DELETE FROM notes WHERE id=?", (int(target),))
            self.con.commit()
            return "deleted"
        rows = self.con.execute("SELECT * FROM notes ORDER BY id DESC").fetchall()
        if target:
            t = target.lower()
            rows = [r for r in rows if t in (r["path"] or "").lower() or t == (r["symbol"] or "").lower() or t in r["text"].lower()]
        if not rows:
            return "no notes" + (f" for '{target}'" if target else "")
        return "\n".join(f"#{r['id']} " + self._note_line(r)[2:] for r in rows[:30])
