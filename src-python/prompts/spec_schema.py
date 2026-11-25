"""prompts/spec_schema.py —— Spec YAML 格式定义与验证 (Phase 43 Stage C.1)

基于 OpenAPI 3.x + JSON Schema 业界标准。不发明新 Schema 格式——
Spec 是对 OpenAPI 3.x paths + JSON Schema models + CI commands 的薄包装。

设计原则:
  1. OpenAPI 3.x paths — API 契约，可被 Swagger UI / Prism 直接消费
  2. JSON Schema — 数据模型，Pydantic model_json_schema() 可自动生成
  3. Shell commands — 验证命令，GitHub Actions / pre-commit hooks 可复用
  4. constraints — 自然语言，供 Agent 理解安全/性能/编码约束
  5. functional_requirements — 自然语言，供人类验收
"""
import copy as _copy
import json as _json
import logging

_log = logging.getLogger("yuncat.spec")


# ═══════════════════════════════════════════════════════════
# Spec JSON Schema (用于验证 Spec YAML 结构)
# ═══════════════════════════════════════════════════════════

SPEC_JSON_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["spec"],
    "properties": {
        "spec": {
            "type": "object",
            "required": ["name", "version", "functional_requirements"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "功能名称，如 '用户管理系统'",
                    "minLength": 1,
                    "maxLength": 200,
                },
                "version": {
                    "type": "integer",
                    "description": "Spec 版本号（循环迭代中递增）",
                    "minimum": 1,
                },
                "rounds": {
                    "type": "integer",
                    "description": "已执行的收敛轮次",
                    "minimum": 0,
                },
                "converged": {
                    "type": "boolean",
                    "description": "是否已收敛（true=所有需求已实现且验证通过）",
                },
                "functional_requirements": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["id", "description"],
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "功能需求 ID，如 FR-1",
                                "pattern": "^[A-Z]{2,4}-\\d+$",
                            },
                            "description": {
                                "type": "string",
                                "minLength": 1,
                                "description": "自然语言功能描述",
                            },
                            "acceptance": {
                                "type": "string",
                                "description": "验收标准，如 'POST /register → 201 + JWT token'",
                            },
                        },
                    },
                },
                "openapi": {
                    "type": "object",
                    "description": "OpenAPI 3.x 路径定义（可选但推荐）",
                    "properties": {
                        "openapi": {"type": "string", "enum": ["3.0.3", "3.1.0"]},
                        "info": {
                            "type": "object",
                            "required": ["title", "version"],
                            "properties": {
                                "title": {"type": "string"},
                                "version": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                        "paths": {
                            "type": "object",
                            "description": "API 端点定义。每个端点需含 requestBody 和 responses。",
                        },
                    },
                },
                "schemas": {
                    "type": "object",
                    "description": "JSON Schema 数据模型。Key=实体名，Value=JSON Schema 对象。",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["object"]},
                            "properties": {"type": "object"},
                            "required": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
                "constraints": {
                    "type": "array",
                    "description": "安全/性能/编码约束（自然语言，供 Agent 理解）",
                    "items": {"type": "string"},
                },
                "verification": {
                    "type": "object",
                    "description": "验证定义",
                    "properties": {
                        "commands": {
                            "type": "array",
                            "description": "Shell 验证命令列表",
                            "items": {"type": "string"},
                        },
                    },
                },
                "files_to_create": {
                    "type": "array",
                    "description": "需新建的文件路径（相对于项目根目录）",
                    "items": {"type": "string"},
                },
                "files_to_modify": {
                    "type": "array",
                    "description": "需修改的现有文件路径",
                    "items": {"type": "string"},
                },
                "protected_files": {
                    "type": "array",
                    "description": "受保护文件——Builder 不可编辑（Phase 45 Gate 模式）",
                    "items": {"type": "string"},
                },
            },
        },
    },
}


# ═══════════════════════════════════════════════════════════
# Spec 字段说明（供 ARCHITECT_PROMPT 参考）
# ═══════════════════════════════════════════════════════════

