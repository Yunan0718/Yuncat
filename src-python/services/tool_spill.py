"""services/tool_spill.py —— 截断工具结果的持久化 side-store

借鉴 deepseek-harness 的 `dsh-spill`：大输出不塞进模型上下文，而是**原样落盘**，
只把一个可检索的 locator/key 交给模型；模型需要时再取回。

为什么 Yuncat 需要它：已有 `request_more_context`（内存 LRU），但
  · 进程重启后全部丢失；
  · LRU 超过 `TOOL_RESULT_CACHE_MAX` 条后旧结果被淘汰；
  · 长任务里“稍后要看”的输出恰好是最容易被丢的。
DSH 的做法是把 spill 作为独立能力：存储只负责**逐字保存**，消费方决定预览/落盘。
这里沿用同一分工：本模块只做存取，截断策略仍由 post_process 决定。

安全：所有路径都由受控目录 + 清洗后的 task/tool 名拼成，读取时再次校验落点必须在
spill 根目录内，避免 `../` 之类的路径穿越。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")

SPILL_DIR_NAME = "tool-spill"
SPILL_ENABLED = True
SPILL_MAX_BYTES = 8 * 1024 * 1024   # 单个产物上限（8MB）
SPILL_MAX_AGE_DAYS = 7


def _safe_part(value: str, fallback: str = "item") -> str:
    """把 task_id / tool_name 清洗成单一路径片段。"""
    text = _SAFE_PART.sub("_", str(value or "")).strip("._")
    return (text or fallback)[:80]


def spill_root() -> Path:
    """spill 根目录（跟随 config.DATA_DIR，测试可 monkeypatch）。"""
    from config import DATA_DIR

    return Path(DATA_DIR) / SPILL_DIR_NAME


def _artifact_path(task_id: str, tool_name: str, index: int) -> Path:
    return spill_root() / _safe_part(task_id, "task") / f"{_safe_part(tool_name, 'tool')}__{int(index)}.txt"


def save_spill(task_id: str, tool_name: str, index: int, content: str) -> str | None:
    """把完整工具结果原样落盘；成功返回绝对路径，失败/超限返回 None。

    失败不抛异常：spill 是增强能力，不能因为它让工具调用本身失败。
    """
    if not SPILL_ENABLED or not content:
        return None
    raw = content.encode("utf-8", errors="replace")
    if len(raw) > SPILL_MAX_BYTES:
        return None
    try:
        path = _artifact_path(task_id, tool_name, index)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, path)
        return str(path)
    except OSError:
        return None


def _read_checked(path: Path) -> str | None:
    """读取前确认目标仍在 spill 根目录内（防路径穿越）。"""
    try:
        root = spill_root().resolve()
        resolved = path.resolve()
        resolved.relative_to(root)
        if not resolved.is_file():
            return None
        return resolved.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def load_spill(task_id: str, tool_name: str, index: int) -> str | None:
    """按精确 key 读取；找不到返回 None。"""
    return _read_checked(_artifact_path(task_id, tool_name, index))


def find_spill(tool_name: str, index: int) -> str | None:
    """在不知道 task_id 时（request_more_context 的调用签名）找最近的匹配产物。

    与内存 LRU 的“最近优先”语义一致：按 mtime 倒序取第一条。
    """
    pattern = f"{_safe_part(tool_name, 'tool')}__{int(index)}.txt"
    try:
        root = spill_root()
        if not root.is_dir():
            return None
        matches = sorted(root.glob(f"*/{pattern}"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in matches:
        content = _read_checked(path)
        if content is not None:
            return content
    return None


def cleanup_spill(max_age_days: int | None = None) -> int:
    """删除超过保留窗口的 spill 文件，返回删除数。"""
    days = SPILL_MAX_AGE_DAYS if max_age_days is None else max_age_days
    cutoff = time.time() - max(0, days) * 86400
    removed = 0
    try:
        root = spill_root()
        if not root.is_dir():
            return 0
        for path in root.glob("*/*.txt"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed
