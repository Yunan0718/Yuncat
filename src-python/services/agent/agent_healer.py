"""services/agent_healer.py —— Agent 健康检查与监控模块

从 agent_service.py 拆分: _agent_healer / get_monitoring_data。
采用委托模式: 模块级函数接收 self 作为首参。
"""
import asyncio
import logging
import time as _time

from config import AGENT_HEALTH_CHECK_INTERVAL, ADAPTER_RESTART_DELAYS, OPENCLAW_AGENT_ID

_log = logging.getLogger("yuncat.agent")


async def _restart_adapter(adapter) -> bool:
    """stop + start + wait_ready，成功返回 True。"""
    try:
        await adapter.stop()
    except Exception:
        _log.debug("停止 Adapter 失败", exc_info=True)
    try:
        await adapter.start()
        await adapter.wait_ready()
        return True
    except Exception:
        _log.warning("Agent restart failed", exc_info=True)
        return False


async def _handle_config_error(self, agent_id: str, adapter, failure_counts: dict) -> bool:
    """adapter 因「配置不可用」而不健康时处理并返回 True；与配置无关返回 False。

    配置问题（缺模型 / API Key 解不开）与「进程挂了」必须分开：前者重启一百次也不会好，
    退避重启只会每 30 秒空转一次、把真实原因淹没在日志里。这里一次性把可执行的原因推给
    界面，由用户去设置里修——对标 VS Code / Codex：凭证不可用时明确要求重新提供，
    而不是反复重试一个必然失败的请求。
    """
    if not hasattr(adapter, "config_error"):
        return False
    try:
        config_error = str(await adapter.config_error() or "")
    except Exception:
        _log.debug("读取 adapter 配置错误失败", exc_info=True)
        return False
    if not config_error:
        if failure_counts.get(agent_id) == -1:
            failure_counts[agent_id] = 0    # 配置修好了 → 恢复正常计数
        return False
    if failure_counts.get(agent_id) != -1:
        failure_counts[agent_id] = -1      # 只报一次，不进入退避循环
        _log.error("Agent %s 配置不可用（不重启）：%s", agent_id, config_error)
        await self.push_agent_status(agent_id, "config_error", reason=config_error)
    return True


async def _check_gateway_adapter(self, agent_id: str, adapter, was_ready: bool,
                              failure_counts: dict, has_port: bool) -> bool:
    """网关型外部 Agent（有端口，如 openclaw）的健康判定；处理过则返回 True。

    健康信号 = 网关真实可达（HTTP 探测），**不是** CLI 是否安装——否则装了 CLI 但没启动
    网关的机器上状态会永远虚标 running（实测）。网关由用户自行启动，Yuncat 不托管其生命
    周期：不可达时清就绪位 + 推 stopped，不做退避重启（重启只是重置自身标志位）。
    """
    if not (has_port and hasattr(adapter, "_gateway_reachable")):
        return False
    if await adapter._gateway_reachable():
        if not was_ready:
            adapter._ready.set()
            await self.push_agent_status(agent_id, "ready")
        failure_counts[agent_id] = 0
        return True
    adapter._ready.clear()
    if was_ready:
        await self.push_agent_status(agent_id, "stopped")
    failure_counts[agent_id] = 0
    _log.info("External gateway %s unreachable; marked stopped (user-managed, no restart)", agent_id)
    return True


