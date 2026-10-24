# Changelog

## 0.1.1 (2026-10-24)

Fixes from a code review.

- `quarry search --repo <folder>` no longer adds the folder to the index list (which made it readable by the MCP tools); it only searches folders you indexed.
- `read_code` only reads files the index holds, so git-ignored files such as `.env` beside them can't be read, and absolute, drive and network (UNC) paths are refused before anything touches the disk.

## 0.1.0 (2026-10-22)

- Incremental indexing into one SQLite file per repository. It respects .gitignore, skips dependencies, build output and binaries, and never follows links.
- Chunking that follows definitions (Python, JS/TS, Rust, Go, C#, Java and more), with symbol names extracted.
- Keyword search with SQLite FTS5 and BM25. Identifier splitting (camelCase/snake_case), path and symbol boosting, and injection-safe queries.
- Optional meaning search with local Ollama embeddings, fused with keyword results by reciprocal rank.
- MCP stdio server: search_code, read_code (confined to indexed repositories), list_repos, reindex.
- CLI: index, search, repos, forget, serve, mcp-config.
- A local embedding server is reached directly, never through a proxy.
- Run with no command, it shows how to use it (and keeps a double-clicked window open).
