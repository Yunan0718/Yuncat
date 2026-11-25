"""tests/unit/test_skill_registry_metadata.py — 复现 §1.1.1 sp.skill_name 崩溃 bug

Phase 1.1.1 修复: skill_registry.get_active_metadata() 原引用 sp.skill_name /
sp.source_file（SkillPrompt dataclass 无此字段），任何 Skill 激活即抛
AttributeError。改为直接迭代 skills，用 skill.title + skill.install_path/SKILL.md
构造挂载点。

本测试验证:
1. 启用带 prompt 的 skill 后 get_active_metadata() 不崩溃（返回 str）
2. 返回的 Markdown 含 skill.title 与挂载点路径
3. install_path 为空时挂载点降级为 '?' 而非崩溃
4. 多 prompt 的 skill 只取首个匹配 prompt
5. disabled skill 不出现
"""
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from services.evolution.skill_models import Skill, SkillPrompt
from services.evolution.skill_registry import SkillRegistry


def _make_skill(title: str, prompts: list[SkillPrompt], enabled: bool = True,
                install_path: str = "") -> Skill:
    return Skill(
        id=f"test-{title}",
        title=title,
        description=f"Test skill {title}",
        prompts=prompts,
        enabled=enabled,
        install_path=install_path,
    )


def test_get_active_metadata_does_not_crash():
    """启用 skill + prompt 后 get_active_metadata() 不抛 AttributeError。"""
    reg = SkillRegistry()
    # SkillPrompt 无 scope 字段——空 scope_tasks 匹配全部
    sp = SkillPrompt(text="You are a helpful assistant.", priority=0)
    skill = _make_skill("TestSkill", [sp], install_path="/tmp/test-skill")
    reg._skills[skill.id] = skill

    # 修复前: 抛 AttributeError: 'SkillPrompt' object has no attribute 'skill_name'
    # 修复后: 返回 Markdown 字符串
    metadata = reg.get_active_metadata()
    assert isinstance(metadata, str)
    assert "TestSkill" in metadata


def test_get_active_metadata_returns_correct_fields():
    """返回的 Markdown 含 title 与挂载点路径。"""
    reg = SkillRegistry()
    sp = SkillPrompt(text="Test prompt text.", priority=5)
    skill = _make_skill("FieldTest", [sp], install_path="/tmp/field-test")
    reg._skills[skill.id] = skill

    metadata = reg.get_active_metadata()
    assert isinstance(metadata, str)
    # 修复后: 用 skill.title 而非 sp.skill_name
    assert "FieldTest" in metadata
    # 修复后: 挂载点 = install_path/SKILL.md
    assert "SKILL.md" in metadata
    assert "Test prompt text." in metadata


def test_get_active_metadata_empty_install_path():
    """install_path 为空时挂载点降级为 '?' 而非崩溃。"""
    reg = SkillRegistry()
    sp = SkillPrompt(text="No path prompt.", priority=0)
    skill = _make_skill("NoPath", [sp], install_path="")
    reg._skills[skill.id] = skill

    # 修复前: sp.source_file 不存在 → AttributeError
    # 修复后: install_path 为空 → source_file="" → 显示 '?'
    metadata = reg.get_active_metadata()
    assert isinstance(metadata, str)
    assert "NoPath" in metadata
    assert "?" in metadata  # 挂载点占位符


def test_get_active_metadata_takes_first_match_per_skill():
    """多 prompt 的 skill 只取首个匹配 prompt（按 priority 降序后取首）。"""
    reg = SkillRegistry()
    # priority 高的排前面——sp1 priority=10 应被选中
    sp1 = SkillPrompt(text="First match.", priority=10)
    sp2 = SkillPrompt(text="Second match.", priority=5)
    skill = _make_skill("MultiPrompt", [sp1, sp2])
    reg._skills[skill.id] = skill

    metadata = reg.get_active_metadata()
    assert "First match." in metadata
    # 第二个 prompt 不应出现（每个 skill 只取首个匹配）
    assert "Second match." not in metadata


def test_get_active_metadata_skips_disabled_skills():
    """disabled skill 的 prompt 不出现在 metadata 中。"""
    reg = SkillRegistry()
    sp = SkillPrompt(text="Should not appear.", priority=0)
    skill = _make_skill("Disabled", [sp], enabled=False)
    reg._skills[skill.id] = skill

    metadata = reg.get_active_metadata()
    # 无任何匹配 → 返回空串
    assert metadata == ""
    assert "Should not appear." not in metadata


def test_get_active_metadata_static_check_no_sp_attribute():
    """静态检查: 源码中不再引用 sp.skill_name / sp.source_file。

    用 ast 解析排除 docstring/comment 中的文本引用，只检查真实代码。
    """
    import ast
    reg_path = os.path.join(_SRC, "services", "evolution", "skill_registry.py")
    with open(reg_path, encoding="utf-8") as f:
        source = f.read()
    # 用 ast 解析，遍历所有 Attribute 节点
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "sp":
                if node.attr in ("skill_name", "source_file"):
                    violations.append(f"line {node.lineno}: sp.{node.attr}")
    assert len(violations) == 0, (
        "get_active_metadata must not reference sp.skill_name / sp.source_file "
        f"(SkillPrompt has no such fields): {violations}"
    )
