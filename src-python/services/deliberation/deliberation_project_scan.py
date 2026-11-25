"""services/deliberation_project_scan.py —— 项目结构扫描

Phase 48 T3-5: 从 deliberation_spec.py 拆分（_get_project_structure +
_detect_key_imports ~83 行），使 deliberation_spec.py 聚焦于 Spec 循环。

纯工具函数，无 self 依赖，可独立测试。
"""
from __future__ import annotations

import os as _os
import re
from pathlib import Path as _Path

from config import (
    PROJECT_SCAN_FILES_PER_DIR_LIMIT,
    PROJECT_SCAN_TOTAL_FILES_LIMIT,
    PROJECT_SCAN_FILES_LIST_LIMIT,
    PROJECT_SCAN_OUTPUT_MAX_CHARS,
    PROJECT_SCAN_TRUNCATE_THRESHOLD_CHARS,
)


def _get_project_structure(workspace_root: str | None = None) -> str:
    """扫描项目目录结构——供 Architect 了解模块划分。

    返回: 树状目录清单 + import 关系摘要（≤3000 字符）。
    对标 OpenAI Ghost Library: Architect 只看到目录骨架，不看到完整代码。
    """
    try:
        from security.sandbox import get_workspace
        root = _Path(workspace_root) if workspace_root else get_workspace()
        root = _Path(str(root))
    except Exception:
        return "（无项目结构信息——请基于功能需求自行设计目录结构）"

    if not root.exists():
        return f"（工作区 {root} 不存在——请基于功能需求自行设计目录结构）"

    lines: list[str] = []
    # 仅扫描 src-python 和 src-frontend 两个核心目录
    for scan_dir_name in ("src-python", "src-frontend"):
        scan_dir = root / scan_dir_name
        if not scan_dir.exists():
            continue

        lines.append(f"### {scan_dir_name}/")
        # 限制深度 3 层，防止输出过长
        for dirpath, dirnames, filenames in _os.walk(str(scan_dir)):
            rel = _Path(dirpath).relative_to(root)
            depth = len(rel.parts)
            if depth > 3:
                dirnames.clear()
                continue
            # 跳过隐藏目录和 venv
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in
                          ("__pycache__", "node_modules", ".venv", "venv",
                           "python-embed", "node-embed", "dist")]
            py_files = [f for f in filenames if f.endswith(".py")]
            if depth <= 2 and py_files:
                indent = "  " * depth
                lines.append(f"{indent}{rel}/")
                for f in sorted(py_files)[:PROJECT_SCAN_FILES_PER_DIR_LIMIT]:  # 每目录最多 N 个文件
                    lines.append(f"{indent}  {f}")
            if len("".join(lines)) > PROJECT_SCAN_TRUNCATE_THRESHOLD_CHARS:
                lines.append("...（项目结构已截断——大型项目请用 list_dir 工具进一步探索）")
                break
    # import 关系摘要——检测关键依赖
    lines.append("\n### Import 关系摘要（跨模块依赖检测）")
    dep_summary = _detect_key_imports(root)
    if dep_summary:
        lines.append(dep_summary)
    lines.append("（以上为项目结构参考——不是让你参考实现，只是了解模块划分）")
    return "\n".join(lines)[:PROJECT_SCAN_OUTPUT_MAX_CHARS]


def _detect_key_imports(root: _Path) -> str:
    """检测关键跨模块 import——供 Architect 理解依赖方向。"""
    key_patterns = [
        (r'from\s+models\.', 'models → '),
        (r'from\s+routes\.', 'routes → '),
        (r'from\s+services\.', 'services → '),
        (r'from\s+adapters\.', 'adapters → '),
        (r'from\s+repositories\b', 'repositories → '),
    ]
    findings: dict[str, set[str]] = {}
    try:
        src_py = root / "src-python"
        if not src_py.exists():
            return ""
        for pyf in list(src_py.glob("**/*.py"))[:PROJECT_SCAN_TOTAL_FILES_LIMIT]:  # 最多 N 个文件
            try:
                content = pyf.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for pattern, label in key_patterns:
                if re.search(pattern, content):
                    findings.setdefault(label, set()).add(pyf.name)
        if not findings:
            return ""
        lines = []
        for label, files in sorted(findings.items()):
            lines.append(f"  {label}{', '.join(sorted(list(files)[:PROJECT_SCAN_FILES_LIST_LIMIT]))}")
        return "\n".join(lines)
    except Exception:
        return ""
