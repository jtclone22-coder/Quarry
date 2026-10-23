"""Quarry tests: temp repositories only, a fake embedder, and the MCP protocol over in-memory streams."""
from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import tempfile
import time
import unittest
from unittest import mock
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quarry import files, search  # noqa: E402
from quarry.index import Index, load_registry  # noqa: E402
from quarry.mcp import Server  # noqa: E402

try:
    import _winapi
    HAS_JUNCTIONS = hasattr(_winapi, "CreateJunction")
except ImportError:
    HAS_JUNCTIONS = False


class WordEmbedder:
    """Deterministic stand-in for a real model: a bag of hashed words, with a few synonyms."""
    model = "fake-words"
    SYNONYMS = {"halt": "cancel", "stop": "cancel", "abort": "cancel", "money": "invoice", "bill": "invoice"}

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * 256
        for word in re.findall(r"[a-z]+", text.lower()):
            word = self.SYNONYMS.get(word, word)
            vector[zlib.crc32(word.encode()) % 256] += 1.0  # stable across runs, unlike hash()
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class Repo(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)  # runs last: index connections are closed first (LIFO)
        self.home = Path(self.temp.name) / "home"
        self.repo = Path(self.temp.name) / "shop"
        os.environ["QUARRY_HOME"] = str(self.home)
        write(self.repo / ".gitignore", "*.txt\n/secrets/\nbuild/\n!keep.txt\n")
        write(self.repo / "src" / "billing.py", (
            "import math\n\n"
            "def computeInvoiceTotal(items, tax_rate):\n"
            "    subtotal = sum(i.price for i in items)\n"
            "    return round(subtotal * (1 + tax_rate), 2)\n\n\n"
            "class RefundPolicy:\n"
            "    def can_refund(self, order):\n"
            "        return order.days_since_purchase < 30\n"))
        write(self.repo / "src" / "jobs.ts", (
            "export async function cancelPendingJobs(queue) {\n"
            "  for (const job of queue.pending()) job.cancel();\n"
            "}\n"))
        write(self.repo / "README.md", "# Shop\nHandles invoices and refunds.\n")
        write(self.repo / "debug.txt", "computeInvoiceTotal crashed\n")
        write(self.repo / "keep.txt", "kept on purpose\n")
        write(self.repo / "secrets" / "key.txt", "computeInvoiceTotal secret\n")
        write(self.repo / "build" / "out.js", "computeInvoiceTotal()\n")
        write(self.repo / "node_modules" / "lib" / "index.js", "computeInvoiceTotal()\n")
        (self.repo / "logo.png").write_bytes(b"\x89PNG\x00\x00binary")

    def tearDown(self) -> None:
        os.environ.pop("QUARRY_HOME", None)

    def index(self) -> Index:
        index = Index.open_for(self.repo, self.home)
        index.update()
        self.addCleanup(index.close)
        return index


class FileTests(Repo):
    def test_walk_respects_gitignore_and_skips(self) -> None:
        found = sorted(p.relative_to(self.repo).as_posix() for p, _ in files.walk_files(self.repo))
        self.assertEqual(found, [".gitignore", "README.md", "keep.txt", "src/billing.py", "src/jobs.ts"])

    @unittest.skipUnless(HAS_JUNCTIONS, "junctions need Windows")
    def test_junctions_are_not_followed(self) -> None:
        outside = Path(self.temp.name) / "elsewhere"
        write(outside / "private.py", "def leaked(): pass\n")
        _winapi.CreateJunction(str(outside), str(self.repo / "linked"))
        found = [p.name for p, _ in files.walk_files(self.repo)]
        self.assertNotIn("private.py", found)

    def test_chunks_follow_definitions(self) -> None:
        source = "\n".join(["import os", ""] + [f"def f{i}():\n" + "\n".join(f"    x = {j}" for j in range(40))
                                               for i in range(3)])
        chunks = files.chunk_text(source)
        self.assertEqual([c.symbols for c in chunks], [("f0",), ("f1",), ("f2",)])
        self.assertEqual(chunks[1].start, source.splitlines().index("def f1():") + 1)
        long = "\n".join(f"line {i}" for i in range(200))
        windows = files.chunk_text(long)
        self.assertEqual((windows[0].start, windows[0].end, windows[1].start), (1, 60, 53))
        self.assertEqual(windows[-1].end, 200)

    def test_identifier_words(self) -> None:
        self.assertEqual(files.identifier_words("computeInvoiceTotal(max_retry_count) + x"),
                         "computeinvoicetotal compute invoice total max_retry_count max retry count")


