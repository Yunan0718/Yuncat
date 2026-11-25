"""命令子进程的环境过滤——不能把命令环境搞坏。

真实事故（2026-10-22）：`_build_filtered_env` 按「变量名含 key/secret/token…」
过滤，恰好把 `GIT_CONFIG_KEY_0` 去掉、却留下 `GIT_CONFIG_VALUE_0`。
git 的 -c 注入机制要求 COUNT + KEY_n + VALUE_n **成套**出现，少一半就直接：

    fatal: unable to parse command-line config
    error: missing config key GIT_CONFIG_KEY_0

于是 agent 里的**所有 git 命令都失败**，而环境过滤本身看起来"很安全"。
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from tools.builtin.system_tool_command import _build_filtered_env


def test_git_config_family_is_removed_as_a_whole(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraheader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Authorization: Bearer secret")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "core.askpass")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "askpass")

    env = _build_filtered_env()
    leaked = [k for k in env if k.upper().startswith("GIT_CONFIG_")]
    assert leaked == [], f"GIT_CONFIG_* 必须整套去掉，残留会让 git 全部命令失败: {leaked}"


def test_secrets_are_still_filtered(monkeypatch):
    monkeypatch.setenv("YUNCAT_TOKEN", "secret-token")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-xxx")
    monkeypatch.setenv("MY_PASSWORD", "pwd")
    env = _build_filtered_env()
    assert "YUNCAT_TOKEN" not in env
    assert "DEEPSEEK_API_KEY" not in env
    assert "MY_PASSWORD" not in env


def test_git_actually_runs_with_filtered_env(tmp_path):
    """真实跑一次 git——过滤后的环境必须还能让 git 正常工作。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _build_filtered_env()
    init = subprocess.run(
        ["git", "init", "-q"], cwd=str(repo), capture_output=True, text=True, env=env,
    )
    if init.returncode != 0:
        pytest.skip(f"环境里没有可用的 git: {init.stderr[:120]}")
    check = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=str(repo),
        capture_output=True, text=True, env=env,
    )
    assert check.returncode == 0, (
        f"过滤后的环境让 git 失败：{check.stderr.strip()[:200]}"
    )
