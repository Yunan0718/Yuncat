"""system_tool_search.py —— 搜索工具"""
import asyncio
import json
import logging
import os
import shutil
import sys

from ..registry import tool
from config import (
    calc_tool_result_limit, MAX_SEARCH_FILES,
    SEARCH_CONTEXT_PREVIEW_CHARS, SEARCH_EXCERPT_CHARS,
    TOOL_SIZE_SEARCH_FILES_CHARS, TOOL_SIZE_CODEGRAPH_CONTEXT_CHARS,
)

_log = logging.getLogger("yuncat.tools.search")


# ---- Task 7.11 (F-047): ripgrep 原生引擎集成（对标 Claude Code Grep 工具）----
# ripgrep 可用时用原生引擎（大仓库提速），不可用/失败时回退 os.walk（graceful degradation）。
# 缓存 shutil.which 结果避免每次 search_files 调用都探测 PATH。
_RIPGREP_CACHE: str | None = None  # None=未检测；""=不可用；其他=可执行文件路径


def _find_ripgrep() -> str:
    """检测 ripgrep 可执行文件路径，结果缓存。返回空字符串表示不可用。

    Task 7.11 (F-047): 对标 Claude Code Grep 工具——ripgrep 原生引擎。
    缓存避免每次 search_files 调用都 shutil.which 探测 PATH。
    """
    global _RIPGREP_CACHE
    if _RIPGREP_CACHE is not None:
        return _RIPGREP_CACHE
    _RIPGREP_CACHE = shutil.which("rg") or ""
    return _RIPGREP_CACHE


def _reset_ripgrep_cache() -> None:
    """重置 ripgrep 检测缓存（测试用）。"""
    global _RIPGREP_CACHE
    _RIPGREP_CACHE = None


async def _search_with_ripgrep(
    pattern: str, search_root, file_pattern: str, max_results: int,
    use_regex: bool, context_lines: int, ws, _MAX_SIZE: int,
) -> dict | None:
    """ripgrep 原生引擎搜索。失败时返回 None 让调用方回退到 os.walk。

    Task 7.11 (F-047): 对标 Claude Code Grep 工具——ripgrep --json 输出。
    - --json: 稳定可解析的 JSON 输出（每行一个对象）
    - --line-number --with-filename: 行号+文件名
    - --fixed-strings: use_regex=False 时字面量匹配（与 os.walk .lower() 包含语义一致）
    - -C N: 上下文行
    - -g glob: 文件过滤；-g '!dir' 跳过目录（与 os.walk 行为对齐）
    - --no-ignore: 不读 .gitignore（与 os.walk 一致——os.walk 不读 .gitignore）
    - --max-filesize: 跳过大文件（与 os.walk getsize 检查对齐）

    ripgrep returncode: 0=有匹配, 1=无匹配, 2=错误（含无效 regex）。
    returncode != 0/1 时返回 None 触发回退（os.walk 会报具体错误）。
    """
    rg = _find_ripgrep()
    if not rg:
        return None

    # 构造 ripgrep 参数
    args = [rg, "--json", "--line-number", "--with-filename", "--no-ignore"]
    if not use_regex:
        args.append("--fixed-strings")
    if context_lines > 0:
        args.extend(["-C", str(context_lines)])
    # 文件过滤（file_pattern == "*" 时不加 -g，搜所有文件——与 os.walk fnmatch("*") 一致）
    if file_pattern and file_pattern != "*":
        args.extend(["-g", file_pattern])
    # 跳过目录（与 os.walk 显式跳过对齐——ripgrep 默认跳过 .git，但 node_modules/__pycache__ 需显式）
    for skip in ("node_modules", "__pycache__", ".git"):
        args.extend(["-g", f"!{skip}"])
    # 大文件跳过（与 os.walk getsize 检查对齐）
    if _MAX_SIZE > 0:
        args.extend(["--max-filesize", str(_MAX_SIZE)])
    # -- 防模式被解析为参数（如 pattern 以 - 开头）
    args.append("--")
    args.append(pattern)
    args.append(str(search_root))

    # subprocess（参考 run_command 的 Windows creationflags——不弹窗）
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x00000200 | 0x08000000  # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        stdout, _stderr = await proc.communicate()
        # returncode: 0=有匹配, 1=无匹配, 2=错误（无效 regex/路径不存在等）
        if proc.returncode not in (0, 1):
            return None  # 让调用方回退到 os.walk（os.walk 会报具体错误）
        output = stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return None  # ripgrep 不可用，回退
    except Exception:
        return None  # 其他异常，回退

    return _parse_ripgrep_json(output, pattern, max_results, context_lines, ws)


