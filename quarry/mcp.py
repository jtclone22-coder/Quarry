"""MCP server over stdio: lets Claude Code, Claude Desktop and other MCP clients search your code privately.

Newline-delimited JSON-RPC 2.0 on stdin/stdout (the MCP stdio transport). Logs go to stderr only.
Tools are read-only and confined to the repositories you indexed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import __version__
from .index import Index, load_registry
from .search import excerpt, fused

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_READ_LINES = 400

TOOLS = [
    {
        "name": "search_code",
        "description": ("Search the user's local code repositories (private, offline index). Finds functions, "
                        "classes, config and docs by keywords, identifiers (camelCase/snake_case aware) and, when "
                        "enabled, meaning. Returns file paths with line ranges and the matching code."),
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to look for, e.g. 'where is the chat deadline enforced'"},
            "repo": {"type": "string", "description": "Optional repository root or its folder name"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8}},
            "required": ["query"]},
    },
    {
        "name": "read_code",
        "description": "Read lines from a file inside an indexed repository (max 400 lines per call).",
        "inputSchema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "Path relative to the repository root (as shown by search_code)"},
            "repo": {"type": "string", "description": "Repository root or folder name; optional if only one is indexed"},
            "start_line": {"type": "integer", "minimum": 1, "default": 1},
            "end_line": {"type": "integer", "minimum": 1}},
            "required": ["path"]},
    },
    {
        "name": "list_repos",
        "description": "List the indexed repositories with file/chunk counts and when they were last updated.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "reindex",
        "description": "Bring a repository's index up to date (only changed files are re-read).",
        "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}}},
    },
]


class ToolError(Exception):
    pass


class Server:
    def __init__(self, roots: list[str] | None = None, embedder=None, home=None):
        registered = list(load_registry(home)) if not roots else [str(Path(r).resolve()) for r in roots]
        self.indexes = {root: Index.open_for(root, home) for root in registered if Path(root).is_dir()}
        self.embedder = embedder

    def close(self) -> None:
        for index in self.indexes.values():
            index.close()

    # -- tool helpers -------------------------------------------------------------------
    def pick(self, repo: str | None) -> list[Index]:
        if not self.indexes:
            raise ToolError("No repositories are indexed yet. Run: quarry index <folder>")
        if not repo:
            return list(self.indexes.values())
        wanted = os.path.normcase(repo.rstrip("\\/"))
        matches = [index for root, index in self.indexes.items()
                   if os.path.normcase(root) == wanted or os.path.normcase(Path(root).name) == wanted]
        if not matches:
            raise ToolError(f"Unknown repository {repo!r}. Indexed: {', '.join(self.indexes)}")
        return matches

    def search_code(self, query: str, repo: str | None = None, limit: int = 8) -> str:
        if not query or not query.strip():
            raise ToolError("query is empty")
        limit = max(1, min(int(limit or 8), 30))
        blocks = []
        for index in self.pick(repo):
            for hit in fused(index, query, limit, self.embedder if self._has_vectors(index) else None):
                label = f" ({hit.symbols})" if hit.symbols else ""
                blocks.append((hit.score, f"## {Path(index.root).name}/{hit.location()}{label}\n"
                                          f"repo: {index.root}\n```\n{excerpt(hit, query)}\n```"))
        if not blocks:
            return f"No matches for {query!r}."
        blocks.sort(key=lambda item: -item[0])
        return "\n\n".join(text for _score, text in blocks[:limit])

    def _has_vectors(self, index: Index) -> bool:
        return self.embedder is not None and index.stats()["vectors"] > 0

    def read_code(self, path: str, repo: str | None = None, start_line: int = 1, end_line: int | None = None) -> str:
        candidates = self.pick(repo)
        for index in candidates:
            target = (index.root / path).resolve()
            try:
                target.relative_to(index.root)
            except ValueError:
                continue  # outside this repository: never read
            if target.is_file():
                lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
                start = max(1, int(start_line or 1))
                end = min(len(lines), int(end_line or start + MAX_READ_LINES - 1), start + MAX_READ_LINES - 1)
                body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(start, end + 1))
                more = f"\n... ({len(lines) - end} more lines)" if end < len(lines) else ""
                return f"{Path(index.root).name}/{path}  lines {start}-{end} of {len(lines)}\n{body}{more}"
        raise ToolError(f"{path!r} is not a file inside an indexed repository")

    def list_repos(self) -> str:
        if not self.indexes:
            return "No repositories are indexed yet. Run: quarry index <folder>"
        rows = []
        for index in self.indexes.values():
            s = index.stats()
            rows.append(f"- {s['root']}: {s['files']} files, {s['chunks']} chunks, "
                        f"{s['vectors']} meaning vectors")
        return "\n".join(rows)

    def reindex(self, repo: str | None = None) -> str:
        lines = []
        for index in self.pick(repo):
            st = index.update()
            lines.append(f"{index.root}: +{st.added} new, {st.changed} changed, -{st.removed} removed "
                         f"({st.unchanged} unchanged) in {st.seconds}s")
        return "\n".join(lines)

    # -- JSON-RPC -------------------------------------------------------------------------
    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:  # notification (e.g. notifications/initialized): no reply
            return None
        try:
            if method == "initialize":
                asked = (message.get("params") or {}).get("protocolVersion")
                result = {"protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "quarry", "version": __version__},
                          "instructions": "Use search_code to find code in the user's private local repositories, "
                                          "then read_code for more context."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                name, arguments = params.get("name"), params.get("arguments") or {}
                handlers = {"search_code": self.search_code, "read_code": self.read_code,
                            "list_repos": self.list_repos, "reindex": self.reindex}
                if name not in handlers:
                    return {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32602, "message": f"unknown tool {name!r}"}}
                try:
                    text, is_error = handlers[name](**arguments), False
                except (ToolError, TypeError, ValueError, OSError) as exc:
                    text, is_error = str(exc), True
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"no method {method}"}}
        except Exception as exc:  # never kill the server on one bad request
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(exc)}}
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def serve(self, stdin=None, stdout=None) -> None:
        if stdin is None:
            # MCP stdio is UTF-8 with bare \n; Windows defaults to the console code page and \r\n.
            sys.stdin.reconfigure(encoding="utf-8")
            sys.stdout.reconfigure(encoding="utf-8", newline="\n")
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            else:
                reply = self.handle(message) if isinstance(message, dict) else None
            if reply is not None:
                stdout.write(json.dumps(reply) + "\n")
                stdout.flush()
