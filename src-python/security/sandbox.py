"""security/sandbox.py —— 工作区隔离与路径验证

v40: 抽取纯函数 canonicalize（boundary 可注入）——harness Phase B path_canonicalize 前置
v39: Agent 工作区子目录隔离 + validate_path 路径穿越防护
"""
import logging
import os
from pathlib import Path

_log = logging.getLogger("yuncat.sandbox")


def _resolve_workspace_base() -> str:
    """解析工作区根目录——未展开的 `%APPDATA%` 或相对路径一律回退默认。

    与 settings.get_data_dir 同一类缺陷：父进程把未展开变量当路径传进来时，
    后端会凭空在当前目录建出字面量 `%APPDATA%` 之类的目录。
    """
    default = os.path.join(os.path.expanduser("~"), "YuncatWorkspace")
    raw = os.environ.get("YUNCAT_WORKSPACE") or default
    if "%" in raw or not os.path.isabs(raw):
        _log.warning("YUNCAT_WORKSPACE=%r 无效（未展开变量/相对路径），回退 %s", raw, default)
        return default
    return raw


def get_workspace() -> Path:
    """获取 Yuncat 工作区根目录。

    默认 ~/YuncatWorkspace/，可通过 YUNCAT_WORKSPACE 环境变量覆盖。
    """
    ws = Path(_resolve_workspace_base())
    ws.mkdir(parents=True, exist_ok=True)
    shared = ws / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    return ws


def get_shared_workspace() -> Path:
    """用户文件区——Agent 只读"""
    # Path(...) 包一层：测试里有人把 get_workspace 桩成返回字符串，
    # 直接 `get_workspace() / "shared"` 会抛 "unsupported operand type(s) for /"。
    shared = Path(get_workspace()) / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    return shared


def get_agent_workspace(agent_id: str, session_id: str) -> Path:
    """每个 Agent+Session 获得独立子目录——并行 Agent 互不冲突"""
    base = Path(get_workspace())
    agent_dir = base / "agents" / f"{agent_id}-{session_id}"
    agent_dir.mkdir(parents=True, exist_ok=True)
    return agent_dir


def get_pending_dir() -> Path:
    """二阶段确认的文件暂存目录"""
    pending = Path(get_workspace()) / ".pending"
    pending.mkdir(parents=True, exist_ok=True)
    return pending


def get_backup_dir() -> Path:
    """auto_backup 规则的备份目录"""
    backup = Path(get_workspace()) / ".backup"
    backup.mkdir(parents=True, exist_ok=True)
    return backup


def canonicalize(
    path: str | os.PathLike,
    root: Path,
    boundary: Path | None = None,
    *,
    must_exist: bool = False,
) -> Path:
    """路径规范化：resolve + boundary 边界校验（纯函数，无写副作用）。

    - root: 解析起点（相对路径的锚点）
    - boundary: 允许的上界；默认 = root（即默认不允许离开 root）
    - must_exist: True 时路径必须已存在，否则 FileNotFoundError
    - 返回解析后的绝对 Path；越界抛 PermissionError；
      root ⊄ boundary 抛 ValueError（配置错误快速失败）
    - symlink 处理: Path.resolve() 默认跟随 symlink（strict=False），所以
      `boundary/evil -> /etc/passwd` 会被解析为 /etc/passwd，boundary 检查拒绝。
      不用 strict=True——会在路径不存在时抛错，破坏 write_file 新文件创建。
    - 纯函数：不 mkdir、不读环境变量——可安全用于 harness 审批前的路径裁决。
    """
    root = Path(root).resolve()
    boundary = Path(boundary).resolve() if boundary is not None else root
    if not root.is_relative_to(boundary):
        raise ValueError(f"root 不在 boundary 内: {root} 越出 {boundary}")
    # 解析路径：处理 .. 和符号链接（resolve 自动跟随 symlink，外部目标会被 boundary 拒绝）
    full = (root / path).resolve()
    # 确保在 boundary 内
    if not full.is_relative_to(boundary):
        raise PermissionError(
            f"路径超出工作区范围: {path} → {full}\n"
            f"边界: {boundary}"
        )
    if must_exist and not full.exists():
        raise FileNotFoundError(f"路径不存在: {full}")
    return full


def validate_path(path: str, base: Path | None = None) -> Path:
    """验证路径在工作区内，拒绝路径穿越（canonicalize 薄壳，行为与 v39 一致）。

    相对路径从 base（默认 workspace 根目录）解析；安全边界恒为 workspace 根目录。
    base 只是解析锚点，不是边界——与 Cursor 一致：用户选择的文件夹 = workspace，
    文件操作在此范围内。返回解析后的绝对路径 Path 对象。
    """
    workspace = Path(get_workspace()).resolve()
    return canonicalize(path, root=base or workspace, boundary=workspace)
