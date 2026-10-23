"""Meaning vectors from a local embedding model (Ollama by default). Optional: keyword search needs none."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # local servers: never through a proxy


class OllamaEmbedder:
    """nomic-embed-text by default; it expects 'search_document:' / 'search_query:' prefixes."""

    def __init__(self, url: str = "http://127.0.0.1:11434", model: str = "nomic-embed-text:v1.5",
                 doc_prefix: str = "search_document: ", query_prefix: str = "search_query: ", timeout: float = 120):
        self.url, self.model = url.rstrip("/"), model
        self.doc_prefix, self.query_prefix, self.timeout = doc_prefix, query_prefix, timeout

    def _embed(self, texts: list[str]) -> list[list[float]]:
        request = urllib.request.Request(
            f"{self.url}/api/embed", method="POST", headers={"Content-Type": "application/json"},
            data=json.dumps({"model": self.model, "input": texts, "truncate": True}).encode("utf-8"))
        host = urllib.parse.urlsplit(self.url).hostname or ""
        opener = DIRECT if host in ("127.0.0.1", "localhost", "::1") else urllib.request.build_opener()
        with opener.open(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RuntimeError(f"embedding server returned {type(vectors).__name__} for {len(texts)} inputs")
        return vectors

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed([self.doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._embed([self.query_prefix + text])[0]
