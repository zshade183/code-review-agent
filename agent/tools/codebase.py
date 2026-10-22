"""代码库检索工具（Agent 的对外上下文来源）。

只给模型**单个文件全文**时，它看不到跨文件上下文，容易给出
「这个函数应该在别处复用」「这个常量应该和配置对齐」这类无法验证的建议。

这里提供一个零依赖的轻量检索层：

- 按「顶层函数 / 类 / 模块头」切块（AST 切块，比定长窗口更贴合代码语义）
- 标识符感知的分词（拆 snake_case / camelCase），配合 BM25 排序
- 符号索引：精确回答「某函数/类定义在哪」

设计说明：``Embedder`` 接口留出了替换成向量检索的口子（见 ``agent/memory.py``），
但在没有 embedding 服务时，BM25 + 标识符分词对代码检索的效果已经相当可用，
且不需要引入任何新依赖。
"""

from __future__ import annotations

import ast
import math
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from agent.schema import ToolResult
from agent.tools.base import Tool

DEFAULT_IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "site-packages",
}

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


def tokenize(text: str) -> List[str]:
    """标识符感知分词：coding_style -> [coding, style]；parseHTTPResponse -> [parse, http, response]。"""
    tokens: List[str] = []
    for raw in _TOKEN_RE.findall(text or ""):
        for part in raw.split("_"):
            if not part:
                continue
            for piece in _CAMEL_RE.split(part):
                piece = piece.strip().lower()
                if piece:
                    tokens.append(piece)
    return tokens


@dataclass
class Chunk:
    """代码块。"""

    path: str
    kind: str
    name: str
    line_start: int
    line_end: int
    text: str
    tokens: List[str] = field(default_factory=list)

    def header(self) -> str:
        return f"{self.path}:{self.line_start}-{self.line_end} [{self.kind} {self.name}]"

    def to_dict(self, max_chars: int = 1500) -> Dict[str, Any]:
        body = self.text
        truncated = len(body) > max_chars
        return {
            "path": self.path,
            "kind": self.kind,
            "name": self.name,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "truncated": truncated,
            "text": body[:max_chars],
        }


def chunk_python_source(path: str, source: str, min_chars: int = 40) -> List[Chunk]:
    """把 Python 源码切成语义块：模块头 + 每个顶层定义。"""
    lines = source.split("\n")
    chunks: List[Chunk] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        text = source.strip()
        if len(text) >= min_chars:
            chunks.append(
                Chunk(path=path, kind="module", name="<unparsable>", line_start=1, line_end=len(lines), text=text)
            )
        return chunks

    def slice_lines(start: int, end: int) -> str:
        return "\n".join(lines[max(0, start - 1) : end])

    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if not defs:
        text = source.strip()
        if len(text) >= min_chars:
            chunks.append(Chunk(path=path, kind="module", name="<module>", line_start=1, line_end=len(lines), text=text))
        return chunks

    header_end = max(0, min(getattr(d, "lineno", 1) for d in defs) - 1)
    if header_end > 0:
        header = "\n".join(lines[:header_end]).strip()
        if len(header) >= min_chars:
            chunks.append(
                Chunk(path=path, kind="module_header", name="<imports>", line_start=1, line_end=header_end, text=header)
            )

    for node in defs:
        start = int(getattr(node, "lineno", 1) or 1)
        end = int(getattr(node, "end_lineno", start) or start)
        text = slice_lines(start, end)
        if len(text) < min_chars:
            continue
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        chunks.append(
            Chunk(path=path, kind=kind, name=node.name, line_start=start, line_end=end, text=text)
        )
    return chunks


