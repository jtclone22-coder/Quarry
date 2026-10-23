"""The index: one SQLite file per repository, updated incrementally (only changed files are re-read).

Lives in %LOCALAPPDATA%\\Quarry (or ~/.quarry): your code never leaves the machine.
"""
from __future__ import annotations

import array
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .files import chunk_text, identifier_words, read_text, walk_files

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, mtime REAL, size INTEGER);
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY, file_id INTEGER, start INTEGER, end INTEGER,
                                  symbols TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(path, body, idents, symbols, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS vectors(chunk_id INTEGER PRIMARY KEY, model TEXT, data BLOB);
"""


def home_dir() -> Path:
    base = os.environ.get("QUARRY_HOME") or (
        os.path.join(os.environ["LOCALAPPDATA"], "Quarry") if os.environ.get("LOCALAPPDATA")
        else os.path.join(os.path.expanduser("~"), ".quarry"))
    path = Path(base)
    path.mkdir(parents=True, exist_ok=True)
    return path


def registry_path(home: Path | None = None) -> Path:
    return (home or home_dir()) / "repos.json"


def load_registry(home: Path | None = None) -> dict[str, str]:
    try:
        return json.loads(registry_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_registry(registry: dict[str, str], home: Path | None = None) -> None:
    registry_path(home).write_text(json.dumps(registry, indent=2), encoding="utf-8")


@dataclass
class UpdateStats:
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    chunks: int = 0
    seconds: float = 0.0


class Index:
    def __init__(self, db_path: Path, root: Path):
        self.root = root.resolve()
        self.db_path = db_path
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('root', ?)", (str(self.root),))
        self.db.commit()

    @classmethod
    def open_for(cls, root, home: Path | None = None) -> "Index":
        root = Path(root).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"not a folder: {root}")
        home = home or home_dir()
        key = hashlib.sha1(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:16]
        (home / "indexes").mkdir(parents=True, exist_ok=True)
        registry = load_registry(home)
        if registry.get(str(root)) is None:
            registry[str(root)] = str(home / "indexes" / f"{key}.sqlite")
            save_registry(registry, home)
        return cls(Path(registry[str(root)]), root)

    def close(self) -> None:
        self.db.close()

    def _drop_file(self, file_id: int) -> None:
        ids = [row[0] for row in self.db.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,))]
        if ids:
            marks = ",".join("?" * len(ids))
            self.db.execute(f"DELETE FROM fts WHERE rowid IN ({marks})", ids)
            self.db.execute(f"DELETE FROM vectors WHERE chunk_id IN ({marks})", ids)
        self.db.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))

    def update(self, progress=None, should_stop=None) -> UpdateStats:
        started = time.monotonic()
        stats = UpdateStats()
        known = {row[1]: (row[0], row[2], row[3]) for row in self.db.execute("SELECT id, path, mtime, size FROM files")}
        seen: set[str] = set()
        for count, (path, info) in enumerate(walk_files(self.root), 1):
            if should_stop and should_stop():
                break
            relative = path.relative_to(self.root).as_posix()
            seen.add(relative)
            previous = known.get(relative)
            if previous and previous[1] == info.st_mtime and previous[2] == info.st_size:
                stats.unchanged += 1
                continue
            text = read_text(path)
            if previous:
                self._drop_file(previous[0])
                self.db.execute("UPDATE files SET mtime=?, size=? WHERE id=?", (info.st_mtime, info.st_size, previous[0]))
                file_id = previous[0]
                stats.changed += 1
            else:
                file_id = self.db.execute("INSERT INTO files(path, mtime, size) VALUES(?,?,?)",
                                          (relative, info.st_mtime, info.st_size)).lastrowid
                stats.added += 1
            for chunk in chunk_text(text or ""):
                chunk_id = self.db.execute(
                    "INSERT INTO chunks(file_id, start, end, symbols, text) VALUES(?,?,?,?,?)",
                    (file_id, chunk.start, chunk.end, " ".join(chunk.symbols), chunk.text)).lastrowid
                self.db.execute("INSERT INTO fts(rowid, path, body, idents, symbols) VALUES(?,?,?,?,?)",
                                (chunk_id, relative, chunk.text, identifier_words(chunk.text), " ".join(chunk.symbols)))
                stats.chunks += 1
            if count % 200 == 0:
                self.db.commit()
                if progress:
                    progress(count, relative)
        if not (should_stop and should_stop()):
            for relative, (file_id, _mtime, _size) in known.items():
                if relative not in seen:
                    self._drop_file(file_id)
                    self.db.execute("DELETE FROM files WHERE id=?", (file_id,))
                    stats.removed += 1
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('updated', ?)", (str(time.time()),))
        self.db.commit()
        stats.seconds = round(time.monotonic() - started, 2)
        return stats

    def embed_missing(self, embedder, batch: int = 32, progress=None, should_stop=None) -> int:
        """Add meaning vectors for chunks that don't have one for this embedder's model yet."""
        rows = self.db.execute(
            "SELECT c.id, f.path, c.text FROM chunks c JOIN files f ON f.id=c.file_id "
            "WHERE c.id NOT IN (SELECT chunk_id FROM vectors WHERE model=?)", (embedder.model,)).fetchall()
        done = 0
        for offset in range(0, len(rows), batch):
            if should_stop and should_stop():
                break
            part = rows[offset:offset + batch]
            vectors = embedder.embed_documents([f"{path}\n{text[:2000]}" for _id, path, text in part])
            for (chunk_id, _path, _text), vector in zip(part, vectors):
                self.db.execute("INSERT OR REPLACE INTO vectors VALUES(?,?,?)",
                                (chunk_id, embedder.model, array.array("f", vector).tobytes()))
            done += len(part)
            self.db.commit()
            if progress:
                progress(done, len(rows))
        return done

    def stats(self) -> dict:
        files = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        vectors = self.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        updated = self.db.execute("SELECT value FROM meta WHERE key='updated'").fetchone()
        return {"root": str(self.root), "files": files, "chunks": chunks, "vectors": vectors,
                "updated": float(updated[0]) if updated else None}
