"""services/skill_registry.py —— Yuncat Skill 系统（Phase 29）

统一打包 Commander prompt 段 + DAG 模板 + Slash Command + 角色 为一个 Skill。
Skill = skill.yaml 入口 + 可选资源文件。

生命周期: install → activate → load(per-request) → update → uninstall
"""
from __future__ import annotations

import json as _json
import logging
import os
import re
import shutil
import time as _time
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import (
    APP_VERSION,
    MAX_TOKENS_SKILL_REGISTRY, ADAPTER_HEALTH_CHECK_TIMEOUT,
    SKILL_REGISTRY_REVIEW_INPUT_MAX, DEFAULT_AGENT_ID,
    SKILL_REGISTRY_ACTIVE_PROMPTS_LIMIT,
    SKILL_REGISTRY_PROMPT_MOUNT_PREVIEW_CHARS,
    SKILL_REGISTRY_DEEP_REVIEW_PROMPT_CHARS,
    ERROR_PREVIEW_CHARS,
)
# Phase 47 阶段 3 拆分：数据模型 + Marketplace 抽到独立模块
from services.evolution.skill_models import SkillPrompt, Skill
from services.evolution.skill_marketplace import SkillMarketplace, get_marketplace

_log = logging.getLogger("yuncat.skill")

# P0 安全修复: skill id 白名单——首字符字母数字，仅允许 字母/数字/./_/-
# 拒绝 / \ .. 起始 等路径穿越形态（"a..b" 作为单段目录名无害，放行）
# 与 skill_installer.download_marketplace_package 的 skill_id 校验保持同一模式
SKILL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _is_within_install_root(install_root: str, path: str) -> bool:
    """P0 安全修复: 判定 path（realpath 归一化后）是否位于 install_root 内。

    防 skill id 穿越后 install/uninstall 读写安装目录外的任意路径。
    对标 npm/pip 的安装前路径包含校验（defense-in-depth）。
    """
    root_real = os.path.realpath(install_root)
    path_real = os.path.realpath(path)
    return path_real.startswith(root_real + os.sep)


def _read_file_text(path: str) -> str:
    """读取文本文件——UTF-8 → GBK → UTF-8(replace) 三级降级。

    YAML 规范要求 UTF-8，但中文 Windows 下部分文件可能为 GBK 或含损坏字节。
    主流做法：先严格 UTF-8，再试 GBK（中文 Windows 常见），最后 UTF-8 replace
    兜底（损坏字节替换为 U+FFFD，保证可解析）。永不抛 UnicodeDecodeError。
    """
    for enc, errors in (("utf-8", "strict"), ("gbk", "strict"), ("utf-8", "replace")):
        try:
            with open(path, encoding=enc, errors=errors) as f:
                return f.read()
        except UnicodeDecodeError:
            continue
    # 最终兜底：latin-1 永不失败
    with open(path, encoding="latin-1") as f:
        return f.read()


def _close_unclosed_single_quotes(text: str) -> str:
    """修复未闭合的单引号——支持多行单引号字符串。

    YAML 单引号字符串可跨行延续（缩进续行）。损坏可能吞掉结尾引号，
    yaml.safe_load 抛 "unexpected end of stream" 或把后续 key 吞进字符串。

    状态机策略：
      - 单行未闭合（下一行是顶级 key）：当行末尾补 '
      - 多行未闭合（下一行是缩进续行）：进入 IN_QUOTE 状态，
        在续行中找到最后一行（下一行回到顶级 key）补 ' 闭合。
    """
    lines = text.splitlines(keepends=True)
    fixed: list[str] = []
    in_quote = False  # 是否在未闭合的多行单引号字符串内

    def _is_toplevel(line: str) -> bool:
        """判断是否为顶级 key 行（无缩进 + 含 `:`）。"""
        s = line.rstrip("\r\n")
        return bool(s) and not s[0].isspace() and ":" in s

    for i, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        nl = line[len(stripped):]
        next_line = lines[i + 1] if i + 1 < len(lines) else ""

        if in_quote:
            # 当前行是续行——若下一行回到顶级 key，则当行末尾补 ' 闭合
            if _is_toplevel(next_line) and not stripped.endswith("'"):
                fixed.append(stripped + "'" + nl)
                in_quote = False
            else:
                fixed.append(line)
            continue

        # NORMAL 状态：检测新字符串开始
        if ": '" in stripped and not stripped.endswith("'"):
            if _is_toplevel(next_line):
                # 单行未闭合——直接补引号
                fixed.append(stripped + "'" + nl)
            else:
                # 多行未闭合——进入 IN_QUOTE 状态，保留原行
                in_quote = True
                fixed.append(line)
        else:
            fixed.append(line)

    return "".join(fixed)