def _parse_ripgrep_json(
    output: str, pattern: str, max_results: int, context_lines: int, ws,
) -> dict:
    """解析 ripgrep --json 输出为 search_files 结果格式。

    ripgrep --json 每行一个 JSON 对象：
    - {"type":"begin","data":{"path":{"text":"..."}}}  # 开始搜一个文件
    - {"type":"match","data":{"path":{"text":"..."},"lines":{"text":"..."},"line_number":N,...}}
    - {"type":"context","data":{...}}  # -C 触发的上下文行
    - {"type":"end","data":{"path":{...}}}
    - {"type":"summary","data":{"stats":{"matches":N,"searched":N,...}}}  # searched=搜索的文件数
    """
    # 按文件累积行：{file: [(line_no, text, is_match), ...]}
    file_lines: dict[str, list[tuple[int, str, bool]]] = {}
    files_scanned = 0

    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        otype = obj.get("type")
        if otype == "summary":
            # stats.searched = 被搜索的文件数（ripgrep 实际打开搜索的文件数）
            stats = obj.get("data", {}).get("stats", {})
            files_scanned = stats.get("searched", 0) or files_scanned
        elif otype in ("match", "context"):
            data = obj.get("data", {})
            path = data.get("path", {}).get("text", "")
            if not path:
                continue
            line_no = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "")
            is_match = (otype == "match")
            file_lines.setdefault(path, []).append((line_no, text, is_match))

    # 若 summary 没提供 searched，用 file_lines 长度作 fallback
    if files_scanned == 0:
        files_scanned = len(file_lines)

    # 对每个文件，按行号排序，提取 match
    results = []
    for path, lines in file_lines.items():
        lines.sort(key=lambda x: x[0])
        # 构建行号 -> text 映射（用于 context 提取——基于行号找前后 N 行）
        line_map = {ln: t for ln, t, _ in lines}
        for line_no, text, is_match in lines:
            if not is_match:
                continue
            entry = {
                "file": os.path.relpath(path, str(ws)).replace("\\", "/"),
                "line": line_no,
            }
            if context_lines > 0:
                # 提取前后 N 行作为 context（基于行号，与 os.walk file_lines 切片语义对齐）
                ctx_parts = []
                for offset in range(-context_lines, context_lines + 1):
                    target_ln = line_no + offset
                    if target_ln in line_map:
                        ctx_parts.append(line_map[target_ln])
                entry["context"] = "".join(ctx_parts).strip()[:SEARCH_CONTEXT_PREVIEW_CHARS]
            else:
                entry["excerpt"] = text.strip()[:SEARCH_EXCERPT_CHARS]
            results.append(entry)
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    return {
        "pattern": pattern,
        "results": results,
        "total_matches": len(results),
        "files_scanned": files_scanned,
    }


