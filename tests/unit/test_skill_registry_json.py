"""tests/unit/test_skill_registry_json.py — 复现 §1.1.2 _json 未定义 bug

Phase 1.1.2 修复: skill_registry.deep_security_review() 使用 _json.loads()
但文件顶部未 import json as _json，调用即抛 NameError。

本测试验证:
1. 静态检查: skill_registry.py 顶部有 `import json as _json`
2. 静态检查: deep_security_review() 内调用 _json.loads 而非 json.loads
3. 静态检查: 源码中无 `json.loads` 裸调用（应统一走 _json 别名）
4. JSON 提取逻辑（content[find("{"):rfind("}")+1]）存在
"""
import os
import sys
import re

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from config import DEFAULT_AGENT_ID


def _read_source() -> str:
    """读取 skill_registry.py 源码。"""
    reg_path = os.path.join(_SRC, "services", "evolution", "skill_registry.py")
    with open(reg_path, encoding="utf-8") as f:
        return f.read()


def test_json_imported_in_module():
    """静态检查: skill_registry.py 顶部有 import json as _json。"""
    source = _read_source()
    assert "import json as _json" in source, (
        "skill_registry.py must have 'import json as _json' at module level "
        "(deep_security_review uses _json.loads)"
    )


def test_deep_security_review_uses_json_loads():
    """静态检查: deep_security_review() 内调用 _json.loads。"""
    source = _read_source()
    # 定位 deep_security_review 方法
    m = re.search(r"def deep_security_review\(", source)
    assert m, "deep_security_review method not found"
    # 截取方法体（到下一个同级 def 或文件末尾）
    start = m.start()
    # 找到方法后的下一个 def（粗略——找到下一个 def 关键字）
    rest = source[start + 10:]
    next_def = rest.find("\n    def ")
    end = start + 10 + next_def if next_def != -1 else len(source)
    method_body = source[start:end]
    assert "_json.loads" in method_body, (
        "deep_security_review must call _json.loads (not json.loads) "
        "to avoid NameError when module-level import is missing"
    )


def test_no_bare_json_loads_in_module():
    """静态检查: 源码中无 json.loads 裸调用（应统一走 _json 别名）。

    裸 json.loads 在未 import json 的模块中会抛 NameError。
    """
    source = _read_source()
    # 查找 json.loads 但不是 _json.loads 的位置
    # 用负向断言：前面不是 _
    matches = re.findall(r"(?<!_)json\.loads", source)
    assert len(matches) == 0, (
        f"Found {len(matches)} bare json.loads calls——should use _json.loads "
        "(module imports json as _json)"
    )


def test_json_extraction_logic_present():
    """静态检查: JSON 片段提取逻辑存在（容错处理 LLM 返回含前后缀文本）。

    修复方案: content[content.find("{"):content.rfind("}")+1]
    """
    source = _read_source()
    # 检查 find("{") 与 rfind("}") 的组合存在
    assert 'find("{")' in source, "JSON extraction must use find('{') to locate start"
    assert 'rfind("}")' in source, "JSON extraction must use rfind('}') to locate end"


def test_deep_security_review_parses_valid_json():
    """集成测试: 模拟 adapter 返回合法 JSON，验证 _json.loads 解析成功。

    用 Mock adapter 绕过真实 LLM 调用，专注验证 JSON 解析路径不抛 NameError。
    """
    import asyncio
    import pytest
    from services.evolution.skill_models import Skill, SkillPrompt
    from services.evolution.skill_registry import SkillRegistry

    class _MockAdapter:
        async def invoke(self, prompt, context=None):
            return {
                "content": '{"risk_level": "low", "findings": [], "safe_to_install": true}',
            }

    async def _run():
        reg = SkillRegistry(adapters={DEFAULT_AGENT_ID: _MockAdapter()})
        skill = Skill(
            id="test-json",
            title="JSON Test Skill",
            description="Test",
            prompts=[SkillPrompt(text="test prompt")],
        )
        # 修复前: 抛 NameError: name '_json' is not defined
        # 修复后: 正常解析 JSON
        return await reg.deep_security_review(skill, model_config={"api_key": "test"})

    result = asyncio.run(_run())
    assert result.get("reviewed") is True
    assert result.get("risk_level") == "low"
    assert result.get("safe_to_install") is True


def test_deep_security_review_fallback_on_invalid_json():
    """集成测试: 非法 JSON 时降级返回 raw_review。"""
    import asyncio
    from services.evolution.skill_models import Skill, SkillPrompt
    from services.evolution.skill_registry import SkillRegistry

    class _MockAdapter:
        async def invoke(self, prompt, context=None):
            return {"content": "This is not JSON at all, just plain text."}

    async def _run():
        reg = SkillRegistry(adapters={DEFAULT_AGENT_ID: _MockAdapter()})
        skill = Skill(
            id="test-bad-json",
            title="Bad JSON Skill",
            description="Test",
            prompts=[SkillPrompt(text="test")],
        )
        return await reg.deep_security_review(skill, model_config={"api_key": "test"})

    result = asyncio.run(_run())
    # 解析失败但 reviewed=True（已审查，只是无法结构化）
    assert result.get("reviewed") is True
    assert "raw_review" in result
    assert "plain text" in result["raw_review"]
