"""services/cron_parser.py —— 纯 Python 5 字段 cron 表达式解析器

AUDIT #9: 原 cron_schedule 工具将 cron 表达式作为死数据存入 loop_trigger_queue，
调度器从不评估表达式——任务插入即触发（~30s 内）。本模块提供 cron 表达式解析 +
next_run_at 计算，供 scheduled_tasks 调度器使用。

设计决策：
- 纯 Python 实现，不引入 croniter 依赖（YAGNI——5 字段 cron 是有界问题）
- 支持 5 字段：minute hour day-of-month month day-of-week
- 字段语法：* | N | */N | N-M | N,M | N-M/S（POSIX 子集）
- day-of-week: 0=Sunday (POSIX)，7 也视为 Sunday（兼容）
- dom 和 dow 同时受限时用 OR 语义（Vixie cron 行为——事实标准）
- next_run_at 最长搜索 366 天，无匹配抛 ValueError

不支持的 cron 特性（YAGNI）：
- @reboot / @daily 等宏
- 6+ 字段（秒/年）
- 自然语言（"in 30 minutes"）——原工具描述支持但从未实现，现废弃

一次性排期见 parse_run_at()：支持 "30m / 2h / 1d / in 30 minutes"、
"HH:MM"（今天或明天）与 ISO 8601。DSH 的 schedule 包支持一次性提醒，
Yuncat 只做**一次性**（cron_expr 留空，触发后标 completed），不复刻
fixed-rate/daily/weekly 那几种宏——用标准 cron 表达同样的事更省事。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

__all__ = [
    "CronParseError", "CronSchedule", "parse_cron", "next_run_at", "parse_run_at",
]

# 各字段范围：(min, max)
_FIELD_RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 7),  # 0 和 7 都表示 Sunday
}


class CronParseError(ValueError):
    """cron 表达式解析错误"""


@dataclass(frozen=True)
class CronSchedule:
    """解析后的 cron 调度——5 个字段的有效值集合"""
    minute: frozenset[int]
    hour: frozenset[int]
    dom: frozenset[int]
    month: frozenset[int]
    dow: frozenset[int]  # 已归一化：7 → 0
    _dom_restricted: bool  # dom 是否非 *（用于 Vixie OR 语义）
    _dow_restricted: bool  # dow 是否非 *（用于 Vixie OR 语义）

    def matches(self, dt: time.struct_time) -> bool:
        """判断 struct_time 是否匹配本调度"""
        cron_dow = (dt.tm_wday + 1) % 7  # Python wday(0=Mon) → cron dow(0=Sun)
        if dt.tm_min not in self.minute:
            return False
        if dt.tm_hour not in self.hour:
            return False
        if dt.tm_mon not in self.month:
            return False
        # Vixie cron: dom 和 dow 同时受限时取 OR，否则取 AND
        if self._dom_restricted and self._dow_restricted:
            return (dt.tm_mday in self.dom) or (cron_dow in self.dow)
        return (dt.tm_mday in self.dom) and (cron_dow in self.dow)


def _parse_field(expr: str, field_name: str, min_val: int, max_val: int) -> tuple[frozenset[int], bool]:
    """解析单个字段，返回 (有效值集合, 是否受限)

    restricted=False 表示通配符 *（字段在 Vixie OR 语义中不参与 OR）
    """
    expr = expr.strip()
    if not expr:
        raise CronParseError(f"空 {field_name} 字段")

    # 通配符
    if expr == "*":
        return frozenset(range(min_val, max_val + 1)), False

    restricted = True
    values: set[int] = set()

    for part in expr.split(","):
        part = part.strip()
        if not part:
            raise CronParseError(f"{field_name} 字段含空段: {expr!r}")

        # 处理 step: N-M/S 或 */S
        step = 1
        range_part = part
        if "/" in part:
            pieces = part.split("/", 1)
            if len(pieces) != 2:
                raise CronParseError(f"{field_name} 字段 step 格式错误: {part!r}")
            range_part, step_str = pieces
            try:
                step = int(step_str)
            except ValueError:
                raise CronParseError(f"{field_name} 字段 step 非整数: {step_str!r}")
            if step <= 0:
                raise CronParseError(f"{field_name} 字段 step 必须 > 0: {step}")

        # 确定 range 起止
        if range_part == "*":
            r_start, r_end = min_val, max_val
        elif "-" in range_part:
            pieces = range_part.split("-", 1)
            if len(pieces) != 2:
                raise CronParseError(f"{field_name} 字段 range 格式错误: {range_part!r}")
            try:
                r_start = int(pieces[0])
                r_end = int(pieces[1])
            except ValueError:
                raise CronParseError(f"{field_name} 字段 range 非整数: {range_part!r}")
        else:
            try:
                r_start = int(range_part)
            except ValueError:
                raise CronParseError(f"{field_name} 字段值非整数: {range_part!r}")
            r_end = r_start if step == 1 else max_val
            # 单值 + step: N/S → 从 N 到 max_val，步长 S
            if "/" in part and "-" not in range_part:
                r_end = max_val

        # 校验范围
        if r_start < min_val or r_end > max_val:
            raise CronParseError(
                f"{field_name} 字段值超出范围 [{min_val}-{max_val}]: {part!r}"
            )
        if r_start > r_end:
            raise CronParseError(f"{field_name} 字段 range 起止反序: {part!r}")

        for v in range(r_start, r_end + 1, step):
            values.add(v)

    return frozenset(values), restricted


def parse_cron(expr: str) -> CronSchedule:
    """解析 5 字段 cron 表达式，返回 CronSchedule

    >>> parse_cron("0 9 * * *")  # 每天 9:00
    >>> parse_cron("*/15 * * * *")  # 每 15 分钟
    >>> parse_cron("0 0 1 1 *")  # 每年 1 月 1 日 0:00
    """
    if not expr or not expr.strip():
        raise CronParseError("空 cron 表达式")
    fields = expr.strip().split()
    if len(fields) != 5:
        raise CronParseError(
            f"cron 表达式必须是 5 字段，收到 {len(fields)} 字段: {expr!r}"
        )

    m, h, dom, mon, dow = fields

    minute_set, _ = _parse_field(m, "minute", *_FIELD_RANGES["minute"])
    hour_set, _ = _parse_field(h, "hour", *_FIELD_RANGES["hour"])
    dom_set, dom_restricted = _parse_field(dom, "dom", *_FIELD_RANGES["dom"])
    month_set, _ = _parse_field(mon, "month", *_FIELD_RANGES["month"])

    # dow: 7 归一化为 0（都表示 Sunday）
    dow_set, dow_restricted = _parse_field(dow, "dow", *_FIELD_RANGES["dow"])
    if 7 in dow_set:
        dow_set = frozenset(dow_set - {7} | {0})

    return CronSchedule(
        minute=minute_set,
        hour=hour_set,
        dom=dom_set,
        month=month_set,
        dow=dow_set,
        _dom_restricted=dom_restricted,
        _dow_restricted=dow_restricted,
    )


def next_run_at(expr: str, after: float | int | None = None) -> int:
    """计算 cron 表达式的下一次触发时间（Unix 时间戳，秒）

    从 `after` 的下一分钟开始搜索。最长搜索 366 天，无匹配抛 ValueError。

    >>> next_run_at("0 9 * * *", after=1735689600)  # 2025-01-01 00:00:00 UTC

    P2-2 已知限制（DST）: 使用 time.localtime 解析本地时间，在 DST 切换时区会有：
    - spring forward（2:00→3:00）: 2:xx 本地时间不存在，该小时配置的任务被跳过
    - fall back（2:59→2:00）: 2:xx 本地时间重复，该小时配置的任务触发两次
    中国时区（Asia/Shanghai）无 DST，不受影响。其他时区用户若需精确 DST 处理，
    可改用 APScheduler/croniter 的 ZoneInfo 感知实现。
    对标 Linux crontab：crontab 本身也有 DST 边界问题，这是 Vixie cron 的已知行为。
    """
    schedule = parse_cron(expr)
    base = int(after) if after is not None else int(time.time())
    # 对齐到下一分钟边界（秒数清零）
    base = base - (base % 60) + 60

    # 最长搜索 366 天（527040 分钟）
    max_minutes = 366 * 24 * 60
    for i in range(max_minutes):
        t = base + i * 60
        dt = time.localtime(t)
        if schedule.matches(dt):
            return t
    raise ValueError(f"366 天内无匹配时间: {expr!r}")


# ── 一次性排期 ────────────────────────────────────────────────────
_REL_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}
_REL_RE = re.compile(r"^(?:in\s+)?(\d+)\s*([a-z]+)$", re.IGNORECASE)
_CLOCK_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_run_at(spec: str, now: int | None = None) -> int:
    """把"什么时候跑一次"解析成 Unix 时间戳（一次性排期）。

    支持三种写法（都按本地时区）：
      - 相对时间："30m" / "2h" / "1d" / "in 30 minutes"
      - 当天时刻："09:30"（已过则算明天）
      - ISO 8601："2026-10-23T09:00" / "2026-10-23 09:00"

    解析不了抛 CronParseError——不猜"大概几点"。
    """
    from datetime import datetime, timedelta

    raw = (spec or "").strip()
    if not raw:
        raise CronParseError("排期为空")
    base = int(now if now is not None else time.time())

    rel = _REL_RE.match(raw)
    if rel:
        amount = int(rel.group(1))
        unit = _REL_UNITS.get(rel.group(2).lower())
        if unit is None or amount <= 0:
            raise CronParseError(f"无法识别的时间单位: {raw}")
        return base + amount * unit

    clock = _CLOCK_RE.match(raw)
    if clock:
        local_now = datetime.fromtimestamp(base)
        target = local_now.replace(
            hour=int(clock.group(1)), minute=int(clock.group(2)),
            second=0, microsecond=0,
        )
        if target <= local_now:
            target = target + timedelta(days=1)
        return int(target.timestamp())

    try:
        target = datetime.fromisoformat(raw)
    except ValueError:
        raise CronParseError(
            f"无法识别的一次性排期: {raw}（用 30m / 2h / 09:30 / 2026-10-23T09:00）"
        ) from None
    if target.tzinfo is None:
        return int(target.timestamp())
    return int(target.timestamp())
