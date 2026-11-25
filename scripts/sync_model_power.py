#!/usr/bin/env python
"""scripts/sync_model_power.py —— 为模型目录补/刷新 `power` 能力档（厂商中立）

## 为什么需要它

MAX 模式要在"已配置可用的模型"里挑能力最强的一个，这需要一个可比较的档位。
这个档位必须是**数据**而不是代码里的厂商判断——所以写在 `config/models/*.yaml`
每个模型条目上（`power`），本脚本负责按通用规则生成/刷新它。

## 规则（只看模型 id，不看厂商名）

先分词（按 `-` `_` `.` `/` 切），再按优先级判定：

1. **高阶词**（pro / max / opus / ultra / large / reasoner / thinking）→ 3
2. **低阶词**（flash / mini / lite / turbo / small / nano / fast）→ 1
3. 其余 → 2（标准档）

分词而非裸子串匹配是必须的：`"mini" in "gemini-3-pro"` 为真，会把 Gemini 的
pro 误判成小模型（本项目真实踩过）。高阶优先于低阶也是必须的：
`gemini-3-pro-preview` 同时含高阶词与 `preview`，应算高阶。

同一档位内的"谁更新"由 `services/model_catalog_query.best_available_model`
用版本号做次级比较（gpt-5.2 > gpt-5.1），不在本脚本生成。

## 用法

    python scripts/sync_model_power.py            # 干跑，打印将发生的改动
    python scripts/sync_model_power.py --write    # 实际写入各 YAML

幂等：重复运行不会产生额外改动。
"""
from __future__ import annotations

import io
import os
import re
import sys

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "models")

HIGH_TIER_WORDS = ("pro", "max", "opus", "ultra", "large", "reasoner", "thinking")
LOW_TIER_WORDS = ("flash", "mini", "lite", "turbo", "small", "nano", "fast")

_ID_RE = re.compile(r"^(\s*)- id:\s*(\S+)\s*$")
_POWER_RE = re.compile(r"^(\s+)power:\s*\d+\s*$")


def tokens(model_id: str) -> list[str]:
    """把模型 id 切成词——避免 'mini' 命中 'gemini' 这类子串陷阱。"""
    s = model_id.lower()
    for sep in ("/", "_", "."):
        s = s.replace(sep, "-")
    return [t for t in s.split("-") if t]


def power_of(model_id: str) -> int:
    """按通用品阶词给出 1/2/3。高阶优先——'pro-preview' 应算高阶。"""
    toks = tokens(model_id)
    if any(t == w for t in toks for w in HIGH_TIER_WORDS):
        return 3
    if any(t == w for t in toks for w in LOW_TIER_WORDS):
        return 1
    # 前缀式高阶写法（如 "promax"）兜底，但不用于低阶词（避免 gemini 误伤）
    if any(t.startswith(w) for t in toks for w in HIGH_TIER_WORDS):
        return 3
    return 2


def _process(path: str, write: bool) -> list[tuple[str, int | None, int]]:
    """返回 [(model_id, 原值, 新值), ...]（仅列出有变化的）。"""
    with io.open(path, encoding="utf-8", newline="") as fh:
        lines = fh.read().split("\n")
    changes: list[tuple[str, int | None, int]] = []
    out: list[str] = []
    current_id: str | None = None
    current_power: int | None = None
    for line in lines:
        m = _ID_RE.match(line)
        if m:
            current_id, current_power = m.group(2), None
            out.append(line)
            continue
        pm = _POWER_RE.match(line)
        if pm and current_id:
            current_power = int(line.split(":", 1)[1].strip())
            want = power_of(current_id)
            if current_power != want:
                changes.append((current_id, current_power, want))
            out.append(f"{pm.group(1)}power: {want}")
            continue
        out.append(line)
    # 没有 power 行的模型：在 id 行后补一行
    if any(_ID_RE.match(l) for l in lines):
        rebuilt: list[str] = []
        cur: str | None = None
        has_power = False
        for line in out:
            m = _ID_RE.match(line)
            if m:
                if cur is not None and not has_power:
                    indent = re.match(r"^(\s*)", line).group(1)
                    rebuilt.append(f"{indent}  power: {power_of(cur)}")
                    changes.append((cur, None, power_of(cur)))
                cur, has_power = m.group(2), False
                rebuilt.append(line)
                continue
            if _POWER_RE.match(line):
                has_power = True
            rebuilt.append(line)
        if cur is not None and not has_power:
            indent = "  "
            rebuilt.append(f"{indent}power: {power_of(cur)}")
            changes.append((cur, None, power_of(cur)))
        out = rebuilt
    if write and changes:
        with io.open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join(out))
    return changes


def main() -> int:
    write = "--write" in sys.argv
    total = 0
    for name in sorted(os.listdir(ROOT)):
        if not name.endswith(".yaml") or name == "_index.yaml":
            continue
        path = os.path.join(ROOT, name)
        try:
            yaml.safe_load(io.open(path, encoding="utf-8"))  # 先确认 YAML 合法
        except Exception as e:
            print(f"  [skip] {name}: YAML 非法 ({e})")
            continue
        changes = _process(path, write)
        if changes:
            total += len(changes)
            for mid, old, new in changes:
                print(f"  {name}: {mid} power {old} -> {new}")
    verb = "已写入" if write else "（干跑）将改动"
    print(f"{verb} {total} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
