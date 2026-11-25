"""services/skill_md_parser.py —— Hermes/OpenClaw 风格 SKILL.md 解析器"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _slug(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", s)


def _extract_section(text: str, headings: list[str]) -> str:
    lines = text.splitlines()
    collecting = False
    buffer: list[str] = []
    for line in lines:
        if not collecting:
            if any(re.match(rf"#+\s*{re.escape(h)}", line, re.I) for h in headings):
                collecting = True
            continue
        if re.match(r"#\s+", line) or re.match(r"#{2,}\s+", line):
            break
        buffer.append(line)
    return "\n".join(buffer).strip()


def _extract_commands(text: str) -> list[dict]:
    section = _extract_section(text, ["commands", "slash commands", "slash"])
    if not section:
        return []
    cmds = []
    for line in section.splitlines():
        m = re.match(r"^[-*]?\s*`?(/\w+)`?\s*[:-]?\s*(.*)", line)
        if m:
            sc, desc = m.group(1), m.group(2).strip()
            cmds.append({
                "name": sc.lstrip("/"),
                "slash_command": sc,
                "description": desc,
            })
    return cmds


def _extract_workflows(text: str) -> list[dict]:
    section = _extract_section(text, ["workflow", "workflows", "dag"])
    if not section:
        return []
    try:
        import yaml
        data = yaml.safe_load(section)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
    except Exception:
        pass
    try:
        import json as _json
        data = _json.loads(section)
        return data if isinstance(data, list) else [data]
    except Exception:
        pass
    return []


def _normalize(data: dict, path: str) -> dict:
    base = os.path.splitext(os.path.basename(path))[0]
    name = data.get("name") or _slug(data.get("title", base)) or base
    return {
        "name": name,
        "version": str(data.get("version", "1.0.0")),
        "title": data.get("title", name),
        "description": data.get("description", ""),
        "author": data.get("author", ""),
        "category": data.get("category", "general"),
        "tags": data.get("tags") or [],
        "icon": data.get("icon", "\U0001f4e6"),
        "min_yuncat": data.get("min_yuncat", "0.2.0"),
        "prompt_text": data.get("prompt_text", ""),
        "commands": data.get("commands") or [],
        "workflows": data.get("workflows") or [],
    }


def parse_skill_md(path: str) -> dict | None:
    """解析 SKILL.md，输出与 skill.yaml 兼容的字典。"""
    if not os.path.exists(path):
        return None
    text = Path(path).read_text(encoding="utf-8")

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                import yaml
                front = yaml.safe_load(parts[1]) or {}
                front["prompt_text"] = parts[2].strip()
                return _normalize(front, path)
            except Exception:
                pass

    data: dict = {}
    lines = text.splitlines()
    for line in lines:
        if line.startswith("# "):
            data["title"] = line[2:].strip()
            break

    desc_lines = []
    started = False
    meta_keys = ("author:", "category:", "tags:", "version:", "icon:", "min_yuncat:")
    for line in lines:
        if line.startswith("# "):
            started = True
            continue
        if not started:
            continue
        stripped = line.strip()
        if not stripped:
            if desc_lines:
                break
            continue
        if stripped.lower().startswith(meta_keys) or re.match(r"#+\s+", stripped):
            break
        desc_lines.append(stripped)
    data["description"] = " ".join(desc_lines)

    for key in ("author", "category"):
        m = re.search(rf"{key}[\s:]+(.+)", text, re.I)
        if m:
            data[key] = m.group(1).strip()
    m = re.search(r"tags[\s:]+(.+)", text, re.I)
    if m:
        data["tags"] = [t.strip() for t in m.group(1).split(",") if t.strip()]

    data["prompt_text"] = _extract_section(text, ["prompt", "system prompt", "instructions"])
    data["commands"] = _extract_commands(text)
    data["workflows"] = _extract_workflows(text)
    return _normalize(data, path)
