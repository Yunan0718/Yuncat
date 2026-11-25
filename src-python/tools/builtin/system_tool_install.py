"""system_tool_install.py —— AI 自动配置工具（MCP / Skill / DAG 安装）"""
import os as _os2
import sys as _sys
from urllib.parse import urlparse as _urlparse

from ..registry import tool
from config import ERROR_PREVIEW_CHARS, INSTALL_README_PREVIEW_MAX_CHARS
# Phase 47 修复: 提升到 security/url_guard.py 统一 SSRF 防护
from security.url_guard import (
    ALLOWED_HOSTS as _ALLOWED_SKILL_HOSTS,
    validate_external_url,
)


def _validate_skill_url(url: str) -> bool:
    """Phase 48 T1-4: URL 白名单校验——委托给 security.url_guard

    保留布尔返回以兼容现有调用方；新代码应直接用 validate_external_url。
    """
    try:
        validate_external_url(url)
        return True
    except ValueError:
        return False


def _safe_extract_tar(tar, target_dir: str) -> None:
    """Phase 48 T1-4: 安全解压 tarfile 防 Zip Slip / 路径穿越

    Python 3.12+ 用 PEP 706 `filter='data'`（拒绝绝对路径/外链/设备文件）；
    <3.12 手动 realpath 校验每个 member 不逃出 target_dir。
    """
    if _sys.version_info >= (3, 12):
        tar.extractall(target_dir, filter="data")
    else:
        base = _os2.path.realpath(target_dir)
        for member in tar.getmembers():
            member_path = _os2.path.realpath(_os2.path.join(target_dir, member.name))
            if not (member_path == base or member_path.startswith(base + _os2.sep)):
                raise ValueError(f"Unsafe tar member path (path traversal): {member.name}")
        tar.extractall(target_dir)


def _safe_extract_zip(zf, target_dir: str) -> None:
    """Phase 48 T1-4: 安全解压 zipfile 防 Zip Slip

    zipfile 在 Python 3.12 仍未提供 filter 参数（PEP 706 仅覆盖 tarfile），
    因此统一走手动 realpath 校验每个 entry 不逃出 target_dir。
    """
    base = _os2.path.realpath(target_dir)
    for name in zf.namelist():
        member_path = _os2.path.realpath(_os2.path.join(target_dir, name))
        if not (member_path == base or member_path.startswith(base + _os2.sep)):
            raise ValueError(f"Unsafe zip member path (path traversal): {name}")
    zf.extractall(target_dir)


@tool(
    name="install_skill_from_url", agents=["hermes", "openclaw"],
    description=(
        "Download and install a Skill from a URL (GitHub raw URL, ClawHub, etc). "
        "The URL must point to a skill.yaml file or a directory containing one. "
        "Use this when the user asks to install a skill from GitHub or the web."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to the skill.yaml or skill directory"},
            "skill_name": {"type": "string", "description": "Optional name for the installed skill (auto-detected if omitted)"},
        },
        "required": ["url"],
    },
    confirmable=True,  # Phase 48 T1-4: 从 URL 安装代码需用户确认
    is_concurrency_safe=True,
    groups=["config"],
    tool_timeout=300,  # P2-2.4: HTTP 下载 + 解压超时
)
async def install_skill_from_url(url: str, skill_name: str = "") -> dict:
    """从 URL 下载并安装 Skill。支持 GitHub raw、ClawHub、AgentSkills 等官方源。"""
    from tools.result import ToolResult
    import aiohttp, tempfile, os as _os, shutil as _shutil

    # Phase 48 T1-4: URL 白名单校验防 SSRF / 恶意源
    if not _validate_skill_url(url):
        return ToolResult(
            success=False,
            error=f"URL host not in allowlist (allowed: {sorted(_ALLOWED_SKILL_HOSTS)})",
            meta={"tool_name": "install_skill_from_url", "url": url},
        ).to_dict()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return ToolResult(success=False, error=f"HTTP {resp.status} fetching {url}",
                                      meta={"tool_name": "install_skill_from_url"}).to_dict()
                content = await resp.read()

        # 判断是单个 skill.yaml 还是 zip/tar.gz 压缩包
        url_lower = url.lower()
        # Phase 5 W: 单一 try/finally 覆盖所有 tmp 清理路径——
        # 解决原代码 3 处泄漏:(1) archive 分支 return 不清理 (2) 单 yaml 分支无 try
        # (3) install_from_dir 抛异常时 rmtree 不可达
        tmp: str | None = None
        try:
            if url_lower.endswith((".zip", ".tar.gz", ".tgz")):
                import zipfile, io
                tmp = tempfile.mkdtemp(prefix="yuncat_skill_")
                if url_lower.endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(content)) as z:
                        _safe_extract_zip(z, tmp)  # Phase 48 T1-4: 安全解压
                else:
                    import tarfile
                    with tarfile.open(fileobj=io.BytesIO(content)) as t:
                        _safe_extract_tar(t, tmp)  # Phase 48 T1-4: 安全解压
                # 找到 skill.yaml
                for root, _, files in _os.walk(tmp):
                    if "skill.yaml" in files:
                        src_dir = root
                        break
                else:
                    return ToolResult(success=False, error="No skill.yaml found in archive",
                                      meta={"tool_name": "install_skill_from_url"}).to_dict()
            else:
                # 单个 skill.yaml 文件
                tmp = tempfile.mkdtemp(prefix="yuncat_skill_")
                yaml_path = _os.path.join(tmp, "skill.yaml")
                with open(yaml_path, "wb") as f:
                    f.write(content)
                src_dir = tmp

            # 安装
            from services.evolution.skill_registry import get_skill_registry
            sr = get_skill_registry()
            skill = await sr.install_from_dir(src_dir)

            if skill:
                return ToolResult(
                    success=True,
                    data={"skill_id": skill.id, "title": skill.title, "version": skill.version,
                          "prompts": len(skill.prompts), "workflows": len(skill.workflows)},
                    observation=f"成功安装 Skill: {skill.title} v{skill.version}",
                    meta={"tool_name": "install_skill_from_url", "source": url},
                ).to_dict()
            return ToolResult(success=False, error="Failed to install skill",
                              meta={"tool_name": "install_skill_from_url"}).to_dict()
        finally:
            if tmp:
                _shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        return ToolResult(success=False, error=str(e)[:ERROR_PREVIEW_CHARS],
                          meta={"tool_name": "install_skill_from_url"}).to_dict()


