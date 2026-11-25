"""tests/contract/test_phase27_commands.py —— Phase 27 Slash Command + Behavior 合约测试"""
import os
import sys
import pytest


# ---- Slash Command Registry ----

def test_registry_loads_builtin():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import get_registry, SlashCommand
    reg = get_registry()
    cmds = reg.list_all()
    assert len(cmds) >= 12, f"Expected ≥12 commands, got {len(cmds)}"


def test_registry_has_all_core_commands():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import get_registry
    reg = get_registry()
    expected = ["/plan", "/review", "/security", "/tdd", "/fix",
                "/coverage", "/clean", "/gate", "/pr", "/docs", "/multi", "/snapshot"]
    for sc in expected:
        assert reg.get(sc) is not None, f"Command '{sc}' not registered"


def test_registry_match():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import get_registry
    reg = get_registry()
    cmd = reg.match("/review src/main.py")
    assert cmd is not None
    assert cmd.slash_command == "/review"


def test_registry_match_no_match():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import get_registry
    reg = get_registry()
    assert reg.match("普通聊天消息") is None


def test_slash_command_extract_args():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import get_registry
    reg = get_registry()
    cmd = reg.get("/review")
    args = cmd.extract_args("/review --scope src/main.py")
    assert args.get("scope") == "src/main.py"


def test_slash_command_dataclass():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import SlashCommand
    cmd = SlashCommand({"name": "test", "slash_command": "/test",
                         "description": "desc", "category": "core"})
    assert cmd.to_dict()["name"] == "test"


# ---- 12 YAML files ----

def test_all_12_yaml_files_exist():
    builtin = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-python", "commands", "builtin",
    )
    expected = ["plan", "review", "security", "tdd", "fix", "coverage",
                "clean", "gate", "pr", "docs", "multi", "snapshot"]
    for name in expected:
        path = os.path.join(builtin, f"{name}.yaml")
        assert os.path.exists(path), f"Missing {name}.yaml"


# ---- Behavior Prompts ----

def test_behaviors_directory_exists():
    beh_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-python", "prompts", "behaviors",
    )
    assert os.path.isdir(beh_dir)


def test_behaviors_files_exist():
    beh_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-python", "prompts", "behaviors",
    )
    for fname in ["general.yaml", "python.yaml", "typescript.yaml", "golang.yaml"]:
        assert os.path.exists(os.path.join(beh_dir, fname)), f"Missing {fname}"


def test_behaviors_total_count():
    """42 条 behavior prompts (general:14 + python:10 + ts:10 + go:8)"""
    import yaml
    beh_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-python", "prompts", "behaviors",
    )
    total = 0
    for fname in ["general.yaml", "python.yaml", "typescript.yaml", "golang.yaml"]:
        with open(os.path.join(beh_dir, fname), encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
            total += len(data)
    assert total >= 40, f"Expected ≥40 behaviors, got {total}"


# ---- Commander prompt behavior loading ----

def test_commander_has_behavior_loading():
    cmdr = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "prompts", "commander.py"),
        encoding="utf-8",
    ).read()
    assert "_load_behaviors" in cmdr
    assert "_detect_project_language" in cmdr


# ---- main.py ----

def test_main_has_slash_commands():
    # M1 拆分后 lifespan.py 为薄编排层，slash command 注册表初始化落到
    # lifespan_postinit.py（_init_commands 等委托链），WS 路由在 routes/ws.py
    main_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "main.py"),
        encoding="utf-8",
    ).read()
    ls_code = open(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "src-python", "lifespan_postinit.py",
        ),
        encoding="utf-8",
    ).read()
    ws_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "routes", "ws.py"),
        encoding="utf-8",
    ).read()
    assert "slash_commands" in main_code  # 端点注册
    assert "get_registry" in ls_code       # M1: 注册表初始化在 lifespan_postinit.py
    assert "/api/v1/slash-commands" in main_code  # 端点路径
    assert "slash_command_matched" in ws_code  # M1: WS 处理已移至 routes/ws.py


# ---- REFERENCES.md ----

def test_references_exists():
    ref_path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "REFERENCES.md")
    assert os.path.exists(ref_path)


def test_references_has_7_projects():
    ref_path = os.path.join(os.path.dirname(__file__), "..", "..", "docs", "REFERENCES.md")
    content = open(ref_path, encoding="utf-8").read()
    projects = ["Jean", "Mozzie", "Commander", "Sandcastle", "open-cowork", "AnySpace", "TeamClaw"]
    for proj in projects:
        assert proj in content, f"REFERENCES.md missing project: {proj}"
    # 每个项目有链接
    assert content.count("github.com") >= 5
