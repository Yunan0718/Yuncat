"""运行中追加指令（steer）inbox 的环境可调上限。"""
import os


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是正整数，当前为 {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} 必须是正整数，当前为 {value}")
    return value


STEER_MAX_PENDING_PER_TASK = _positive_int("YUNCAT_STEER_MAX_PENDING_PER_TASK", 20)
STEER_MAX_PENDING_TASKS = _positive_int("YUNCAT_STEER_MAX_PENDING_TASKS", 100)
