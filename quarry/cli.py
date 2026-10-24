"""Command line: index, search, repos, forget, serve (MCP), mcp-config."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .embed import OllamaEmbedder
from .index import Index, home_dir, load_registry, save_registry
from .search import excerpt, fused


def make_embedder(args):
    if not getattr(args, "meaning", False):
        return None
    return OllamaEmbedder(args.ollama, args.embed_model)


def cmd_index(args) -> int:
    for folder in args.folders:
        index = Index.open_for(folder)
        print(f"Indexing {index.root} ...", file=sys.stderr)
        stats = index.update(progress=lambda n, path: print(f"  {n} files ({path})", file=sys.stderr))
        print(f"{index.root}: {stats.added} new, {stats.changed} changed, {stats.removed} removed, "
              f"{stats.unchanged} unchanged; {stats.chunks} chunks written in {stats.seconds}s")
        embedder = make_embedder(args)
        if embedder:
            done = index.embed_missing(embedder, progress=lambda d, t: print(f"  meaning vectors {d}/{t}",
                                                                              file=sys.stderr))
            print(f"  added {done} meaning vectors with {embedder.model}")
        index.close()
    return 0


def cmd_search(args) -> int:
    registry = load_registry()
    roots = list(registry)
    if args.repo:  # only a folder that was indexed on purpose: searching must never add one to what the AI tools can read
        wanted = os.path.normcase(str(Path(args.repo).resolve()))
        roots = [root for root in registry if os.path.normcase(root) == wanted]
        if not roots:
            print(f"{args.repo} isn't indexed. Index it first: quarry index \"{args.repo}\"", file=sys.stderr)
            return 1
    if not roots:
        print("Nothing indexed yet. Run: quarry index <folder>", file=sys.stderr)
        return 1
    embedder = make_embedder(args)
    hits = []
    for root in roots:
        index = Index.open_for(root)
        hits += [(hit, index.root) for hit in fused(index, args.query, args.limit, embedder)]
    hits.sort(key=lambda pair: -pair[0].score)
    for hit, root in hits[:args.limit]:
        label = f"  [{hit.symbols}]" if hit.symbols else ""
        print(f"\n{root.name}/{hit.location()}{label}  ({hit.found_by})")
        print(excerpt(hit, args.query, max_lines=args.lines))
    if not hits:
        print(f"No matches for {args.query!r}.")
    return 0


def cmd_repos(args) -> int:
    registry = load_registry()
    if not registry:
        print("Nothing indexed yet.")
    for root in registry:
        if Path(root).is_dir():
            s = Index.open_for(root).stats()
            print(f"{root}\n  {s['files']} files, {s['chunks']} chunks, {s['vectors']} meaning vectors")
        else:
            print(f"{root}\n  (folder missing; run: quarry forget \"{root}\")")
    return 0


def cmd_forget(args) -> int:
    registry = load_registry()
    root = str(Path(args.folder).resolve())
    db = registry.pop(root, None)
    if db is None:
        print(f"{root} is not indexed.")
        return 1
    save_registry(registry)
    try:
        Path(db).unlink()
    except OSError:
        pass
    print(f"Forgot {root} (its index file was deleted; your code was not touched).")
    return 0


def cmd_serve(args) -> int:
    from .mcp import Server
    Server(args.folders or None, embedder=make_embedder(args)).serve()
    return 0


def cmd_mcp_config(args) -> int:
    program = sys.executable.replace("\\", "/")
    folders = [str(Path(f).resolve()) for f in args.folders]
    if getattr(sys, "frozen", False):  # Quarry.exe takes the command directly and needs no PYTHONPATH
        command, env = [program, "serve", *folders], {}
    else:
        package_root = str(Path(__file__).resolve().parents[1])  # lets `-m quarry` work without pip install
        command, env = [program, "-m", "quarry", "serve", *folders], {"PYTHONPATH": package_root}
    quote = lambda c: f'"{c}"' if " " in c else c  # noqa: E731
    flags = "".join(f" -e {key}={quote(value)}" for key, value in env.items())
    print("Claude Code (run in a terminal):")
    print(f"  claude mcp add quarry{flags} -- " + " ".join(quote(c) for c in command))
    print("\nClaude Desktop / other MCP clients (add to the mcpServers section of their config):")
    entry = {"command": program, "args": command[1:], **({"env": env} if env else {})}
    print(json.dumps({"quarry": entry}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quarry", description="Private, offline code search for your repositories.")
    parser.add_argument("--version", action="version", version=f"Quarry {__version__}")
    sub = parser.add_subparsers(dest="command")

    def meaning_options(p) -> None:
        p.add_argument("--meaning", action="store_true", help="also use local embeddings (Ollama)")
        p.add_argument("--ollama", default="http://127.0.0.1:11434")
        p.add_argument("--embed-model", default="nomic-embed-text:v1.5")

    p = sub.add_parser("index", help="index (or update) one or more folders")
    p.add_argument("folders", nargs="+")
    meaning_options(p)
    p.set_defaults(func=cmd_index)
    p = sub.add_parser("search", help="search indexed code")
    p.add_argument("query")
    p.add_argument("--repo")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--lines", type=int, default=14)
    meaning_options(p)
    p.set_defaults(func=cmd_search)
    sub.add_parser("repos", help="list indexed folders").set_defaults(func=cmd_repos)
    p = sub.add_parser("forget", help="delete a folder's index (not the code)")
    p.add_argument("folder")
    p.set_defaults(func=cmd_forget)
    p = sub.add_parser("serve", help="run the MCP server on stdio (for Claude Code and other agents)")
    p.add_argument("folders", nargs="*", help="limit to these indexed folders (default: all)")
    meaning_options(p)
    p.set_defaults(func=cmd_serve)
    p = sub.add_parser("mcp-config", help="print the config to add Quarry to Claude Code / Claude Desktop")
    p.add_argument("folders", nargs="*")
    p.set_defaults(func=cmd_mcp_config)
    return parser


def opened_by_double_click() -> bool:
    """True when this console window belongs to Quarry alone (started from Explorer, not from a terminal)."""
    if os.name != "nt":
        return False
    import ctypes
    processes = (ctypes.c_uint * 4)()
    return ctypes.windll.kernel32.GetConsoleProcessList(processes, 4) <= 1


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:  # no command: show how to use it, and keep a double-clicked window open long enough to read
        parser.print_help()
        print('\nStart with:  Quarry index D:\\code\\my-app   then   Quarry search "where do we retry uploads"')
        if argv is None and opened_by_double_click():
            input("\nQuarry is a command-line tool: open a terminal in this folder to use it. Press Enter to close.")
        return 0
    try:
        return args.func(args)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
