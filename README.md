# Quarry

**Private code search for your machine, and for your AI agents.** Index your repositories locally, search them by keyword, identifier or meaning, and give Claude Code (or any MCP client) a `search_code` tool that never sends your code anywhere.

- **Offline by default.** Keyword search uses SQLite full-text search with BM25 ranking, built into Python, with nothing to install.
- **Understands code.** It splits identifiers, so `compute invoice total` finds `computeInvoiceTotal` and `max retry` finds `max_retry_count`. Chunks follow functions and classes, and file paths and symbol names rank higher.
- **Optional meaning search** with a local embedding model through Ollama (`nomic-embed-text` by default), fused with keyword results. Your code stays on your PC.
- **MCP server** for Claude Code, Claude Desktop and other agents, with `search_code`, `read_code`, `list_repos` and `reindex`. It is read-only, confined to the folders you indexed, and refuses path tricks like `../`.
- **Incremental.** Re-indexing only re-reads changed files. It respects `.gitignore`, skips dependencies, build output and binaries, and never follows symlinks or junctions.

## Download

Get **Quarry-0.1.1-windows.zip** from [Releases](../../releases/latest) and unzip it anywhere. Quarry is a command-line tool: open a terminal in the Quarry folder and type `Quarry.exe` wherever this page says `py -m quarry`, for example `Quarry.exe index D:\code\my-app`. No Python needed.

Windows may say "Windows protected your PC" because the app is new: click **More info**, then **Run anyway**.

Prefer Python? With Python 3.9 or newer, download this repository and run `py -m quarry` in its folder.

## Quick start

```
py -m quarry index D:\code\my-app D:\code\other-repo
py -m quarry search "where do we retry failed uploads"
py -m quarry repos
```

With meaning search (needs [Ollama](https://ollama.com) and `ollama pull nomic-embed-text`):

```
py -m quarry index D:\code\my-app --meaning
py -m quarry search "how is the user's session refreshed" --meaning
```

## Use it from Claude Code

```
py -m quarry mcp-config D:\code\my-app
```

This prints the exact `claude mcp add ...` line to run, and the JSON block for Claude Desktop or other MCP clients. After that, your agent can call `search_code` to find code in your private repositories without uploading anything.

## Where things are stored

Indexes live in `%LOCALAPPDATA%\Quarry` (`~/.quarry` elsewhere), one SQLite file per repository. Set `QUARRY_HOME` to move them. `quarry forget <folder>` deletes an index; it never touches your code.

## Tests

```
py -m unittest discover -s tests
```

## License

The core is MIT licensed. A future Pro edition (a desktop app with folder watching and a local "ask your codebase" chat) is planned as a paid add-on.

## More tools

Made by [jtclone22-coder](https://github.com/jtclone22-coder): more free Windows tools are on that profile, and paid ones (DevSweep, Overnight, AssetAtlas) are on [Gumroad](https://clonemaster152.gumroad.com).
