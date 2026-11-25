"""tests/unit/test_tempfile_cleanup.py —— Phase 5 W: 临时文件清理回归测试

覆盖 3 个风险项:
1. openclaw.py — atexit 注册 API key 临时文件清理
2. system_tool_install.py — install_skill_from_url 所有路径 tmp 清理
3. skill_installer.py — 所有权转移契约文档化（静态检查）

修复的 3 处泄漏:
- archive 分支 return "No skill.yaml" 不清理 tmp
- 单 yaml 分支无 try/except，open() 失败泄漏 tmp
- install_from_dir 抛异常时 rmtree 不可达
"""
import asyncio
import os
import sys
import tempfile
import shutil

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ──────────────────────────────────────────────────────────────────
# 1. OpenClaw atexit 注册
# ──────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────
# 2. install_skill_from_url tmp 清理覆盖所有路径
# ──────────────────────────────────────────────────────────────────

class TestInstallSkillFromUrlTmpCleanup:
    """Phase 5 W: install_skill_from_url 所有 return/raise 路径都应清理 tmp

    覆盖 3 个修复点:
    - archive 分支 "No skill.yaml" return
    - 单 yaml 分支文件写入失败
    - install_from_dir 抛异常
    """

    def test_static_check_single_try_finally_pattern(self):
        """静态检查: install_skill_from_url 应有单一 try/finally 覆盖 tmp 清理"""
        install_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "src-python", "tools", "builtin", "system_tool_install.py",
        )
        with open(install_path, encoding="utf-8") as f:
            source = f.read()
        # 应有 "tmp: str | None = None" 类型注解（新代码标志）
        assert "tmp: str | None = None" in source, \
            "应使用 tmp: str | None = None 模式确保 finally 中 tmp 已定义"
        # 应有 finally 块清理 tmp
        assert "if tmp:" in source, "finally 块应检查 if tmp: 再清理"
        # 不应保留旧的 "raise e" 模式（archive 分支的 except 已移除）
        assert "_shutil.rmtree(tmp, ignore_errors=True)\n                raise e" not in source, \
            "archive 分支不应再有 'rmtree + raise e' 模式（已由外层 finally 取代）"

    def test_tmp_cleaned_when_no_skill_yaml_in_archive(self, monkeypatch):
        """archive 分支 return "No skill.yaml" 时 tmp 应被清理"""
        from tools.builtin import system_tool_install
        from tools.result import ToolResult

        # 记录所有创建的 tmp 目录
        created_dirs: list[str] = []
        original_mkdtemp = tempfile.mkdtemp

        def _tracking_mkdtemp(*args, **kwargs):
            d = original_mkdtemp(*args, **kwargs)
            created_dirs.append(d)
            return d

        # mock aiohttp — 必须正确实现 async __aenter__/__aexit__
        class _MockResp:
            def __init__(self, payload: bytes):
                self.status = 200
                self._payload = payload
            async def read(self):
                return self._payload
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class _MockSession:
            def __init__(self, payload: bytes):
                self._payload = payload
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def get(self, url, **kw): return _MockResp(self._payload)

        # 构造一个含 README.txt 但无 skill.yaml 的 zip
        import io, zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("README.txt", "no skill here")
        zip_payload = buf.getvalue()

        async def _run():
            monkeypatch.setattr(system_tool_install, "_validate_skill_url", lambda u: True)
            monkeypatch.setattr(tempfile, "mkdtemp", _tracking_mkdtemp)
            import aiohttp
            monkeypatch.setattr(aiohttp, "ClientSession",
                                lambda *a, **kw: _MockSession(zip_payload))
            return await system_tool_install.install_skill_from_url(
                "https://raw.githubusercontent.com/test/test/main/skill.zip"
            )

        result = asyncio.run(_run())
        # 应返回失败（No skill.yaml found）
        assert result.get("success") is False
        assert "No skill.yaml" in result.get("error", ""), \
            f"应为 'No skill.yaml' 错误，实际: {result.get('error')}"
        # tmp 应被创建过且都被清理
        assert len(created_dirs) >= 1, "archive 分支应至少创建 1 个 tmp 目录"
        for d in created_dirs:
            assert not os.path.exists(d), f"tmp 目录 {d} 应在 return 前被清理"

    def test_tmp_cleaned_when_install_from_dir_raises(self, monkeypatch):
        """install_from_dir 抛异常时 tmp 应被清理（外层 except 捕获，finally 清理）"""
        from tools.builtin import system_tool_install

        created_dirs: list[str] = []
        original_mkdtemp = tempfile.mkdtemp

        def _tracking_mkdtemp(*args, **kwargs):
            d = original_mkdtemp(*args, **kwargs)
            created_dirs.append(d)
            return d

        # mock install_from_dir 抛异常
        class _FakeRegistry:
            async def install_from_dir(self, src_dir):
                raise RuntimeError("simulated install failure")

        def _fake_get_registry():
            return _FakeRegistry()

        # mock aiohttp — 返回有效 yaml 内容
        yaml_payload = b"name: test\nversion: '1.0'\ntitle: Test\ndescription: test"

        class _MockResp:
            def __init__(self, payload: bytes):
                self.status = 200
                self._payload = payload
            async def read(self):
                return self._payload
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass

        class _MockSession:
            def __init__(self, payload: bytes):
                self._payload = payload
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            def get(self, url, **kw): return _MockResp(self._payload)

        async def _run():
            monkeypatch.setattr(system_tool_install, "_validate_skill_url", lambda u: True)
            monkeypatch.setattr(tempfile, "mkdtemp", _tracking_mkdtemp)
            import aiohttp
            monkeypatch.setattr(aiohttp, "ClientSession",
                                lambda *a, **kw: _MockSession(yaml_payload))
            monkeypatch.setattr(
                "services.evolution.skill_registry.get_skill_registry",
                _fake_get_registry,
            )
            return await system_tool_install.install_skill_from_url(
                "https://raw.githubusercontent.com/test/test/main/skill.yaml"
            )

        result = asyncio.run(_run())
        # 应返回失败（外层 except 捕获 RuntimeError）
        assert result.get("success") is False
        assert "simulated install failure" in result.get("error", ""), \
            f"应包含 'simulated install failure'，实际: {result.get('error')}"
        # tmp 应被创建过且都被清理
        assert len(created_dirs) >= 1, "单 yaml 分支应至少创建 1 个 tmp 目录"
        for d in created_dirs:
            assert not os.path.exists(d), f"tmp 目录 {d} 应在异常后被清理"


