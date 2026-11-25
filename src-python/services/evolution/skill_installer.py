"""services/skill_installer.py —— Skill Git/市场安装辅助（Phase 51-C）"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import zipfile
from typing import TYPE_CHECKING

import aiohttp

from services.evolution.skill_md_parser import parse_skill_md

if TYPE_CHECKING:
    from .skill_registry import SkillRegistry

_log = logging.getLogger("yuncat.skill.installer")


def find_skill_entry(root: str) -> tuple[str, str] | None:
    """在 root 下查找 skill.yaml 或 SKILL.md 入口目录。

    返回 ("yaml", dir) 或 ("md", dir)；找不到返回 None。
    """
    for dirpath, _, filenames in os.walk(root):
        if "skill.yaml" in filenames:
            return "yaml", dirpath
    for dirpath, _, filenames in os.walk(root):
        if "SKILL.md" in filenames:
            return "md", dirpath
    return None


async def clone_git_repo(url: str, branch: str | None = None) -> str | None:
    """克隆 Git 仓库到临时目录，返回临时目录路径。

    P0 安全修复: 入口统一走 security/url_guard 校验（fail-closed）——
    拒绝 file:// / ext:: / scp 语法 / 内网地址，防止任意协议 git clone
    与 SSRF。对标 Claude Code WebFetch(domain:...) 域名约束。

    Phase 5 W: 所有权转移契约——成功返回时调用方负责清理 tmp 目录
    （用 try/finally + shutil.rmtree(tmp, ignore_errors=True)）。
    失败时（返回 None）本函数内部已清理，调用方无需处理。
    参考调用方: skill_registry.install_from_git()。
    """
    from security.url_guard import validate_external_url

    if not url or not isinstance(url, str):
        return None
    try:
        validate_external_url(url)
    except ValueError as e:
        _log.error("git clone URL rejected by url_guard: %s (%s)", url, e)
        raise ValueError(f"Git 仓库 URL 不被允许: {e}") from e
    tmp = tempfile.mkdtemp(prefix="skill_git_")
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["-b", branch]
    cmd += [url, tmp]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        _log.error("git clone failed: %s", err.decode(errors="replace")[:200])
        shutil.rmtree(tmp, ignore_errors=True)
        return None
    return tmp


async def download_marketplace_package(skill_id: str, source: str = "agentskills.io") -> str | None:
    """从 marketplace 下载 Skill 包并解压到临时目录。

    P0 安全修复: skill_id 格式白名单（防 URL path 注入/穿越）+ 完整 URL
    走 url_guard 校验（source 可为任意裸 hostname，但必须解析到公网 IP
    且 https）。对标主流包管理器的 registry 源校验。

    Phase 5 W: 所有权转移契约——成功返回时调用方负责清理 tmp_dir
    （用 try/finally + shutil.rmtree(tmp_dir, ignore_errors=True)）。
    失败时（返回 None）本函数内部已清理，调用方无需处理。
    参考调用方: skill_registry.install_from_marketplace()。
    """
    import re

    from security.url_guard import validate_external_url

    if not skill_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", skill_id):
        raise ValueError(f"非法 skill_id: {skill_id!r}")
    # source 必须是裸 hostname（无 scheme/path/credentials），防 URL 拼接注入
    if not source or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z0-9.-]+", source):
        raise ValueError(f"非法 marketplace source: {source!r}")
    url = f"https://{source}/api/skills/{skill_id}/download"
    try:
        validate_external_url(url)
    except ValueError as e:
        raise ValueError(f"marketplace 源不被允许: {e}") from e
    tmp_dir = tempfile.mkdtemp(prefix="skill_mp_")
    pkg_path = os.path.join(tmp_dir, "package.zip")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                with open(pkg_path, "wb") as f:
                    while True:
                        chunk = await resp.content.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
        with zipfile.ZipFile(pkg_path, "r") as zf:
            zf.extractall(tmp_dir)
        return tmp_dir
    except Exception:
        _log.exception("Failed to download marketplace package")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None


def _write_skill_dir(data: dict, dest: str) -> str:
    """将解析后的 Skill 字典写入临时目录，返回目录路径。"""
    import yaml

    os.makedirs(dest, exist_ok=True)
    skill_yaml = {
        "name": data["name"],
        "version": data["version"],
        "title": data["title"],
        "description": data["description"],
        "author": data["author"],
        "category": data["category"],
        "tags": data["tags"],
        "icon": data["icon"],
        "min_yuncat": data["min_yuncat"],
    }
    if data.get("prompt_text"):
        with open(os.path.join(dest, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(data["prompt_text"])
        skill_yaml["prompt"] = {
            "file": "prompt.md",
            "placement": "append",
            "priority": 50,
            "scope": {},
        }
    if data.get("commands"):
        with open(os.path.join(dest, "commands.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(data["commands"], f, allow_unicode=True)
        skill_yaml["commands"] = {"file": "commands.yaml"}
    if data.get("workflows"):
        with open(os.path.join(dest, "workflow.yaml"), "w", encoding="utf-8") as f:
            yaml.dump(data["workflows"], f, allow_unicode=True)
        skill_yaml["workflow"] = {"file": "workflow.yaml"}
    with open(os.path.join(dest, "skill.yaml"), "w", encoding="utf-8") as f:
        yaml.dump(skill_yaml, f, allow_unicode=True, default_flow_style=False)
    return dest


async def install_parsed_md(registry: "SkillRegistry", md_path: str):
    """从 SKILL.md 解析并安装 Skill。"""
    data = parse_skill_md(md_path)
    if not data:
        return None
    tmp = tempfile.mkdtemp(prefix="skill_md_")
    try:
        _write_skill_dir(data, tmp)
        return await registry.install_from_dir(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
