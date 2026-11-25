"""services/memory/history_trim.py —— 避免当前用户消息重复下发

agent_lifecycle 会先把当前 prompt 写入会话历史，而 adapter 的
build_openai_messages(prompt, context) 又会把它作为最后一条 user 追加。
两者叠加会让模型每次请求都看到同一句任务两遍（token 放大、指令重复）。

当前 prompt 始终由调用方单独传入，因此只要历史末尾就是它，就先从历史里摘掉。
"""
from __future__ import annotations


def drop_current_prompt(history: list[dict], prompt: str) -> list[dict]:
    """若 history 末尾正是当前 prompt 的 user 消息，返回去掉它的新列表。"""
    if not history or not prompt:
        return history
    last = history[-1]
    if not isinstance(last, dict):
        return history
    if last.get("role") == "user" and str(last.get("content") or "") == str(prompt):
        return history[:-1]
    return history