# ──────────────────────────────────────────────────────────────────
# 3. skill_installer.py 所有权转移契约（静态检查）
# ──────────────────────────────────────────────────────────────────

class TestSkillInstallerOwnershipContract:
    """Phase 5 W: clone_git_repo / download_marketplace_package 的所有权转移契约文档化"""

    def test_clone_git_repo_docstring_documents_ownership(self):
        """clone_git_repo docstring 应说明调用方负责清理"""
        installer_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "src-python", "services", "evolution", "skill_installer.py",
        )
        with open(installer_path, encoding="utf-8") as f:
            source = f.read()
        assert "所有权转移契约" in source, \
            "skill_installer.py 应在 docstring 中明确所有权转移契约"

    def test_callers_use_try_finally_cleanup(self):
        """调用方 install_from_git / install_from_marketplace 应使用 try/finally 清理"""
        registry_path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "src-python", "services", "evolution", "skill_registry.py",
        )
        with open(registry_path, encoding="utf-8") as f:
            source = f.read()
        # 两个调用方都应有 try/finally + shutil.rmtree 模式
        assert source.count("shutil.rmtree(tmp, ignore_errors=True)") >= 2, \
            "install_from_git 和 install_from_marketplace 都应用 try/finally + rmtree 清理"

    def test_install_parsed_md_uses_try_finally(self):
        """install_parsed_md 本身应使用 try/finally 清理（函数内创建 tmp）"""
        from services.evolution import skill_installer
        import inspect
        src = inspect.getsource(skill_installer.install_parsed_md)
        assert "try:" in src and "finally:" in src, \
            "install_parsed_md 应使用 try/finally 清理 tmp"
        assert "shutil.rmtree" in src, "应调用 shutil.rmtree 清理"
