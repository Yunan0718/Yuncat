"""services/file_checkpoint.py — 文件自动检查点系统

对标: VS Code localHistory · Claude Code snapshots · JetBrains Local History
设计: 零 git 依赖 · 轻量副本 · ~/.yuncat/checkpoints/ 存储 · 自动清理

F1 修复（Round 2）:
- 去重窗口改为模块级共享（_LAST_SAVE_TS），跨实例生效——修复每次 new FileCheckpoint()
  导致 _last 永远为空、5s 去重失效、每次 write_file 都建 checkpoint 的问题。
- 拷贝改为 temp + os.replace 原子写入——避免崩溃残留部分 .bak（"checkpoint 创建
  成功后再执行写入" 契约：save 返回 cid 时 .bak 必然完整存在）。

P0-3 修复（2026-07-04）:
- save() 在拷贝失败/size mismatch 时不再静默返回 None——抛 CheckpointError。
  原行为：调用方（tool_service.write_file）继续执行写操作，用户以为可撤销但实际无副本。
  新行为：调用方捕获 CheckpointError 后记录 warning + 在 tool result meta 中标注
  checkpoint_warning，前端展示红色 toast 提示"本次修改无法撤销"。
  仍返回 None 的场景：5s 内重复 save（去重）、源文件不存在（预期跳过）。
  对标 VS Code Local History / JetBrains Local History：失败时通知用户，不静默。
"""

import logging
import os
import shutil
import time as _time
from pathlib import Path

_log = logging.getLogger("yuncat.checkpoint")

from config import DATA_DIR, FILE_CHECKPOINT_MAX_PER_FILE, FILE_CHECKPOINT_MAX_AGE_SECONDS

# 数据目录只有一处真源（config.DATA_DIR ← settings.get_data_dir）
CHECKPOINT_DIR = os.path.join(DATA_DIR, "checkpoints")

MAX_PER_FILE = FILE_CHECKPOINT_MAX_PER_FILE
MAX_AGE_SEC = FILE_CHECKPOINT_MAX_AGE_SECONDS

# F1: 模块级共享去重窗口——跨 FileCheckpoint 实例生效。
# 之前 self._last 是实例属性，而 tool_service.py 每次调用都 new 一个新实例，
# 导致 5s 去重永远失效。改为模块级 dict 后，所有实例共享同一去重状态。
_LAST_SAVE_TS: dict[str, float] = {}

# 5s 内同路径重复 save 被去重
_DEDUP_WINDOW_SEC = 5


class CheckpointError(RuntimeError):
    """P0-3: 检查点创建失败——拷贝异常或完整性校验失败。

    调用方应捕获此异常并通知用户"本次修改无法撤销"，但不阻塞写操作本身
    （用户意图是写文件，检查点失败不应阻止写）。
    """


class FileCheckpoint:

    def __init__(self, db=None):
        # F1: _last 保留为实例属性以兼容外部访问，但实际去重走模块级 _LAST_SAVE_TS
        self._last = _LAST_SAVE_TS
        self._db = db

    def save(self, file_path: str, label: str = "") -> str | None:
        """保存文件副本。返回 checkpoint id 或 None（被去重/文件不存在）。

        F1: save 返回 cid 时 .bak 必然完整存在（temp+replace 原子写入），
        满足"checkpoint 创建成功后再执行写入"的契约。

        P0-3: 拷贝失败或 size mismatch 时抛 CheckpointError（不再静默返回 None）。
        仍返回 None 的场景：5s 内重复 save（去重）、源文件不存在（预期跳过）。
        """
        src = Path(file_path)
        if not src.is_absolute():
            from security.sandbox import get_workspace
            src = get_workspace() / file_path
        if not src.exists():
            return None
        now = _time.time()
        key = str(src)
        # F1: 去重走模块级共享状态
        if key in _LAST_SAVE_TS and now - _LAST_SAVE_TS[key] < _DEDUP_WINDOW_SEC:
            return None
        _LAST_SAVE_TS[key] = now
        ts = _time.strftime("%Y-%m-%d_%H-%M-%S", _time.localtime(now))
        cid = f"{ts}_{label}" if label else ts
        dest_dir = Path(CHECKPOINT_DIR) / str(src).replace(chr(92), "/").replace(":", "")
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{cid}.bak"
        # F1: 原子拷贝——先写 .tmp 临时文件，os.replace 原子重命名。
        # 崩溃时残留 .tmp（下次 _cleanup 清理），不会留下部分 .bak 误导 restore。
        tmp = dest_dir / f"{cid}.tmp"
        try:
            shutil.copy2(src, tmp)
            os.replace(str(tmp), str(dest))  # Windows/Linux 均原子
            # 验证拷贝完整性——size 不匹配则视为失败
            # P0-3: 不再静默 return None——抛 CheckpointError 让调用方通知用户
            if os.path.getsize(dest) != os.path.getsize(src):
                _log.warning("Checkpoint size mismatch for %s, removing partial", file_path[:40])
                try:
                    dest.unlink()
                except Exception:
                    pass
                raise CheckpointError(
                    f"size mismatch after copy (src={os.path.getsize(src)}, "
                    f"dest={os.path.getsize(dest)})"
                )
            _log.debug("Checkpoint: %s @ %s", file_path[:40], cid)
            self._cleanup(dest_dir)
            return cid
        except CheckpointError:
            raise
        except Exception as exc:
            # 清理残留 .tmp
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            # P0-3: 拷贝异常也抛 CheckpointError（不再静默 return None）
            raise CheckpointError(f"copy failed: {exc}") from exc

    def list(self, file_path: str) -> list[dict]:
        dest_dir = Path(CHECKPOINT_DIR) / str(file_path).replace(chr(92), "/").replace(":", "")
        if not dest_dir.exists():
            return []
        results = []
        for f in sorted(dest_dir.glob("*.bak"), reverse=True,
                        key=lambda f: f.stat().st_mtime):
            results.append({
                "id": f.stem,
                "file": file_path,
                "size_bytes": f.stat().st_size,
                "time": _time.strftime("%Y-%m-%d %H:%M:%S",
                                       _time.localtime(f.stat().st_mtime)),
            })
        return results[:MAX_PER_FILE]

    def restore(self, file_path: str, checkpoint_id: str) -> bool:
        dest_dir = Path(CHECKPOINT_DIR) / str(file_path).replace(chr(92), "/").replace(":", "")
        src = dest_dir / f"{checkpoint_id}.bak"
        if not src.exists():
            return False
        try:
            target = Path(file_path)
            if not target.is_absolute():
                from security.sandbox import get_workspace
                target = get_workspace() / file_path
            shutil.copy2(src, target)
            _log.info("Checkpoint restored: %s <- %s", file_path[:40], checkpoint_id)
            return True
        except Exception:
            return False

    def _cleanup(self, dest_dir: Path) -> None:
        files = sorted(dest_dir.glob("*.bak"), key=lambda f: f.stat().st_mtime)
        for f in files[:-MAX_PER_FILE]:
            try:
                f.unlink()
            except Exception:
                _log.debug("删除多余备份文件失败", exc_info=True)
        cutoff = _time.time() - MAX_AGE_SEC
        for f in files:
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                _log.debug("删除过期备份文件失败", exc_info=True)
        # F1: 顺手清理残留 .tmp 临时文件（崩溃遗留）
        for tmp in dest_dir.glob("*.tmp"):
            try:
                tmp.unlink()
            except Exception:
                pass
