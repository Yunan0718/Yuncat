"""services/code_indexer.py — 仓库级代码符号索引 (Phase 43 V.2)

对标: Aider --map-refresh · Cursor .cursor/index/ · Copilot @workspace。

Phase 1: Python ast.parse 提取 ClassDef/FunctionDef → JSON 缓存。
Phase 2: tree-sitter 扩展 .ts/.tsx/.js/.css/.yaml (可选)。
Batch 5.4: 增量更新——监听 file_changed 事件，只重新解析变更文件。
"""
import ast
import json as _json
import logging
import os as _os
import re as _re
import time as _time
from pathlib import Path as _Path

from config import (
    CODE_INDEX_MAX_FILES, CODE_INDEX_SUMMARY_LIMIT,
    CODE_INDEX_CLASS_METHODS_PREVIEW, CODE_INDEX_FUNCTION_ARGS_PREVIEW,
    CODE_INDEX_INTERVAL_SECONDS,
    calc_list_limit,
)

_log = logging.getLogger("yuncat.code_indexer")

INDEX_DIR = _Path(_os.environ.get("YUNCAT_DATA_DIR", _os.path.expanduser("~/.yuncat"))) / "index"
INDEX_FILE = INDEX_DIR / "symbols.json"
INDEX_INTERVAL = CODE_INDEX_INTERVAL_SECONDS

# 跳过索引的目录/文件模式
_IGNORE_PARTS = {"node_modules", "__pycache__", "python-embed"}

# 支持的源码扩展名：Python（ast） + JS/TS 系（正则符号提取，零依赖）
_PY_EXT = {".py"}
_JS_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

# JS/TS 符号提取正则（对标 tree-sitter 粗粒度：类/函数/箭头/接口/类型）
_JS_FUNCTION_RE = _re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(([^)]*)\)",
    _re.MULTILINE,
)
_JS_CLASS_RE = _re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)",
    _re.MULTILINE,
)
_JS_ARROW_RE = _re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>",
    _re.MULTILINE,
)
_JS_TYPE_RE = _re.compile(
    r"^\s*(?:export\s+)?(?:interface|type)\s+([A-Za-z_$][\w$]*)",
    _re.MULTILINE,
)


def _is_ignored(path: _Path) -> bool:
    """判断路径是否应被排除在索引之外。"""
    parts = set(path.parts)
    return bool(parts & _IGNORE_PARTS)


