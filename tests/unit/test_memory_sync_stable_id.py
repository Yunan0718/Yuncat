"""tests/unit/test_memory_sync_stable_id.py — 验证跨进程 ID 稳定性

Phase 1.1.5 修复: memory_sync.py 用 abs(hash(key)) % 1000000 生成 ID，
Python 3.3+ 启用 PYTHONHASHSEED 随机化，重启进程后同一字符串 hash 不同，
导致 INSERT OR REPLACE 永远插入新行而非更新——记忆条目每次重启都重复。

改为 _stable_id() 用 SHA-1 前 12 位（跨进程确定性）。

本测试验证:
1. _stable_id 对同一输入跨调用返回相同结果
2. _stable_id 不同输入返回不同结果（无碰撞）
3. _stable_id 格式正确（prefix_ + 12 hex chars）
4. _stable_id 不依赖 PYTHONHASHSEED（与 hash() 对比）
"""
import os
import sys
import hashlib

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from services.memory.memory_sync import _stable_id


def test_stable_id_deterministic_same_input():
    """同一输入多次调用返回相同结果。"""
    id1 = _stable_id("mem_sync", "MEMORY.md section 1")
    id2 = _stable_id("mem_sync", "MEMORY.md section 1")
    assert id1 == id2


def test_stable_id_different_inputs_different_output():
    """不同输入返回不同 ID。"""
    id1 = _stable_id("mem_sync", "section A")
    id2 = _stable_id("mem_sync", "section B")
    assert id1 != id2


def test_stable_id_format():
    """ID 格式: prefix_ + 12 hex chars。"""
    sid = _stable_id("mem_sync", "test key")
    assert sid.startswith("mem_sync_")
    hex_part = sid[len("mem_sync_"):]
    assert len(hex_part) == 12
    # 验证是合法十六进制
    int(hex_part, 16)


def test_stable_id_different_prefixes():
    """不同 prefix 生成不同 ID（即使 key 相同）。"""
    id1 = _stable_id("mem_sync", "same key")
    id2 = _stable_id("oc_mem", "same key")
    id3 = _stable_id("ha_mem", "same key")
    assert id1 != id2
    assert id2 != id3
    assert id1 != id3


def test_stable_id_matches_sha1():
    """_stable_id 结果与手动 SHA-1 一致（验证算法正确性）。"""
    key = "verification key"
    expected = f"mem_sync_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"
    actual = _stable_id("mem_sync", key)
    assert actual == expected


def test_stable_id_not_dependent_on_hash_seed():
    """_stable_id 不依赖 Python hash()——跨进程稳定。

    验证: _stable_id 结果与 hash(key) 无关。如果用 hash() 实现，
    不同 PYTHONHASHSEED 下结果会不同。SHA-1 始终相同。
    """
    key = "cross-process stability test"
    sid = _stable_id("mem_sync", key)
    # 手动计算期望值（不依赖 hash()）
    expected = f"mem_sync_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"
    assert sid == expected
    # 额外验证: hash(key) 在不同进程中可能不同，但 _stable_id 始终相同
    # （这里只能验证一次，但 SHA-1 的确定性保证了跨进程一致性）
    py_hash = hash(key)
    # py_hash 不应出现在 sid 中（证明不是用 hash() 生成的）
    assert str(abs(py_hash)) not in sid or len(str(abs(py_hash))) < 12
