"""tests/unit/test_config.py —— 配置模块单元测试"""
import os
import sys
import tempfile
import pytest

# Ensure src-python is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestConfig:
    """测试 config 模块的路径解析和常量"""

    def test_data_dir_uses_env_override(self, monkeypatch):
        """DATA_DIR 优先使用 YUNCAT_DATA_DIR 环境变量"""
        monkeypatch.setenv("YUNCAT_DATA_DIR", "/custom/data/dir")
        # Re-import 以获取 Monkeypatch 后的值
        import importlib
        import config
        importlib.reload(config)
        assert config.DATA_DIR == "/custom/data/dir"

    def test_data_dir_falls_back_to_appdata(self, monkeypatch):
        """无环境变量时使用 APPDATA（Win）或 ~（其他）"""
        import importlib
        import config

        # 清理环境变量
        for key in ("YUNCAT_DATA_DIR", "APPDATA"):
            monkeypatch.delenv(key, raising=False)

        importlib.reload(config)
        # 应该 fallback 到 ~/.Yuncat 或类似
        assert "Yuncat" in config.DATA_DIR
        assert config.DATA_DIR == os.path.join(
            os.environ.get("APPDATA", os.path.expanduser("~")), "Yuncat"
        )

    def test_db_path_inside_data_dir(self, monkeypatch):
        """DB_PATH 应该在 DATA_DIR 内"""
        monkeypatch.setenv("YUNCAT_DATA_DIR", "/tmp/test_yuncat")
        import importlib
        import config
        importlib.reload(config)
        assert config.DB_PATH == os.path.join("/tmp/test_yuncat", "yuncat.db")

    def test_tauri_origins_removed_after_electron_migration(self):
        """Phase 42.13: Tauri→Electron 迁移后 TAURI_ORIGINS 应已移除"""
        import config
        assert not hasattr(config, "TAURI_ORIGINS") or config.TAURI_ORIGINS is None, \
            "TAURI_ORIGINS should be removed after Phase 42.13 Electron migration"

    def test_phase43_dynamic_limits(self):
        """Phase 43 P0-DYN: 动态上限函数存在且可调用"""
        from config import (calc_tool_result_limit, calc_max_tool_rounds,
                           calc_max_parallel_roles, adaptive_truncate,
                           protect_critical_info)
        # calc_tool_result_limit — DeepSeek V4 1M
        ds_cfg = {"model": "deepseek-v4-flash", "models": []}
        limit = calc_tool_result_limit(ds_cfg)
        assert 90000 <= limit <= 130000, f"DS limit={limit} should be ~100k"

        # multi-tool分摊
        limit_8 = calc_tool_result_limit(ds_cfg, tool_count=8)
        assert limit_8 < limit, f"8-tool limit={limit_8} should be < single={limit}"

        # calc_max_tool_rounds — Task 6.10: 硬上限默认 100（对标 Claude Code max_turns=100）
        # DeepSeek V4 1M 动态派生 500 轮，受硬上限约束降为 100
        rounds = calc_max_tool_rounds(ds_cfg)
        assert 10 <= rounds <= 100, f"DS rounds={rounds} should be 10-100 (hard limit)"

        # adaptive_truncate — 4段factor
        s20 = adaptive_truncate("x" * 10000, 128000, 2000, 0.20)
        s95 = adaptive_truncate("x" * 10000, 128000, 2000, 0.95)
        assert len(s20) > len(s95), f"20%={len(s20)} should be > 95%={len(s95)}"

        # protect_critical_info
        result = protect_critical_info("ok\nError: fail\ndef foo():\npath: /x")
        assert "Error" in result

    def test_token_not_set_raises_error(self, monkeypatch):
        """YUNCAT_TOKEN 为空时应该报错"""
        monkeypatch.delenv("YUNCAT_TOKEN", raising=False)
        import importlib
        import config
        importlib.reload(config)
        # 不应该在 import 时就死——由 main.py 决定是否退出
        assert config.TOKEN == ""

    def test_token_from_env(self, monkeypatch):
        """从环境变量读取 Token"""
        monkeypatch.setenv("YUNCAT_TOKEN", "test-token-abc123")
        import importlib
        import config
        importlib.reload(config)
        assert config.TOKEN == "test-token-abc123"


class TestCalcMaxToolRoundsHardLimit:
    """Task 6.10 (F-015): calc_max_tool_rounds 硬上限测试。

    对标 Claude Code max_turns=100——大窗口模型动态派生数百轮时封顶。
    """

    def test_default_hard_limit_constant(self):
        """DEFAULT_MAX_TOOL_ROUNDS_HARD_LIMIT 常量 = 100（对标 Claude Code）"""
        from config import DEFAULT_MAX_TOOL_ROUNDS_HARD_LIMIT
        assert DEFAULT_MAX_TOOL_ROUNDS_HARD_LIMIT == 100

    def test_large_model_capped_at_default_100(self):
        """大模型（1M）动态派生 500 轮 → 默认硬上限封顶 100"""
        from config import calc_max_tool_rounds
        ds_cfg = {"model": "deepseek-v4-flash", "models": []}  # 1M 上下文
        rounds = calc_max_tool_rounds(ds_cfg)
        # 动态派生 ~500，受默认 100 硬上限约束
        assert rounds == 100, f"1M 模型应封顶 100，实际 {rounds}"

    def test_small_model_not_affected_by_hard_limit(self):
        """小模型（128K）动态派生 15 轮 < 100 → 不受硬上限影响"""
        from config import calc_max_tool_rounds
        small_cfg = {"model": "gpt-4o", "models": []}  # 128K 上下文
        rounds = calc_max_tool_rounds(small_cfg)
        # 动态派生 ~15，小于 100 硬上限，原值返回
        assert rounds < 100, f"128K 模型应 < 100，实际 {rounds}"
        assert rounds >= 10, f"128K 模型应 >= 10，实际 {rounds}"

    def test_user_override_lower_hard_limit(self):
        """用户覆盖 hard_limit=50 → 大模型封顶 50"""
        from config import calc_max_tool_rounds
        ds_cfg = {"model": "deepseek-v4-flash", "models": []}
        rounds = calc_max_tool_rounds(ds_cfg, hard_limit=50)
        assert rounds == 50, f"用户 hard_limit=50 应封顶 50，实际 {rounds}"

    def test_user_override_higher_hard_limit(self):
        """用户覆盖 hard_limit=200 → 大模型 min(500, 200) = 200"""
        from config import calc_max_tool_rounds
        ds_cfg = {"model": "deepseek-v4-flash", "models": []}
        rounds = calc_max_tool_rounds(ds_cfg, hard_limit=200)
        assert rounds == 200, f"用户 hard_limit=200 应返回 200，实际 {rounds}"

    def test_hard_limit_zero_disables_cap(self):
        """hard_limit=0 禁用硬上限 → 仅用动态派生值（500）"""
        from config import calc_max_tool_rounds
        ds_cfg = {"model": "deepseek-v4-flash", "models": []}
        rounds = calc_max_tool_rounds(ds_cfg, hard_limit=0)
        # 禁用硬上限，返回动态派生值（~500）
        assert rounds > 100, f"hard_limit=0 应禁用硬上限返回 >100，实际 {rounds}"

    def test_hard_limit_none_uses_default(self):
        """hard_limit=None 使用默认 100"""
        from config import calc_max_tool_rounds
        ds_cfg = {"model": "deepseek-v4-flash", "models": []}
        assert calc_max_tool_rounds(ds_cfg, hard_limit=None) == calc_max_tool_rounds(ds_cfg)
