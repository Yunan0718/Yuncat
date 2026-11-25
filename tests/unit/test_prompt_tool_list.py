"""tests/unit/test_prompt_tool_list.py —— 提示词里的工具清单必须来自注册表

对标 deepseek-harness 的 PromptAssembly 原则：给模型看的"我能用什么"只能有一处来源。
Yuncat 的 native agent 请求里**不带 wire 级 tools 字段**（context["tools"] 无人赋值），
所以提示词里这份清单就是模型唯一的工具认知——漏一个工具，模型就永远不会调它。
上一轮加 browser_* 工具时正是靠手工补提示词才没有漏，这种"靠人记着"的做法必须有守卫。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from prompts.commander import render_commander_prompt
from tools.builtin import system_tools  # noqa: F401  触发内置工具注册
from tools.registry import get_all_tools


def test_prompt_lists_every_owned_tool():
    tools = get_all_tools()
    # 原生 agent 与 list_tools 同语义：注册表里的工具它都能执行，提示词必须全列
    owned = sorted(tools)
    assert len(owned) > 20, f"工具太少（{len(owned)}），守卫会形同虚设"

    prompt = render_commander_prompt(agent_id="yuncat")
    missing = [name for name in owned if name not in prompt]
    assert not missing, f"这些工具已注册但没出现在提示词里（模型不会知道它们存在）：{missing}"


def test_prompt_tool_list_is_generated_not_hardcoded():
    """清单段落里应当能看到注册表里的工具名，而不是固定的十几个名字。"""
    prompt = render_commander_prompt(agent_id="yuncat")
    assert "可用工具:" in prompt
    for name in ("read_file", "run_command", "browser_navigate", "store_memory"):
        assert name in prompt, f"提示词缺少 {name}"