def _search_with_oswalk(
    pattern: str, search_root, file_pattern: str, max_results: int,
    use_regex: bool, context_lines: int, ws, _MAX_SIZE: int,
) -> dict:
    """os.walk fallback 搜索（ripgrep 不可用/失败时使用）。

    Task 7.11: 从原 search_files 提取为独立函数，保持原有行为不变。
    返回 dict 含 pattern/results/total_matches/files_scanned；
    若 use_regex 编译失败或外层异常，返回 {"error": "..."}。
    """
    import fnmatch as _fnmatch
    results = []
    files_scanned = 0
    MAX_FILES = MAX_SEARCH_FILES  # Phase 48-H: 放宽上限对标主流

    # Phase 34: regex 模式（Python re）vs 简单字符串匹配
    if use_regex:
        try:
            import re as _re_search
            _matcher = _re_search.compile(pattern)
            def _matches(text): return bool(_matcher.search(text))
        except Exception as e:
            return {"error": f"Invalid regex: {e}"}
    else:
        pattern_lower = pattern.lower()
        def _matches(text): return pattern_lower in text.lower()

    try:
        for root, dirs, files in os.walk(str(search_root)):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git")]
            if files_scanned >= MAX_FILES:
                break
            for fname in files:
                if files_scanned >= MAX_FILES:
                    break
                if not _fnmatch.fnmatch(fname, file_pattern):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    if os.path.getsize(fpath) > _MAX_SIZE:
                        continue
                    files_scanned += 1
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        file_lines = f.readlines() if context_lines > 0 else None
                    with open(fpath, encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, 1):
                            if _matches(line):
                                entry = {
                                    "file": os.path.relpath(fpath, str(ws)).replace("\\", "/"),
                                    "line": lineno,
                                }
                                if context_lines > 0 and file_lines:
                                    ctx_start = max(0, lineno - context_lines - 1)
                                    ctx_end = min(len(file_lines), lineno + context_lines)
                                    entry["context"] = "".join(file_lines[ctx_start:ctx_end]).strip()[:SEARCH_CONTEXT_PREVIEW_CHARS]
                                else:
                                    entry["excerpt"] = line.strip()[:SEARCH_EXCERPT_CHARS]
                                results.append(entry)
                                if len(results) >= max_results:
                                    break
                except Exception:
                    continue  # 回退：单个文件读失败就跳过它
                if len(results) >= max_results:
                    break
    except Exception as e:
        return {"error": str(e)}

    return {
        "pattern": pattern,
        "results": results,
        "total_matches": len(results),
        "files_scanned": files_scanned,
    }


# ---- Phase 33: search_files — grep-like file content search ----

@tool(
    name="search_files", agents=["hermes", "openclaw"],
    description=(
        "Search file contents in the workspace using patterns (similar to grep). "
        "Returns matching files with line numbers and excerpts. "
        "Use this instead of run_command('find ... | xargs grep ...') to safely find code."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Text or regex pattern to search for"},
            "path": {"type": "string", "description": "Subdirectory within workspace to search (empty = entire workspace)", "default": ""},
            "file_pattern": {"type": "string", "description": "Glob to filter files (e.g. '*.py', '*.ts')", "default": "*"},
            "max_results": {"type": "integer", "default": 20, "description": "Maximum results to return"},
            "use_regex": {"type": "boolean", "default": False, "description": "Treat pattern as regex (Python re)"},
            "context_lines": {"type": "integer", "default": 0, "description": "Lines of context around each match"},
        },
        "required": ["pattern"],
    },
    is_read_only=True, is_concurrency_safe=True,
    groups=["search"],
    max_result_size_chars=TOOL_SIZE_SEARCH_FILES_CHARS,  # Phase 56-P8: 自我声明结果大小
    tool_timeout=120,  # P2-2.4: 全仓库搜索超时（大仓库 os.walk 可能慢）
)
async def search_files(pattern: str, path: str = "", file_pattern: str = "*",
                       max_results: int = 20, use_regex: bool = False,
                       context_lines: int = 0) -> dict:
    """Search file contents using ripgrep (preferred) or os.walk fallback.

    Task 7.11 (F-047): ripgrep 原生引擎优先（对标 Claude Code Grep 工具），
    ripgrep 不可用/失败时优雅回退到 os.walk。两路径返回结构一致。
    """
    from security.sandbox import validate_path, get_workspace
    from tools.result import ToolResult
    ws = get_workspace()
    try:
        search_root = validate_path(path) if path else ws
    except PermissionError as e:
        return ToolResult(success=False, error=str(e), meta={"tool_name": "search_files"}).to_dict()

    # Phase 43 P0-DYN / P2-2.1: search_files 文件大小上限从当前默认模型上下文窗口派生
    from config import get_default_model_config
    _MAX_SIZE = calc_tool_result_limit(get_default_model_config())

    # Task 7.11 (F-047): ripgrep 优先，不可用/失败回退 os.walk
    rg_result = await _search_with_ripgrep(
        pattern, search_root, file_pattern, max_results,
        use_regex, context_lines, ws, _MAX_SIZE,
    )
    if rg_result is not None:
        return ToolResult(
            success=True,
            data=rg_result,
            observation=f"搜索 '{pattern}'：找到 {rg_result['total_matches']} 处匹配（扫描 {rg_result['files_scanned']} 个文件）",
            meta={"tool_name": "search_files", "matches": rg_result["total_matches"], "engine": "ripgrep"},
        ).to_dict()

    # Fallback: os.walk（ripgrep 不可用或子进程失败）
    os_result = _search_with_oswalk(
        pattern, search_root, file_pattern, max_results,
        use_regex, context_lines, ws, _MAX_SIZE,
    )
    if "error" in os_result:
        return ToolResult(success=False, error=os_result["error"], meta={"tool_name": "search_files"}).to_dict()
    return ToolResult(
        success=True,
        data=os_result,
        observation=f"搜索 '{pattern}'：找到 {os_result['total_matches']} 处匹配（扫描 {os_result['files_scanned']} 个文件）",
        meta={"tool_name": "search_files", "matches": os_result["total_matches"], "engine": "oswalk"},
    ).to_dict()


