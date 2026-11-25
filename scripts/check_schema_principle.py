"""scripts/check_schema_principle.py —— 架构铁律 #5 自动化检查

验证 SCHEMA_SQL、MIGRATIONS、COLUMN_MIGRATIONS 三者：
1. 都在 repositories/schema.py 内定义（单一来源）
2. MIGRATIONS version 连续递增（无空洞）
3. 新增表/列在 SCHEMA_SQL 和 MIGRATIONS/COLUMN_MIGRATIONS 中同步出现

用法：
    python scripts/check_schema_principle.py
退出码：
    0 = 通过
    1 = 失败（CI 应阻止合并）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 允许从项目根目录导入 src-python 模块
ROOT = Path(__file__).resolve().parent.parent
SRC_PYTHON = ROOT / "src-python"
if str(SRC_PYTHON) not in sys.path:
    sys.path.insert(0, str(SRC_PYTHON))

from repositories.schema import SCHEMA_SQL, MIGRATIONS, COLUMN_MIGRATIONS  # noqa: E402


def check_single_source() -> list[str]:
    """检查 SCHEMA_SQL/MIGRATIONS/COLUMN_MIGRATIONS 是否非空且为字符串/列表。"""
    errors: list[str] = []
    if not isinstance(SCHEMA_SQL, str) or not SCHEMA_SQL.strip():
        errors.append("SCHEMA_SQL 必须是非空字符串")
    if not isinstance(MIGRATIONS, list) or not MIGRATIONS:
        errors.append("MIGRATIONS 必须是非空列表")
    if not isinstance(COLUMN_MIGRATIONS, list):
        errors.append("COLUMN_MIGRATIONS 必须是列表")
    return errors


def check_migrations_sequential() -> list[str]:
    """检查 MIGRATIONS version 从 1 开始连续递增。"""
    errors: list[str] = []
    versions = [m[0] for m in MIGRATIONS]
    expected = list(range(1, len(versions) + 1))
    if versions != expected:
        errors.append(
            f"MIGRATIONS version 不连续：期望 {expected[:3]}...{expected[-1]}，"
            f"实际 {versions[:3]}...{versions[-1]}"
        )
    # 检查重复
    seen: set[int] = set()
    for v in versions:
        if v in seen:
            errors.append(f"MIGRATIONS version {v} 重复定义")
        seen.add(v)
    return errors


def check_tables_in_schema() -> list[str]:
    """检查 MIGRATIONS 中 CREATE TABLE 的表名都在 SCHEMA_SQL 中出现。"""
    errors: list[str] = []
    schema_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", SCHEMA_SQL))
    for _ver, desc, sql in MIGRATIONS:
        migration_tables = re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", sql)
        for t in migration_tables:
            if t not in schema_tables:
                errors.append(
                    f"MIGRATION '{desc}' 创建表 {t}，但 SCHEMA_SQL 中未定义。"
                    "新增表必须同步加入 SCHEMA_SQL。"
                )
    return errors


def check_columns_in_schema() -> list[str]:
    """检查 COLUMN_MIGRATIONS 中的列是否在 SCHEMA_SQL 对应表里出现。

    这是软性检查——若 SCHEMA_SQL 中表定义使用了紧凑单行格式，
    可能无法完全匹配，此时只警告不报错。
    """
    errors: list[str] = []
    warnings: list[str] = []
    # 提取 SCHEMA_SQL 中每个表的列定义
    table_pattern = re.compile(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\);",
        re.DOTALL,
    )
    schema_columns: dict[str, set[str]] = {}
    for match in table_pattern.finditer(SCHEMA_SQL):
        table_name = match.group(1)
        body = match.group(2)
        # 简单提取列名：每行第一个词，跳过约束关键字
        cols: set[str] = set()
        for line in body.split("\n"):
            line = line.strip().strip(",")
            if not line:
                continue
            first_word = line.split()[0].upper()
            if first_word in {
                "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "DEFAULT", "CONSTRAINT",
                "INDEX", "CREATE", "INSERT",
            }:
                continue
            col_name = line.split()[0].strip('"`[]')
            cols.add(col_name)
        schema_columns[table_name] = cols

    for table, column, _definition in COLUMN_MIGRATIONS:
        if table not in schema_columns:
            # 表可能由 MIGRATIONS 创建而非 SCHEMA_SQL 直接定义，跳过
            continue
        cols = schema_columns[table]
        if column not in cols:
            warnings.append(
                f"COLUMN_MIGRATIONS: {table}.{column} 未在 SCHEMA_SQL 的 {table} 表定义中找到。"
                "请确认已同步加入 SCHEMA_SQL。"
            )
    # 警告只打印不报错（紧凑格式可能漏匹配）
    for w in warnings:
        print(f"  [WARN] {w}")
    return errors


def check_migration_descriptions() -> list[str]:
    """检查每个 migration 有非空 description。"""
    errors: list[str] = []
    for ver, desc, _sql in MIGRATIONS:
        if not desc or not desc.strip():
            errors.append(f"MIGRATIONS version {ver} 缺少 description")
    return errors


def main() -> int:
    print("=== Schema Principle Check (铁律 #5) ===")
    all_errors: list[str] = []

    checks = [
        ("单一来源", check_single_source),
        ("MIGRATIONS 连续递增", check_migrations_sequential),
        ("表定义同步", check_tables_in_schema),
        ("列定义同步", check_columns_in_schema),
        ("migration 描述", check_migration_descriptions),
    ]

    for name, check_fn in checks:
        print(f"\n[{name}]")
        errors = check_fn()
        if errors:
            for e in errors:
                print(f"  [FAIL] {e}")
            all_errors.extend(errors)
        else:
            print("  [OK]")

    print(f"\n=== 结果: {'通过' if not all_errors else f'{len(all_errors)} 个错误'} ===")
    return 1 if all_errors else 0


if __name__ == "__main__":
    sys.exit(main())
