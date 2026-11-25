"""tests/unit/test_history_trim.py — 当前用户消息不得重复下发

agent_lifecycle 先写入当前 prompt，adapter 又追加 prompt；若不 trim，
模型每次请求会看到同一句任务两遍。
"""
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src-python"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from adapters.base import build_openai_messages  # noqa: E402
from services.memory.history_trim import drop_current_prompt  # noqa: E402


def test_drops_trailing_current_prompt():
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "写一个游戏"},
    ]
    trimmed = drop_current_prompt(history, "写一个游戏")
    assert [m["content"] for m in trimmed] == ["old", "ok"]


def test_keeps_history_when_last_is_different():
    history = [{"role": "user", "content": "上一句"}]
    assert drop_current_prompt(history, "新任务") == history


def test_keeps_intentional_repeat_of_earlier_message():
    history = [
        {"role": "user", "content": "继续"},
        {"role": "user", "content": "继续"},
    ]
    trimmed = drop_current_prompt(history, "继续")
    assert len(trimmed) == 1


def test_wire_messages_contain_prompt_once():
    history = [{"role": "user", "content": "写一个游戏"}]
    trimmed = drop_current_prompt(history, "写一个游戏")
    wire = build_openai_messages("写一个游戏", {"history": trimmed})
    assert [m["content"] for m in wire] == ["写一个游戏"]
