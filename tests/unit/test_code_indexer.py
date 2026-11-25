"""tests/unit/test_code_indexer.py — Task 4.7: 代码符号索引动态上限测试

覆盖：
- load_summary() 动态 limit 派生（model_cfg / explicit limit / fallback）
- build() 全量持久化（移除 MAX_SYMBOLS 截断，对标 Aider --map）
- build() + load_summary() 集成
"""
import asyncio
import json
from pathlib import Path

import pytest

from services import code_indexer
from services.code_indexer import CodeIndexer
from config import CODE_INDEX_SUMMARY_LIMIT


# ═══════════════════════════════════════════════════════════
# Fixtures / Helpers
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def temp_index(tmp_path, monkeypatch):
    """将 INDEX_DIR/INDEX_FILE 指向 tmp_path，隔离测试不污染真实缓存。"""
    index_dir = tmp_path / "index"
    index_file = index_dir / "symbols.json"
    monkeypatch.setattr(code_indexer, "INDEX_DIR", index_dir)
    monkeypatch.setattr(code_indexer, "INDEX_FILE", index_file)
    return index_file


def _make_symbols(n: int) -> list[dict]:
    """生成 n 个测试符号（function 类型，无 args）。"""
    return [
        {
            "type": "function",
            "name": f"func_{i}",
            "file": f"mod_{i}.py",
            "args": [],
            "line": i + 1,
        }
        for i in range(n)
    ]


