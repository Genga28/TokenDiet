"""Discover files, parse changed ones, resolve imports. Incremental by (mtime, size) then content hash."""

import hashlib
import os
import posixpath
import subprocess
import sys
import time
from pathlib import Path

from . import tokens
from .langs import EXT_LANG, EXTRACTOR_VERSION, extract
from .store import delete_file_rows, open_db

IGNORE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "vendor", "third_party", "dist", "build", "out",
    "target", ".next", ".nuxt", ".svelte-kit", "coverage", "__pycache__", ".venv", "venv", "env", ".tox",
    ".mypy_cache", ".pytest_cache", ".tokendiet", ".idea", ".vscode", ".gradle", "Pods", ".turbo", ".cache",
}
SKIP_SUFFIXES = (".min.js", ".min.css", ".bundle.js", ".map", ".d.ts.map", "-lock.json", ".lock", ".pb.go", "_pb2.py")
MAX_BYTES = 512 * 1024

_parsers = {}


def _parser(lang):
    if lang not in _parsers:
        try:
            from tree_sitter_language_pack import get_parser
            _parsers[lang] = get_parser(lang)
        except Exception as e:  # grammar unavailable: file stays indexed at file level
            print(f"[tokendiet] no grammar for {lang}: {e}", file=sys.stderr)
            _parsers[lang] = None
    return _parsers[lang]


def discover(repo: Path) -> list:
    """Relative posix paths of candidate source files. Uses git when available so .gitignore is honoured."""
    paths = None
    if (repo / ".git").exists():
        try:
            out = subprocess.run(["git", "-C", str(repo), "ls-files", "-co", "--exclude-standard", "-z"],
                                 capture_output=True, timeout=60)
            if out.returncode == 0:
                paths = [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
        except Exception:
            paths = None
    if paths is None:
        paths = []
        for root, dirs, files in os.walk(repo, followlinks=False):
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS and not d.startswith(".")]
            rel = os.path.relpath(root, repo)
            for f in files:
                paths.append(f if rel == "." else posixpath.join(rel.replace(os.sep, "/"), f))
    keep = []
    for p in paths:
        parts = p.split("/")
        if any(x in IGNORE_DIRS for x in parts[:-1]):
            continue
        low = p.lower()
        if low.endswith(SKIP_SUFFIXES):
            continue
        if os.path.splitext(low)[1] in EXT_LANG:
            keep.append(p)
    return sorted(set(keep))


def _looks_generated(src: bytes) -> bool:
    if b"\0" in src[:8000]:
        return True
    head = src[:2000]
    if b"@generated" in head or b"DO NOT EDIT" in head or b"Code generated" in head:
        return True
    nl = src.count(b"\n") + 1
    return len(src) / nl > 300  # minified


