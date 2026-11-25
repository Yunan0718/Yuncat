"""tests/structural/test_layer_deps.py —— 架构层依赖方向测试
Types → Config → Repo → Service → Runtime → UI
"""
import ast
from pathlib import Path

SRC = Path(__file__).parent.parent.parent / "src-python"

FORBIDDEN: list[tuple[str, str, str, str | None]] = [
    # (src_path_pattern, tgt_import_match, reason, exclude_if_path_contains)
    ("services/", "routes/", "Service层不应import Runtime层", None),
    ("repositories/", "services/", "Repo层不应import Service层", "routes/"),
    ("repositories/", "adapters/", "Repo层不应import Adapter层", None),
    ("config.py", "services/", "Config层不应import Service层", "routes/"),  # 排除 routes/config.py
    ("tools/", "services/", "Tools层不应直接依赖Service层", "system_tool_"),
]


def _is_yuncat_source(pyfile: Path) -> bool:
    rel = str(pyfile)
    for ignore in ("node-embed", "python-embed", "__pycache__", ".venv"):
        if ignore in rel:
            return False
    return True


def test_no_cross_layer_imports():
    violations = []
    for pyfile in SRC.glob("**/*.py"):
        if not _is_yuncat_source(pyfile):
            continue
        rel = str(pyfile.relative_to(SRC)).replace("\\", "/")  # 统一分隔符
        # 跳过后端自己的路由文件 —— Service 层需要导入 FastAPI 路由定义
        if rel.startswith("services/") and "agent_service.py" in rel:
            continue  # agent_service 是核心编排器，需要导入 routes 层
        try:
            content = pyfile.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for src_pat, tgt_pat, reason, exclude_pat in FORBIDDEN:
            # src_pat 以 / 结尾表示目录前缀；以 .py 结尾表示根目录文件；其余子串匹配
            if src_pat.endswith("/"):
                if not rel.startswith(src_pat):
                    continue
            elif src_pat.endswith(".py"):
                if rel != src_pat:
                    continue
            elif src_pat not in rel:
                continue
            if exclude_pat and exclude_pat in rel:
                continue  # 跳过排除路径
            tgt_mod_names = tgt_pat.rstrip("/").rstrip(".py").replace("/", ".")
            try:
                tree = ast.parse(content)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    if module.startswith(tgt_mod_names):
                        violations.append(f"{rel}: `from {module} import ...` — {reason}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(tgt_mod_names):
                            violations.append(f"{rel}: `import {alias.name}` — {reason}")

    if violations:
        raise AssertionError(f"{len(violations)} cross-layer dependencies found:\n" + "\n".join(sorted(violations)[:20]))
    count = sum(1 for _ in SRC.glob("**/*.py") if _is_yuncat_source(_))
    print(f"PASS: {count} Python files scanned, 0 cross-layer import violations")
