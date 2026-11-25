"""tests/contract/test_trajectory_contract.py —— 轨迹卡片（一条回复一张卡片）契约

Phase 71 的 ThinkingBlock.tsx / ExecutionTimeline.tsx 已在 2d1b221 被
TrajectoryCard.tsx + lib/trajectory.ts 取代，本文件接替
test_phase71_thinking_timeline.py 的守卫职责：

1. lib/trajectory.ts 导出 projectTrajectory，并支持 i18n 文案函数（缺省中文兜底）
2. TrajectoryCard.tsx 一条回复一张卡片：过程行折叠 + 正文挂卡片内 + 耗时
3. taskStore.ts 工具结果写入 startedAt/finishedAt，且 tool_start 事件时间戳优先
4. wsHandlers/toolHandlers.ts 记录 tool_start 时间戳（_toolStartAt Map）
5. 4 语言 i18n 有 trajectory.* keys
"""
import json
import os
import re

_SRC_FE = os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend")
_TRAJECTORY = os.path.join(_SRC_FE, "src", "lib", "trajectory.ts")
_CARD = os.path.join(_SRC_FE, "src", "components", "chat", "TrajectoryCard.tsx")
_MESSAGE_ITEM = os.path.join(_SRC_FE, "src", "components", "chat", "MessageItem.tsx")
_TASK_STORE = os.path.join(_SRC_FE, "src", "store", "taskStore.ts")
_WS_TOOL_HANDLERS = os.path.join(_SRC_FE, "src", "hooks", "wsHandlers", "toolHandlers.ts")
_I18N_DIR = os.path.join(_SRC_FE, "src", "i18n", "locales")
_LOCALES = ["zh", "en", "ja", "ko"]
_TRAJECTORY_KEYS = {"thinking", "plan", "error", "message"}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_trajectory_projection_is_exported():
    """lib/trajectory.ts 导出 projectTrajectory（纯函数投影）"""
    code = _read(_TRAJECTORY)
    assert re.search(r"export\s+function\s+projectTrajectory\s*\(", code), (
        "lib/trajectory.ts 应导出 projectTrajectory()"
    )


def test_trajectory_projection_accepts_translate():
    """projectTrajectory 接受 i18n 文案函数，保证行标题可翻译"""
    code = _read(_TRAJECTORY)
    assert "TrajectoryTranslate" in code, "应声明 TrajectoryTranslate 文案函数类型"
    assert re.search(r"projectTrajectory\(\s*input:[^)]*,\s*t\??:", code), (
        "projectTrajectory 第二个参数应为可选文案函数"
    )


def test_message_item_passes_translate():
    """MessageItem.tsx 将 t 传给 projectTrajectory（行标题走 i18n）"""
    code = _read(_MESSAGE_ITEM)
    assert "projectTrajectory(task, t)" in code, (
        "MessageItem 应传 t，否则轨迹行标题永远是中文兜底值"
    )


def test_trajectory_card_is_one_card_per_reply():
    """TrajectoryCard.tsx：一张卡片，正文作为 children 挂在卡片内"""
    code = _read(_CARD)
    assert 'data-testid="trajectory-card"' in code, "卡片应带 trajectory-card 测试锚点"
    assert re.search(r"kind !== \"message\"", code), "过程行应排除正文"
    assert "children" in code, "正文应作为 children 渲染在卡片内"


def test_trajectory_row_shows_duration():
    """TrajectoryRow 在折叠行上显示耗时（1.2s / 850ms）"""
    code = _read(_CARD)
    assert "durationMs" in code, "行上应读 durationMs"
    assert re.search(r"durationMs\s*>=\s*1000", code), "应区分秒/ms 两种格式"


def test_task_store_tool_result_has_timestamps():
    """taskStore.ts 工具结果带 startedAt/finishedAt，且事件时间戳优先"""
    code = _read(_TASK_STORE)
    assert "finishedAt: Date.now()" in code, "appendToolResult 应写 finishedAt"
    assert re.search(r"startedAtFromEvent\s*\?\?\s*\(", code), (
        "tool_start 事件时间戳应优先于按序配对的回退值"
    )


def test_ws_records_tool_start_timestamp():
    """toolHandlers.ts 记录 tool_start 时间戳，结果到达时取出"""
    code = _read(_WS_TOOL_HANDLERS)
    assert "_toolStartAt" in code, "应用 _toolStartAt Map 记录工具开始时间"
    assert "toolStartKey" in code, "应有 toolStartKey 配对函数"
    assert re.search(r"export\s+const\s+toolStart[\s\S]*?_toolStartAt\.set", code), (
        "tool_start handler 应调 _toolStartAt.set"
    )
    assert re.search(r"export\s+const\s+toolResultPreview[\s\S]*?_toolStartAt\.get", code), (
        "tool_result_preview handler 应调 _toolStartAt.get"
    )


def test_i18n_locales_have_trajectory_keys():
    """4 语言 i18n 均有 trajectory.* keys（行标题不再硬编码中文）"""
    for lang in _LOCALES:
        path = os.path.join(_I18N_DIR, f"{lang}.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        group = data.get("trajectory", {})
        assert isinstance(group, dict), f"{lang}.json trajectory 应为对象"
        missing = _TRAJECTORY_KEYS - set(group.keys())
        assert not missing, f"{lang}.json trajectory 缺少 keys: {missing}"
        for key, value in group.items():
            assert isinstance(value, str) and value.strip(), (
                f"{lang}.json trajectory.{key} 应为非空字符串"
            )