class CodeIndexer:
    """仓库代码符号索引器——Agent 知道 "哪个类在哪个文件里"。"""

    def __init__(self, workspace: str = ""):
        self._workspace = _Path(workspace) if workspace else _Path.cwd()
        self._symbols: list[dict] = []

    async def build(self) -> list[dict]:
        """扫描 workspace → 提取符号（Python=ast / JS-TS=正则）→ 缓存 JSON。"""
        files = [f for f in self._workspace.rglob("*")
                 if f.suffix in (_PY_EXT | _JS_EXT) and not _is_ignored(f)][:CODE_INDEX_MAX_FILES]

        symbols: list[dict] = []
        files_meta: dict[str, dict] = {}
        for fp in files:
            parsed = self._parse_file(fp)
            if parsed is None:
                continue
            rel, file_symbols, meta = parsed
            symbols.extend(file_symbols)
            files_meta[rel] = meta

        unique = self._deduplicate(symbols)
        self._persist(unique, files_meta)
        _log.info("CodeIndexer: %d symbols from %d files", len(unique), len(files))
        self._symbols = unique
        return unique

    def incremental_update(self, paths: list[str]) -> list[dict]:
        """Batch 5.4: 增量更新——只重新解析变更/新增/删除的文件。

        Args:
            paths: 相对于 workspace 或绝对路径的列表。

        Returns:
            更新后的 symbols 列表。
        """
        data = self._load_index()
        symbols = list(data.get("symbols", []))
        files_meta = dict(data.get("files", {}))

        changed_rels: set[str] = set()
        deleted_rels: set[str] = set()

        for p in paths:
            rel = self._to_rel(p)
            if not rel:
                continue
            abs_path = self._workspace / rel
            if abs_path.is_dir():
                continue
            if not abs_path.exists():
                deleted_rels.add(rel)
                continue
            try:
                stat = abs_path.stat()
                old_meta = files_meta.get(rel)
                if (
                    old_meta
                    and old_meta.get("mtime") == int(stat.st_mtime)
                    and old_meta.get("size") == stat.st_size
                ):
                    continue
            except Exception:
                _log.debug("CodeIndexer: stat failed for %s", rel)
            changed_rels.add(rel)

        affected_rels = changed_rels | deleted_rels
        symbols = [s for s in symbols if s.get("file") not in affected_rels]

        for rel in changed_rels:
            abs_path = self._workspace / rel
            parsed = self._parse_file(abs_path)
            if parsed is None:
                files_meta.pop(rel, None)
                continue
            _, file_symbols, meta = parsed
            symbols.extend(file_symbols)
            files_meta[rel] = meta

        for rel in deleted_rels:
            files_meta.pop(rel, None)

        unique = self._deduplicate(symbols)
        self._persist(unique, files_meta)
        _log.info("CodeIndexer incremental: %d symbols across %d tracked files",
                  len(unique), len(files_meta))
        self._symbols = unique
        return unique

    def _to_rel(self, path: str) -> str | None:
        """将路径转换为 workspace 相对路径；无法转换时返回 None。"""
        p = _Path(path)
        try:
            if p.is_absolute():
                return str(p.relative_to(self._workspace)).replace("\\", "/")
            return str(p).replace("\\", "/")
        except ValueError:
            _log.debug("CodeIndexer: path outside workspace: %s", path)
            return None

    def _load_index(self) -> dict:
        """加载现有索引 JSON；不存在或损坏时返回空结构。"""
        if not INDEX_FILE.exists():
            return {"symbols": [], "files": {}, "updated_at": 0, "total": 0}
        try:
            return _json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            _log.warning("CodeIndexer: failed to load existing index, rebuilding")
            return {"symbols": [], "files": {}, "updated_at": 0, "total": 0}

    def _parse_file(self, fp: _Path) -> tuple[str, list[dict], dict] | None:
        """按扩展名分发解析：JS/TS 走正则符号提取，其余走 Python ast。"""
        if _is_ignored(fp):
            return None
        if fp.suffix in _JS_EXT:
            return self._parse_js_file(fp)
        return self._parse_py_file(fp)

    def _parse_py_file(self, fp: _Path) -> tuple[str, list[dict], dict] | None:
        """解析单个 Python 文件，返回 (rel_path, symbols, file_meta)。"""
        try:
            rel = str(fp.relative_to(self._workspace)).replace("\\", "/")
            stat = fp.stat()
            text = fp.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(text)
        except SyntaxError:
            _log.debug("CodeIndexer syntax error: %s", fp.name)
            return None
        except Exception:
            _log.debug("CodeIndexer parse skip: %s", fp.name)
            return None

        symbols: list[dict] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = [
                    n.name for n in ast.iter_child_nodes(node)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                ][:CODE_INDEX_CLASS_METHODS_PREVIEW]
                symbols.append({
                    "type": "class", "name": node.name,
                    "file": rel, "methods": methods,
                    "line": node.lineno,
                })
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                args = [a.arg for a in node.args.args][:CODE_INDEX_FUNCTION_ARGS_PREVIEW]
                symbols.append({
                    "type": "function", "name": node.name,
                    "file": rel, "args": args,
                    "line": node.lineno,
                })

        return rel, symbols, {"mtime": int(stat.st_mtime), "size": stat.st_size}

    def _parse_js_file(self, fp: _Path) -> tuple[str, list[dict], dict] | None:
        """正则提取 JS/TS 符号（function/class/const箭头/interface/type）。

        零依赖替代 tree-sitter：够 repo map 用（哪个组件/函数在哪个文件），
        不求精确 AST。跳过 _ 前缀私有符号与 .d.ts 声明文件。
        """
        if fp.name.endswith(".d.ts"):
            return None
        try:
            rel = str(fp.relative_to(self._workspace)).replace("\\", "/")
            stat = fp.stat()
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            _log.debug("CodeIndexer js parse skip: %s", fp.name)
            return None

        symbols: list[dict] = []

        def _line_of(match) -> int:
            return text.count("\n", 0, match.start()) + 1

        for m in _JS_CLASS_RE.finditer(text):
            symbols.append({"type": "class", "name": m.group(1), "file": rel, "line": _line_of(m)})
        for m in _JS_FUNCTION_RE.finditer(text):
            name = m.group(1)
            if name.startswith("_"):
                continue
            args = [a.strip() for a in m.group(2).split(",") if a.strip()][:CODE_INDEX_FUNCTION_ARGS_PREVIEW]
            symbols.append({"type": "function", "name": name, "file": rel, "args": args, "line": _line_of(m)})
        for m in _JS_ARROW_RE.finditer(text):
            name = m.group(1)
            if name.startswith("_"):
                continue
            args = [a.strip() for a in m.group(2).split(",") if a.strip()][:CODE_INDEX_FUNCTION_ARGS_PREVIEW]
            symbols.append({"type": "function", "name": name, "file": rel, "args": args, "line": _line_of(m)})
        for m in _JS_TYPE_RE.finditer(text):
            symbols.append({"type": "type", "name": m.group(1), "file": rel, "line": _line_of(m)})

        return rel, symbols, {"mtime": int(stat.st_mtime), "size": stat.st_size}

    @staticmethod
    def _deduplicate(symbols: list[dict]) -> list[dict]:
        """按 (type, name, file) 去重。"""
        seen: set[str] = set()
        unique: list[dict] = []
        for s in symbols:
            key = f"{s['type']}:{s['name']}:{s['file']}"
            if key in seen:
                continue
            seen.add(key)
            unique.append(s)
        return unique

    def _persist(self, symbols: list[dict], files_meta: dict[str, dict]) -> None:
        """将 symbols + files 元数据持久化到 JSON 缓存。"""
        INDEX_DIR.mkdir(parents=True, exist_ok=True)
        INDEX_FILE.write_text(_json.dumps({
            "symbols": symbols,
            "files": files_meta,
            "updated_at": int(_time.time()),
            "total": len(symbols),
            "workspace": str(self._workspace),
        }, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def load_summary(model_cfg: dict | None = None, limit: int | None = None) -> str:
        """加载符号索引→Agent prompt 注入用的文本摘要。

        Task 4.7: limit 动态派生（对标 Aider --map / Cursor @workspace 按模型窗口预算）。
        优先级：显式 limit > model_cfg 动态派生 > CODE_INDEX_SUMMARY_LIMIT fallback。

        派生规则：base=50, fraction_per_100k=8 → 128K 模型≈58, 1M 模型≈114 符号
        （每符号 ~20-30 token，1M 窗口下占比 <0.3%）。
        实际返回数量不超过索引中符号总数（slice 自然截断）。

        Args:
            model_cfg: 模型配置 dict（含 context 窗口信息）；None 时用 fallback
            limit: 显式覆盖上限；None 时按 model_cfg 动态派生
        """
        if limit is None:
            if model_cfg:
                try:
                    limit = calc_list_limit(
                        model_cfg, CODE_INDEX_SUMMARY_LIMIT,
                        fraction_per_100k=8,
                    )
                except Exception:
                    limit = CODE_INDEX_SUMMARY_LIMIT
            else:
                limit = CODE_INDEX_SUMMARY_LIMIT
        if not INDEX_FILE.exists():
            return ""
        try:
            data = _json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            symbols = data.get("symbols", [])[:limit]
            if not symbols:
                return ""
            lines = ["## 项目符号索引 (top-{}, 30min 刷新)".format(len(symbols))]
            for s in symbols:
                extra = ""
                if s["type"] == "class" and s.get("methods"):
                    extra = f"({', '.join(s['methods'][:CODE_INDEX_CLASS_METHODS_PREVIEW])})"
                elif s["type"] == "function" and s.get("args"):
                    extra = f"({', '.join(s['args'][:CODE_INDEX_FUNCTION_ARGS_PREVIEW])})"
                lines.append(
                    f"- {s['type']} {s['name']}{extra} → {s['file']}:{s.get('line', '')}"
                )
            return "\n".join(lines)
        except Exception:
            return ""
