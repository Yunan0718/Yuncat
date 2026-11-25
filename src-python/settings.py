"""src-python/settings.py — 类型安全配置 (pydantic-settings)

使用 BaseSettings 提供环境变量校验 + .env 文件支持。
config.py 中的常量和辅助函数保持不变，此模块仅管理 env-var 驱动的配置。
"""
from __future__ import annotations

import logging
import os

from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger("yuncat.settings")


def _default_data_dir() -> str:
    """平台默认数据目录——APPDATA 本身也可能是未展开的字面量。"""
    base = os.environ.get("APPDATA") or ""
    if "%" in base or not os.path.isabs(base):
        base = os.path.expanduser("~")
    return os.path.join(base, "Yuncat")


class Settings(BaseSettings):
    """Yuncat 应用配置——从环境变量和 .env 文件加载。"""

    model_config = SettingsConfigDict(
        env_prefix="YUNCAT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Token (安全: 读取后从环境变量删除，防止子进程继承) ----
    token: str = ""

    # ---- 路径 ----
    data_dir: str = ""

    # ---- 网络 ----
    host: str = "127.0.0.1"
    port: int = 0  # 0 = 动态分配

    # ---- 超时 ----
    startup_timeout_sec: int = 30
    health_check_interval_sec: int = 15

    # ---- Agent ----
    agent_health_check_interval: int = 30

    # ---- 会话 ----
    max_active_sessions: int = 50
    session_idle_ttl: int = 86400
    cleanup_interval: int = 600

    # ---- 用户 ----
    default_user_id: str = "default"

    # ---- WS 心跳 ----
    ws_heartbeat_interval_sec: int = 30
    ws_heartbeat_timeout_sec: int = 60

    # ---- LLM 响应缓存（Batch 8.4 MVP：精确哈希，默认关闭）----
    llm_cache_enabled: bool = False
    llm_cache_ttl_sec: int = 3600
    llm_cache_max_entries: int = 128

    # ---- 会话标题（自动生成 provider 润色）----
    session_title_llm_enabled: bool = True
    session_title_llm_max_tokens: int = 48
    session_title_llm_timeout_sec: int = 20

    # ---- 会话任务清单（todo_write）----
    # 允许多个 in_progress：Yuncat 的 agent 会并行起子 Agent / 后台命令，
    # 强制单活动项会让清单与真实执行状态脱节。设 False 可切回单活动纪律。
    todo_allow_parallel: bool = True

    # ---- 交付物声明（present 工具单次最多几个文件）----
    present_max_files: int = 8

    # ---- tool 结果裁剪（DSH compaction-tool-result-pruner）----
    tool_result_prune_threshold_chars: int = 4000
    tool_result_prune_head_chars: int = 1500
    tool_result_prune_tail_chars: int = 800

    # ---- 文件快照（撤销 Agent 改动 / 交付物行数统计的同源数据）----
    # 单文件上限：超过就不备份，回退时显式报告"未覆盖"，不假装成功。
    snapshot_max_file_bytes: int = 8 * 1024 * 1024
    # 目录树上限：delete_dir 的递归备份，超过则只登记不备份。
    snapshot_max_dir_bytes: int = 32 * 1024 * 1024
    # 快照保留天数：进程内首次快照时清理过期目录与行，避免无限增长。
    snapshot_retention_days: int = 7
    # 交付物行数统计的单文件读取上限：更大只标 oversized，不做 O(n²) 行对比
    deliverable_diff_max_bytes: int = 2 * 1024 * 1024
    # 单个文件 diff 的最大输出行数：超出的部分截断并显式标注（保护 UI 与内存）
    deliverable_diff_max_lines: int = 2000

    def get_data_dir(self) -> str:
        """解析数据目录——未设置时回退到 APPDATA/~/Yuncat。

        2026-10-21：仓库里出现过字面量 ``%APPDATA%\\Yuncat``（含真实库与日志），
        原因是父进程把**未展开**的 `%APPDATA%` 原样传进了环境变量。未展开变量或
        相对路径会让后端在错误位置建库，用户真实数据看起来"消失"。这里 fail-closed
        回退默认目录并留下 warning——.gitignore 只是遮住症状，根因在这一层。
        """
        candidate = self.data_dir or _default_data_dir()
        if "%" in candidate or not os.path.isabs(candidate):
            fallback = _default_data_dir()
            _log.warning(
                "数据目录 %r 不是有效绝对路径（未展开变量/相对路径），回退 %s",
                candidate, fallback,
            )
            return fallback
        return candidate

    def get_db_path(self) -> str:
        """数据库路径。"""
        return os.path.join(self.get_data_dir(), "yuncat.db")


def _create_settings() -> Settings:
    """创建 Settings 实例。

    注意: 不在此处 pop 环境变量——由 main.py 在启动时决定是否删除 TOKEN。
    这样 importlib.reload(config) 能正确读取 monkeypatched 环境变量。
    """
    return Settings()


settings = _create_settings()