def _write_index(path: Path, symbols: list[dict]) -> None:
    """直接写入测试用 symbols.json（绕过 build）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "symbols": symbols,
                "updated_at": 1234567890,
                "total": len(symbols),
                "workspace": "/test",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _count_symbol_lines(summary: str) -> int:
    """统计摘要中符号行数（每行以 '\\n- ' 开头）。"""
    return summary.count("\n- ")


# ═══════════════════════════════════════════════════════════
# TestLoadSummaryDynamicLimit — Task 4.7 核心：动态 limit 派生
# ═══════════════════════════════════════════════════════════

class TestLoadSummaryDynamicLimit:
    """load_summary 按模型上下文窗口动态派生注入数量。"""

    def test_fallback_when_no_model_cfg(self, temp_index):
        """model_cfg=None 时用 CODE_INDEX_SUMMARY_LIMIT fallback。"""
        _write_index(temp_index, _make_symbols(200))
        summary = CodeIndexer.load_summary()
        assert _count_symbol_lines(summary) == CODE_INDEX_SUMMARY_LIMIT

    def test_dynamic_limit_small_model(self, temp_index):
        """128K 模型：base=50 + (131072/131072)*8=8 → 58 符号。"""
        _write_index(temp_index, _make_symbols(200))
        model_cfg = {"model": "gpt-4o", "context": 131072}
        summary = CodeIndexer.load_summary(model_cfg=model_cfg)
        assert _count_symbol_lines(summary) == 58

    def test_dynamic_limit_large_model(self, temp_index):
        """1M 模型：base=50 + (1048576/131072)*8=64 → 114 符号。"""
        _write_index(temp_index, _make_symbols(200))
        model_cfg = {"model": "deepseek-v4", "context": 1048576}
        summary = CodeIndexer.load_summary(model_cfg=model_cfg)
        assert _count_symbol_lines(summary) == 114

    def test_large_model_gets_more_symbols_than_small(self, temp_index):
        """大模型应注入更多符号（动态化核心目标）。"""
        _write_index(temp_index, _make_symbols(200))
        small = CodeIndexer.load_summary(model_cfg={"model": "gpt-4o", "context": 131072})
        large = CodeIndexer.load_summary(model_cfg={"model": "deepseek-v4", "context": 1048576})
        assert _count_symbol_lines(large) > _count_symbol_lines(small)

    def test_explicit_limit_overrides_model_cfg(self, temp_index):
        """显式 limit 参数优先于 model_cfg 动态派生。"""
        _write_index(temp_index, _make_symbols(200))
        summary = CodeIndexer.load_summary(
            model_cfg={"model": "deepseek-v4", "context": 1048576},
            limit=10,
        )
        assert _count_symbol_lines(summary) == 10

    def test_limit_exceeds_total_returns_all(self, temp_index):
        """limit 超过符号总数时返回全部（slice 自然截断，不报错）。"""
        _write_index(temp_index, _make_symbols(30))
        summary = CodeIndexer.load_summary(
            model_cfg={"model": "deepseek-v4", "context": 1048576},
        )
        # 1M 模型 limit=114，但只有 30 个符号 → 返回 30
        assert _count_symbol_lines(summary) == 30

    def test_empty_index_returns_empty_string(self, temp_index):
        """索引文件不存在时返回空字符串（不抛异常）。"""
        # temp_index 未写入任何内容
        assert CodeIndexer.load_summary(model_cfg={"model": "gpt-4o"}) == ""

    def test_corrupt_index_returns_empty_string(self, temp_index):
        """索引文件损坏时返回空字符串（异常被吞，不向上抛）。"""
        temp_index.parent.mkdir(parents=True, exist_ok=True)
        temp_index.write_text("not valid json {{{", encoding="utf-8")
        assert CodeIndexer.load_summary() == ""

    def test_summary_contains_header_and_symbol_format(self, temp_index):
        """摘要包含标题行和符号格式行。"""
        _write_index(temp_index, _make_symbols(3))
        summary = CodeIndexer.load_summary(limit=3)
        assert "## 项目符号索引" in summary
        assert "top-3" in summary
        assert "func_0" in summary
        assert "mod_0.py" in summary


# ═══════════════════════════════════════════════════════════
# TestBuildPersistsAllSymbols — Task 4.7: 移除 MAX_SYMBOLS 持久化截断
# ═══════════════════════════════════════════════════════════

class TestBuildPersistsAllSymbols:
    """build() 全量持久化符号，不再截断到 MAX_SYMBOLS=200。"""

    def test_build_persists_all_symbols_beyond_old_cap(self, tmp_path, monkeypatch):
        """250 个符号全部持久化（原 MAX_SYMBOLS=200 会丢弃 50 个）。"""
        index_dir = tmp_path / "index"
        monkeypatch.setattr(code_indexer, "INDEX_DIR", index_dir)
        monkeypatch.setattr(code_indexer, "INDEX_FILE", index_dir / "symbols.json")

        ws = tmp_path / "ws"
        ws.mkdir()
        for i in range(250):
            (ws / f"mod_{i}.py").write_text(
                f"def func_{i}():\n    pass\n", encoding="utf-8"
            )

        indexer = CodeIndexer(workspace=str(ws))
        result = asyncio.run(indexer.build())

        # 250 文件 × 1 函数 = 250 符号，全部持久化
        assert len(result) == 250
        data = json.loads((index_dir / "symbols.json").read_text(encoding="utf-8"))
        assert len(data["symbols"]) == 250
        assert data["total"] == 250

    def test_build_includes_private_file_not_private_func(self, tmp_path, monkeypatch):
        """build() 跳过私有函数（_ 开头）但保留公开函数。"""
        index_dir = tmp_path / "index"
        monkeypatch.setattr(code_indexer, "INDEX_DIR", index_dir)
        monkeypatch.setattr(code_indexer, "INDEX_FILE", index_dir / "symbols.json")

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "mod.py").write_text(
            "def public_func():\n    pass\n"
            "def _private_func():\n    pass\n"
            "class MyClass:\n"
            "    def method_a(self):\n        pass\n",
            encoding="utf-8",
        )

        indexer = CodeIndexer(workspace=str(ws))
        result = asyncio.run(indexer.build())

        names = {s["name"] for s in result}
        assert "public_func" in names
        assert "MyClass" in names
        assert "method_a" in names
        assert "_private_func" not in names  # 私有函数被跳过

    def test_build_then_load_summary_integration(self, tmp_path, monkeypatch):
        """build() 持久化 → load_summary(model_cfg) 读取，端到端验证。"""
        index_dir = tmp_path / "index"
        index_file = index_dir / "symbols.json"
        monkeypatch.setattr(code_indexer, "INDEX_DIR", index_dir)
        monkeypatch.setattr(code_indexer, "INDEX_FILE", index_file)

        ws = tmp_path / "ws"
        ws.mkdir()
        for i in range(80):
            (ws / f"mod_{i}.py").write_text(
                f"def func_{i}():\n    pass\n", encoding="utf-8"
            )

        indexer = CodeIndexer(workspace=str(ws))
        asyncio.run(indexer.build())

        # 80 符号，128K 模型 limit=58 → 返回 58
        summary = CodeIndexer.load_summary(model_cfg={"model": "gpt-4o", "context": 131072})
        assert _count_symbol_lines(summary) == 58

        # 80 符号，1M 模型 limit=114 → 返回 80（全部，slice 自然截断）
        summary_large = CodeIndexer.load_summary(
            model_cfg={"model": "deepseek-v4", "context": 1048576}
        )
        assert _count_symbol_lines(summary_large) == 80


# ═══════════════════════════════════════════════════════════
# TestIncrementalUpdate — Batch 5.4: 增量更新
# ═══════════════════════════════════════════════════════════

class TestIncrementalUpdate:
    """只重新解析变更/新增/删除的文件，未变更文件基于 mtime/size 跳过。"""

    def test_incremental_update_modifies_existing_symbol(self, tmp_path, temp_index):
        """文件内容修改后，旧符号被移除、新符号被加入。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        mod = ws / "mod.py"
        mod.write_text("def func_a():\n    pass\n", encoding="utf-8")

        indexer = CodeIndexer(workspace=str(ws))
        asyncio.run(indexer.build())

        mod.write_text("def func_b():\n    return 1\n", encoding="utf-8")
        result = indexer.incremental_update([str(mod)])

        names = {s["name"] for s in result}
        assert "func_b" in names
        assert "func_a" not in names
        data = json.loads(temp_index.read_text(encoding="utf-8"))
        assert "mod.py" in data["files"]

    def test_incremental_update_deletes_file(self, tmp_path, temp_index):
        """文件删除后，对应符号与元数据一并移除。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        mod = ws / "mod.py"
        mod.write_text("def func_a():\n    pass\n", encoding="utf-8")

        indexer = CodeIndexer(workspace=str(ws))
        asyncio.run(indexer.build())

        mod.unlink()
        result = indexer.incremental_update([str(mod)])

        assert result == []
        data = json.loads(temp_index.read_text(encoding="utf-8"))
        assert data["symbols"] == []
        assert "mod.py" not in data["files"]

    def test_incremental_update_rename(self, tmp_path, temp_index):
        """重命名产生 src+dest 两个路径：旧文件删除、新文件解析。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        old = ws / "old.py"
        new = ws / "new.py"
        old.write_text("def original():\n    pass\n", encoding="utf-8")

        indexer = CodeIndexer(workspace=str(ws))
        asyncio.run(indexer.build())

        old.unlink()
        new.write_text("def renamed():\n    pass\n", encoding="utf-8")
        result = indexer.incremental_update([str(old), str(new)])

        names = {s["name"] for s in result}
        assert "original" not in names
        assert "renamed" in names
        data = json.loads(temp_index.read_text(encoding="utf-8"))
        assert "old.py" not in data["files"]
        assert "new.py" in data["files"]

    def test_incremental_update_skips_unchanged_file(self, tmp_path, temp_index):
        """mtime/size 未变时不重新解析，文件元数据保持不变。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        mod = ws / "mod.py"
        mod.write_text("def stable():\n    pass\n", encoding="utf-8")

        indexer = CodeIndexer(workspace=str(ws))
        asyncio.run(indexer.build())

        before = json.loads(temp_index.read_text(encoding="utf-8"))
        before_meta = dict(before["files"]["mod.py"])

        result = indexer.incremental_update([str(mod)])
        after = json.loads(temp_index.read_text(encoding="utf-8"))

        assert len(result) == 1
        assert after["files"]["mod.py"] == before_meta
        assert [s["name"] for s in after["symbols"]] == ["stable"]

    def test_incremental_update_ignores_non_python_files(self, tmp_path, temp_index):
        """非 Python 文件不破坏已有索引。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "mod.py").write_text("def real():\n    pass\n", encoding="utf-8")

        indexer = CodeIndexer(workspace=str(ws))
        asyncio.run(indexer.build())

        (ws / "note.txt").write_text("hello", encoding="utf-8")
        result = indexer.incremental_update([str(ws / "note.txt")])

        assert len(result) == 1
        assert result[0]["name"] == "real"

    def test_incremental_update_path_outside_workspace(self, tmp_path, temp_index):
        """workspace 外部路径被忽略，不污染索引。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        outside = tmp_path / "outside.py"
        outside.write_text("def outside():\n    pass\n", encoding="utf-8")
        (ws / "mod.py").write_text("def inside():\n    pass\n", encoding="utf-8")

        indexer = CodeIndexer(workspace=str(ws))
        asyncio.run(indexer.build())

        result = indexer.incremental_update([str(outside)])

        assert len(result) == 1
        assert result[0]["name"] == "inside"


# ═══════════════════════════════════════════════════════════
# TestJsTsSymbolExtraction — JS/TS 符号提取（零依赖正则，补齐前端盲区）
# ═══════════════════════════════════════════════════════════

class TestJsTsSymbolExtraction:
    """_parse_js_file 提取 function/class/箭头函数/interface/type 符号。"""

    def test_extract_tsx_component(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "App.tsx").write_text(
            "import React from 'react'\n"
            "export function App() {\n  return <div/>\n}\n"
            "export default App\n",
            encoding="utf-8",
        )
        indexer = CodeIndexer(workspace=str(ws))
        _rel, symbols, _meta = indexer._parse_js_file(ws / "App.tsx")
        names = {s["name"] for s in symbols}
        assert "App" in names

    def test_extract_class_arrow_interface_type(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "store.ts").write_text(
            "export class Store {\n}\n"
            "export const useStore = () => ({})\n"
            "export interface State {}\n"
            "export type User = { id: string }\n",
            encoding="utf-8",
        )
        indexer = CodeIndexer(workspace=str(ws))
        _rel, symbols, _meta = indexer._parse_js_file(ws / "store.ts")
        names = {s["name"] for s in symbols}
        assert {"Store", "useStore", "State", "User"} <= names

    def test_build_indexes_ts_and_py_together(self, tmp_path, monkeypatch):
        index_dir = tmp_path / "index"
        monkeypatch.setattr(code_indexer, "INDEX_DIR", index_dir)
        monkeypatch.setattr(code_indexer, "INDEX_FILE", index_dir / "symbols.json")

        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "Comp.tsx").write_text(
            "export function Comp() {\n  return null\n}\n", encoding="utf-8"
        )
        (ws / "mod.py").write_text("def py_func():\n    pass\n", encoding="utf-8")

        indexer = CodeIndexer(workspace=str(ws))
        result = asyncio.run(indexer.build())

        names = {s["name"] for s in result}
        assert "Comp" in names
        assert "py_func" in names

    def test_dts_declaration_skipped(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "types.d.ts").write_text("export interface Foo {}\n", encoding="utf-8")
        indexer = CodeIndexer(workspace=str(ws))
        assert indexer._parse_js_file(ws / "types.d.ts") is None