SPEC_FIELD_DESCRIPTIONS: dict[str, str] = {
    "functional_requirements": (
        "功能需求列表（自然语言）。每条含 id (如 FR-1)、description、可选 acceptance。"
        "这是 Spec 的核心——所有后续工作都围绕 functional_requirements 展开。"
    ),
    "openapi": (
        "API 契约 → OpenAPI 3.x 格式。含 paths 和每个端点的 requestBody/responses。"
        "不是必填字段——纯前端功能或工具脚本可以省略。"
        "填了可以用 Swagger UI 可视化、Prism mock、自动生成 SDK。"
    ),
    "schemas": (
        "数据模型 → JSON Schema 格式。Key 是实体名 (如 User)，"
        "Value 是标准 JSON Schema 对象 (type/properties/required)。"
        "Pydantic model_json_schema() 可自动生成此格式。"
    ),
    "constraints": (
        "约束列表（自然语言）。安全约束必须有——密码哈希、输入验证、"
        "防注入、密钥管理。还应包含性能约束和编码规范约束。"
    ),
    "verification": (
        "验证策略。commands 数组是 Shell 命令列表——Agent 实现后运行。"
        "建议包含: 单元测试、类型检查、lint。"
    ),
    "files_to_create": (
        "需要新建的文件路径列表（相对于项目根目录）。"
        "Developer 角色看到这个就知道具体在哪里写什么文件。"
    ),
    "files_to_modify": (
        "需要修改的现有文件路径列表。"
        "如果功能只需要新增文件，此字段可以为空。"
    ),
}


# ═══════════════════════════════════════════════════════════
# Spec 模板 — 最小可用示例
# ═══════════════════════════════════════════════════════════

SPEC_MINIMAL_TEMPLATE: str = """spec:
  name: "功能名称"
  version: 1
  functional_requirements:
    - id: FR-1
      description: "功能需求描述"
      acceptance: "验收标准（可选）"
  constraints:
    - "安全约束1"
    - "编码约束1"
  files_to_create:
    - "src/module/file.py"
"""


# ═══════════════════════════════════════════════════════════
# 验证与辅助函数
# ═══════════════════════════════════════════════════════════

def validate_spec_structure(spec_dict: dict) -> list[str]:
    """验证 Spec 字典结构的合规性。

    不依赖 jsonschema 库——纯 Python 轻量验证。
    返回: 错误列表。空列表 = 通过。
    """
    errors: list[str] = []

    if not isinstance(spec_dict, dict):
        return ["Spec 必须是 dict/object"]

    inner = spec_dict.get("spec")
    if not isinstance(inner, dict):
        errors.append("缺少 'spec' 顶层键或类型不是 dict")
        return errors

    # 必填字段检查
    required = ["name", "version", "functional_requirements"]
    for field in required:
        if field not in inner:
            errors.append(f"缺少必填字段: spec.{field}")

    if errors:
        return errors

    # name 校验
    name = inner.get("name", "")
    if not isinstance(name, str) or not name.strip():
        errors.append("spec.name 必须是非空字符串")
    elif len(name) > 200:
        errors.append(f"spec.name 太长 ({len(name)} > 200)")

    # version 校验
    version = inner.get("version", 0)
    if not isinstance(version, int) or version < 1:
        errors.append(f"spec.version 必须是 ≥1 的整数, 当前: {version}")

    # functional_requirements 校验
    frs = inner.get("functional_requirements", [])
    if not isinstance(frs, list) or len(frs) == 0:
        errors.append("spec.functional_requirements 必须是非空数组")
    else:
        for i, fr in enumerate(frs):
            if not isinstance(fr, dict):
                errors.append(f"FR[{i}] 必须是 dict")
                continue
            fr_id = fr.get("id", "")
            if not fr_id or not isinstance(fr_id, str):
                errors.append(f"FR[{i}] 缺少 id 或格式不对")
            fr_desc = fr.get("description", "")
            if not fr_desc or not isinstance(fr_desc, str):
                errors.append(f"FR[{i}] (id={fr_id}) 缺少 description")

    # constraints 校验（非必填但若存在必须为数组）
    constraints = inner.get("constraints")
    if constraints is not None:
        if not isinstance(constraints, list):
            errors.append("spec.constraints 必须是数组")
        else:
            for i, c in enumerate(constraints):
                if not isinstance(c, str):
                    errors.append(f"spec.constraints[{i}] 必须是字符串")

    # files_to_create / files_to_modify 格式校验
    for field in ("files_to_create", "files_to_modify"):
        val = inner.get(field)
        if val is not None:
            if not isinstance(val, list):
                errors.append(f"spec.{field} 必须是数组")
            else:
                for i, fpath in enumerate(val):
                    if not isinstance(fpath, str):
                        errors.append(f"spec.{field}[{i}] 必须是字符串路径")

    # schemas 格式校验
    schemas = inner.get("schemas")
    if schemas is not None:
        if not isinstance(schemas, dict):
            errors.append("spec.schemas 必须是 dict")
        else:
            for entity_name, schema_def in schemas.items():
                if not isinstance(schema_def, dict):
                    errors.append(f"spec.schemas.{entity_name} 必须是 JSON Schema 对象")

    # openapi 格式校验
    openapi = inner.get("openapi")
    if openapi is not None:
        if not isinstance(openapi, dict):
            errors.append("spec.openapi 必须是 dict")
        elif "paths" not in openapi:
            errors.append("spec.openapi 缺少 paths")

    # verification.commands 格式校验
    verification = inner.get("verification")
    if verification is not None:
        if not isinstance(verification, dict):
            errors.append("spec.verification 必须是 dict")
        else:
            commands = verification.get("commands")
            if commands is not None:
                if not isinstance(commands, list):
                    errors.append("spec.verification.commands 必须是数组")
                else:
                    for i, cmd in enumerate(commands):
                        if not isinstance(cmd, str):
                            errors.append(f"spec.verification.commands[{i}] 必须是字符串命令")

    return errors


