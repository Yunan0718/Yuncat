"""contract: 版本号五处必须一致

发版要同步的版本号位置散落在 5 个文件里（RELEASE.md 说 5 处、docs/BUILD.md 说 3 处，
两边对不上）。全人工同步，漏一处不会报错，只会让"应用显示的版本"与"安装包文件名/
更新检测版本"打架——用户侧表现为更新提示反复出现或版本号显示错。

本测试把 5 处钉在一起：任一处漂移，CI 直接红，并指出是哪一处。
BUILD.md 的清单同步收敛为同一份（见该文件「版本号更新」节）。
"""
import json
import os
import re
import sys

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "..")

# 版本号真源（相对仓库根，读取方式，展示名）
_SOURCES = (
    ("package.json", "json:version"),
    ("src-electron/package.json", "json:version"),
    ("src-frontend/package.json", "json:version"),
    ("pyproject.toml", "toml:version"),
    ("src-python/config.py", "py:APP_VERSION"),
)


def _read_version(rel: str, how: str) -> str:
    path = os.path.join(_SRC, rel)
    assert os.path.exists(path), f"版本号来源文件不存在: {rel}"
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    kind, _, key = how.partition(":")
    if kind == "json":
        return json.loads(text)[key]
    if kind == "toml":
        match = re.search(rf'^{key}\s*=\s*"([^"]+)"', text, re.MULTILINE)
    else:
        match = re.search(rf'^{key}\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, f"{rel} 里找不到 {key}"
    return match.group(1)


def test_all_version_sources_agree():
    found = {rel: _read_version(rel, how) for rel, how in _SOURCES}
    distinct = set(found.values())
    assert len(distinct) == 1, (
        "版本号不一致，发版前必须同步这 5 处：\n"
        + "\n".join(f"  {rel:34} {ver}" for rel, ver in found.items())
    )


def test_version_looks_like_semver():
    """防止把版本号写成空串/占位符后 5 处"一致地错"。"""
    version = _read_version("package.json", "json:version")
    assert re.fullmatch(r"\d+\.\d+\.\d+([-+].+)?", version), f"版本号形态异常: {version!r}"


def test_build_doc_lists_same_sources():
    """BUILD.md 的版本号清单必须与本测试的来源集合一致（文档不得漏项）。"""
    path = os.path.join(_SRC, "docs", "BUILD.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    section = text.split("## 版本号更新", 1)
    assert len(section) == 2, "docs/BUILD.md 缺少「版本号更新」章节"
    body = section[1].split("\n## ", 1)[0]
    missing = [rel for rel, _ in _SOURCES if rel not in body]
    assert not missing, f"docs/BUILD.md 的版本号清单漏了: {missing}"