class IndexAndSearchTests(Repo):
    def test_incremental_updates(self) -> None:
        index = Index.open_for(self.repo, self.home)
        self.addCleanup(index.close)
        first = index.update()
        self.assertEqual((first.added, first.changed, first.removed), (5, 0, 0))  # incl. .gitignore, keep.txt
        time.sleep(0.05)
        write(self.repo / "src" / "jobs.ts", "export function retryJobs() {}\n")
        (self.repo / "README.md").unlink()
        write(self.repo / "src" / "new.py", "def fresh(): pass\n")
        second = index.update()
        self.assertEqual((second.added, second.changed, second.removed, second.unchanged), (1, 1, 1, 3))
        self.assertFalse(any("cancelPendingJobs" in hit.text for hit in search.keyword(index, "cancelPendingJobs")))
        self.assertTrue(search.keyword(index, "retry jobs"))
        self.assertIn(str(self.repo.resolve()), load_registry(self.home))

    def test_keyword_search_understands_identifiers(self) -> None:
        index = self.index()
        hits = search.keyword(index, "where do we compute the invoice total")
        self.assertEqual(hits[0].path, "src/billing.py")
        self.assertIn("computeInvoiceTotal", hits[0].symbols)
        self.assertEqual(search.keyword(index, "cancel pending")[0].path, "src/jobs.ts")
        refund = search.keyword(index, "refund policy")[0]
        self.assertEqual(refund.path, "src/billing.py")
        self.assertIn("RefundPolicy", refund.symbols.split())

    def test_queries_are_injection_safe(self) -> None:
        index = self.index()
        for nasty in ('"', "*", "NEAR(a b)", "a OR", "^start", "col:value", "(((", "-- ;DROP TABLE"):
            search.keyword(index, nasty)  # must not raise
        self.assertIsNone(search.fts_query("the and of"))

    def test_meaning_search_and_fusion(self) -> None:
        index = self.index()
        embedder = WordEmbedder()
        self.assertEqual(index.embed_missing(embedder), index.stats()["chunks"])
        self.assertEqual(index.embed_missing(embedder), 0)  # nothing left to embed
        hits = search.meaning(index, embedder, "halt the queued work", limit=3)
        self.assertEqual(hits[0].path, "src/jobs.ts")  # "halt" ~ "cancel" via the fake model's synonyms
        fused = search.fused(index, "cancel jobs", 3, embedder)
        self.assertEqual(fused[0].found_by, "keyword+meaning")
        self.assertIn("cancelPendingJobs", search.excerpt(fused[0], "cancel jobs"))


class McpTests(Repo):
    def server(self) -> Server:
        self.index()
        server = Server([str(self.repo)], home=self.home)
        self.addCleanup(server.close)
        return server

    def call(self, server: Server, name: str, **arguments) -> dict:
        reply = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})
        return reply["result"]

    def test_handshake_and_tool_list(self) -> None:
        server = self.server()
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-03-26", "capabilities": {}}})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(reply["result"]["serverInfo"]["name"], "quarry")
        newer = server.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                               "params": {"protocolVersion": "2099-01-01"}})
        self.assertEqual(newer["result"]["protocolVersion"], "2025-06-18")
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = {t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})["result"]["tools"]}
        self.assertEqual(tools, {"search_code", "read_code", "list_repos", "reindex"})
        self.assertEqual(server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"], -32601)

    def test_search_and_read(self) -> None:
        server = self.server()
        found = self.call(server, "search_code", query="invoice total")
        self.assertFalse(found["isError"])
        self.assertIn("shop/src/billing.py:", found["content"][0]["text"])
        read = self.call(server, "read_code", path="src/billing.py", start_line=3, end_line=4)
        self.assertIn("    3  def computeInvoiceTotal(items, tax_rate):", read["content"][0]["text"])
        self.assertIn("lines 3-4 of 10", read["content"][0]["text"])

    def test_reads_cannot_escape_the_repository(self) -> None:
        server = self.server()
        write(Path(self.temp.name) / "outside.txt", "private")
        for path in ("../outside.txt", "..\\outside.txt", str(Path(self.temp.name) / "outside.txt"), "nope.py"):
            result = self.call(server, "read_code", path=path)
            self.assertTrue(result["isError"], path)
            self.assertNotIn("private", result["content"][0]["text"])

    def test_bad_arguments_are_tool_errors_not_crashes(self) -> None:
        server = self.server()
        self.assertTrue(self.call(server, "search_code", query="  ")["isError"])
        self.assertTrue(self.call(server, "search_code", query="x", repo="other")["isError"])
        self.assertTrue(self.call(server, "read_code", wrong_argument=1)["isError"])

    def test_stdio_loop(self) -> None:
        server = self.server()
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 "this is not json",
                 json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "list_repos", "arguments": {}}})]
        out = io.StringIO()
        server.serve(io.StringIO("\n".join(lines) + "\n"), out)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r.get("id") for r in replies], [1, None, 2])
        self.assertEqual(replies[1]["error"]["code"], -32700)
        self.assertIn("5 files", replies[2]["result"]["content"][0]["text"])


class McpConfigTests(unittest.TestCase):
    def test_config_for_python_and_for_the_exe(self) -> None:
        from quarry import cli
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            cli.main(["mcp-config", "."])
        entry = json.loads(out.getvalue().split("config):", 1)[1])["quarry"]
        self.assertEqual(entry["args"][:3], ["-m", "quarry", "serve"])
        self.assertIn("PYTHONPATH", entry["env"])
        out = io.StringIO()
        with mock.patch.object(sys, "frozen", True, create=True),                 mock.patch.object(sys, "executable", r"C:\Tools\Quarry\Quarry.exe"), mock.patch("sys.stdout", out):
            cli.main(["mcp-config", "."])
        text = out.getvalue()
        self.assertIn("claude mcp add quarry -- C:/Tools/Quarry/Quarry.exe serve ", text)
        entry = json.loads(text.split("config):", 1)[1])["quarry"]
        self.assertEqual((entry["command"], entry["args"][0], "env" in entry), ("C:/Tools/Quarry/Quarry.exe", "serve", False))


if __name__ == "__main__":
    unittest.main()