@tool(
    name="configure_mcp", agents=["hermes", "openclaw"],
    description=(
        "Auto-configure an MCP (Model Context Protocol) server from a GitHub URL or npm package. "
        "Use this when the user asks to 'configure MCP' or 'add MCP server X'. "
        "Reads the repo's README/package.json to detect the MCP command and installs config."
    ),
    parameters={
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "GitHub URL (e.g. https://github.com/modelcontextprotocol/servers) or npm package name"},
            "server_name": {"type": "string", "description": "Name for the MCP server (auto-detected if omitted)"},
        },
        "required": ["source"],
    },
    is_concurrency_safe=True,
    groups=["config"],
)
async def configure_mcp(source: str, server_name: str = "") -> dict:
    """自动配置 MCP 服务器——从 GitHub/npm 源安装。"""
    from tools.result import ToolResult
    import aiohttp, os as _os, json as _json2
    from urllib.parse import urlparse as _urlparse
    from security.url_guard import validate_external_url
    import re as _re_url

    # H2 修复：npm 包名格式验证（@scope/name 或 name）
    _NPM_PKG_PATTERN = _re_url.compile(r'^(@[\w.-]+/)?[\w][\w.-]*$')

    try:
        # H2 修复：精确 hostname 匹配 + validate_external_url 白名单校验防 SSRF
        is_url = source.startswith("http://") or source.startswith("https://")
        is_github = False
        if is_url:
            try:
                safe_url = validate_external_url(source)
            except ValueError as e:
                return ToolResult(success=False, error=f"URL rejected: {e}",
                                  meta={"tool_name": "configure_mcp"}).to_dict()
            parsed = _urlparse(safe_url)
            # H2 修复：精确 hostname 匹配（非子串检查），含 www.github.com
            is_github = parsed.hostname in ("github.com", "www.github.com")
            if not is_github:
                return ToolResult(
                    success=False,
                    error=f"Only GitHub URLs supported (got hostname: {parsed.hostname})",
                    meta={"tool_name": "configure_mcp"},
                ).to_dict()
            source = safe_url  # 使用校验后的 URL

        if is_github:
            # 从 URL path 提取 owner/repo（比字符串分割更健壮）
            path_parts = [p for p in parsed.path.split("/") if p]
            if len(path_parts) < 2:
                return ToolResult(
                    success=False,
                    error="GitHub URL must contain owner/repo",
                    meta={"tool_name": "configure_mcp"},
                ).to_dict()
            owner, repo = path_parts[0], path_parts[1]
            api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
            if not server_name:
                server_name = repo

            async with aiohttp.ClientSession() as session:
                # 读取 README 找 MCP 配置命令
                async with session.get(
                    api_url,
                    headers={"Accept": "application/vnd.github.v3.raw"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    readme = (await resp.text())[:INSTALL_README_PREVIEW_MAX_CHARS] if resp.status == 200 else ""

            # 从 README 中提取 npx 命令
            npx_cmd = None
            patterns = [
                (r'npx\s+(-y\s+)?([\w@/.-]+)', 2),      # npx -y pkg → group 2
                (r'npm\s+exec\s+(-y\s+)?([\w@/.-]+)', 2),  # npm exec -y pkg → group 2
                (r'node\s+([\w/.-]+\.m?js)', 1),          # node server.mjs → group 1
            ]
            for pat, grp in patterns:
                m = _re_url.search(pat, readme)
                if m:
                    npx_cmd = m.group(grp)
                    break

            command = f"npx -y {npx_cmd}" if npx_cmd else f"npx -y {server_name}"
            args = []
        else:
            # H2 修复：验证 npm 包名格式，防止命令注入
            if not _NPM_PKG_PATTERN.match(source):
                return ToolResult(
                    success=False,
                    error=f"Invalid npm package name: {source!r}",
                    meta={"tool_name": "configure_mcp"},
                ).to_dict()
            command = f"npx -y {source}"
            args = []

        # 写入 MCP 配置到 Yuncat settings
        config = {
            "mcpServers": {
                server_name: {
                    "command": command.split()[0],
                    "args": command.split()[1:] + args,
                }
            }
        }

        # 持久化到 Yuncat 的 MCP 配置目录
        from config import DATA_DIR
        mcp_dir = _os.path.join(DATA_DIR, "mcp")
        _os.makedirs(mcp_dir, exist_ok=True)
        config_path = _os.path.join(mcp_dir, f"{server_name}.json")
        with open(config_path, "w", encoding="utf-8") as f:
            _json2.dump(config, f, indent=2)

        return ToolResult(
            success=True,
            data={"server_name": server_name, "command": command,
                  "config_path": config_path, "source": source},
            observation=f"已配置 MCP 服务器 '{server_name}' — {command}。重启 Yuncat 后生效。",
            meta={"tool_name": "configure_mcp"},
        ).to_dict()
    except Exception as e:
        return ToolResult(success=False, error=str(e)[:ERROR_PREVIEW_CHARS],
                          meta={"tool_name": "configure_mcp"}).to_dict()


@tool(
    name="install_workflow_from_url", agents=["hermes", "openclaw"],
    description=(
        "Download and install a DAG workflow template from a URL. "
        "The URL must point to a JSON file containing {nodes, edges}. "
        "Use this when the user wants to import a workflow template from the web."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to the workflow JSON file"},
            "template_name": {"type": "string", "description": "Name for the imported template"},
        },
        "required": ["url", "template_name"],
    },
    is_concurrency_safe=True,
    groups=["config"],
    tool_timeout=300,  # P2-2.4: HTTP 下载 + 解压超时
)
async def install_workflow_from_url(url: str, template_name: str) -> dict:
    """从 URL 下载 DAG 工作流模板并安装。"""
    from tools.result import ToolResult
    import aiohttp, uuid as _uuid3, json as _json3, time as _time3

    # Phase 47 修复: SSRF 防护——校验 URL 不指向内网/云元数据/非白名单源
    try:
        url = validate_external_url(url)
    except ValueError as e:
        return ToolResult(
            success=False,
            error=f"URL rejected: {e} (allowed: {sorted(_ALLOWED_SKILL_HOSTS)})",
            meta={"tool_name": "install_workflow_from_url", "url": url},
        ).to_dict()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return ToolResult(success=False, error=f"HTTP {resp.status}",
                                      meta={"tool_name": "install_workflow_from_url"}).to_dict()
                graph = await resp.json()

        if "nodes" not in graph:
            return ToolResult(success=False, error="Invalid workflow JSON: missing 'nodes'",
                              meta={"tool_name": "install_workflow_from_url"}).to_dict()

        # 写入 DB (Phase 47: context 单例)
        wid = str(_uuid3.uuid4())[:12]
        desc = f"从 {url} 导入的工作流模板"
        try:
            from context import get_db
            db = get_db()
            if not db:
                return ToolResult(success=False, error="DB connection not found",
                                  meta={"tool_name": "install_workflow_from_url"}).to_dict()
            await db.execute(
                "INSERT INTO workflows(id, name, description, graph, is_template, created_at, updated_at) VALUES(?,?,?,?,0,?,?)",
                (wid, template_name, desc, _json3.dumps(graph, ensure_ascii=False),
                 int(_time3.time()), int(_time3.time())),
            )
            await db.commit()
        except Exception as e:
            return ToolResult(success=False, error=f"Failed to save: {e}",
                              meta={"tool_name": "install_workflow_from_url"}).to_dict()

        return ToolResult(
            success=True,
            data={
                "workflow_id": wid, "name": template_name,
                "nodes": len(graph.get("nodes", [])),
                "edges": len(graph.get("edges", [])),
            },
            observation=f"已导入工作流模板 '{template_name}'（{len(graph.get('nodes', []))} 节点）— ID: {wid}",
            meta={"tool_name": "install_workflow_from_url", "source": url},
        ).to_dict()
    except Exception as e:
        return ToolResult(success=False, error=str(e)[:ERROR_PREVIEW_CHARS],
                          meta={"tool_name": "install_workflow_from_url"}).to_dict()
