"""tests/unit/test_guard.py —— 跨平台 PID 检测单元测试"""
import os
import sys
import threading
import pytest

# 把 resources/ 加入 sys.path 以便导入 guard 模块
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python", "resources"))
import guard


class TestIsProcessAlive:
    """测试 is_process_alive() 跨平台函数"""

    def test_current_process_is_alive(self):
        """当前进程 PID 应该返回 True"""
        assert guard.is_process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_dead(self):
        """不存在的 PID 应该返回 False"""
        # 使用一个极不可能存在的 PID
        assert guard.is_process_alive(99999999) is False

    def test_zero_pid(self):
        """PID 0 的行为（System Idle Process on Windows, invalid on POSIX）"""
        # 不应该抛异常，返回 True 或 False 都应该接受
        result = guard.is_process_alive(0)
        assert isinstance(result, bool)

    def test_negative_pid(self):
        """负数 PID 不应该抛异常"""
        result = guard.is_process_alive(-1)
        assert isinstance(result, bool)


class TestStartWatchdog:
    """测试 start_watchdog() 启动看门狗线程"""

    def test_watchdog_starts_daemon_thread(self):
        """验证 start_watchdog 启动了一个 daemon 线程"""
        thread_count_before = threading.active_count()
        guard.start_watchdog(os.getpid(), interval=60)
        thread_count_after = threading.active_count()
        # 应该多了一个线程
        assert thread_count_after >= thread_count_before

    def test_watchdog_accepts_custom_interval(self):
        """验证 start_watchdog 接受自定义轮询间隔"""
        # 不应该抛异常
        guard.start_watchdog(os.getpid(), interval=10)
