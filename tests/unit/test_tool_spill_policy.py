"""工具结果 spill 策略：read 豁免、精确省略字节和落盘失败降级。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.agent import post_process  # noqa: E402


class _Service:
    def __init__(self):
        self.events = []

    async def _safe_send(self, _task_id, payload):
        self.events.append(payload)


def _result(text: str) -> str:
    return json.dumps({
        "success": True,
        "observation": "",
        "meta": {"tool_name": "run_command"},
        "data": {"output": text},
    }, ensure_ascii=False)


async def _run(tool_name: str, payload: str, monkeypatch):
    monkeypatch.setattr(
        post_process, "calc_tool_result_limit_with_declaration",
        lambda *_args, **_kwargs: 180,
    )
    return await post_process._post_process_tool_result(
        _Service(), {"id": "call-1"}, tool_name, {}, payload,
        "task-spill-policy", "yuncat", {}, None, {}, set(), 1, 0,
    )


@pytest.mark.asyncio
async def test_read_file_is_not_retruncated_by_generic_spill(monkeypatch):
    payload = _result("正文-" + "甲" * 500)
    result = await _run("read_file", payload, monkeypatch)
    assert result["content"] == payload
    assert not result.get("truncated")


@pytest.mark.asyncio
async def test_non_read_result_reports_exact_omitted_bytes(monkeypatch):
    payload = _result("错误\n" + "乙" * 1000 + "\n完成")
    result = await _run("run_command", payload, monkeypatch)
    retained = result["content"].split("\n\n[结果已摘要", 1)[0]
    expected_omitted = len(payload.encode("utf-8")) - len(retained.encode("utf-8"))
    assert result["truncated"] is True
    assert result["omitted_bytes"] == expected_omitted
    assert f"省略 {expected_omitted} bytes" in result["content"]
    assert "request_more_context" in result["content"]


@pytest.mark.asyncio
async def test_spill_save_failure_keeps_inline_result(monkeypatch):
    payload = _result("失败输出\n" + "x" * 1000)
    monkeypatch.setattr("services.tool_spill.save_spill", lambda *_args: None)
    result = await _run("run_command", payload, monkeypatch)
    assert result["truncated"] is True
    assert "request_more_context" in result["content"]
