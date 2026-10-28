"""SQLite storage for the graph. One DB per repo at <repo>/.tokendiet/graph.db."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, path TEXT UNIQUE, lang TEXT, hash TEXT, mtime REAL, size INTEGER,
  lines INTEGER, tokens INTEGER);
CREATE TABLE IF NOT EXISTS symbols(
  id INTEGER PRIMARY KEY, file_id INTEGER, name TEXT, kind TEXT, parent_id INTEGER,
  depth INTEGER, start_line INTEGER, end_line INTEGER, signature TEXT);
CREATE TABLE IF NOT EXISTS calls(file_id INTEGER, caller_id INTEGER, callee TEXT, line INTEGER);
CREATE TABLE IF NOT EXISTS refs(file_id INTEGER, name TEXT, n INTEGER);
CREATE TABLE IF NOT EXISTS imports(file_id INTEGER, spec TEXT, target_id INTEGER);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, path TEXT, symbol TEXT, text TEXT, file_hash TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS usage(
  ts TEXT, session TEXT, client TEXT, tool TEXT, served INTEGER, lean INTEGER, full INTEGER);
CREATE INDEX IF NOT EXISTS sym_name ON symbols(name);
CREATE INDEX IF NOT EXISTS sym_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS calls_callee ON calls(callee);
CREATE INDEX IF NOT EXISTS calls_file ON calls(file_id);
CREATE INDEX IF NOT EXISTS calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS refs_name ON refs(name);
CREATE INDEX IF NOT EXISTS refs_file ON refs(file_id);
CREATE INDEX IF NOT EXISTS imports_file ON imports(file_id);
CREATE INDEX IF NOT EXISTS imports_target ON imports(target_id);
"""


def open_db(repo: Path) -> sqlite3.Connection:
    d = repo / ".tokendiet"
    d.mkdir(exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    con = sqlite3.connect(str(d / "graph.db"), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    return con


def delete_file_rows(con, file_id: int):
    for t in ("symbols", "calls", "refs", "imports"):
        con.execute(f"DELETE FROM {t} WHERE file_id=?", (file_id,))
