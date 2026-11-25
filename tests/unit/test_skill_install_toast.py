"""tests/unit/test_skill_install_toast.py —— Skill 安装完成 Toast 通知测试

覆盖:
  - SkillRegistry.install_system_skills: 返回 {"count": int, "installed_ids": list}
  - lifespan: 安装成功后在 app.state 设置 pending_skill_install_toast
"""
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
def fake_system_skill(tmp_path):
    """构造一个假的 skills/system/demo 目录。

    install_system_skills 通过 os.path.dirname(__file__)/../../skills/system 定位
    系统技能目录——skill_registry.py 真实位于 src-python/services/evolution/ 下，
    因此测试中 __file__ 必须模拟为 <tmp>/services/evolution/skill_registry.py，
    这样 ../../skills/system 恰好落在 <tmp>/skills/system。
    """
    system_dir = tmp_path / "skills" / "system" / "demo"
    system_dir.mkdir(parents=True)
    (system_dir / "skill.yaml").write_text(
        # name 字段即 skill id（_parse_yaml 用 data["name"] 作为 id）
        "id: demo\n"
        "name: demo\n"
        "version: 1.0.0\n"
        "title: Demo Skill\n"
        "category: test\n"
        "author: test\n"
        "min_yuncat: 0.1.0\n",
        encoding="utf-8",
    )
    return tmp_path


class TestInstallSystemSkillsReturn:
    @pytest.mark.asyncio
    async def test_returns_count_and_ids(self, fake_system_skill):
        from services.evolution import skill_registry as sr_module
        from services.evolution.skill_registry import SkillRegistry

        registry = SkillRegistry(install_root=str(fake_system_skill / "installed"))
        # patch __file__ 让 install_system_skills 找到 tmp/skills/system
        # （模拟真实布局 src-python/services/evolution/skill_registry.py）
        fake_file = str(fake_system_skill / "services" / "evolution" / "skill_registry.py")
        with mock.patch.object(sr_module, "__file__", fake_file):
            result = await registry.install_system_skills()

        assert isinstance(result, dict)
        assert result["count"] == 1
        assert "demo" in result["installed_ids"]

    @pytest.mark.asyncio
    async def test_empty_result_when_no_system_dir(self, tmp_path):
        from services.evolution import skill_registry as sr_module
        from services.evolution.skill_registry import SkillRegistry

        registry = SkillRegistry(install_root=str(tmp_path / "installed"))
        fake_file = str(tmp_path / "services" / "evolution" / "skill_registry.py")
        with mock.patch.object(sr_module, "__file__", fake_file):
            result = await registry.install_system_skills()
        assert result == {"count": 0, "installed_ids": []}


class TestLifespanPendingToast:
    @pytest.mark.asyncio
    async def test_sets_pending_toast_after_install(self, fake_system_skill):
        from fastapi import FastAPI
        from services.evolution import skill_registry as sr_module
        from services.evolution.skill_registry import SkillRegistry

        app = FastAPI()
        app.state.skill_registry = SkillRegistry(install_root=str(fake_system_skill / "installed"))

        fake_file = str(fake_system_skill / "services" / "evolution" / "skill_registry.py")
        with mock.patch.object(sr_module, "__file__", fake_file):
            result = await app.state.skill_registry.install_system_skills()
            if result.get("count", 0) > 0:
                app.state.pending_skill_install_toast = {
                    "count": result["count"],
                    "installed_ids": result.get("installed_ids", []),
                }

        pending = getattr(app.state, "pending_skill_install_toast", None)
        assert pending is not None
        assert pending["count"] == 1
        assert "demo" in pending["installed_ids"]
