#!/usr/bin/env python3
"""从 WorkBuddy 会话 JSONL 中提取可迁移的上下文交接文档。

用法:
    python scripts/session_handoff.py <session.jsonl> [输出.md]
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

MAX_TEXT = 400
MAX_USERS = 400
MAX_EVENTS = 1200


def fmt_ts(ms: int | None) -> str:
    if not ms:
        return "?"
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")


def brief_args(raw: str, limit: int = 160) -> str:
    try:
        obj = json.loads(raw)
    except Exception:
        return raw[:limit]
    if not isinstance(obj, dict):
        return str(obj)[:limit]
    for key in ("command", "file_path", "pattern", "prompt", "query", "url", "script", "path"):
        if key in obj and isinstance(obj[key], str):
            return f"{obj[key]}"[:limit]
    return json.dumps(obj, ensure_ascii=False)[:limit]


def extract(path: Path):
    users: list[tuple[int, str]] = []
    events: list[tuple[int, str, str]] = []
    errors: list[tuple[int, str]] = []
    first_ts = None
    last_ts = None

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            ts = d.get("timestamp") or 0
            if ts:
                first_ts = first_ts or ts
                last_ts = max(last_ts or 0, ts)
            typ = d.get("type")

            if typ == "message":
                role = d.get("role")
                content = d.get("content")
                if isinstance(content, list):
                    parts = []
                    for blk in content:
                        if not isinstance(blk, dict):
                            continue
                        if blk.get("type") in ("input_text", "output_text", "text"):
                            parts.append(str(blk.get("text", "")).strip())
                    text = "\n".join(p for p in parts if p)
                else:
                    text = str(content or "")
                if not text.strip():
                    continue
                if role == "user":
                    meta = (d.get("providerData") or {}).get("isMeta")
                    if meta:
                        events.append((ts, "sys", text[:MAX_TEXT]))
                    else:
                        users.append((ts, text[:MAX_TEXT]))
                elif role == "assistant":
                    events.append((ts, "ai", text[:MAX_TEXT]))

            elif typ == "function_call":
                name = d.get("name") or "?"
                args = brief_args(d.get("arguments") or "{}")
                events.append((ts, "call", f"{name}: {args}"))

            elif typ == "function_call_result":
                content = d.get("content")
                txt = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                if txt and ("error" in txt.lower() or "Traceback" in txt or "FAILED" in txt):
                    errors.append((ts, txt[:300]))

    return users, events, errors, first_ts, last_ts


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".handoff.md")

    users, events, errors, t0, t1 = extract(src)

    lines: list[str] = []
    lines.append(f"# 会话上下文交接 — {src.stem}")
    lines.append("")
    lines.append(f"- 源文件: `{src}`")
    lines.append(f"- 时间跨度: {fmt_ts(t0)} → {fmt_ts(t1)}")
    lines.append(f"- 用户消息: {len(users)} 条 / 事件: {len(events)} 条")
    lines.append("")

    lines.append("## 一、用户原始诉求（按时间）")
    lines.append("")
    for ts, text in users[:MAX_USERS]:
        body = text.replace("\n", "\n   ")
        lines.append(f"- **[{fmt_ts(ts)}]** {body}")
        lines.append("")
    if len(users) > MAX_USERS:
        lines.append(f"> （仅保留最近 {MAX_USERS} 条，完整内容见源 JSONL）")
        lines.append("")

    lines.append("## 二、执行流水（助手输出 + 工具调用）")
    lines.append("")
    for ts, kind, text in events[-MAX_EVENTS:]:
        tag = {"ai": "助手", "call": "调用", "sys": "系统"}.get(kind, kind)
        body = text.replace("\n", "\n   ")
        lines.append(f"- **[{fmt_ts(ts)}][{tag}]** {body}")
    lines.append("")

    if errors:
        lines.append("## 三、期间出现的错误/失败片段（最后 40 条）")
        lines.append("")
        for ts, text in errors[-40:]:
            lines.append(f"- **[{fmt_ts(ts)}]** `{text}`")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"written: {out}  (users={len(users)} events={len(events)} errors={len(errors)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
