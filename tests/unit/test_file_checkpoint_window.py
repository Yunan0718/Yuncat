"""tests/unit/test_file_checkpoint_window.py —— F1: FileCheckpoint 写入窗口修复

覆盖:
- 去重窗口跨实例生效（FileCheckpoint 每次 new 但 _last 应共享）
- 原子拷贝：.bak 文件完整（size 匹配），无部分写入
- save 返回 cid 后 .bak 必然完整存在（"checkpoint 创建成功后再执行写入" 契约）
"""
import os
import sys
import time
import shutil
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
def ck_dir(tmp_path, monkeypatch):
    """隔离 checkpoint 目录 + 重设模块级 CHECKPOINT_DIR"""
    monkeypatch.setenv("YUNCAT_DATA_DIR", str(tmp_path))
    import services.infra.file_checkpoint as fcp
    fcp.CHECKPOINT_DIR = str(tmp_path / "checkpoints")
    # 重置共享 dedup 状态，避免跨测试污染
    fcp._LAST_SAVE_TS.clear()
    return fcp.CHECKPOINT_DIR


class TestDedupWindowSharedAcrossInstances:
    """F1 核心：dedup 窗口必须跨实例生效——当前每次 new FileCheckpoint() 都是新实例，
    导致 _last 永远为空，5s 去重失效，每次 write_file 都建 checkpoint（垃圾）"""

    def test_second_save_within_window_returns_none(self, ck_dir, tmp_path):
        """5s 内第二次 save 同一文件应被去重（返回 None）"""
        from services.infra.file_checkpoint import FileCheckpoint
        f = tmp_path / "a.txt"
        f.write_text("v1", encoding="utf-8")

        ck1 = FileCheckpoint()
        cid1 = ck1.save(str(f), label="first")
        assert cid1 is not None

        # 新实例（模拟 tool_service 每次调用 new 一个）
        ck2 = FileCheckpoint()
        cid2 = ck2.save(str(f), label="second")
        assert cid2 is None, "5s 内同路径第二次 save 应被去重"

    def test_save_after_window_expires_succeeds(self, ck_dir, tmp_path):
        """5s 窗口过后 save 应成功"""
        from services.infra.file_checkpoint import FileCheckpoint
        import services.infra.file_checkpoint as fcp
        f = tmp_path / "b.txt"
        f.write_text("v1", encoding="utf-8")

        ck1 = FileCheckpoint()
        cid1 = ck1.save(str(f), label="first")
        assert cid1 is not None

        # 模拟窗口过期——手动回拨 _LAST_SAVE_TS
        key = str(f)
        fcp._LAST_SAVE_TS[key] = time.time() - 6

        ck2 = FileCheckpoint()
        cid2 = ck2.save(str(f), label="after_window")
        assert cid2 is not None

    def test_different_files_not_deduped(self, ck_dir, tmp_path):
        """不同文件路径不互相去重"""
        from services.infra.file_checkpoint import FileCheckpoint
        f1 = tmp_path / "c.txt"
        f1.write_text("1", encoding="utf-8")
        f2 = tmp_path / "d.txt"
        f2.write_text("2", encoding="utf-8")

        ck = FileCheckpoint()
        cid1 = ck.save(str(f1), label="f1")
        cid2 = ck.save(str(f2), label="f2")
        assert cid1 is not None
        assert cid2 is not None


class TestAtomicCopy:
    """F1: 拷贝必须原子——.bak 要么完整存在，要么不存在，不能有部分写入"""

    def test_bak_size_matches_source(self, ck_dir, tmp_path):
        """save 后 .bak 文件大小应与源文件一致"""
        from services.infra.file_checkpoint import FileCheckpoint
        content = "x" * 4096  # 4KB
        f = tmp_path / "sized.txt"
        f.write_text(content, encoding="utf-8")

        ck = FileCheckpoint()
        cid = ck.save(str(f), label="size_check")
        assert cid is not None

        # 找到 .bak 文件
        dest_dir_part = str(f).replace(chr(92), "/").replace(":", "")
        bak_dir = os.path.join(ck_dir, dest_dir_part)
        bak_files = [x for x in os.listdir(bak_dir) if x.endswith(".bak")]
        assert len(bak_files) >= 1
        bak_path = os.path.join(bak_dir, f"{cid}.bak")
        assert os.path.exists(bak_path)
        assert os.path.getsize(bak_path) == len(content.encode("utf-8"))

    def test_no_temp_file_left_after_save(self, ck_dir, tmp_path):
        """save 完成后不应残留 .tmp 临时文件"""
        from services.infra.file_checkpoint import FileCheckpoint
        f = tmp_path / "clean.txt"
        f.write_text("clean", encoding="utf-8")

        ck = FileCheckpoint()
        ck.save(str(f), label="clean")

        dest_dir_part = str(f).replace(chr(92), "/").replace(":", "")
        bak_dir = os.path.join(ck_dir, dest_dir_part)
        if os.path.exists(bak_dir):
            leftovers = [x for x in os.listdir(bak_dir) if x.endswith(".tmp")]
            assert leftovers == [], f"残留临时文件: {leftovers}"

    def test_save_returns_cid_only_after_bak_complete(self, ck_dir, tmp_path):
        """save 返回 cid 时 .bak 必然存在且可读（'checkpoint 创建成功后再执行写入' 契约）"""
        from services.infra.file_checkpoint import FileCheckpoint
        f = tmp_path / "contract.txt"
        f.write_text("contract-content", encoding="utf-8")

        ck = FileCheckpoint()
        cid = ck.save(str(f), label="contract")
        assert cid is not None

        # cid 返回后 .bak 必须立即可读
        dest_dir_part = str(f).replace(chr(92), "/").replace(":", "")
        bak_path = os.path.join(ck_dir, dest_dir_part, f"{cid}.bak")
        assert os.path.exists(bak_path), "save 返回 cid 后 .bak 必须存在"
        with open(bak_path, encoding="utf-8") as bf:
            assert bf.read() == "contract-content"


class TestNonExistentFile:
    """save 不存在的文件返回 None（已有行为，回归保护）"""

    def test_save_nonexistent_returns_none(self, ck_dir):
        from services.infra.file_checkpoint import FileCheckpoint
        ck = FileCheckpoint()
        assert ck.save("/nonexistent/file.txt") is None
