"""security/job_object.py —— Windows Job Object 沙箱（Phase 3 OS 级纵深防御）

通过 ctypes 封装 Windows Job Object API:
  - 父进程退出时自动终止子进程 (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
  - 内存上限 2GB (JOB_OBJECT_LIMIT_JOB_MEMORY)——防止子进程泄漏拖垮系统

不设 ACTIVE_PROCESS_LIMIT——OpenClaw/Hermes 需要 spawn 子进程（npm/pip install 等）。
对标 Docker --memory=2g + --kill-on-stop；VS Code 子进程 Job Object。
"""
from __future__ import annotations

import ctypes
import logging
import sys

_log = logging.getLogger("yuncat.job_object")

if sys.platform != "win32":
    # 非 Windows 平台提供空实现（签名与 Windows 版本一致）
    _log.warning("非 Windows 平台，进程沙箱（job_object）不生效。子进程生命周期不受父进程控制。")

    def create_job_object(job_name: str = "") -> int | None:
        return None

    def assign_process_to_job(hjob: int, pid: int) -> bool:
        return False

    def terminate_job(hjob: int, exit_code: int = 1) -> bool:
        return False

else:
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    # ---- 常量 ----
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
    JOB_OBJECT_LIMIT_AFFINITY = 0x00000010
    JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800

    # 使用 ctypes 原生结构体——自动适配 32/64 位对齐
    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_ulonglong),  # ULONG_PTR (64-bit)
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _SetInformationJobObject = kernel32.SetInformationJobObject
    _SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    _SetInformationJobObject.restype = wintypes.BOOL

    def create_job_object(job_name: str = "") -> int | None:
        """创建 Windows Job Object 并设置限制。返回 Job Handle。

        主流做法（VS Code / Docker / Chrome）：
          JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 父进程退出时 OS 自动杀子进程
          JOB_OBJECT_LIMIT_JOB_MEMORY = 内存上限防止子进程泄漏拖垮系统
        不需要 PID 轮询、不需要 watchdog、不需要 stdin pipe 检测。
        """
        hjob = kernel32.CreateJobObjectW(None, job_name or None)
        if not hjob:
            _log.error(f"CreateJobObject failed: {kernel32.GetLastError()}")
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # KILL_ON_JOB_CLOSE: 父进程退出时 OS 自动杀子进程
        # JOB_MEMORY: 限制 Job 总内存（2GB）——防止子进程内存泄漏拖垮系统
        #   对标 Docker --memory=2g；Python+ML 库正常使用 <1.5GB，2GB 留余量
        # 注意: 不设 ACTIVE_PROCESS_LIMIT——OpenClaw/Hermes 可能需要 spawn 子进程
        #   (npm install / pip install 等)，限制为 1 会破坏功能
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_JOB_MEMORY
        )
        info.JobMemoryLimit = 2 * 1024 * 1024 * 1024  # 2GB

        result = _SetInformationJobObject(
            hjob,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not result:
            _log.error(f"SetInformationJobObject failed: {kernel32.GetLastError()}")
        return hjob


    def assign_process_to_job(hjob: int, pid: int) -> bool:
        """将进程绑定到 Job Object"""
        hproc = kernel32.OpenProcess(0x1F0FFF, False, pid)  # PROCESS_ALL_ACCESS
        if not hproc:
            _log.error(f"OpenProcess({pid}) failed: {kernel32.GetLastError()}")
            return False
        result = kernel32.AssignProcessToJobObject(hjob, hproc)
        kernel32.CloseHandle(hproc)
        if not result:
            _log.error(f"AssignProcessToJobObject({pid}) failed: {kernel32.GetLastError()}")
        return bool(result)

    _TerminateJobObject = kernel32.TerminateJobObject
    _TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _TerminateJobObject.restype = wintypes.BOOL

    def terminate_job(hjob: int, exit_code: int = 1) -> bool:
        """终止 Job 内全部进程（Phase C 超限强杀用）。

        TerminateJobObject 是 OS 级"进程组杀"的正确实现——Job 内所有进程
        （含孙进程）一起终止，等价于 POSIX killpg(SIGKILL)。
        """
        if not hjob:
            return False
        result = _TerminateJobObject(hjob, exit_code)
        if not result:
            _log.error(f"TerminateJobObject failed: {kernel32.GetLastError()}")
        return bool(result)