def _extract_minimal_fields(text: str) -> dict:
    """YAML 解析失败时的最终兜底——用正则提取标量字段，跳过损坏的 description。

    主流做法：当结构化解析失败时，退化为字段级提取，保证 Skill 可加载。
    损坏的 description 留空——Skill 仍可用（prompt 文件通常完好）。
    """
    data: dict[str, Any] = {}
    for key in ("name", "version", "title", "category", "icon", "author", "min_yuncat"):
        m = re.search(rf"^{key}:\s*(.+?)\s*$", text, re.MULTILINE)
        if m:
            val = m.group(1).strip()
            # 去引号（开闭可能不匹配，容忍处理）
            if val.startswith("'"):
                val = val[1:]
            if val.endswith("'"):
                val = val[:-1]
            data[key] = val
    return data


# ============================================================
# Skill 数据模型已抽到 services/evolution/skill_models.py
# SkillPrompt / Skill 由该模块导入
# ============================================================


# ============================================================
# Skill Registry
# ============================================================

class SkillRegistry:
    """Skill 注册表——管理已安装 Skill 的生命周期。

    用法:
        registry = SkillRegistry(install_root="/path/to/skills/installed")
        skill = await registry.install_from_dir("/path/to/my-skill/")
        prompts = registry.get_active_prompts(task_type="agent", language="python")
        await registry.uninstall("my-skill")
    """

    def __init__(self, install_root: str | None = None, data_dir: str | None = None, adapters=None):
        if install_root:
            self._install_root = install_root
        elif data_dir:
            self._install_root = os.path.join(data_dir, "skills", "installed")
        else:
            # Phase 48: 项目级 skills 目录（src-python/skills）
            self._install_root = os.path.join(
                os.path.dirname(__file__), "..", "..", "skills", "installed",
            )
        os.makedirs(self._install_root, exist_ok=True)
        self._skills: dict[str, Skill] = {}
        self._adapters = adapters or {}

    async def load_installed(self) -> int:
        """启动时从安装目录加载所有已安装 Skill"""
        count = 0
        if not os.path.isdir(self._install_root):
            return 0
        for dirname in sorted(os.listdir(self._install_root)):
            skill_dir = os.path.join(self._install_root, dirname)
            if not os.path.isdir(skill_dir):
                continue
            yaml_path = os.path.join(skill_dir, "skill.yaml")
            if not os.path.exists(yaml_path):
                continue
            try:
                skill = self._parse_yaml(yaml_path, skill_dir)
                if skill:
                    self._skills[skill.id] = skill
                    count += 1
            except Exception:
                _log.exception("Failed to load skill: %s", dirname)
        _log.info("Loaded %d installed skills", count)
        return count

    async def install_system_skills(self) -> dict:
        """首次启动时，自动安装系统预装 Skill（从 skills/system/ → skills/installed/）。
        幂等——已安装则跳过。

        返回 {"count": 新安装数量, "installed_ids": 新安装 skill ID 列表}
        供 lifespan 推送前端 Toast 通知。
        """
        system_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "skills", "system",
        )
        if not os.path.isdir(system_dir):
            _log.info("No system skills directory found")
            return {"count": 0, "installed_ids": []}

        count = 0
        installed_ids: list[str] = []
        for dirname in sorted(os.listdir(system_dir)):
            src = os.path.join(system_dir, dirname)
            if not os.path.isdir(src):
                continue
            yaml_path = os.path.join(src, "skill.yaml")
            if not os.path.exists(yaml_path):
                continue

            # 检查是否已安装
            dest = os.path.join(self._install_root, dirname)
            if os.path.exists(os.path.join(dest, "skill.yaml")):
                # 已安装——确保已加载到内存
                if dirname not in self._skills:
                    skill = self._parse_yaml(
                        os.path.join(dest, "skill.yaml"), dest,
                    )
                    if skill:
                        skill.is_system = True
                        self._skills[skill.id] = skill
                        await self._activate(skill)  # Phase 31 fix: re-activate on reload
                continue

            # 首次安装——复制到 installed/
            await self._copy_dir(src, dest)
            skill = self._parse_yaml(
                os.path.join(dest, "skill.yaml"), dest,
            )
            if skill:
                skill.is_system = True
                skill.installed_at = _time.time()
                skill.install_path = dest
                self._skills[skill.id] = skill
                count += 1
                installed_ids.append(skill.id)
                _log.info("Installed system skill: %s v%s", skill.id, skill.version)

        _log.info("System skills: %d installed (%d total loaded)", count, len(self._skills))
        return {"count": count, "installed_ids": installed_ids}

    # ---- 安装/卸载 ----

    async def install_from_dir(self, source_dir: str) -> Skill | None:
        """从本地目录安装 Skill——解析 skill.yaml → 复制到 installed/ → 激活"""
        yaml_path = os.path.join(source_dir, "skill.yaml")
        if not os.path.exists(yaml_path):
            _log.error("No skill.yaml found in %s", source_dir)
            return None

        skill = self._parse_yaml(yaml_path, source_dir)
        if not skill:
            return None

        # 检查依赖
        for req_id in skill.requires:
            if req_id not in self._skills:
                _log.warning("Skill '%s' requires '%s' which is not installed", skill.id, req_id)

        # 复制到 installed/
        dest_dir = os.path.join(self._install_root, skill.id)
        # P0 安全修复: 路径包含校验——dest 必须落在 install_root 内（纵深防御，
        # 正常情况下 SKILL_ID_PATTERN 已拦截穿越 id）
        if not _is_within_install_root(self._install_root, dest_dir):
            _log.error("Refusing to install skill '%s' outside install root: %s",
                       skill.id, dest_dir)
            return None
        if os.path.exists(dest_dir):
            _log.warning("Skill '%s' already installed at %s, skipping copy", skill.id, dest_dir)
        else:
            await self._copy_dir(source_dir, dest_dir)

        skill.installed_at = _time.time()
        skill.install_path = dest_dir
        self._skills[skill.id] = skill

        # 激活——注册四模块
        await self._activate(skill)

        _log.info("Installed skill: %s v%s", skill.id, skill.version)
        return skill

    async def install_from_git(self, url: str, branch: str | None = None) -> Skill | None:
        """从 Git 仓库克隆并安装 Skill（支持 skill.yaml 或 SKILL.md）。"""
        if not url:
            return None
        from services.evolution.skill_installer import clone_git_repo, find_skill_entry, install_parsed_md

        tmp = await clone_git_repo(url, branch)
        if not tmp:
            return None
        try:
            entry = find_skill_entry(tmp)
            if not entry:
                return None
            kind, directory = entry
            if kind == "yaml":
                return await self.install_from_dir(directory)
            return await install_parsed_md(self, os.path.join(directory, "SKILL.md"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def install_from_marketplace(self, skill_id: str, source: str = "agentskills.io") -> Skill | None:
        """从 agentskills.io 下载并安装 Skill。"""
        if not skill_id:
            return None
        from services.evolution.skill_installer import download_marketplace_package, find_skill_entry, install_parsed_md

        tmp = await download_marketplace_package(skill_id, source)
        if not tmp:
            return None
        try:
            entry = find_skill_entry(tmp)
            if not entry:
                return None
            kind, directory = entry
            if kind == "yaml":
                return await self.install_from_dir(directory)
            return await install_parsed_md(self, os.path.join(directory, "SKILL.md"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def parse_skill_md(path: str) -> dict | None:
        """解析 Hermes/OpenClaw 风格 SKILL.md。"""
        from services.evolution.skill_md_parser import parse_skill_md as _parser

        return _parser(path)

    async def uninstall(self, skill_id: str) -> bool:
        """卸载 Skill——注销四模块 + 删除文件"""
        skill = self._skills.get(skill_id)
        if not skill:
            return False
        if skill.is_system:
            _log.warning("Cannot uninstall system skill: %s", skill_id)
            return False

        # P0 安全修复: rmtree 前路径包含校验（在 _deactivate 之前——校验失败时
        # 保持 skill 注册状态不变，不留半注销状态）——install_path 必须位于
        # install_root 内，防止历史污染数据/被篡改的 install_path 删除任意目录
        if skill.install_path and os.path.isdir(skill.install_path):
            if not _is_within_install_root(self._install_root, skill.install_path):
                _log.error("Refusing to uninstall path outside install root: %s",
                           skill.install_path)
                return False

        # 注销
        await self._deactivate(skill)

        # 删除文件
        if skill.install_path and os.path.isdir(skill.install_path):
            shutil.rmtree(skill.install_path, ignore_errors=True)

        del self._skills[skill_id]
        _log.info("Uninstalled skill: %s", skill_id)
        return True

    def get(self, skill_id: str) -> Skill | None:
        return self._skills.get(skill_id)

    def list_installed(self, enabled_only: bool = False) -> list[Skill]:
        skills = list(self._skills.values())
        if enabled_only:
            skills = [s for s in skills if s.enabled]
        return sorted(skills, key=lambda s: (not s.is_system, s.title))

    def toggle(self, skill_id: str) -> bool:
        """启用/禁用 Skill——同时持久化到 skill.yaml"""
        skill = self._skills.get(skill_id)
        if not skill:
            return False
        skill.enabled = not skill.enabled
        if skill.install_path:
            yaml_path = os.path.join(skill.install_path, "skill.yaml")
            if os.path.exists(yaml_path):
                try:
                    import yaml
                    with open(yaml_path, encoding="utf-8") as f:
                        data = yaml.safe_load(f) or {}
                    data["enabled"] = skill.enabled
                    with open(yaml_path, "w", encoding="utf-8") as f:
                        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
                except Exception:
                    _log.warning("Failed to persist enabled state for skill '%s'", skill_id)
        _log.info("Skill '%s' %s", skill_id, "enabled" if skill.enabled else "disabled")
        return True

    # ---- Phase 33: Skill 推荐引擎 ----
    def get_recommended_skills(self, project_language: str = "",
                                 recent_tasks: list[str] | None = None) -> list[dict]:
        """根据项目语言和最近任务推荐合适的 Skill。"""
        recommended = []
        for skill in self._skills.values():
            if not skill.enabled or skill.is_system:
                continue
            reason = None
            if project_language:
                for sp in skill.prompts:
                    if project_language in sp.scope_languages:
                        reason = f"匹配语言 {project_language}"
                        break
            if not reason and recent_tasks:
                task_text = " ".join(recent_tasks).lower()
                for tag in skill.tags:
                    if tag.lower() in task_text:
                        reason = f"匹配标签 {tag}"
                        break
                if not reason:
                    desc = skill.description.lower()
                    keywords = ["review", "test", "fix", "code", "doc", "build", "security"]
                    if desc and any(k in task_text and k in desc for k in keywords):
                        reason = "匹配任务类型"
            if reason:
                recommended.append({"skill_id": skill.id, "title": skill.title,
                                     "category": skill.category, "reason": reason})
        return sorted(recommended, key=lambda r: 0 if r["category"] == "workflow" else 1)

    # ---- 运行时加载（核心——每轮 _run_agent 前调用）----

    def get_active_prompts(
        self, task_type: str = "agent", language: str = "",
        framework: str = "", mode: str = "agent",
    ) -> list[SkillPrompt]:
        """返回匹配当前上下文的 Skill prompt 段（按 priority 降序）。

        匹配规则: (task_type in scope_tasks OR scope_tasks is empty)
                 AND (language in scope_languages OR scope_languages is empty)
                 AND (framework in scope_frameworks OR scope_frameworks is empty)
                 AND (mode in scope_modes OR scope_modes is empty)
        """
        matched: list[SkillPrompt] = []
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            for sp in skill.prompts:
                if self._scope_matches(sp, task_type, language, framework, mode):
                    matched.append(sp)
        matched.sort(key=lambda sp: -sp.priority)
        return matched

    def get_active_workflows(self) -> list[dict]:
        """获取所有已激活 Skill 的 DAG 模板"""
        result = []
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            result.extend(skill.workflows)
        return result

    def get_active_commands(self) -> list[dict]:
        """获取所有已激活 Skill 的 Slash Command"""
        result = []
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            result.extend(skill.commands)
        return result

    def get_active_metadata(
        self, task_type: str = "agent", language: str = "",
        framework: str = "", mode: str = "agent",
    ) -> str:
        """Phase OH.6: Skill 挂载模式 — 强制"先读再执行"工作流约束。

        对标 OpenHands containerized mount points: 只注入挂载点元数据，
        Agent 必须主动 read_file 加载完整 SKILL.md 后才能使用技能。
        与 get_active_prompts 的区别: 不加载完整文本 + 强制"先读"约束。

        Phase 47 修复: 原 impl 引用 sp.skill_name / sp.source_file（SkillPrompt
        dataclass 无此字段），任何 Skill 激活即抛 AttributeError。改为直接迭代
        skills，用 skill.title + skill.install_path/SKILL.md 构造挂载点。
        """
        # 收集 (title, source_file, text, priority)——每个 skill 只取首个匹配 prompt
        matched: list[tuple[str, str, str, int]] = []
        for skill in self._skills.values():
            if not skill.enabled:
                continue
            for sp in skill.prompts:
                if self._scope_matches(sp, task_type, language, framework, mode):
                    source_file = (
                        os.path.join(skill.install_path, "SKILL.md")
                        if skill.install_path else ""
                    )
                    matched.append((skill.title, source_file, sp.text, sp.priority))
                    break  # 每个 skill 只取第一个匹配 prompt
        if not matched:
            return ""
        matched.sort(key=lambda x: -x[3])  # priority 降序
        lines = [
            "## 已挂载技能 (只读挂载点)",
            "<skill_mount_rule>",
            "以下技能已挂载到你的工作区。每个技能是一个只读挂载点——",
            "你**必须**先用 read_file 读取对应的 SKILL.md 文件，",
            "理解技能的全部能力和约束后，才能执行该技能描述的任务。",
            "禁止跳过读取步骤直接猜测技能行为。",
            "读取后你会发现: 完整prompt模板·DAG定义·工具约束·安全规则。",
            "</skill_mount_rule>",
        ]
        for title, source_file, text, _ in matched[:SKILL_REGISTRY_ACTIVE_PROMPTS_LIMIT]:
            lines.append(
                f"- **{title or '?'}**: {text[:SKILL_REGISTRY_PROMPT_MOUNT_PREVIEW_CHARS]}\n"
                f"  → 挂载点: `{source_file or '?'}`"
            )
        return "\n".join(lines)

    @staticmethod
    def _scope_matches(sp: SkillPrompt, task_type: str, language: str,
                       framework: str, mode: str) -> bool:
        """检查 SkillPrompt 的 scope 是否匹配当前上下文"""
        # 空 scope = 匹配全部
        if sp.scope_tasks and task_type not in sp.scope_tasks:
            return False
        if sp.scope_languages and language not in sp.scope_languages:
            return False
        if sp.scope_frameworks and framework not in sp.scope_frameworks:
            return False
        if sp.scope_modes and mode not in sp.scope_modes:
            return False
        return True

    # ---- 内部 ----

    def _parse_yaml(self, yaml_path: str, skill_dir: str) -> Skill | None:
        """解析 skill.yaml → Skill 对象。

        三级降级：
          1. yaml.safe_load（UTF-8/GBK 多编码降级由 _read_file_text 处理）
          2. _close_unclosed_single_quotes 修复后重试（多行单引号未闭合）
          3. _extract_minimal_fields 正则提取标量字段（跳过损坏 description）
        """
        try:
            import yaml
        except ImportError:
            _log.error("PyYAML not available")
            return None

        raw_text = _read_file_text(yaml_path)
        data: dict = {}
        try:
            data = yaml.safe_load(raw_text) or {}
        except yaml.YAMLError:
            # 损坏文件兜底 1：闭合未关闭的单引号后重试
            try:
                fixed_text = _close_unclosed_single_quotes(raw_text)
                data = yaml.safe_load(fixed_text) or {}
                _log.warning("Recovered corrupted YAML (closed unclosed quotes): %s", yaml_path)
            except yaml.YAMLError:
                # 损坏文件兜底 2：正则提取标量字段，跳过损坏的 description
                data = _extract_minimal_fields(raw_text)
                if not data.get("name"):
                    _log.error("Cannot recover skill name from %s", yaml_path)
                    return None
                _log.warning(
                    "Recovered corrupted YAML (minimal fields extract, "
                    "description skipped): %s", yaml_path,
                )
            except Exception:
                _log.exception("Failed to parse %s", yaml_path)
                return None
        except Exception:
            _log.exception("Failed to parse %s", yaml_path)
            return None

        # P0 安全修复: skill id 白名单校验——恶意 skill.yaml name: "../../Users/x"
        # 可在 install/uninstall 时穿越到安装目录外，此处 fail-closed 拒绝加载
        # name 为主键（对齐 agentskills.io 开放标准）；id 仅作遗留回退——
        # 早期自动生成的 skill.yaml 误用 id 作主键，此处兼容磁盘上已存在的文件。
        # 两个来源均同样经过下面的 SKILL_ID_PATTERN 白名单校验，不削弱防穿越能力。
        raw_name = data.get("name") or data.get("id") or ""
        if not raw_name or not SKILL_ID_PATTERN.match(str(raw_name)):
            _log.error("Invalid skill id %r in %s (rejected by whitelist)",
                       raw_name, yaml_path)
            return None

        skill = Skill(
            id=str(raw_name),
            version=str(data.get("version", "1.0.0")),
            title=data.get("title", data.get("name", "")),
            description=data.get("description", ""),
            author=data.get("author", ""),
            tags=data.get("tags", []),
            category=data.get("category", "general"),
            icon=data.get("icon", "📦"),
            requires=data.get("requires", []),
            min_yuncat=data.get("min_yuncat", APP_VERSION),
            install_path=skill_dir,
        )

        # 加载 prompt 模块
        prompt_cfg = data.get("prompt", {})
        if prompt_cfg:
            prompt_file = prompt_cfg.get("file", "")
            if prompt_file:
                prompt_path = os.path.join(skill_dir, prompt_file)
                if os.path.exists(prompt_path):
                    prompt_text = _read_file_text(prompt_path)
                    scope = prompt_cfg.get("scope", {})
                    skill.prompts.append(SkillPrompt(
                        text=prompt_text,
                        placement=prompt_cfg.get("placement", "append"),
                        priority=prompt_cfg.get("priority", 50),
                        scope_tasks=scope.get("tasks", []),
                        scope_languages=scope.get("languages", []),
                        scope_frameworks=scope.get("frameworks", []),
                        scope_modes=scope.get("modes", []),
                    ))

        # 加载 workflow 模块
        wf_cfg = data.get("workflow", {})
        if wf_cfg:
            wf_file = wf_cfg.get("file", "")
            if wf_file:
                wf_path = os.path.join(skill_dir, wf_file)
                if os.path.exists(wf_path):
                    try:
                        import yaml
                        wf_data = yaml.safe_load(_read_file_text(wf_path)) or {}
                        if isinstance(wf_data, list):
                            skill.workflows = wf_data
                        elif isinstance(wf_data, dict):
                            skill.workflows = [wf_data]
                    except Exception:
                        _log.warning("Failed to load workflow from %s", wf_file)

        # 加载 commands 模块
        cmd_cfg = data.get("commands", {})
        if cmd_cfg:
            cmd_file = cmd_cfg.get("file", "")
            if cmd_file:
                cmd_path = os.path.join(skill_dir, cmd_file)
                if os.path.exists(cmd_path):
                    try:
                        import yaml
                        cmd_data = yaml.safe_load(_read_file_text(cmd_path)) or []
                        skill.commands = cmd_data if isinstance(cmd_data, list) else [cmd_data]
                    except Exception:
                        _log.warning("Failed to load commands from %s", cmd_file)

        # 加载 roles 模块
        roles_cfg = data.get("roles", {})
        if roles_cfg:
            role_file = roles_cfg.get("file", "")
            if role_file:
                role_path = os.path.join(skill_dir, role_file)
                if os.path.exists(role_path):
                    try:
                        import yaml
                        role_data = yaml.safe_load(_read_file_text(role_path)) or []
                        skill.roles = role_data if isinstance(role_data, list) else [role_data]
                    except Exception:
                        _log.warning("Failed to load roles from %s", role_file)

        # rules 目录
        rules_dir = data.get("rules", {}).get("directory", "")
        if rules_dir:
            skill.rules_dir = os.path.join(skill_dir, rules_dir)

        # Phase 29 v2: 运行时配置
        config_data = data.get("config")
        if config_data:
            skill._config_schema = config_data

        return skill

    async def _activate(self, skill: Skill) -> None:
        """激活 Skill——将四模块注册到对应的运行系统中"""
        # 1. Workflows → workflow_templates BUILTIN_TEMPLATES（动态追加）
        if skill.workflows:
            from prompts.workflow_templates import BUILTIN_TEMPLATES
            for wf in skill.workflows:
                wf_name = wf.get("name", "")
                if wf_name and not any(t["name"] == wf_name for t in BUILTIN_TEMPLATES):
                    BUILTIN_TEMPLATES.append(wf)
                    _log.info("Skill '%s': registered workflow '%s'", skill.id, wf_name)

        # 2. Commands → SlashCommandRegistry
        if skill.commands:
            try:
                from commands.registry import SlashCommand, get_registry
                reg = get_registry()
                for cmd_data in skill.commands:
                    if cmd_data.get("slash_command") and cmd_data.get("name"):
                        cmd = SlashCommand(cmd_data)
                        reg._commands[cmd.slash_command] = cmd
                        _log.info("Skill '%s': activated command '%s'", skill.id, cmd.slash_command)
            except Exception:
                _log.exception("Failed to activate commands for skill '%s'", skill.id)

        # 3. Roles → DB（如果提供了 DB 连接，插入角色）
        # (在 main.py lifespan 中通过 _seed_roles 处理)

    async def _deactivate(self, skill: Skill) -> None:
        """注销 Skill"""
        if skill.workflows:
            from prompts.workflow_templates import BUILTIN_TEMPLATES
            wf_names = {w["name"] for w in skill.workflows if "name" in w}
            idx = 0
            while idx < len(BUILTIN_TEMPLATES):
                if BUILTIN_TEMPLATES[idx].get("name") in wf_names:
                    BUILTIN_TEMPLATES.pop(idx)
                else:
                    idx += 1

    # ============================================================
    # Phase 29 v2: 冲突检测 + 安全沙箱 + 运行时配置
    # ============================================================

    def detect_conflicts(self, skill: Skill) -> list[dict]:
        """安装前检测 Skill 与已安装 Skill 的冲突。
        返回冲突列表（空=无冲突，可以安全安装）。
        """
        conflicts = []

        # 1. Workflow 同名冲突
        for wf in skill.workflows:
            wf_name = wf.get("name", "")
            if not wf_name:
                continue
            for existing_id, existing_skill in self._skills.items():
                for ewf in existing_skill.workflows:
                    if ewf.get("name") == wf_name:
                        conflicts.append({
                            "type": "workflow_name_conflict",
                            "module": "workflow",
                            "new_skill": skill.id,
                            "existing_skill": existing_id,
                            "detail": f"DAG 模板 '{wf_name}' 已由 Skill '{existing_id}' 注册",
                        })

        # 2. Slash Command 冲突（同 /command）
        for cmd in skill.commands:
            sc = cmd.get("slash_command", "")
            if not sc:
                continue
            try:
                from commands.registry import get_registry
                reg = get_registry()
                existing = reg.get(sc)
                if existing:
                    conflicts.append({
                        "type": "slash_command_conflict",
                        "module": "command",
                        "new_skill": skill.id,
                        "detail": f"命令 '{sc}' 已被注册",
                    })
            except Exception:
                _log.debug("Unexpected error", exc_info=True)

        # 3. Prompt placement 同段（同为 append+相同 priority）
        for sp in skill.prompts:
            for existing_id, existing_skill in self._skills.items():
                for esp in existing_skill.prompts:
                    if esp.placement == sp.placement and esp.priority == sp.priority:
                        conflicts.append({
                            "type": "prompt_priority_conflict",
                            "module": "prompt",
                            "new_skill": skill.id,
                            "existing_skill": existing_id,
                            "detail": f"Prompt 段 placement='{sp.placement}' priority={sp.priority} 与 Skill '{existing_id}' 冲突",
                        })

        return conflicts

    def get_config_schema(self, skill_id: str) -> dict | None:
        """获取 Skill 的运行时配置 schema（从 skill.yaml config 段读取）"""
        skill = self._skills.get(skill_id)
        if not skill:
            return None
        return getattr(skill, "_config_schema", None)

    # ============================================================
    # Phase 29 v3: 安全沙箱 + 评分系统 + 创作工具
    # ============================================================

    def check_permissions(self, skill: Skill) -> dict:
        """安装前安全审查——规则引擎 + LLM 第二道防线。

        返回: {"safe": bool, "warnings": [...], "required_permissions": {...}, "llm_review": str|None}
        """
        warnings = []
        perms = dict(skill.permissions)

        # 规则引擎: 检测 prompt 是否有注入风险关键词
        for sp in skill.prompts:
            risky = ["ignore previous", "forget rules", "bypass", "override system",
                     "execute arbitrary", "sudo", "rm -rf", "DROP TABLE"]
            for kw in risky:
                if kw.lower() in sp.text.lower():
                    warnings.append({
                        "type": "prompt_injection_risk",
                        "detail": f"Prompt 包含潜在注入关键词: '{kw}'",
                        "severity": "high",
                    })

        # 检测是否有文件/命令操作声明
        if skill.workflows or any("write" in sp.text.lower() or "file" in sp.text.lower() for sp in skill.prompts):
            perms["allow_file_access"] = True
        if any("command" in sp.text.lower() or "exec" in sp.text.lower() or "shell" in sp.text.lower() for sp in skill.prompts):
            perms["allow_command_exec"] = True
            warnings.append({
                "type": "command_exec_risk",
                "detail": "Skill 可能执行系统命令——安装后请审查",
                "severity": "medium",
            })

        return {
            "safe": not any(w["severity"] == "high" for w in warnings),
            "warnings": warnings,
            "required_permissions": perms,
        }

    async def deep_security_review(self, skill: Skill, model_config: dict | None = None) -> dict:
        """Phase 33: LLM 深度安全审查——作为规则引擎的第二道防线。
        需要模型配置提供 API key。
        """
        adapter = self._adapters.get(DEFAULT_AGENT_ID)
        if not adapter:
            return {"reviewed": False, "reason": "No adapter available"}
        if not model_config:
            return {"reviewed": False, "reason": "No model config available"}
        if not model_config.get("api_key"):
            return {"reviewed": False, "reason": "No API key configured"}
        try:
            prompt_text = ""
            for sp in skill.prompts:
                prompt_text += sp.text[:SKILL_REGISTRY_DEEP_REVIEW_PROMPT_CHARS] + "\n\n"
            if not prompt_text:
                prompt_text = skill.description

            result = await adapter.invoke(
                f"Skill: {skill.title}\n{skill.description}\n\nPrompts:\n{prompt_text[:SKILL_REGISTRY_REVIEW_INPUT_MAX]}",
                context={
                    "system": "你是安全审查专家。审查以下 Skill 的 prompt 内容是否存在安全风险（注入攻击、越权操作、恶意代码）。用 JSON 输出: {\"risk_level\": \"none|low|medium|high|critical\", \"findings\": [\"发现1\", \"发现2\"], \"safe_to_install\": true/false}",
                    "model_config": model_config,
                    "max_tokens": MAX_TOKENS_SKILL_REGISTRY,
                    "temperature": 0.0,
                },
            )
            if result.get("error"):
                return {"reviewed": False, "reason": result.get("error")}
            content = result.get("content", "")
            try:
                review = _json.loads(content[content.find("{"):content.rfind("}")+1])
                return {"reviewed": True, **review}
            except Exception:
                return {"reviewed": True, "raw_review": content[:ERROR_PREVIEW_CHARS]}
        except Exception:
            _log.debug("Unexpected error", exc_info=True)
        return {"reviewed": False, "reason": "LLM review failed"}

    def rate_skill(self, skill_id: str, rating: int, user_id: str = "") -> bool:
        """用户评分——写入内存评分缓存（持久化需 DB 连接，由调用方传入）"""
        if skill_id not in self._skills:
            return False
        if not hasattr(self, '_ratings_cache'):
            self._ratings_cache = {}
        ratings = self._ratings_cache.setdefault(skill_id, [])
        ratings.append(rating)
        _log.info("Skill '%s' rated %d/5 by %s (total: %d ratings)", skill_id, rating, user_id or "anonymous", len(ratings))
        return True

    def get_rating(self, skill_id: str) -> dict:
        """获取 Skill 评分统计——从内存缓存计算"""
        ratings = getattr(self, '_ratings_cache', {}).get(skill_id, [])
        avg = round(sum(ratings) / len(ratings), 1) if ratings else 0
        return {
            "skill_id": skill_id,
            "avg_rating": avg,
            "total_ratings": len(ratings),
        }

    @staticmethod
    async def _copy_dir(src: str, dst: str) -> None:
        """复制目录（不含 __pycache__）"""
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))


# ============================================================
# SkillMarketplace + get_marketplace 已抽到 services/evolution/skill_marketplace.py
# ============================================================


# 全局单例
_registry: SkillRegistry | None = None


def get_skill_registry(data_dir: str | None = None, adapters=None) -> SkillRegistry:
    global _registry
    if _registry is None:
        _registry = SkillRegistry(data_dir=data_dir, adapters=adapters)
    return _registry