class Indexer:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        self.con = open_db(self.repo)
        self._last_refresh = 0.0
        v = self.con.execute("SELECT v FROM meta WHERE k='extractor'").fetchone()
        if v is None or v[0] != EXTRACTOR_VERSION:
            self.reset()

    def reset(self):
        """Drop the parsed index (notes are kept)."""
        for t in ("files", "symbols", "calls", "refs", "imports"):
            self.con.execute(f"DELETE FROM {t}")
        self.con.execute("INSERT OR REPLACE INTO meta VALUES('extractor', ?)", (EXTRACTOR_VERSION,))
        self.con.commit()
        self.con.execute("VACUUM")

    def refresh(self, force=False, throttle=2.0, verbose=False) -> dict:
        """Bring the index up to date. Cheap when nothing changed (one stat per file)."""
        now = time.time()
        if not force and now - self._last_refresh < throttle:
            return {"checked": False}
        t0 = time.time()
        con = self.con
        known = {r["path"]: r for r in con.execute("SELECT id, path, hash, mtime, size FROM files")}
        current = discover(self.repo)
        cur_set = set(current)
        removed = [p for p in known if p not in cur_set]
        for p in removed:
            fid = known[p]["id"]
            delete_file_rows(con, fid)
            con.execute("DELETE FROM files WHERE id=?", (fid,))
        changed = parsed = skipped = 0
        total = len(current)
        for i, p in enumerate(current):
            full = self.repo / p
            try:
                st = full.stat()
            except OSError:
                continue
            row = known.get(p)
            if row is not None and row["mtime"] == st.st_mtime and row["size"] == st.st_size:
                continue
            if st.st_size > MAX_BYTES:
                skipped += 1
                continue
            try:
                src = full.read_bytes()
            except OSError:
                continue
            h = hashlib.sha1(src).hexdigest()
            if row is not None and row["hash"] == h:
                con.execute("UPDATE files SET mtime=?, size=? WHERE id=?", (st.st_mtime, st.st_size, row["id"]))
                continue
            if _looks_generated(src):
                skipped += 1
                continue
            self._index_file(p, src, h, st, row)
            changed += 1
            parsed += 1
            if verbose and parsed % 200 == 0:
                print(f"[tokendiet] parsed {parsed} files ({i + 1}/{total})", file=sys.stderr)
        if changed or removed:
            self._resolve_imports()
        con.execute("INSERT OR REPLACE INTO meta VALUES('indexed_at', ?)", (time.strftime("%Y-%m-%dT%H:%M:%S"),))
        con.commit()
        self._last_refresh = time.time()
        return {"checked": True, "files": total, "reparsed": changed, "removed": len(removed),
                "skipped": skipped, "seconds": round(time.time() - t0, 2)}

    def _index_file(self, path, src, h, st, row):
        con = self.con
        lang = EXT_LANG[os.path.splitext(path.lower())[1]]
        text = src.decode("utf-8", "replace")
        if row is not None:
            fid = row["id"]
            delete_file_rows(con, fid)
            con.execute("UPDATE files SET lang=?, hash=?, mtime=?, size=?, lines=?, tokens=? WHERE id=?",
                        (lang, h, st.st_mtime, st.st_size, text.count("\n") + 1, tokens.count(text), fid))
        else:
            cur = con.execute("INSERT INTO files(path, lang, hash, mtime, size, lines, tokens) VALUES(?,?,?,?,?,?,?)",
                              (path, lang, h, st.st_mtime, st.st_size, text.count("\n") + 1, tokens.count(text)))
            fid = cur.lastrowid
        parser = _parser(lang)
        if parser is None:
            return
        try:
            tree = parser.parse(src)
            facts = extract(tree, src, lang)
        except Exception as e:
            print(f"[tokendiet] parse failed {path}: {e}", file=sys.stderr)
            return
        ids = []
        for s in facts.symbols:
            parent = ids[s.parent] if s.parent >= 0 else None
            cur = con.execute(
                "INSERT INTO symbols(file_id, name, kind, parent_id, depth, start_line, end_line, signature) "
                "VALUES(?,?,?,?,?,?,?,?)", (fid, s.name, s.kind, parent, s.depth, s.start, s.end, s.signature))
            ids.append(cur.lastrowid)
        con.executemany("INSERT INTO calls VALUES(?,?,?,?)",
                        [(fid, ids[e] if e >= 0 else None, c, ln) for c, ln, e in facts.calls])
        con.executemany("INSERT INTO refs VALUES(?,?,?)", [(fid, n, c) for n, c in facts.refs.items()])
        con.executemany("INSERT INTO imports VALUES(?,?,NULL)", [(fid, s) for s in dict.fromkeys(facts.imports)])

    def _resolve_imports(self):
        con = self.con
        files = {r["path"]: r["id"] for r in con.execute("SELECT id, path FROM files")}
        by_noext = {}
        for p, fid in files.items():
            base = os.path.splitext(p)[0]
            by_noext.setdefault(base, []).append(fid)
            if posixpath.basename(base) in ("index", "__init__", "mod"):
                by_noext.setdefault(posixpath.dirname(base), []).append(fid)
        by_stem = {}
        for base in by_noext:
            by_stem.setdefault(posixpath.basename(base), []).append(base)
        dirs = {}
        for p, fid in files.items():
            dirs.setdefault(posixpath.dirname(p), []).append(fid)
        lang_of = {r["id"]: r["lang"] for r in con.execute("SELECT id, lang FROM files")}
        path_of = {v: k for k, v in files.items()}

        def suffix_match(modpath):
            modpath = modpath.strip("/")
            if not modpath:
                return []
            stem = posixpath.basename(modpath)
            hits = [b for b in by_stem.get(stem, []) if b == modpath or b.endswith("/" + modpath)]
            if len(hits) > 3:
                return []
            return [f for b in hits for f in by_noext[b]][:3]

        rows = con.execute("SELECT DISTINCT file_id, spec FROM imports").fetchall()
        resolved = []
        for r in rows:
            fid, spec = r["file_id"], r["spec"]
            lang = lang_of.get(fid)
            src_dir = posixpath.dirname(path_of[fid])
            targets = []
            if lang in ("javascript", "typescript", "tsx") or spec.startswith("./") or spec.startswith("../"):
                if spec.startswith("."):
                    norm = posixpath.normpath(posixpath.join(src_dir, spec))
                    targets = by_noext.get(norm) or by_noext.get(os.path.splitext(norm)[0], [])
                else:
                    s = spec[2:] if spec.startswith(("@/", "~/")) else spec
                    targets = suffix_match(s) if "/" in s else []
            elif lang == "python":
                if spec.startswith("."):
                    level = len(spec) - len(spec.lstrip("."))
                    base = src_dir
                    for _ in range(level - 1):
                        base = posixpath.dirname(base)
                    rest = spec.lstrip(".").replace(".", "/")
                    targets = by_noext.get(posixpath.join(base, rest) if rest else base, [])
                else:
                    targets = suffix_match(spec.replace(".", "/"))
            elif lang == "go":
                parts = spec.split("/")
                for k in range(min(3, len(parts)), 0, -1):
                    tail = "/".join(parts[-k:])
                    hits = [d for d in dirs if d == tail or d.endswith("/" + tail)]
                    if len(hits) == 1:
                        targets = dirs[hits[0]][:20]
                        break
            elif lang == "rust":
                if spec.startswith("mod:"):
                    name = spec[4:]
                    targets = by_noext.get(posixpath.join(src_dir, name), [])
                else:
                    segs = [s for s in spec.replace("{", "::").split("::") if s and s not in ("crate", "self", "super")]
                    for k in range(len(segs), 0, -1):
                        targets = suffix_match("/".join(segs[:k]))
                        if targets:
                            break
            else:  # java, kotlin, scala, csharp, c/cpp, ruby
                if lang in ("c", "cpp", "ruby"):
                    norm = posixpath.normpath(posixpath.join(src_dir, spec)) if spec.startswith(".") else spec
                    targets = by_noext.get(os.path.splitext(norm)[0], []) or suffix_match(os.path.splitext(spec)[0])
                else:
                    targets = suffix_match(spec.replace(".", "/"))
            targets = [t for t in dict.fromkeys(targets) if t != fid]
            if targets:
                resolved.extend((fid, spec, t) for t in targets)
            else:
                resolved.append((fid, spec, None))
        con.execute("DELETE FROM imports")
        con.executemany("INSERT INTO imports VALUES(?,?,?)", resolved)
