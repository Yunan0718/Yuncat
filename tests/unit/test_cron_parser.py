"""tests/unit/test_cron_parser.py —— AUDIT #9 cron 表达式解析器单元测试

覆盖：
1. parse_cron 字段解析（通配/单值/范围/步长/列表）
2. next_run_at 时间计算（下一分钟/下一小时/跨天/跨月）
3. Vixie cron dom/dow OR 语义
4. 错误处理（非法格式/超范围/空表达式）
"""
import time

import pytest

from services.cron_parser import (
    CronParseError,
    next_run_at,
    parse_cron,
)


# ═══════════════════════════════════════════════════════════════
# parse_cron 字段解析
# ═══════════════════════════════════════════════════════════════

class TestParseCronFields:
    def test_wildcard_all(self):
        """* * * * * → 每分钟"""
        s = parse_cron("* * * * *")
        assert 0 in s.minute and 59 in s.minute
        assert 0 in s.hour and 23 in s.hour
        assert 1 in s.dom and 31 in s.dom
        assert 1 in s.month and 12 in s.month
        assert 0 in s.dow and 6 in s.dow

    def test_single_value(self):
        """0 9 * * * → 每天 9:00"""
        s = parse_cron("0 9 * * *")
        assert s.minute == frozenset({0})
        assert s.hour == frozenset({9})

    def test_step_division(self):
        """*/15 * * * * → 每 15 分钟 (0,15,30,45)"""
        s = parse_cron("*/15 * * * *")
        assert s.minute == frozenset({0, 15, 30, 45})

    def test_step_range(self):
        """0-30/10 * * * * → 0,10,20,30"""
        s = parse_cron("0-30/10 * * * *")
        assert s.minute == frozenset({0, 10, 20, 30})

    def test_comma_list(self):
        """0,15,30,45 * * * * → 4 个值"""
        s = parse_cron("0,15,30,45 * * * *")
        assert s.minute == frozenset({0, 15, 30, 45})

    def test_range(self):
        """* 9-17 * * * → 9 到 17 点"""
        s = parse_cron("* 9-17 * * *")
        assert s.hour == frozenset({9, 10, 11, 12, 13, 14, 15, 16, 17})

    def test_dow_7_normalized_to_0(self):
        """7 → 0（都表示 Sunday）"""
        s = parse_cron("0 0 * * 7")
        assert s.dow == frozenset({0})

    def test_dow_0_and_7_both_sunday(self):
        """0,7 → 归一化为 {0}"""
        s = parse_cron("0 0 * * 0,7")
        assert s.dow == frozenset({0})


# ═══════════════════════════════════════════════════════════════
# next_run_at 时间计算
# ═══════════════════════════════════════════════════════════════

class TestNextRunAt:
    def test_next_minute(self):
        """* * * * * → 下一分钟"""
        now = int(time.time())
        nxt = next_run_at("* * * * *", after=now)
        assert nxt > now
        assert nxt <= now + 120  # 最多 2 分钟内

    def test_specific_hour_next_day(self):
        """0 9 * * * → 明天 9:00（如果当前时间已过 9:00）"""
        # 构造一个已过今天 9:00 的时刻
        t = time.strptime("2025-06-15 10:30:00", "%Y-%m-%d %H:%M:%S")
        after = int(time.mktime(t))
        nxt = next_run_at("0 9 * * *", after=after)
        dt = time.localtime(nxt)
        assert dt.tm_hour == 9
        assert dt.tm_min == 0
        # 应该是明天
        assert dt.tm_mday == 16

    def test_specific_hour_same_day(self):
        """0 9 * * * → 今天 9:00（如果当前时间未到 9:00）"""
        t = time.strptime("2025-06-15 08:30:00", "%Y-%m-%d %H:%M:%S")
        after = int(time.mktime(t))
        nxt = next_run_at("0 9 * * *", after=after)
        dt = time.localtime(nxt)
        assert dt.tm_mday == 15
        assert dt.tm_hour == 9
        assert dt.tm_min == 0

    def test_first_of_month(self):
        """0 0 1 * * → 每月 1 日 0:00"""
        t = time.strptime("2025-06-15 12:00:00", "%Y-%m-%d %H:%M:%S")
        after = int(time.mktime(t))
        nxt = next_run_at("0 0 1 * *", after=after)
        dt = time.localtime(nxt)
        assert dt.tm_mday == 1
        assert dt.tm_mon == 7  # 7 月 1 日
        assert dt.tm_hour == 0
        assert dt.tm_min == 0

    def test_every_15_minutes(self):
        """*/15 * * * * → 15 分钟后"""
        t = time.strptime("2025-06-15 10:07:00", "%Y-%m-%d %H:%M:%S")
        after = int(time.mktime(t))
        nxt = next_run_at("*/15 * * * *", after=after)
        dt = time.localtime(nxt)
        assert dt.tm_min == 15
        assert dt.tm_hour == 10

    def test_aligns_to_next_minute_boundary(self):
        """after 含秒数 → next_run_at 对齐到下一分钟边界"""
        t = time.strptime("2025-06-15 10:00:45", "%Y-%m-%d %H:%M:%S")
        after = int(time.mktime(t))
        nxt = next_run_at("* * * * *", after=after)
        # 应该是 10:01:00，不是 10:00:45 或 10:01:45
        dt = time.localtime(nxt)
        assert dt.tm_sec == 0
        assert dt.tm_min == 1


