"""tests/unit/test_requirements_lock.py —— Phase 5 S: requirements.lock 哈希校验测试

验证 pip-compile 生成的 requirements.lock 满足 --require-hashes 安装要求:
1. 文件存在且格式正确
2. 所有包条目都有 --hash=sha256: 哈希
3. 不含镜像源 --index-url（使用官方 PyPI）
4. 生成脚本存在且可执行
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
LOCKFILE = os.path.abspath(os.path.join(ROOT, "requirements.lock"))
GEN_SCRIPT = os.path.abspath(os.path.join(ROOT, "src-electron", "scripts", "generate-requirements-lock.ps1"))


class TestRequirementsLockFormat:
    """Phase 5 S: requirements.lock 格式与哈希完整性"""

    def test_lockfile_exists(self):
        """requirements.lock 应存在于项目根目录"""
        assert os.path.exists(LOCKFILE), \
            "requirements.lock 不存在——请运行 src-electron/scripts/generate-requirements-lock.ps1 生成"

    def test_lockfile_has_no_mirror_index_url(self):
        """lockfile 不应含镜像 --index-url（使用官方 PyPI 默认源）"""
        if not os.path.exists(LOCKFILE):
            pytest.skip("requirements.lock 不存在")
        with open(LOCKFILE, encoding="utf-8") as f:
            content = f.read()
        # 不应含清华/阿里等镜像源
        mirror_patterns = [
            "pypi.tuna.tsinghua.edu.cn",
            "mirrors.aliyun.com/pypi",
            "pypi.douban.com",
            "mirrors.163.com",
        ]
        for mirror in mirror_patterns:
            assert mirror not in content, \
                f"lockfile 不应含镜像源 {mirror}——应使用官方 PyPI"

    def test_all_packages_have_hashes(self):
        """所有包条目都必须有 --hash=sha256: 哈希（--require-hashes 要求）"""
        if not os.path.exists(LOCKFILE):
            pytest.skip("requirements.lock 不存在")
        with open(LOCKFILE, encoding="utf-8") as f:
            lines = f.read().split('\n')

        no_hash_pkgs = []
        for i, line in enumerate(lines):
            # 匹配 "包名==版本 \" 或 "包名==版本"
            m = re.match(r'^([a-zA-Z0-9_-]+)==[0-9]', line)
            if m:
                # 检查后续行是否有 --hash
                has_hash = '--hash=' in line
                j = i + 1
                while j < len(lines) and lines[j].startswith('    '):
                    if '--hash=' in lines[j]:
                        has_hash = True
                        break
                    j += 1
                if not has_hash:
                    no_hash_pkgs.append(m.group(1))

        assert not no_hash_pkgs, \
            f"以下包缺少哈希（--require-hashes 会拒绝安装）: {no_hash_pkgs}"

    def test_lockfile_uses_sha256_hashes(self):
        """所有哈希应为 sha256（SRI 标准）"""
        if not os.path.exists(LOCKFILE):
            pytest.skip("requirements.lock 不存在")
        with open(LOCKFILE, encoding="utf-8") as f:
            content = f.read()
        # 所有 --hash= 都应是 sha256
        all_hashes = re.findall(r'--hash=(\w+):', content)
        non_sha256 = [h for h in all_hashes if h != 'sha256']
        assert not non_sha256, \
            f"发现非 sha256 哈希算法: {set(non_sha256)}——应统一用 sha256"

    def test_lockfile_contains_core_dependencies(self):
        """lockfile 应包含 pyproject.toml 中的核心依赖"""
        if not os.path.exists(LOCKFILE):
            pytest.skip("requirements.lock 不存在")
        with open(LOCKFILE, encoding="utf-8") as f:
            content = f.read()
        # pyproject.toml 中的核心依赖
        core_deps = [
            "fastapi", "uvicorn", "aiohttp", "aiosqlite",
            "pyyaml", "cryptography", "psutil", "watchdog",
            "pydantic-settings",
        ]
        missing = [dep for dep in core_deps if dep not in content.lower()]
        assert not missing, \
            f"lockfile 缺少核心依赖: {missing}"

    def test_lockfile_package_count_reasonable(self):
        """包数量应合理（核心 9 个 + 传递依赖，预期 20-50 个）"""
        if not os.path.exists(LOCKFILE):
            pytest.skip("requirements.lock 不存在")
        with open(LOCKFILE, encoding="utf-8") as f:
            content = f.read()
        pkg_count = len(re.findall(r'^[a-zA-Z0-9_-]+==[0-9]', content, re.MULTILINE))
        assert 20 <= pkg_count <= 60, \
            f"包数量 {pkg_count} 异常——预期 20-60（核心+传递依赖）"


class TestGenerateScript:
    """Phase 5 S: 生成脚本存在且可用"""

    def test_generate_script_exists(self):
        """generate-requirements-lock.ps1 应存在"""
        assert os.path.exists(GEN_SCRIPT), \
            "生成脚本不存在: src-electron/scripts/generate-requirements-lock.ps1"

    def test_generate_script_uses_generate_hashes(self):
        """脚本应使用 --generate-hashes 标志"""
        if not os.path.exists(GEN_SCRIPT):
            pytest.skip("生成脚本不存在")
        with open(GEN_SCRIPT, encoding="utf-8") as f:
            content = f.read()
        assert "--generate-hashes" in content, \
            "脚本应使用 --generate-hashes 生成哈希钉定的 lockfile"
        assert "--require-hashes" in content, \
            "脚本文档应说明安装时用 --require-hashes 校验"

    def test_generate_script_documents_workflow(self):
        """脚本应文档化生成 + 安装工作流"""
        if not os.path.exists(GEN_SCRIPT):
            pytest.skip("生成脚本不存在")
        with open(GEN_SCRIPT, encoding="utf-8") as f:
            content = f.read()
        # 应提到 pip-compile
        assert "piptools compile" in content or "pip-compile" in content, \
            "脚本应使用 pip-compile (pip-tools) 生成 lockfile"
        # 应提到 --require-hashes 安装命令
        assert "pip install --require-hashes" in content, \
            "脚本应文档化安装命令: pip install --require-hashes -r requirements.lock"
