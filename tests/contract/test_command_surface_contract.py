"""tests/contract/test_command_surface_contract.py —— 命令入口的接线（slash / 快捷键 / 驾驶舱）

这三个入口都出过“组件在、但没人接上”的问题：
驾驶舱抽屉没有任何地方渲染、命令面板默认快捷键为空导致键盘打不开、
slash 菜单只做前缀匹配。下面每条断言对应一个真实用户可见行为。
"""
import json
import os

_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
_SRC_FE = os.path.join(_ROOT, "src-frontend")
_LOCALES = ["zh", "en", "ja", "ko"]


def _read(*parts: str) -> str:
    with open(os.path.join(_SRC_FE, "src", *parts), encoding="utf-8") as f:
        return f.read()


def test_slash_menu_uses_fuzzy_ranking():
    input_area = _read("components", "chat", "InputArea.tsx")
    assert "rankSlashCommands(" in input_area, "slash 菜单应用模糊排序"
    assert ".toLowerCase().includes(slashMenu.query)" not in input_area, (
        "不能回到只做子串过滤"
    )


def test_suggestion_menu_height_follows_available_space():
    menu = _read("components", "chat", "SuggestionMenu.tsx")
    assert "maxHeight" in menu, "菜单高度应按可用空间自适应"
    assert "getBoundingClientRect().top" in menu, "应量输入框上方剩下多少空间"


def test_command_palette_has_a_default_shortcut():
    shortcuts = _read("lib", "shortcuts.ts")
    assert 'id: "command_palette"' in shortcuts
    line = next(l for l in shortcuts.splitlines() if 'id: "command_palette"' in l)
    assert 'defaultKeys: ""' not in line, (
        "命令面板默认快捷键为空时键盘打不开面板"
    )


def test_cockpit_is_mounted_and_reachable():
    title_bar = _read("components", "TitleBar.tsx")
    assert "<LongTaskSupervisorDrawer />" in title_bar, (
        "驾驶舱抽屉必须真的被渲染，否则点了没反应"
    )
    chat = _read("components", "chat", "ChatView.tsx")
    assert "setSupervisorDrawerOpen(true)" in chat, "对话区入口应能打开驾驶舱"
    assert "if (entries.length === 0) return;" not in chat, (
        "没有长任务时不能点了没反应"
    )
    palette = _read("components", "CommandPalette.tsx")
    assert "open-supervisor" in palette, "命令面板里也应有一个入口"


def test_cockpit_entry_has_wording():
    for lang in _LOCALES:
        path = os.path.join(_SRC_FE, "src", "i18n", "locales", f"{lang}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for key in ("openSupervisor", "supervisorKeyword"):
            assert data.get("cmd", {}).get(key, "").strip(), f"{lang}.json cmd.{key} 缺文案"


def test_user_commands_are_actually_loaded(tmp_path, monkeypatch):
    """用户自定义命令目录必须真的被加载——load_user() 之前从没被调用过"""
    from commands import registry as reg

    user_dir = tmp_path / "commands"
    user_dir.mkdir()
    (user_dir / "hello.yaml").write_text(
        "name: hello\nslash_command: /hello\ndescription: 自定义命令\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("YUNCAT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(reg, "_registry", None)

    registry = reg.get_registry()

    assert reg.user_commands_dir() == str(user_dir)
    assert registry.get("/hello") is not None, "用户命令没加载进来"
    assert registry.get("/compact") is not None, "内置命令不能因此丢失"
