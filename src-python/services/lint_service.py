"""lint_service.py —— 共享 lint 逻辑（hook + 路由共用）

E3: PostToolUse hook 和 /lint 路由都调用此服务，避免 ruff 解析逻辑重复。
hook 用 async 版本（不阻塞工具循环），路由用 sync 版本（FastAPI 线程池）。
CodeWhale 借鉴: TypeScript lint + 诊断严重度过滤 + 按文件截断。
"""
import asyncio
import json as _json
import logging
import os
import re
import subprocess
from pathlib import Path

from config import (
    WORKSPACE_LINT_TIMEOUT,
    WORKSPACE_TSC_TIMEOUT,
    LINT_INCLUDE_WARNINGS,
    LINT_MAX_DIAGS_PER_FILE,
    LINT_TS_ENABLED,
)

_log = logging.getLogger("yuncat.lint_service")

# tsc 输出行格式: file(line,col): error TSxxxx: message
_TSC_DIAG_RE = re.compile(
    r"^(.+?)\((\d+),(\d+)\):\s+(error|warning)\s+(TS\d+):\s+(.+)$"
)


def detect_language(path: str) -> str:
    """从文件扩展名推断 lint 语言。返回 "" 表示不支持。"""
    ext = Path(path).suffix.lower()
    if ext == ".py":
        return "python"
    if ext in (".js", ".jsx", ".ts", ".tsx"):
        return "typescript"
    return ""


def parse_ruff_json(stdout: str) -> list[dict]:
    """解析 ruff --format=json 输出为标准诊断列表。

    返回 [{line, col, message, severity, code}]——字段对齐 Monaco IMarkerData。
    """
    if not stdout or not stdout.strip():
        return []
    try:
        ruff_results = _json.loads(stdout)
    except (ValueError, TypeError):
        return []
    results: list[dict] = []
    for item in ruff_results:
        loc = item.get("location", {})
        code = item.get("code", "")
        # severity 基于 fix 可用性——有 fix → warning（agent 可自动修复，低优先级），
        # 无 fix → error（需 agent 手动处理，高优先级）。
        # 这是面向 agent 的优先级分类，非 ruff 规则本身的 severity 等级。
        # 注意: 用 `is not None` 而非 truthiness——`fix: {}` 表示有 fix 字段（可自动修复）。
        if item.get("fix") is not None:
            severity = "warning"
        else:
            severity = "error"
        results.append({
            "line": loc.get("row", 1),
            "col": loc.get("column", 1),
            "message": item.get("message", ""),
            "severity": severity,
            "code": code,
        })
    return results


