"""config_stop_hooks.py —— 项目技术栈检测 + stop_hook 命令生成

Phase 47 修复（阶段 3 拆分）：从 config.py 抽出。
对标 Claude Code / Cursor / Codex CLI 的项目类型识别 + 验证命令生成。
"""
from __future__ import annotations

import os


def detect_project_tech_stacks(workspace: str | os.PathLike) -> list[str]:
    """扫描工作区，返回检测到的技术栈标签列表。

    主流做法：Claude Code / Cursor / Codex CLI 都会在执行验证前检测项目类型，
    不会对所有项目都跑 tsc / pytest。例如 Codex CLI 的 envinfo 会识别语言环境。
    """
    root = os.path.abspath(str(workspace))
    stacks: list[str] = []
    if os.path.exists(os.path.join(root, "package.json")):
        stacks.append("node")
        if os.path.exists(os.path.join(root, "tsconfig.json")):
            stacks.append("typescript")
    if os.path.exists(os.path.join(root, "pyproject.toml")) or \
       os.path.exists(os.path.join(root, "requirements.txt")) or \
       os.path.exists(os.path.join(root, "setup.py")):
        stacks.append("python")
    if os.path.exists(os.path.join(root, "Cargo.toml")):
        stacks.append("rust")
    if os.path.exists(os.path.join(root, "go.mod")):
        stacks.append("go")
    if os.path.exists(os.path.join(root, "pom.xml")) or \
       os.path.exists(os.path.join(root, "build.gradle")) or \
       os.path.exists(os.path.join(root, "build.gradle.kts")):
        stacks.append("java")
    return stacks


def build_stop_hook_commands(workspace: str | os.PathLike) -> list[dict]:
    """根据工作区技术栈生成验证命令列表。

    返回: [{"hook": "typecheck", "command": "...", "cwd": "..."}, ...]

    规则（对标主流 CLI 默认行为）:
      - TypeScript: npx tsc --noEmit（优先）
      - Node (JS only): npm test 或 npm run lint（如果存在）
      - Python: pytest -q（存在 tests/ 或 pyproject.toml 有测试配置）
      - Rust: cargo check
      - Go: go build ./...
      - Java: ./gradlew test 或 mvn test

    命令按"先类型检查/编译，再测试"排序；找不到则跳过，不阻塞。
    """
    root = os.path.abspath(str(workspace))
    stacks = detect_project_tech_stacks(root)
    commands: list[dict] = []

    if "typescript" in stacks:
        commands.append({"hook": "typecheck", "command": "npx tsc --noEmit", "cwd": root})
    elif "node" in stacks:
        # JS 项目没有类型检查，尝试 lint
        commands.append({"hook": "lint", "command": "npx eslint . --max-warnings=0", "cwd": root})

    if "python" in stacks:
        has_tests = os.path.isdir(os.path.join(root, "tests")) or \
                    os.path.isdir(os.path.join(root, "test"))
        if has_tests:
            commands.append({"hook": "unit_tests", "command": "pytest -q --timeout=30", "cwd": root})
        # 额外跑 ruff（如果项目配置了 ruff）
        pyproject = os.path.join(root, "pyproject.toml")
        if os.path.exists(pyproject):
            try:
                with open(pyproject, encoding="utf-8") as f:
                    if "ruff" in f.read():
                        commands.append({"hook": "lint", "command": "ruff check .", "cwd": root})
            except OSError:
                pass

    if "rust" in stacks:
        commands.append({"hook": "compile", "command": "cargo check", "cwd": root})

    if "go" in stacks:
        commands.append({"hook": "compile", "command": "go build ./...", "cwd": root})

    if "java" in stacks:
        if os.path.exists(os.path.join(root, "gradlew")):
            commands.append({"hook": "unit_tests", "command": "./gradlew test", "cwd": root})
        elif os.path.exists(os.path.join(root, "pom.xml")):
            commands.append({"hook": "unit_tests", "command": "mvn test -q", "cwd": root})

    return commands
