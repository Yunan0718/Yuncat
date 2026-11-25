"""services/spec_parser.py —— Spec YAML 解析器 (Phase 43 Stage C.2)

设计原则:
  1. 解析 Spec YAML → 验证 → 结构化 dict
  2. 对 Agent 生成的 YAML 宽容解析（with 块标量、引号差异、缩进歧义）
  3. 验证错误含具体文件路径/行号/建议修正
  4. 与 spec_schema.py 的验证函数直接衔接
"""
import json as _json
import logging
import re as _re

_log = logging.getLogger("yuncat.spec_parser")


# ═══════════════════════════════════════════════════════════
# YAML 块提取 — Agent 输出中经常夹带解释文本
# ═══════════════════════════════════════════════════════════

def _extract_yaml_block(text: str) -> str:
    """从 Agent 输出中提取 YAML 块。

    支持:
      1. ```yaml ... ``` 围栏代码块
      2. ``` ... ``` 无语言标签
      3. 纯 YAML（以 spec: 开头）

    返回: 纯 YAML 文本。
    """
    # 策略 1: 围栏代码块 ```yaml ... ```
    yaml_fence = _re.search(r'```\s*yaml?\s*\n(.*?)```', text, _re.DOTALL)
    if yaml_fence:
        return yaml_fence.group(1).strip()

    # 策略 2: 通用围栏代码块 ``` ... ```
    generic_fence = _re.search(r'```\s*\n(.*?)```', text, _re.DOTALL)
    if generic_fence:
        content = generic_fence.group(1).strip()
        if content.startswith("spec:"):
            return content

    # 策略 3: 纯 YAML——找到 spec: 开头的那一行
    spec_match = _re.search(r'(?:^|\n)(spec:\s*\n.*)', text, _re.DOTALL)
    if spec_match:
        return spec_match.group(1).strip()

    # fallback: 返回去除了前后非 YAML 行的原始文本
    lines = text.split("\n")
    start_idx = None
    for i, line in enumerate(lines):
        if line.strip().startswith("spec:"):
            start_idx = i
            break
    if start_idx is not None:
        return "\n".join(lines[start_idx:]).strip()

    return text.strip()


# ═══════════════════════════════════════════════════════════
# YAML 宽容解析
# ═══════════════════════════════════════════════════════════

def parse_spec_yaml(yaml_str: str, strict: bool = False) -> dict:
    """解析 Spec YAML 字符串 → 结构化 dict。

    Args:
        yaml_str: YAML 字符串（可能含 Agent 的解释文字）
        strict: True=严格验证（最佳实践检查），False=基本结构验证

    Returns:
        {
            "ok": True/False,
            "spec": {...},           # 解析后的 Spec 字典（失败时为 None）
            "errors": [...],         # 验证错误列表
            "warnings": [...],       # 非阻断警告
            "index": {...},          # Spec 索引信息
            "raw_yaml": "...",       # 提取的原始 YAML
        }
    """
    from prompts.spec_schema import (
        validate_spec_structure,
        validate_spec_strict,
        build_spec_index,
    )

    result: dict = {
        "ok": False,
        "spec": None,
        "errors": [],
        "warnings": [],
        "index": {},
        "raw_yaml": "",
    }

    # Step 1: 提取 YAML 块
    clean_yaml = _extract_yaml_block(yaml_str)
    result["raw_yaml"] = clean_yaml

    if not clean_yaml or not clean_yaml.strip():
        result["errors"].append("YAML 内容为空——Agent 未生成 Spec")
        return result

    # Step 2: YAML 解析
    try:
        import yaml as _yaml
    except ImportError:
        _log.warning("PyYAML not available, trying yaml from python_embed")
        try:
            import sys as _sys
            import os as _os
            _embed = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)),
                                   "python-embed", "Lib", "site-packages")
            if _embed not in _sys.path:
                _sys.path.insert(0, _embed)
            import yaml as _yaml2
            _yaml = _yaml2
        except ImportError:
            result["errors"].append(
                "PyYAML 不可用——无法解析 YAML。请 pip install pyyaml。"
            )
            return result

    try:
        spec_dict = _yaml.safe_load(clean_yaml)
    except _yaml.YAMLError as e:
        result["errors"].append(f"YAML 语法错误: {str(e)[:500]}")
        # 尝试常见修复
        _fixed = _try_fix_common_yaml_errors(clean_yaml)
        if _fixed != clean_yaml:
            try:
                spec_dict = _yaml.safe_load(_fixed)
                result["warnings"].append(
                    "YAML 已自动修复常见格式问题。请检查输出。"
                )
            except _yaml.YAMLError:
                return result
        else:
            return result

    if not spec_dict:
        result["errors"].append("解析后空的 Spec——YAML 可能只有注释")
        return result

    if not isinstance(spec_dict, dict):
        result["errors"].append(
            f"解析后类型不对: {type(spec_dict).__name__}（应为 dict）。"
            f" Spec YAML 必须以 'spec:' 开头。"
        )
        return result

    # Step 3: 结构验证
    if strict:
        errors = validate_spec_strict(spec_dict)
    else:
        errors = validate_spec_structure(spec_dict)

    result["errors"] = errors
    if errors:
        return result

    # Step 4: 构建索引
    result["spec"] = spec_dict
    result["index"] = build_spec_index(spec_dict)
    result["ok"] = True

    return result


def _try_fix_common_yaml_errors(yaml_str: str) -> str:
    """尝试自动修复 Agent 生成的 YAML 中常见格式问题。

    修复:
      1. 列表项缩进不一致（4 空格 vs 2 空格）
      2. 冒号后缺少空格（key:value → key: value）
      3. 制表符替换为 2 空格
    """
    lines = yaml_str.split("\n")
    fixed_lines = []
    for line in lines:
        # 制表符 → 2 空格
        line = line.replace("\t", "  ")
        # 列表项缩进规范化: 必须是 2 的倍数
        stripped = line.lstrip(" ")
        if stripped.startswith("- "):
            indent = len(line) - len(stripped)
            # 确保缩进是 2 的倍数
            normalized_indent = (indent // 2) * 2
            line = " " * normalized_indent + stripped
        # 冒号后缺少空格（排除 URL 中的 :// 和时间中的 :）
        if ": " not in line and "://" not in line:
            colon_pos = line.find(":")
            if colon_pos > 0 and colon_pos < len(line) - 1:
                after = line[colon_pos + 1]
                if after != " " and after != "\n":
                    line = line[:colon_pos + 1] + " " + line[colon_pos + 1:]
        fixed_lines.append(line)
    return "\n".join(fixed_lines)


def parse_spec_from_file(filepath: str, strict: bool = False) -> dict:
    """从文件读取并解析 Spec YAML。"""
    import os as _os
    if not _os.path.exists(filepath):
        return {
            "ok": False,
            "spec": None,
            "errors": [f"文件不存在: {filepath}"],
            "warnings": [],
            "index": {},
            "raw_yaml": "",
        }
    try:
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return {
            "ok": False,
            "spec": None,
            "errors": [f"读取文件失败: {e}"],
            "warnings": [],
            "index": {},
            "raw_yaml": "",
        }
    return parse_spec_yaml(content, strict=strict)


def format_validation_errors(errors: list[str]) -> str:
    """格式化验证错误为可读的 Markdown 列表。

    供 Agent 在下一轮修复时理解具体问题和位置。
    """
    if not errors:
        return "✅ Spec 结构验证通过"
    lines = [f"❌ {len(errors)} 个 Spec 结构问题:\n"]
    for i, err in enumerate(errors, 1):
        lines.append(f"  {i}. {err}")
    return "\n".join(lines)
