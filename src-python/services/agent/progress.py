"""services/agent/progress.py —— 进度评估与预估函数（从 agent_tool_loop 拆分）

Phase 86: stall_check 建议文案 + 实质进度判定 + 预估计算。
采用委托模式: 模块级函数接收 self 作为首参（对齐 agent_tool_loop.py 模式）。
"""
import time as _time

# Phase 86: stall_check 建议文案——基于 last_tool 分类
_STALL_SUGGESTIONS = {
    "read_file": "未找到目标内容，建议更换搜索关键词或扩大范围",
    "search_files": "未找到目标内容，建议更换搜索关键词或扩大范围",
    "list_dir": "未找到目标内容，建议更换搜索关键词或扩大范围",
    "grep": "未找到目标内容，建议更换搜索关键词或扩大范围",
    "write_file": "文件写入可能受阻，建议检查路径权限或换路径",
    "edit_file": "文件写入可能受阻，建议检查路径权限或换路径",
    "rename_file": "文件写入可能受阻，建议检查路径权限或换路径",
    "run_command": "命令无输出，建议检查命令参数或换方案",
    "execute_command": "命令无输出，建议检查命令参数或换方案",
}


def _stall_suggestion(last_tool: str | None) -> str:
    """Phase 86: 根据 last_tool 生成 stall 建议文案"""
    if last_tool is None:
        return "连续多轮无工具调用，建议明确下一步动作"
    return _STALL_SUGGESTIONS.get(
        last_tool, "连续 3 轮无实质进度，建议重新分解任务或调整策略",
    )


def _evaluate_round_progress(
    tool_results: list[dict], round_text: str, round_reasoning: str = "",
) -> bool:
    """Phase 86: 判定本轮是否有实质进度——有成功工具调用 OR 文本增长 > 100 字符
    OR 推理(reasoning_content)增长 > 100 字符。

    成功工具判定：tool_result.content 解析为 JSON 后非 {success: False}，
    或 content 为非 JSON 文本（视为有输出）。

    AUDIT #7: 新增 round_reasoning 参数——DeepSeek 等重推理模型在思考阶段
    可能少可见文本(round_text)但累积大量 reasoning_content。原实现只看
    round_text 会导致连续 ≥3 轮纯推理误触发 stall_check。纳入
    reasoning_content 长度判定后，纯推理轮也算有进度。
    """
    if tool_results:
        import json as _json
        for _tr in tool_results:
            # Batch B M8: 防御非 dict 类型的 tool_result（字符串/None 等）导致 AttributeError 崩溃
            if not isinstance(_tr, dict):
                return True  # 非 dict 视为有输出
            _c_raw = _tr.get("content", "")
            if isinstance(_c_raw, list):
                _c_raw = str(_c_raw[0]) if _c_raw else ""
            try:
                _c = _json.loads(_c_raw) if isinstance(_c_raw, str) else _c_raw
                if isinstance(_c, dict) and _c.get("success") is False:
                    continue
                return True
            except Exception:
                return True
    # AUDIT #7: 文本 OR 推理增长 > 100 字符均视为有进度
    if len(round_text) > 100:
        return True
    if len(round_reasoning) > 100:
        return True
    return False


def _compute_estimate(
    loop_started_at: float, tool_round: int, max_rounds: int,
) -> tuple[float, float | None, float]:
    """Phase 86: 计算预估——返回 (elapsed_seconds, estimated_remaining_seconds, confidence)。

    tool_round < 2 时 estimated_remaining 为 None（数据不足）。
    confidence = min(0.9, 0.2 + tool_round * 0.05)，随轮次增长封顶 0.9。
    """
    _elapsed = _time.time() - loop_started_at
    if tool_round >= 2:
        _remaining = _elapsed * (max_rounds - tool_round) / tool_round
    else:
        _remaining = None
    _confidence = min(0.9, 0.2 + tool_round * 0.05)
    return (
        round(_elapsed, 1),
        round(_remaining, 1) if _remaining is not None else None,
        round(_confidence, 2),
    )


def _get_tool_declared_max(tool_name: str) -> int | None:
    """读取工具自我声明的 max_result_size_chars（如 codegraph_context 可声明 500K）。"""
    from tools.registry import get_tool
    meta = get_tool(tool_name)
    return meta.max_result_size_chars if meta else None
