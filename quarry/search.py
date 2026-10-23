"""Keyword search (SQLite FTS5 with BM25, identifier-aware), meaning search (local embeddings), and both fused."""
from __future__ import annotations

import array
import math
import re
from dataclasses import dataclass

from .files import identifier_words
from .index import Index

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from", "how", "i", "if", "in", "is",
    "it", "of", "on", "or", "the", "this", "to", "we", "what", "when", "where", "which", "who", "why", "with",
    "code", "function", "file", "our", "my", "me", "there", "that", "get",
}
FTS_WEIGHTS = (3.0, 1.0, 1.5, 4.0)  # path, body, idents, symbols


@dataclass
class Hit:
    path: str
    start: int
    end: int
    symbols: str
    text: str
    score: float
    found_by: str

    def location(self) -> str:
        return f"{self.path}:{self.start}-{self.end}"


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: every useful word, quoted, OR-ed (BM25 ranks the overlap)."""
    words: dict[str, None] = {}
    for token in re.findall(r"[A-Za-z0-9_]+", text):
        lowered = token.lower()
        if len(lowered) > 1 and lowered not in STOPWORDS:
            words[lowered] = None
    for part in identifier_words(text).split():
        if len(part) > 1 and part not in STOPWORDS:
            words[part] = None
    if not words:
        return None
    return " OR ".join(f'"{word}"' for word in words)


def keyword(index: Index, query: str, limit: int = 10) -> list[Hit]:
    expression = fts_query(query)
    if expression is None:
        return []
    weights = ", ".join(str(w) for w in FTS_WEIGHTS)
    rows = index.db.execute(
        f"SELECT f.path, c.start, c.end, c.symbols, c.text, bm25(fts, {weights}) AS rank "
        "FROM fts JOIN chunks c ON c.id = fts.rowid JOIN files f ON f.id = c.file_id "
        "WHERE fts MATCH ? ORDER BY rank LIMIT ?", (expression, limit)).fetchall()
    return [Hit(p, s, e, sym, text, -rank, "keyword") for p, s, e, sym, text, rank in rows]


def _cosine(a: array.array, b: array.array) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def meaning(index: Index, embedder, query: str, limit: int = 10) -> list[Hit]:
    rows = index.db.execute(
        "SELECT f.path, c.start, c.end, c.symbols, c.text, v.data FROM vectors v "
        "JOIN chunks c ON c.id = v.chunk_id JOIN files f ON f.id = c.file_id WHERE v.model=?",
        (embedder.model,)).fetchall()
    if not rows:
        return []
    target = array.array("f", embedder.embed_query(query))
    try:
        import numpy as np  # optional: much faster on large repos
        matrix = np.frombuffer(b"".join(r[5] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        q = np.frombuffer(target.tobytes(), dtype=np.float32)
        scores = (matrix @ q) / (np.linalg.norm(matrix, axis=1) * (np.linalg.norm(q) or 1) + 1e-9)
        order = list(np.argsort(-scores)[:limit])
        scored = [(float(scores[i]), rows[i]) for i in order]
    except ImportError:
        scored = sorted(((_cosine(array.array("f", r[5]), target), r) for r in rows), key=lambda x: -x[0])[:limit]
    return [Hit(r[0], r[1], r[2], r[3], r[4], score, "meaning") for score, r in scored]


def fused(index: Index, query: str, limit: int = 10, embedder=None, k: int = 60) -> list[Hit]:
    """Reciprocal rank fusion of keyword and meaning results (keyword only when no embedder/vectors)."""
    lists = [keyword(index, query, limit * 2)]
    if embedder is not None:
        lists.append(meaning(index, embedder, query, limit * 2))
    merged: dict[tuple[str, int], Hit] = {}
    scores: dict[tuple[str, int], float] = {}
    for hits in lists:
        for rank, hit in enumerate(hits):
            key = (hit.path, hit.start)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key in merged and merged[key].found_by != hit.found_by:
                merged[key].found_by = "keyword+meaning"
            merged.setdefault(key, hit)
    ranked = sorted(merged, key=lambda key: -scores[key])[:limit]
    result = []
    for key in ranked:
        hit = merged[key]
        hit.score = round(scores[key], 5)
        result.append(hit)
    return result


def excerpt(hit: Hit, query: str, max_lines: int = 18) -> str:
    """The most relevant lines of a chunk, numbered."""
    lines = hit.text.splitlines()
    terms = [w.strip('"') for w in (fts_query(query) or "").split(" OR ")]
    best = 0
    if terms and len(lines) > max_lines:
        scores = [sum(term in line.lower() for term in terms) for line in lines]
        window = [sum(scores[i:i + max_lines]) for i in range(len(lines) - max_lines + 1)]
        best = max(range(len(window)), key=window.__getitem__)
    shown = lines[best:best + max_lines]
    return "\n".join(f"{hit.start + best + i:>5}  {line}" for i, line in enumerate(shown))