class BM25Index:
    """极简 BM25 检索（k1/b 采用 TREC 常用默认值）。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self.chunks: List[Chunk] = []
        self.doc_freq: Counter[str] = Counter()
        self.avg_len: float = 0.0

    def add(self, chunks: Iterable[Chunk]) -> None:
        for chunk in chunks:
            if not chunk.tokens:
                chunk.tokens = tokenize(f"{chunk.name} {chunk.path} {chunk.text}")
            self.chunks.append(chunk)
            for token in set(chunk.tokens):
                self.doc_freq[token] += 1
        self.avg_len = (
            sum(len(c.tokens) for c in self.chunks) / len(self.chunks) if self.chunks else 0.0
        )

    def _idf(self, token: str) -> float:
        n = len(self.chunks)
        df = self.doc_freq.get(token, 0)
        if df == 0:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 5) -> List[Tuple[float, Chunk]]:
        query_tokens = tokenize(query)
        if not query_tokens or not self.chunks:
            return []
        scored: List[Tuple[float, Chunk]] = []
        for chunk in self.chunks:
            counts = Counter(chunk.tokens)
            length = len(chunk.tokens) or 1
            score = 0.0
            for token in query_tokens:
                tf = counts.get(token, 0)
                if tf == 0:
                    continue
                idf = self._idf(token)
                denom = tf + self.k1 * (1 - self.b + self.b * length / (self.avg_len or 1))
                score += idf * (tf * (self.k1 + 1)) / (denom or 1)
            # 名称精确命中给予额外权重（代码检索中符号名是最强信号）
            lowered = chunk.name.lower()
            for token in query_tokens:
                if token == lowered or token in lowered:
                    score += 2.0
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].path, item[1].line_start))
        return scored[:top_k]


class CodebaseIndex:
    """代码库索引。

    ``ensure_indexed`` 是线程安全的：评测与 API 都会在多线程下共享同一个索引实例，
    懒加载若不加锁会出现「两个线程同时构建」的竞态（重复扫描全仓库）。
    """

    def __init__(self, root: str, max_file_bytes: int = 300_000) -> None:
        self.root = Path(root).resolve()
        self.max_file_bytes = max_file_bytes
        self.index = BM25Index()
        self.symbols: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
        self.files: List[str] = []
        self.indexed = False
        self._lock = threading.Lock()

    # ---------- 构建 ----------

    def build(self) -> Dict[str, Any]:
        """扫描并建立索引。重复调用会重新构建（调用方通常用 ensure_indexed）。"""
        with self._lock:
            return self._build_locked()

    def _build_locked(self) -> Dict[str, Any]:
        self.index = BM25Index()
        self.symbols = defaultdict(list)
        self.files = []
        chunks: List[Chunk] = []
        errors: List[str] = []
        for path in self._iter_python_files():
            try:
                source = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            rel = str(path.relative_to(self.root)).replace("\\", "/")
            self.files.append(rel)
            for chunk in chunk_python_source(rel, source):
                chunks.append(chunk)
                if chunk.kind in ("function", "class"):
                    self.symbols[chunk.name].append((rel, chunk.line_start))
        self.index.add(chunks)
        self.indexed = True
        return {
            "root": str(self.root),
            "files": len(self.files),
            "chunks": len(chunks),
            "symbols": len(self.symbols),
            "errors": errors[:5],
        }

    def _iter_python_files(self) -> Iterable[Path]:
        for path in self.root.rglob("*.py"):
            if any(part in DEFAULT_IGNORE_DIRS for part in path.parts):
                continue
            try:
                if path.stat().st_size > self.max_file_bytes:
                    continue
            except OSError:
                continue
            yield path

    # ---------- 查询 ----------

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        self.ensure_indexed()
        return [chunk.to_dict() for _score, chunk in self.index.search(query, top_k)]

    def find_symbol(self, name: str, top_k: int = 10) -> List[Dict[str, Any]]:
        self.ensure_indexed()
        hits = self.symbols.get(name)
        if hits:
            return [{"name": name, "path": p, "line": line} for p, line in hits[:top_k]]
        # 退化为模糊匹配
        matched: List[Dict[str, Any]] = []
        for symbol, locations in self.symbols.items():
            if name.lower() in symbol.lower():
                matched.extend({"name": symbol, "path": p, "line": line} for p, line in locations)
            if len(matched) >= top_k:
                break
        return matched[:top_k]

    def read(self, relative_path: str, start: Optional[int] = None, end: Optional[int] = None) -> Dict[str, Any]:
        """读取仓库内文件（带路径穿越防护）。"""
        target = (self.root / relative_path).resolve()
        if not self._is_inside_root(target):
            raise ValueError(f"路径越界，拒绝访问: {relative_path}")
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"文件不存在: {relative_path}")
        text = target.read_text(encoding="utf-8", errors="ignore")
        lines = text.split("\n")
        s = max(1, int(start or 1))
        e = min(len(lines), int(end or len(lines)))
        if e < s:
            e = s
        selected = lines[s - 1 : e]
        return {
            "path": relative_path,
            "line_start": s,
            "line_end": e,
            "total_lines": len(lines),
            "content": "\n".join(f"{i + s}| {line}" for i, line in enumerate(selected)),
        }

    def _is_inside_root(self, target: Path) -> bool:
        try:
            target.relative_to(self.root)
            return True
        except ValueError:
            return False

    def ensure_indexed(self) -> None:
        if self.indexed:
            return
        with self._lock:
            if not self.indexed:  # double-checked locking
                self._build_locked()

    def stats(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "indexed": self.indexed,
            "files": len(self.files),
            "chunks": len(self.index.chunks),
            "symbols": len(self.symbols),
        }


# ---------------------------------------------------------------------------
# 工具封装
# ---------------------------------------------------------------------------


class SearchCodebaseTool(Tool):
    name = "search_codebase"
    description = (
        "在项目代码库中检索与查询语义相关的代码块（函数/类/模块头），返回文件路径、行号范围"
        "与代码内容。当需要了解「项目里其他地方是怎么写 SQL / 怎么处理异常 / 该函数还有谁在用」"
        "时使用，避免脱离项目上下文给出空泛建议。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "查询语句，可以是自然语言或标识符名称"},
            "top_k": {"type": "integer", "description": "返回条数，默认 5", "default": 5},
        },
        "required": ["query"],
    }

    def __init__(self, index: CodebaseIndex) -> None:
        self._index = index

    def run(self, query: str, top_k: int = 5) -> ToolResult:
        results = self._index.search(query, top_k=top_k)
        if not results:
            return ToolResult(
                tool=self.name,
                ok=True,
                content="未检索到相关内容。可尝试改用标识符名称或更具体的关键词。",
                data={"results": []},
            )
        import json

        return ToolResult(
            tool=self.name,
            ok=True,
            content=json.dumps({"query": query, "results": results}, ensure_ascii=False, indent=2),
            data={"results": results},
        )


class ReadFileTool(Tool):
    name = "read_project_file"
    description = "读取项目内某个文件的指定行范围（带行号）。在检索结果不足、需要看完整实现时使用。"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "相对代码库根目录的文件路径"},
            "start_line": {"type": "integer", "description": "起始行（从 1 开始）", "default": 1},
            "end_line": {"type": "integer", "description": "结束行；省略则读到文件末尾", "default": 0},
        },
        "required": ["path"],
    }

    def __init__(self, index: CodebaseIndex) -> None:
        self._index = index

    def run(self, path: str, start_line: int = 1, end_line: int = 0) -> ToolResult:
        import json

        try:
            payload = self._index.read(path, start_line, end_line or None)
        except (ValueError, FileNotFoundError) as exc:
            return ToolResult(tool=self.name, ok=False, error=str(exc), content=f"读取失败：{exc}")
        return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False, indent=2), data=payload)


class FindSymbolInProjectTool(Tool):
    name = "find_symbol_in_project"
    description = "在整个项目中查找某个函数/类的定义位置（文件 + 行号）。用于确认复用点与改动影响面。"
    parameters = {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "函数名或类名"}},
        "required": ["symbol"],
    }

    def __init__(self, index: CodebaseIndex) -> None:
        self._index = index

    def run(self, symbol: str) -> ToolResult:
        import json

        hits = self._index.find_symbol(symbol)
        payload = {"symbol": symbol, "definitions": hits}
        return ToolResult(tool=self.name, ok=True, content=json.dumps(payload, ensure_ascii=False, indent=2), data=payload)
