"""tests/unit/test_magic_numbers_config.py —— Phase 5 Y: Magic Numbers 集中管理验证

验证 config.py 中新增的常量段（loop intervals / confidence thresholds / LLM temperature）
存在且可导入，确保 magic numbers 有统一的命名来源。

Phase 5 Y 已完成:
- config.py 新增 ~70 个命名常量
- lifespan.py 替换 15 处 magic numbers（loop intervals / autostart / watchdog）
- 剩余 magic numbers（confidence thresholds / LLM temperature 等）常量已定义，
  调用方迁移可渐进进行（不阻塞本次交付）
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestLoopIntervalConstants:
    """Phase 5 Y: Loop interval 常量应存在于 config.py"""

    def test_loop_interval_constants_exist(self):
        """所有 LOOP_INTERVAL_* 常量应定义"""
        from config import (
            LOOP_INTERVAL_WORKFLOW_CRON_SEC,
            LOOP_INTERVAL_HEALER_SEC,
            LOOP_INTERVAL_MEMORY_TREE_SEC,
            LOOP_INTERVAL_AUTO_DREAM_SEC,
            LOOP_INTERVAL_EVOLUTION_SEC,
            LOOP_INTERVAL_MEMORY_SYNC_SEC,
            LOOP_INTERVAL_GC_AGENT_SEC,
            LOOP_INTERVAL_CLEANUP_SEC,
            LOOP_INTERVAL_APPROVAL_EXPIRE_SEC,
        )
        # 验证值合理（非零正数）
        assert LOOP_INTERVAL_WORKFLOW_CRON_SEC == 60
        assert LOOP_INTERVAL_HEALER_SEC == 30
        assert LOOP_INTERVAL_MEMORY_TREE_SEC == 900
        assert LOOP_INTERVAL_GC_AGENT_SEC == 604800  # 7 天

    def test_loop_intervals_all_positive(self):
        """所有 loop interval 应为正数"""
        from config import (
            LOOP_INTERVAL_WORKFLOW_CRON_SEC,
            LOOP_INTERVAL_HEALER_SEC,
            LOOP_INTERVAL_MEMORY_TREE_SEC,
            LOOP_INTERVAL_AUTO_DREAM_SEC,
            LOOP_INTERVAL_EVOLUTION_SEC,
            LOOP_INTERVAL_MEMORY_SYNC_SEC,
            LOOP_INTERVAL_GC_AGENT_SEC,
            LOOP_INTERVAL_CLEANUP_SEC,
            LOOP_INTERVAL_APPROVAL_EXPIRE_SEC,
        )
        for v in [
            LOOP_INTERVAL_WORKFLOW_CRON_SEC, LOOP_INTERVAL_HEALER_SEC,
            LOOP_INTERVAL_MEMORY_TREE_SEC, LOOP_INTERVAL_AUTO_DREAM_SEC,
            LOOP_INTERVAL_EVOLUTION_SEC, LOOP_INTERVAL_MEMORY_SYNC_SEC,
            LOOP_INTERVAL_GC_AGENT_SEC, LOOP_INTERVAL_CLEANUP_SEC,
            LOOP_INTERVAL_APPROVAL_EXPIRE_SEC,
        ]:
            assert isinstance(v, (int, float)) and v > 0


class TestConfidenceThresholdConstants:
    """Phase 5 Y: 置信度/分数阈值常量应存在于 config.py"""

    def test_confidence_thresholds_exist(self):
        """关键置信度阈值常量应定义"""
        from config import (
            ROUTE_AUTO_SWITCH_CONFIDENCE_THRESHOLD,
            DELEGATION_DEFAULT_SUCCESS_RATE,
            KNOWLEDGE_HIGH_SCORE_THRESHOLD,
            KNOWLEDGE_LOW_SCORE_THRESHOLD,
            MEMORY_DEFAULT_CONFIDENCE,
            TMP_ROLE_PROMOTION_THRESHOLD,
            ESCALATION_SCORE_DEFAULT,
        )
        # 验证值在 [0, 1] 范围
        assert 0 <= ROUTE_AUTO_SWITCH_CONFIDENCE_THRESHOLD <= 1
        assert 0 <= DELEGATION_DEFAULT_SUCCESS_RATE <= 1
        assert 0 <= KNOWLEDGE_HIGH_SCORE_THRESHOLD <= 1
        assert 0 <= MEMORY_DEFAULT_CONFIDENCE <= 1

    def test_feedback_deltas_exist(self):
        """反馈学习增量常量应定义"""
        from config import FEEDBACK_ACCEPT_DELTA, FEEDBACK_REJECT_DELTA
        assert FEEDBACK_ACCEPT_DELTA > 0   # 接受为正增量
        assert FEEDBACK_REJECT_DELTA < 0   # 拒绝为负增量


class TestLLMTemperatureConstants:
    """Phase 5 Y: LLM temperature 预设常量应存在于 config.py"""

    def test_temperature_constants_exist(self):
        """所有 TEMPERATURE_* 常量应定义"""
        from config import (
            TEMPERATURE_WORKFLOW_NODE,
            TEMPERATURE_FAILURE_ANALYSIS,
            TEMPERATURE_SKILL_TEST,
            TEMPERATURE_SELF_EVOLUTION,
            TEMPERATURE_WORKSPACE_LLM,
            TEMPERATURE_CODE_REVIEW,
            TEMPERATURE_SKILL_REGISTRY,
            TEMPERATURE_INTENT_CLASSIFY,
            TEMPERATURE_MEMORY_COMPACT,
        )
        # 验证值在 [0, 2] 范围（LLM temperature 通常 0-2）
        for v in [
            TEMPERATURE_WORKFLOW_NODE, TEMPERATURE_FAILURE_ANALYSIS,
            TEMPERATURE_SKILL_TEST, TEMPERATURE_SELF_EVOLUTION,
            TEMPERATURE_WORKSPACE_LLM, TEMPERATURE_CODE_REVIEW,
            TEMPERATURE_SKILL_REGISTRY, TEMPERATURE_INTENT_CLASSIFY,
            TEMPERATURE_MEMORY_COMPACT,
        ]:
            assert 0 <= v <= 2


class TestTimeoutConstants:
    """Phase 5 Y: 超时相关常量应存在于 config.py"""

    def test_adapter_timeout_constants_exist(self):
        """Adapter 启动/退出超时常量应定义"""
        from config import (
            OC_AUTOSTART_DELAY_SEC,
            OC_AUTOSTART_TIMEOUT_SEC,
            HA_AUTOSTART_TIMEOUT_SEC,
            ADAPTER_PROCESS_WAIT_TIMEOUT_SEC,
            ADAPTER_RESTART_BACKOFF_SEC,
        )
        assert OC_AUTOSTART_DELAY_SEC > 0
        assert OC_AUTOSTART_TIMEOUT_SEC > 0

    def test_terminal_mcp_timeout_constants_exist(self):
        """Terminal/MCP/Shell 超时常量应定义"""
        from config import (
            TERMINAL_CLOSE_TIMEOUT_SEC,
            TERMINAL_SESSION_MAX_AGE_SEC,
            MCP_PROCESS_CLOSE_TIMEOUT_SEC,
            MCP_REQUEST_TIMEOUT_SEC,
            SHELL_CLOSE_TIMEOUT_SEC,
        )
        assert TERMINAL_CLOSE_TIMEOUT_SEC > 0
        assert MCP_REQUEST_TIMEOUT_SEC > 0

    def test_sqlite_constants_exist(self):
        """SQLite 连接超时常量应定义"""
        from config import SQLITE_CONNECT_TIMEOUT_SEC, SQLITE_BUSY_TIMEOUT_MS
        assert SQLITE_CONNECT_TIMEOUT_SEC > 0
        assert SQLITE_BUSY_TIMEOUT_MS > 0


class TestLifespanUsesConfigConstants:
    """Phase 5 Y: lifespan 文件群应使用 config 常量而非 magic numbers

    M1 拆分后 lifespan.py 为薄编排层，常量 import 分散在子模块：
      - lifespan_services.py: LOOP_INTERVAL_* / OC_AUTOSTART_* / HA_AUTOSTART_*
      - lifespan_helpers.py: PROMPT_WATCHDOG_KEEPALIVE_SEC
    """

    # M1: lifespan.py 拆分为薄编排层 + 4 个子模块，常量 import 在子模块中
    LIFESPAN_FILES = [
        "lifespan.py",
        "lifespan_services.py",
        "lifespan_postinit.py",
        "lifespan_helpers.py",
        "lifespan_event_handlers.py",
    ]

    @classmethod
    def _read_lifespan_source(cls) -> str:
        """聚合 lifespan 文件群源码供契约检查。"""
        lifespan_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "src-python"
        )
        combined = ""
        for fname in cls.LIFESPAN_FILES:
            fpath = os.path.join(lifespan_dir, fname)
            if os.path.exists(fpath):
                with open(fpath, encoding="utf-8") as f:
                    combined += f.read()
        return combined

    def test_lifespan_imports_loop_interval_constants(self):
        """lifespan 文件群应 import LOOP_INTERVAL_* 常量"""
        source = self._read_lifespan_source()
        # 至少应 import 这些关键常量
        required_imports = [
            "LOOP_INTERVAL_WORKFLOW_CRON_SEC",
            "LOOP_INTERVAL_HEALER_SEC",
            "LOOP_INTERVAL_MEMORY_TREE_SEC",
            "LOOP_INTERVAL_GC_AGENT_SEC",
            "OC_AUTOSTART_DELAY_SEC",
            "PROMPT_WATCHDOG_KEEPALIVE_SEC",
            # HA_AUTOSTART_DELAY_SEC 已移除：外部 Agent（Hermes/OpenClaw）不再自动预热，
            # 改由用户自行启动，lifespan 自然不再需要该常量（2026-09-10 架构决策）
        ]
        for const in required_imports:
            assert const in source, \
                f"lifespan 文件群应 import {const}"

    def test_lifespan_no_hardcoded_critical_intervals(self):
        """lifespan 文件群不应残留关键 magic number interval（604800/900/600 等）"""
        source = self._read_lifespan_source()
        # 这些值已被替换为常量——不应再以 interval=N 形式出现
        # 注: 60 仍可能出现于其他 LoopSpec（event_bus 等），不在此校验范围
        hardcoded_patterns = [
            "interval=604800",   # GC agent weekly — 已替换
            "interval=900",      # memory_tree/sync — 已替换
            "interval=600",      # auto_dream/evolution/cleanup — 已替换
        ]
        for pattern in hardcoded_patterns:
            assert pattern not in source, \
                f"lifespan 文件群不应残留硬编码 {pattern}——应使用 config 常量"