async def lint_python_file(full_path: str, cwd: str = "") -> list[dict]:
    """异步执行 ruff check 并返回诊断列表。

    单文件粒度，快速——适合 PostToolUse hook 实时触发。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ruff", "check", "--format=json", full_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=WORKSPACE_LINT_TIMEOUT)
        if proc.returncode in (0, 1) and stdout:
            return parse_ruff_json(stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        _log.debug("ruff not found")
    except asyncio.TimeoutError:
        _log.debug("ruff check timed out for %s", full_path)
    except Exception:
        _log.debug("lint failed for %s", full_path, exc_info=True)
    return []


def lint_python_file_sync(full_path: str, cwd: str = "") -> list[dict]:
    """同步版 ruff check——供 /lint 路由复用解析逻辑。"""
    try:
        proc = subprocess.run(
            ["ruff", "check", "--format=json", full_path],
            capture_output=True, text=True, timeout=WORKSPACE_LINT_TIMEOUT, cwd=cwd or None,
        )
        if proc.returncode in (0, 1) and proc.stdout.strip():
            return parse_ruff_json(proc.stdout)
    except FileNotFoundError:
        _log.debug("ruff not found")
    except Exception:
        _log.debug("lint failed for %s", full_path, exc_info=True)
    return []


# ============================================================
# CodeWhale 借鉴: TypeScript lint + 诊断过滤
# ============================================================

def parse_tsc_output(stdout: str, target_file: str = "") -> list[dict]:
    """解析 tsc --pretty false 输出为标准诊断列表。

    tsc 格式: file(line,col): error TSxxxx: message
    target_file: 若提供，只返回该文件的诊断（相对路径匹配）。
    返回 [{line, col, message, severity, code}]——字段对齐 ruff 解析结果。
    """
    if not stdout or not stdout.strip():
        return []
    results: list[dict] = []
    target_norm = target_file.replace("\\", "/") if target_file else ""
    for line in stdout.strip().splitlines():
        m = _TSC_DIAG_RE.match(line.strip())
        if not m:
            continue
        file_path, line_no, col_no, severity, code, message = m.groups()
        # 过滤到目标文件——tsc 检查整个项目，只需当前编辑文件的诊断
        if target_norm:
            file_norm = file_path.replace("\\", "/")
            if not file_norm.endswith(target_norm) and not target_norm.endswith(file_norm):
                continue
        results.append({
            "line": int(line_no),
            "col": int(col_no),
            "message": message.strip(),
            "severity": severity,  # "error" or "warning"
            "code": code,
        })
    return results


async def lint_typescript_file(full_path: str, cwd: str = "") -> list[dict]:
    """异步执行 tsc --noEmit 并返回该文件的诊断列表。

    CodeWhale 借鉴: 失败完全静默——tsc 不存在/超时/崩溃时返回空列表，永不阻塞 Agent。
    使用 --incremental 利用 .tsbuildinfo 缓存加速后续检查。
    """
    if not LINT_TS_ENABLED:
        return []
    try:
        # tsc 需要在前端项目根目录运行（tsconfig.json 所在目录）
        tsc_cwd = cwd or os.path.dirname(full_path)
        # 向上查找 tsconfig.json
        check_dir = Path(tsc_cwd)
        tsconfig_found = False
        for _ in range(5):
            if (check_dir / "tsconfig.json").exists():
                tsc_cwd = str(check_dir)
                tsconfig_found = True
                break
            if check_dir.parent == check_dir:
                break
            check_dir = check_dir.parent
        if not tsconfig_found:
            _log.debug("No tsconfig.json found near %s, skipping tsc", full_path)
            return []
        # 获取相对 tsc_cwd 的文件路径用于过滤
        try:
            rel_file = str(Path(full_path).resolve().relative_to(Path(tsc_cwd).resolve()))
        except (ValueError, OSError):
            rel_file = os.path.basename(full_path)
        proc = await asyncio.create_subprocess_exec(
            "npx", "tsc", "--noEmit", "--pretty", "false", "--incremental",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=tsc_cwd,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=WORKSPACE_TSC_TIMEOUT)
        # tsc 将类型错误输出到 stdout，退出码 1 表示有错误
        if stdout:
            return parse_tsc_output(stdout.decode("utf-8", errors="replace"), rel_file)
    except FileNotFoundError:
        _log.debug("npx/tsc not found")
    except asyncio.TimeoutError:
        _log.debug("tsc check timed out for %s", full_path)
    except Exception:
        _log.debug("tsc lint failed for %s", full_path, exc_info=True)
    return []


def filter_and_truncate_diagnostics(
    diags: list[dict],
    include_warnings: bool = LINT_INCLUDE_WARNINGS,
    max_per_file: int = LINT_MAX_DIAGS_PER_FILE,
) -> list[dict]:
    """CodeWhale 借鉴: 诊断严重度过滤 + 按文件截断。

    - include_warnings=False 时只保留 error（减少上下文噪音）
    - 截断前按 severity 排序（error 优先，然后 warning）
    - 每文件最多 max_per_file 条
    """
    if not diags:
        return []
    # 严重度过滤
    if not include_warnings:
        filtered = [d for d in diags if d.get("severity") != "warning"]
    else:
        filtered = list(diags)
    # 按 severity 排序——error 优先
    severity_order = {"error": 0, "warning": 1, "info": 2, "hint": 3}
    filtered.sort(key=lambda d: severity_order.get(d.get("severity", "error"), 0))
    # 截断
    return filtered[:max_per_file]
