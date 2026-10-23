"""Which files to index and how to cut them into searchable chunks.

Respects .gitignore (the common subset: globs, directory patterns, anchored patterns, negation),
skips binaries, huge files, dependency/build folders and anything behind a symlink or junction.
Chunks follow top-level definitions where they can (functions, classes, impls ...) so a search hit
shows a whole function, not half of two.
"""
from __future__ import annotations

import fnmatch
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 1_000_000
CHUNK_LINES = 60
OVERLAP = 8
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", "dist", "build", "target", "bin", "obj", ".next", ".nuxt", ".svelte-kit", ".turbo", ".gradle",
    ".idea", ".vs", "Intermediate", "DerivedDataCache", "Binaries", "Saved", ".godot", ".terraform",
    "coverage", ".cache",
}  # names only; a .gitignore "!" rule can bring any of them back
TEXT_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte", ".go", ".rs", ".java", ".kt",
    ".kts", ".scala", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cs", ".fs", ".vb", ".swift", ".m", ".mm",
    ".rb", ".php", ".lua", ".dart", ".ex", ".exs", ".erl", ".hs", ".ml", ".clj", ".r", ".jl", ".zig", ".nim",
    ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".sql", ".graphql", ".proto", ".html", ".css", ".scss",
    ".less", ".md", ".rst", ".txt", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".conf", ".xml", ".gradle",
    ".cmake", ".mk", ".tf", ".hcl", ".glsl", ".hlsl", ".usf", ".ush", ".shader", ".gd", ".uproject", ".uplugin",
}
TEXT_NAMES = {"dockerfile", "makefile", "cmakelists.txt", "readme", "license", "justfile", ".gitignore", ".env.example"}

DEFINITION = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:static\s+)?"
    r"(?:def|class|function|fn|func|impl|struct|enum|trait|interface|type|module|object|const\s+\w+\s*=\s*(?:async\s*)?\(|"
    r"(?:public|private|protected|internal)\s+(?:static\s+)?(?:async\s+)?[\w<>\[\],\s]+\s+\w+\s*\()",
)
NAME = re.compile(r"^[ 	]*(?:export\s+)?(?:default\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
                  r"(?:def|class|function|fn|func|impl|struct|enum|trait|interface|type|const)[ 	]+([A-Za-z_]\w*)",
                  re.MULTILINE)


@dataclass
class Chunk:
    start: int        # 1-based first line
    end: int          # last line, inclusive
    text: str
    symbols: tuple[str, ...]


class IgnoreRules:
    """A practical .gitignore matcher: one instance per directory level, checked root-down."""

    def __init__(self, base: Path, lines: list[str]):
        self.base = base
        self.rules: list[tuple[str, bool, bool, bool]] = []  # pattern, negate, dir_only, anchored
        for raw in lines:
            line = raw.rstrip("\n").rstrip()
            if not line or line.startswith("#"):
                continue
            negate = line.startswith("!")
            if negate:
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.strip("/") if dir_only else line
            anchored = line.startswith("/") or "/" in line.rstrip("/")
            self.rules.append((line.lstrip("/"), negate, dir_only, anchored))

    @classmethod
    def load(cls, folder: Path) -> "IgnoreRules | None":
        path = folder / ".gitignore"
        try:
            return cls(folder, path.read_text(encoding="utf-8", errors="replace").splitlines())
        except OSError:
            return None

    def verdict(self, path: Path, is_dir: bool) -> bool | None:
        """True = ignored, False = explicitly re-included, None = no rule matched."""
        relative = path.relative_to(self.base).as_posix()
        result = None
        for pattern, negate, dir_only, anchored in self.rules:
            if dir_only and not is_dir:
                continue
            if anchored:
                matched = fnmatch.fnmatchcase(relative, pattern) or fnmatch.fnmatchcase(relative, pattern + "/**")
            else:
                matched = fnmatch.fnmatchcase(path.name, pattern)
            if matched:
                result = not negate
        return result


def is_link(entry: os.DirEntry) -> bool:
    try:
        if entry.is_symlink():
            return True
        return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return True


def looks_textual(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS or path.name.lower() in TEXT_NAMES


def walk_files(root: Path, extra_ignores: tuple[str, ...] = ()):
    """Yield (path, stat) for indexable files under root."""
    root = root.resolve()
    extra = IgnoreRules(root, list(extra_ignores)) if extra_ignores else None
    stack: list[tuple[Path, list[IgnoreRules]]] = [(root, [r for r in (IgnoreRules.load(root), extra) if r])]
    while stack:
        folder, rules = stack.pop()
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if is_link(entry):
                continue
            path = Path(entry.path)
            is_dir = entry.is_dir(follow_symlinks=False)
            ignored = None
            for rule in rules:
                verdict = rule.verdict(path, is_dir)
                if verdict is not None:
                    ignored = verdict
            if ignored:
                continue
            if is_dir:
                if entry.name in SKIP_DIRS and ignored is not False:
                    continue
                child_rules = rules + [r for r in (IgnoreRules.load(path),) if r]
                stack.append((path, child_rules))
            elif looks_textual(path):
                info = entry.stat(follow_symlinks=False)
                if 0 < info.st_size <= MAX_FILE_BYTES:
                    yield path, info


def read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    for encoding in ("utf-8", "utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def chunk_text(text: str, max_lines: int = CHUNK_LINES, overlap: int = OVERLAP) -> list[Chunk]:
    lines = text.splitlines()
    if not lines:
        return []
    starts = [0] + [i for i, line in enumerate(lines) if i and DEFINITION.match(line)]
    blocks: list[tuple[int, int]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        if blocks and ((blocks[-1][1] - blocks[-1][0]) < 8 or (end - blocks[-1][0]) <= max_lines // 2):
            blocks[-1] = (blocks[-1][0], end)  # fold imports/tiny blocks into the next definition
        else:
            blocks.append((start, end))
    chunks: list[Chunk] = []
    for start, end in blocks:
        position = start
        while position < end:
            stop = min(end, position + max_lines)
            body = "\n".join(lines[position:stop])
            if body.strip():
                chunks.append(Chunk(position + 1, stop, body, tuple(dict.fromkeys(NAME.findall(body)))))
            if stop >= end:
                break
            position = stop - overlap
    return chunks


IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def identifier_words(text: str) -> str:
    """'getUserName and max_retry_count' -> 'getusername get user name max_retry_count max retry count'."""
    words: dict[str, None] = {}
    for identifier in IDENTIFIER.findall(text):
        parts = [p.lower() for piece in identifier.split("_") for p in CAMEL.findall(piece)]
        if len(parts) > 1:
            words[identifier.lower()] = None
            for part in parts:
                words[part] = None
    return " ".join(words)
