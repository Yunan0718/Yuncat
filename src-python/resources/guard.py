"""resources/guard.py —— 跨平台 PID 检测，零第三方依赖

用于 Sidecar 子进程监控父进程存活状态。
Phase 0: 仅 is_process_alive() + start_watchdog()。
"""

import os
import sys
import time
import threading
import ctypes
import logging

_log = logging.getLogger("yuncat.guard")

if sys.platform == "win32":
    # 显式声明 OpenProcess 返回 HANDLE（64-bit Windows 上为 64 位指针），
    # 避免默认 c_int 截断高 32 位。
    _kernel32 = ctypes.windll.kernel32
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.WaitForSingleObject.restype = ctypes.c_uint32

    _WAIT_TIMEOUT = 0x00000102
    _SYNCHRONIZE = 0x00100000


def is_process_alive(pid: int) -> bool:
    """跨平台：检查 PID 对应进程是否仍在运行。

    Windows: SYNCHRONIZE + WaitForSingleObject（timeout=0）
        比 PROCESS_QUERY_LIMITED_INFORMATION 更可靠——SYNCHRONIZE 权限几乎总是可用，
        WaitForSingleObject(handle, 0) 立即返回：
          WAIT_TIMEOUT (0x102) = 进程还在跑
          其他值            = 进程已退出或无法访问
    POSIX:   os.kill(pid, 0) 标准惯用法
    """
    if sys.platform == "win32":
        try:
            handle = ctypes.windll.kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
            if handle:
                result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
                ctypes.windll.kernel32.CloseHandle(handle)
                return result == _WAIT_TIMEOUT
            return False
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def start_watchdog(parent_pid: int, interval: int = 5) -> None:
    """启动后台看门狗线程：父进程消失 → 记录日志并退出。

    不再调用 os._exit(1)——Electron 主进程已通过 before-quit + taskkill /T 管理
    子进程生命周期。若父进程异常崩溃，此 watchdog 仅记录警告，
    残留 Python 进程由下次 Yuncat 启动时的清理代码处理。
    """

    _log_lock = threading.Lock()

    def _watchdog_loop() -> None:
        misses = 0
        while True:
            time.sleep(interval)
            if is_process_alive(parent_pid):
                misses = 0
                continue
            misses += 1
            if misses >= 3:
                # 父进程连续 15s 无响应——可能已崩溃，记录并退出
                with _log_lock:
                    try:
                        import logging
                        logging.getLogger("yuncat").warning(
                            "Watchdog: parent PID %s not responding after %s checks, exiting",
                            parent_pid, misses,
                        )
                    except Exception:
                        _log.debug("Watchdog 日志输出失败", exc_info=True)
                # 不再 os._exit——让 Electron 的 before-quit / taskkill 处理
                # 或者由下次启动的残留进程清理逻辑处理
                return

    thread = threading.Thread(target=_watchdog_loop, daemon=True)
    thread.start()
