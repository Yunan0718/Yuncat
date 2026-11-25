"""utils/runtime_paths.py — 运行时路径查找工具

集中管理 python-embed 路径查找，解决打包后路径不匹配问题。

Phase 融合重构已移除 node-embed（不再内置 Node.js）；依赖 Node 的工具
（context7 / codegraph 等）统一使用系统 PATH 中的 node / npx。

查找链（所有函数一致）：
  1. sys.executable 推断（打包 + 嵌入式 Python 开发）
  2. __file__ 相对路径（系统 Python 开发）
  3. %APPDATA%/Yuncat/（Electron 运行时解压 fallback）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def get_python_embed_dir() -> Path:
    """返回 python-embed 目录路径。

    打包后: resources/python-embed/
    开发时: src-python/python-embed/
    """
    # 1. sys.executable 推断——打包后 python.exe 在 resources/python-embed/
    exe_dir = Path(sys.executable).resolve().parent
    # 判断是否在 python-embed 目录内（有 _pth 文件或目录名为 python-embed）
    if exe_dir.name == "python-embed" or (exe_dir / "python312._pth").exists():
        return exe_dir

    # 2. __file__ 相对路径（开发模式——系统 Python 启动）
    src_python_dir = Path(__file__).resolve().parent.parent
    dev_embed = src_python_dir / "python-embed"
    if dev_embed.exists():
        return dev_embed

    # 3. %APPDATA%/Yuncat/python-embed/（Electron 运行时解压 fallback）
    appdata = os.environ.get("APPDATA")
    if appdata:
        runtime_embed = Path(appdata) / "Yuncat" / "python-embed"
        if runtime_embed.exists():
            return runtime_embed

    # fallback：返回 exe_dir（即使不是 python-embed，让调用方自行判断）
    return exe_dir


def get_node_exe() -> str:
    """返回 node 可执行文件路径，优先系统 PATH 中的 node，找不到返回 'node'。

    Phase 融合重构已移除 node-embed（不再内置 Node.js）；context7 / codegraph
    等依赖 Node 的工具统一使用系统 PATH 中的 node / npx。
    """
    import shutil
    node = shutil.which("node")
    return node or "node"


def get_python_exe() -> str:
    """返回 python.exe 路径。"""
    return sys.executable


def get_hermes_exe() -> Path | None:
    """返回 hermes 可执行文件路径，找不到返回 None。

    hermes 通过 pip 安装，在 python-embed/Scripts/hermes.exe（Windows）
    或 python-embed/bin/hermes（Unix）。
    """
    py_embed = get_python_embed_dir()
    candidates = [
        py_embed / "Scripts" / "hermes.exe",   # Windows
        py_embed / "bin" / "hermes",           # Unix
    ]
    for c in candidates:
        if c.exists():
            return c
    return None