def validate_spec_strict(spec_dict: dict) -> list[str]:
    """严格模式验证——与 validate_spec_structure 相同但额外检查最佳实践。

    额外检查:
      - constraints 是否包含安全约束关键词
      - files_to_create 是否有路径重复
      - functional_requirements id 是否重复
    """
    errors = validate_spec_structure(spec_dict)

    inner = spec_dict.get("spec", {})
    if not isinstance(inner, dict):
        return errors

    # 安全检查: constraints 中是否提到了密码/注入/密钥
    constraints = inner.get("constraints", [])
    if isinstance(constraints, list):
        constraint_text = " ".join(str(c).lower() for c in constraints)
        security_keywords = ["hash", "bcrypt", "密码", "注入", "injection",
                            "密钥", "secret", "api key", "sanitize"]
        if not any(kw in constraint_text for kw in security_keywords):
            errors.append(
                "安全建议: constraints 中缺少安全相关约束"
                " (密码哈希/输入验证/防注入/密钥管理)"
            )

    # 路径去重检查
    for field in ("files_to_create", "files_to_modify"):
        val = inner.get(field, [])
        if isinstance(val, list) and len(val) != len(set(val)):
            errors.append(f"spec.{field} 存在重复路径")

    # FR id 去重
    fr_ids = [fr.get("id", "") for fr in inner.get("functional_requirements", [])
              if isinstance(fr, dict)]
    if len(fr_ids) != len(set(fr_ids)):
        errors.append("spec.functional_requirements 存在重复 id")

    return errors


def build_spec_index(spec_dict: dict) -> dict:
    """从 Spec 字典中提取索引信息——供检索和跨会话引用。

    返回: {name, version, fr_count, api_count, schema_entities, file_count}
    """
    inner = spec_dict.get("spec", {})
    if not isinstance(inner, dict):
        return {}

    return {
        "name": inner.get("name", ""),
        "version": inner.get("version", 1),
        "fr_count": len(inner.get("functional_requirements", [])),
        "api_count": len(inner.get("openapi", {}).get("paths", {})),
        "schema_entities": list(inner.get("schemas", {}).keys()),
        "constraints_count": len(inner.get("constraints", [])),
        "files_to_create_count": len(inner.get("files_to_create", [])),
        "files_to_modify_count": len(inner.get("files_to_modify", [])),
    }
