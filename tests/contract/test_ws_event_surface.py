"""后端 WS 事件 ↔ 前端 handler 的面对账守卫。

问题形态（2026-10-22 实测两例）：后端发了、前端没注册 handler，消息被静默丢弃——
不报错、不掉线、界面只是"少了一件事"：
- `tool_output_chunk`：命令实时输出只存在于后端；
- `memory_saved`：`#记住` 写完记忆，界面毫无反馈。

正向的"前端注册了 handler"不查——前端可能处理来自别处（Electron 主进程、
工作区/LSP 通道）的事件。反向的"后端发了、前端没接"必须查：每一条要么前端处理，
要么在 `scripts/ws_event_baseline.json` 里写明原因。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

from ws_event_scan import collect_backend_events, collect_frontend_events

BASELINE = os.path.join(
    os.path.dirname(__file__), "..", "..", "scripts", "ws_event_baseline.json",
)


def _baseline_events() -> dict:
    with open(BASELINE, encoding="utf-8") as fh:
        return json.load(fh)["events"]


def _unhandled() -> set:
    backend = set(collect_backend_events())
    frontend = collect_frontend_events()
    return backend - frontend


def test_no_new_unhandled_backend_events():
    """后端发了、前端没接的事件——每条都要么前端处理，要么登记原因。"""
    baseline = set(_baseline_events())
    new = sorted(_unhandled() - baseline)
    assert new == [], (
        "后端发了但前端从不处理的事件——请在前端注册 handler，"
        "或写进 scripts/ws_event_baseline.json 并注明原因: " + str(new)
    )


def test_baseline_has_no_stale_entries():
    """基线里登记的事件已经没人发了——请删掉，别让清单烂在里面。"""
    baseline = set(_baseline_events())
    stale = sorted(baseline - _unhandled())
    assert stale == [], (
        "基线里的事件前端已经处理（或后端不再发）——请更新 "
        "scripts/ws_event_baseline.json: " + str(stale)
    )


def test_baseline_entries_have_reasons():
    """每条登记都要写明"为什么前端不处理"——否则登记就变成了垃圾桶。"""
    missing = sorted(k for k, v in _baseline_events().items() if not str(v).strip())
    assert missing == [], (
        "以下基线条目没有写原因（AGENTS.md 规则 17：登记必须注明原因）: " + str(missing)
    )


def _exported_handlers() -> dict[str, str]:
    """{handler 函数名: 文件名}——wsHandlers 目录下导出的所有 Handler。"""
    import re

    out: dict[str, str] = {}
    base = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "src-frontend", "src", "hooks", "wsHandlers",
    )
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".ts") or fn == "index.ts":
            continue
        src = open(os.path.join(base, fn), encoding="utf-8", errors="replace").read()
        for name in re.findall(r"export const (\w+)\s*:\s*Handler", src):
            out[name] = fn
    return out


def test_every_handler_is_registered_in_the_map():
    """写了 handler 却没注册进 map = 事件照样被丢弃（dispatchToStore 找不到就跳过）。

    2026-10-22 实测：memory_saved / slash_command_matched 的 handler 写好了，
    忘了在 index.ts 注册——后端发了、前端收不到，事件被静默丢弃。
    """
    index_path = os.path.join(
        os.path.dirname(__file__), "..", "..",
        "src-frontend", "src", "hooks", "wsHandlers", "index.ts",
    )
    index_text = open(index_path, encoding="utf-8").read()
    dead = sorted(
        f"{name}（{fn}）"
        for name, fn in _exported_handlers().items()
        if name not in index_text
    )
    assert not dead, (
        "这些 handler 写了但没注册进 wsHandlers/index.ts 的 handler map——"
        "事件会被静默丢弃: " + "; ".join(dead)
    )