# ---- Phase 31 fix: codegraph_context 工具注册（CRITICAL #3）----


@tool(
    name="codegraph_context", agents=["hermes", "openclaw"],
    description=(
        "Query code structure and symbol relationships using CodeGraph. "
        "Use this INSTEAD of read_file + grep when you need to understand "
        "code structure, find function/class definitions, trace call paths, "
        "or analyze impact of changes. "
        "Example: codegraph_context(query='find authentication functions')"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to find in the code (e.g. 'auth middleware', 'error handlers')",
            },
            "max_symbols": {
                "type": "integer", "default": 20,
                "description": "Maximum number of symbols to return",
            },
        },
        "required": ["query"],
    },
    is_read_only=True,
    is_concurrency_safe=True,
    groups=["search"],
    max_result_size_chars=TOOL_SIZE_CODEGRAPH_CONTEXT_CHARS,  # Phase 56-P8: 自我声明结果大小
    tool_timeout=120,  # P2-2.4: CodeGraph MCP 子进程查询超时
)
async def codegraph_context(query: str, max_symbols: int = 20) -> dict:
    """查询代码结构——CodeGraph MCP 优先（复用 app.state.codegraph 单例），回退 search_files。"""
    from tools.result import ToolResult
    # Task 4.2: 优先复用 lifespan 启动的 app.state.codegraph 单例（已 start）。
    # 原实现每次调用都 CodeGraphClient()+start()+stop()——fork 新 Node 子进程，浪费资源
    # 且与 lifespan 的 app.state.codegraph 死对象冲突。
    # 对标 Aider repo-map 单例 / Cursor codebase index 复用——启动建索引，运行时复用。
    client = None
    try:
        from context import get_app
        _app = get_app()
        if _app and getattr(_app.state, "codegraph", None):
            client = _app.state.codegraph
    except Exception:
        pass  # 回退：拿不到 app 时按"没有 codegraph"处理

    if client is not None:
        try:
            symbols = await client.context(query, max_symbols)
            return ToolResult(
                success=True,
                data={"query": query, "symbols": symbols, "count": len(symbols)},
                observation=f"CodeGraph: 找到 {len(symbols)} 个相关符号",
                meta={"tool_name": "codegraph_context"},
            ).to_dict()
        except Exception:
            # 回退：单例调用失败（如子进程崩溃）——走 search_files fallback
            pass

    # Fallback: CodeGraph 单例不可用 → search_files 自动搜索常见模式
    patterns = []
    q_lower = query.lower()
    if any(w in q_lower for w in ("function", "def", "函数", "func")):
        patterns.append(("def ", "*.py"))
    if any(w in q_lower for w in ("class", "类", "struct")):
        patterns.append(("class ", "*.py"))
    if any(w in q_lower for w in ("import", "导入", "require")):
        patterns.append(("import ", "*.py"))
    if not patterns:
        # 提取 query 中的关键名词作为搜索词
        words = [w for w in query.split() if len(w) > 3]
        if words:
            return ToolResult(
                success=True,
                data={"query": query, "fallback_search": words[0]},
                observation=f"CodeGraph 不可用。建议用 search_files 搜索 '{words[0]}'。",
                meta={"tool_name": "codegraph_context", "fallback": "search_files"},
            ).to_dict()
    return ToolResult(
        success=True,
        data={
            "query": query,
            "suggested_searches": [{"pattern": p, "file_pattern": f} for p, f in patterns],
            "hint": "CodeGraph 不可用。用 search_files 按以下模式搜索：",
        },
        observation=f"CodeGraph 不可用。建议 search_files 搜索: {', '.join(p for p,_ in patterns)}",
        meta={"tool_name": "codegraph_context", "fallback": "search_files"},
    ).to_dict()