# ═══════════════════════════════════════════════════════════════
# Vixie cron dom/dow OR 语义
# ═══════════════════════════════════════════════════════════════

class TestVixieOrSemantics:
    def test_dom_and_dow_both_restricted_uses_or(self):
        """dom=15 且 dow=0(周日) → 每月 15 日 OR 每周日"""
        s = parse_cron("0 0 15 * 0")
        # 两个都受限
        assert s._dom_restricted
        assert s._dow_restricted
        # 2025-06-15 是周日（Python wday=6）
        t = time.strptime("2025-06-15 00:00:00", "%Y-%m-%d %H:%M:%S")
        dt = time.localtime(int(time.mktime(t)))
        assert s.matches(dt)  # dom=15 匹配
        # 2025-06-22 是周日但不是 15 日——应匹配（dow OR）
        t2 = time.strptime("2025-06-22 00:00:00", "%Y-%m-%d %H:%M:%S")
        dt2 = time.localtime(int(time.mktime(t2)))
        assert s.matches(dt2)  # dow=0 匹配

    def test_dom_restricted_dow_wildcard_uses_and(self):
        """dom=15, dow=* → 仅每月 15 日"""
        s = parse_cron("0 0 15 * *")
        assert s._dom_restricted
        assert not s._dow_restricted
        # 2025-06-15 匹配
        t = time.strptime("2025-06-15 00:00:00", "%Y-%m-%d %H:%M:%S")
        dt = time.localtime(int(time.mktime(t)))
        assert s.matches(dt)
        # 2025-06-22 不匹配
        t2 = time.strptime("2025-06-22 00:00:00", "%Y-%m-%d %H:%M:%S")
        dt2 = time.localtime(int(time.mktime(t2)))
        assert not s.matches(dt2)

    def test_dow_restricted_dom_wildcard_uses_and(self):
        """dom=*, dow=0 → 仅每周日"""
        s = parse_cron("0 0 * * 0")
        assert not s._dom_restricted
        assert s._dow_restricted
        # 2025-06-15 周日——匹配
        t = time.strptime("2025-06-15 00:00:00", "%Y-%m-%d %H:%M:%S")
        dt = time.localtime(int(time.mktime(t)))
        assert s.matches(dt)
        # 2025-06-16 周一——不匹配
        t2 = time.strptime("2025-06-16 00:00:00", "%Y-%m-%d %H:%M:%S")
        dt2 = time.localtime(int(time.mktime(t2)))
        assert not s.matches(dt2)


# ═══════════════════════════════════════════════════════════════
# 错误处理
# ═══════════════════════════════════════════════════════════════

class TestCronErrors:
    def test_empty_expression(self):
        with pytest.raises(CronParseError, match="空"):
            parse_cron("")

    def test_wrong_field_count(self):
        with pytest.raises(CronParseError, match="5 字段"):
            parse_cron("0 9 * *")  # 只有 4 字段

    def test_minute_out_of_range(self):
        with pytest.raises(CronParseError, match="超出范围"):
            parse_cron("60 9 * * *")  # 分钟 60 超界

    def test_hour_out_of_range(self):
        with pytest.raises(CronParseError, match="超出范围"):
            parse_cron("0 24 * * *")  # 小时 24 超界

    def test_month_out_of_range(self):
        with pytest.raises(CronParseError, match="超出范围"):
            parse_cron("0 0 1 13 *")  # 月 13 超界

    def test_dom_out_of_range(self):
        with pytest.raises(CronParseError, match="超出范围"):
            parse_cron("0 0 32 * *")  # 日 32 超界

    def test_non_integer(self):
        with pytest.raises(CronParseError, match="非整数"):
            parse_cron("abc 9 * * *")

    def test_step_zero(self):
        with pytest.raises(CronParseError, match="step 必须 > 0"):
            parse_cron("*/0 * * * *")

    def test_range_reversed(self):
        with pytest.raises(CronParseError, match="起止反序"):
            parse_cron("30-10 * * * *")

    def test_natural_language_rejected(self):
        """自然语言（in 30 minutes）已被废弃——必须用 cron 表达式"""
        with pytest.raises(CronParseError):
            parse_cron("in 30 minutes")
