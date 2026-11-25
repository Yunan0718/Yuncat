"""工具事件 seq 计数器：单调递增 + 内存护栏（上限/过期/显式清理）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from services.agent import event_seq  # noqa: E402


def test_seq_increases_per_task_independently():
    event_seq.task_event_seq.clear()
    assert event_seq.next_seq("t1") == 1
    assert event_seq.next_seq("t1") == 2
    assert event_seq.next_seq("t2") == 1


def test_registry_is_bounded():
    event_seq.task_event_seq.clear()
    try:
        for i in range(event_seq._MAX_TASKS + 5):
            event_seq.next_seq(f"t{i}")
        assert len(event_seq.task_event_seq) <= event_seq._MAX_TASKS
    finally:
        event_seq.task_event_seq.clear()


def test_expired_entry_is_evicted():
    event_seq.task_event_seq.clear()
    try:
        event_seq.next_seq("old")
        event_seq.task_event_seq["old"] = (1, 0.0)  # 伪装成很久以前
        event_seq.next_seq("fresh")
        assert "old" not in event_seq.task_event_seq
        assert "fresh" in event_seq.task_event_seq
    finally:
        event_seq.task_event_seq.clear()


def test_clear_task_seq_removes_entry():
    event_seq.task_event_seq.clear()
    event_seq.next_seq("t1")
    event_seq.clear_task_seq("t1")
    assert "t1" not in event_seq.task_event_seq