async def _heal_adapter(self, agent_id: str, adapter, failure_counts: dict) -> None:
    """单个 adapter 的健康检查 + 退避重启。

    Batch 3a A2: 原实现仅 was_ready 才重启，而重启失败会 clear _ready → 下轮
    was_ready=False 永不重试（死局）。现 unhealthy 一律按 ADAPTER_RESTART_DELAYS
    退避重试，healthy 或重启成功时清零计数。
    """
    was_ready = adapter._ready.is_set()
    has_port = bool(getattr(adapter, "_port", 0) or getattr(adapter, "port_hint", 0))
    if await _check_gateway_adapter(self, agent_id, adapter, was_ready, failure_counts, has_port):
        return
    # CLI 按需型外部 Agent（无端口、有 cli_name，如 codex/claude）：health()=「二进制已安装」
    # ≠「运行中」。这类 agent 调用时才按需 spawn（invoke 内部自检 health），不应置 _ready
    # 也不推 ready——否则顶栏「N 运行中」把已安装的 CLI 全算成运行中（实测 4 运行中虚高）。
    is_cli_on_demand = bool(getattr(adapter, "cli_name", "")) and not has_port
    is_healthy = await adapter.health()
    if is_healthy:
        # 恢复健康 → 重置计数器
        if not was_ready and not is_cli_on_demand:
            adapter._ready.set()
            await self.push_agent_status(agent_id, "ready")
        failure_counts[agent_id] = 0
        return
    adapter._ready.clear()
    # 配置不可用（缺模型/Key 解不开）不是进程挂了，重启修不好 → 单独处理
    if await _handle_config_error(self, agent_id, adapter, failure_counts):
        return
    failures = failure_counts.get(agent_id, 0) + 1
    failure_counts[agent_id] = failures
    delay = ADAPTER_RESTART_DELAYS[min(failures - 1, len(ADAPTER_RESTART_DELAYS) - 1)]
    _log.warning("Agent %s unhealthy (streak %d), restart in %ds backoff",
                 agent_id, failures, delay)
    await asyncio.sleep(delay)
    if await _restart_adapter(adapter):
        failure_counts[agent_id] = 0
        _log.info("Agent %s restarted successfully", agent_id)
        await self.push_agent_status(agent_id, "ready")
    else:
        await self.push_agent_status(agent_id, "error")


async def agent_healer(self):
    """Phase 3: Agent 僵死自愈——定期健康检查 → 退避重启（Batch 3a A2）

    Phase 36 修复: 外层 try/except 防崩溃; 单 adapter 逻辑见 _heal_adapter。
    """
    failure_counts: dict[str, int] = {}
    while True:
        await asyncio.sleep(AGENT_HEALTH_CHECK_INTERVAL)
        for agent_id, adapter in list(self.adapters.items()):
            try:
                await _heal_adapter(self, agent_id, adapter, failure_counts)
            except Exception:
                _log.exception("Agent healer iteration failed for %s", agent_id)

        # Phase 43: OpenClaw Skills 每 1h 自动刷新
        oc = self.adapters.get(OPENCLAW_AGENT_ID)
        if oc and oc._ready.is_set() and hasattr(oc, "refresh_skills"):
            now = _time.time()
            last_refresh = getattr(oc, "_last_skills_refresh", 0.0)
            if now - last_refresh > 3600:
                try:
                    skills = await oc.refresh_skills()
                    if skills:
                        oc._last_skills_refresh = now
                        _log.info("OpenClaw skills refreshed: %d skills", len(skills))
                except Exception:
                    _log.debug("刷新 OpenClaw Skills 失败", exc_info=True)


async def get_monitoring_data(self) -> dict:
    """Phase 7b: 监控 API——返回 Agent 资源占用的 Record 格式"""
    agents: dict[str, dict] = {}
    for adapter in self.adapters.values():
        pid = getattr(adapter, 'process', None) and adapter.process.pid
        data: dict = {
            "status": "running" if adapter._ready.is_set() else "stopped",
            "pid": pid,
            "dropped_logs": getattr(adapter, '_dropped_logs', 0),
        }
        # 尝试获取 CPU/内存（需要 psutil）
        if pid:
            try:
                import psutil
                proc = psutil.Process(pid)
                data["cpu_percent"] = round(proc.cpu_percent(interval=0.1), 1)
                mem = proc.memory_info()
                data["memory_mb"] = round(mem.rss / 1024 / 1024, 1)
            except Exception:
                data["cpu_percent"] = 0.0
                data["memory_mb"] = 0.0
        else:
            data["cpu_percent"] = 0.0
            data["memory_mb"] = 0.0
        agents[adapter.agent_id] = data
    return {"agents": agents, "timestamp": int(_time.time())}
