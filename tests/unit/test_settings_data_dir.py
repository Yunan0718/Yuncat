"""数据目录解析：未展开的环境变量 / 相对路径必须 fail-closed 回退。

背景：仓库里出现过字面量 `%APPDATA%\\Yuncat`（含真实库 1.8MB），父进程把未展开的
`%APPDATA%` 当环境变量值传进来时，后端会在错误位置建库——用户真实数据看起来"消失"。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from settings import Settings  # noqa: E402


def test_unexpanded_appdata_falls_back(monkeypatch):
    monkeypatch.setenv("APPDATA", "%APPDATA%")
    monkeypatch.delenv("YUNCAT_DATA_DIR", raising=False)
    assert Settings().get_data_dir() == os.path.join(os.path.expanduser("~"), "Yuncat")


def test_relative_data_dir_falls_back(monkeypatch):
    monkeypatch.setenv("APPDATA", os.path.expanduser("~"))
    expected = os.path.join(os.path.expanduser("~"), "Yuncat")
    assert Settings(data_dir="relative/dir").get_data_dir() == expected


def test_absolute_data_dir_kept(tmp_path):
    assert Settings(data_dir=str(tmp_path)).get_data_dir() == str(tmp_path)


def test_unexpanded_workspace_env_falls_back(monkeypatch):
    monkeypatch.setenv("YUNCAT_WORKSPACE", "%APPDATA%")
    from security.sandbox import _resolve_workspace_base

    assert _resolve_workspace_base() == os.path.join(
        os.path.expanduser("~"), "YuncatWorkspace")


def test_relative_workspace_env_falls_back(monkeypatch):
    monkeypatch.setenv("YUNCAT_WORKSPACE", "relative/workspace")
    from security.sandbox import _resolve_workspace_base

    assert _resolve_workspace_base() == os.path.join(
        os.path.expanduser("~"), "YuncatWorkspace")
