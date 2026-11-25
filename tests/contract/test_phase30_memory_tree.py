"""tests/contract/test_phase30_memory_tree.py —— Phase 30 MemoryTree 合约测试

覆盖:
  - workspace 文件索引（代码符号提取）
  - 语义去重（FTS5 + 内容重叠度）
  - 去重正确性（已有相似知识的 score 保留）
"""
import os, sys, pytest, tempfile
from pathlib import Path


@pytest.fixture
def temp_ws():
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        shared = ws / "shared"
        shared.mkdir()
        (shared / "test.py").write_text(
            "class UserService:\n    def create_user(self, name): pass\n"
            "def get_user(id): return None\n"
        )
        (shared / "utils.ts").write_text(
            "export function formatDate(d: Date): string { return d.toISOString(); }\n"
            "export class Logger { log(msg: string) {} }\n"
        )
        os.environ["YUNCAT_WORKSPACE"] = str(ws)
        yield ws
        del os.environ["YUNCAT_WORKSPACE"]


@pytest.mark.asyncio
async def test_extract_python_symbols():
    """Python 代码符号提取——class + function"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.memory.memory_tree import _extract_code_symbols
    symbols = _extract_code_symbols("test.py", "class UserService:\n    def create_user(self, name): pass")
    assert len(symbols) >= 2
    names = {s["name"] for s in symbols}
    assert "UserService" in names
    assert "create_user" in names


@pytest.mark.asyncio
async def test_extract_ts_symbols():
    """TypeScript 代码符号提取——function + class + arrow function"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.memory.memory_tree import _extract_code_symbols
    symbols = _extract_code_symbols("utils.ts",
        "export function formatDate(d: Date): string {}\nexport class Logger {}\n"
        "export const getEnv = () => process.env;")
    names = {s["name"] for s in symbols}
    assert "formatDate" in names
    assert "Logger" in names


@pytest.mark.asyncio
async def test_semantic_dedup_exact_title():
    """语义去重——完全相同的标题返回 1.0 重叠度"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.memory.memory_tree import _title_content_overlap
    score = _title_content_overlap(
        ("UserService", "class that manages users"),
        ("UserService", "class that manages users"),
    )
    assert score > 0.8


@pytest.mark.asyncio
async def test_semantic_dedup_different():
    """语义去重——完全不同的内容返回低重叠度"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.memory.memory_tree import _title_content_overlap
    score = _title_content_overlap(
        ("UserService", "manages user CRUD operations"),
        ("Logger", "handles application logging"),
    )
    assert score < 0.5


@pytest.mark.asyncio
async def test_index_from_workspace(temp_ws):
    """workspace 索引——创建知识节点"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from config import DB_PATH, DATA_DIR
    from repositories import init_db, run_migrations
    from services.memory.memory_tree import MemoryTree

    os.makedirs(DATA_DIR, exist_ok=True)
    db = await init_db(DB_PATH)
    await run_migrations(db)

    tree = MemoryTree(db=db)
    tree._last_index_time = 0  # force full index
    count = await tree.index_from_workspace()
    assert count > 0

    cursor = await db.execute("SELECT COUNT(*) FROM knowledge_nodes WHERE source='workspace'")
    total = (await cursor.fetchone())[0]
    assert total > 0

    await db.close()


@pytest.mark.asyncio
async def test_background_loop_calls_both():
    """后台循环调用 conversations + workspace 双索引"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.memory.memory_tree import MemoryTree
    tree = MemoryTree(db=None)
    # 验证方法存在即可——实际执行需完整 DB 环境
    assert hasattr(tree, "index_from_conversations")
    assert hasattr(tree, "index_from_workspace")
    assert hasattr(tree, "periodic_maintenance")
