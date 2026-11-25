"""tests/contract/test_phase64_commands.py —— Phase 64 / 命令系统合约测试

验证：
1. 5 个本地命令 YAML 文件存在并加载
2. SlashCommand.local 字段正确序列化
3. /api/v1/commands 路由别名存在
4. SuggestionMenu.tsx 与 InputArea.tsx 集成正确
"""
import os
import sys
import pytest


# ---- 5 个本地命令 YAML 文件存在 ----

def test_5_local_yaml_files_exist():
    builtin = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-python", "commands", "builtin",
    )
    expected = ["clear", "compact", "model", "agent", "help"]
    for name in expected:
        path = os.path.join(builtin, f"{name}.yaml")
        assert os.path.exists(path), f"Missing {name}.yaml"


# ---- Registry 加载 5 个本地命令并标记 local=True ----

def test_registry_has_5_local_commands():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import get_registry
    reg = get_registry()
    expected = ["/clear", "/compact", "/model", "/agent", "/help"]
    for sc in expected:
        cmd = reg.get(sc)
        assert cmd is not None, f"Command '{sc}' not registered"
        assert cmd.local is True, f"Command '{sc}' should be local=True"


def test_registry_total_at_least_19():
    """14 原始 + 5 新增 = 19"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import get_registry
    reg = get_registry()
    cmds = reg.list_all()
    assert len(cmds) >= 19, f"Expected ≥19 commands, got {len(cmds)}"


def test_to_dict_includes_local_field():
    """Phase 64: to_dict() 必须包含 local 字段——前端依赖此标记区分本地/服务端命令"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import SlashCommand
    cmd = SlashCommand({"name": "test", "slash_command": "/test", "local": True})
    d = cmd.to_dict()
    assert "local" in d
    assert d["local"] is True


def test_to_dict_local_defaults_false():
    """未指定 local 时默认 False（向后兼容已有 14 个命令）"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import SlashCommand
    cmd = SlashCommand({"name": "old", "slash_command": "/old"})
    assert cmd.local is False
    assert cmd.to_dict()["local"] is False


# ---- /api/v1/commands 路由别名存在 ----

def test_main_has_commands_endpoint():
    main_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "main.py"),
        encoding="utf-8",
    ).read()
    assert "/api/v1/commands" in main_code, "Phase 64: /api/v1/commands 路由未注册"
    assert "async def list_commands" in main_code


def test_main_keeps_legacy_slash_commands_endpoint():
    """向后兼容——/api/v1/slash-commands 不能删除"""
    main_code = open(
        os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "main.py"),
        encoding="utf-8",
    ).read()
    assert "/api/v1/slash-commands" in main_code


# ---- /agent 命令带 arguments ----

def test_agent_command_has_agent_id_argument():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from commands.registry import get_registry
    reg = get_registry()
    cmd = reg.get("/agent")
    assert cmd is not None
    assert len(cmd.arguments) == 1
    assert cmd.arguments[0]["name"] == "agent_id"


# ---- 前端 SuggestionMenu + InputArea 集成 ----

def test_suggestion_menu_component_exists():
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-frontend", "src",
        "components", "chat", "SuggestionMenu.tsx",
    )
    assert os.path.exists(path), "SuggestionMenu.tsx 未创建"


def test_suggestion_menu_exports_generic_component():
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-frontend", "src",
        "components", "chat", "SuggestionMenu.tsx",
    )
    code = open(path, encoding="utf-8").read()
    assert "export function SuggestionMenu" in code
    assert "SuggestionMenuProps<T>" in code  # 泛型
    # 键盘交互
    assert "ArrowDown" in code and "ArrowUp" in code
    assert "Enter" in code and "Escape" in code


def test_input_area_integrates_suggestion_menu():
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-frontend", "src",
        "components", "chat", "InputArea.tsx",
    )
    code = open(path, encoding="utf-8").read()
    assert "SuggestionMenu" in code
    assert "slashMenu" in code
    assert "handleSlashAutocomplete" in code
    assert "SlashCommandDef" in code


def test_chatview_handles_local_commands():
    # Phase 64 将本地 slash 命令处理抽离到 useSlashCommands hook，
    # ChatView 通过 hook 消费 handleLocalSlashCommand；契约检查目标文件随之迁移。
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-frontend", "src",
        "components", "chat", "useSlashCommands.ts",
    )
    code = open(path, encoding="utf-8").read()
    assert "handleLocalSlashCommand" in code
    assert 'case "/clear"' in code
    assert 'case "/compact"' in code
    assert 'case "/model"' in code
    assert 'case "/agent"' in code
    assert 'case "/help"' in code
    assert "/api/v1/commands" in code


# ---- i18n 翻译 ----

@pytest.mark.parametrize("lang", ["zh", "en", "ja", "ko"])
def test_i18n_has_phase64(lang):
    import json
    path = os.path.join(
        os.path.dirname(__file__), "..", "..", "src-frontend", "src",
        "i18n", "locales", f"{lang}.json",
    )
    data = json.load(open(path, encoding="utf-8"))
    assert "phase64" in data, f"{lang}.json 缺少 phase64 翻译"
    p64 = data["phase64"]
    for key in ["slashHint", "clearDone", "modelPanelOpened",
                "agentUsage", "agentNotFound", "agentSwitched", "helpTitle"]:
        assert key in p64, f"{lang}.json phase64.{key} 缺失"
