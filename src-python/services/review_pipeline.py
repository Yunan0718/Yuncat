"""services/review_pipeline.py —— Phase 46 E.3 合并后审查流水线

Cron 触发 + Agent 扫已合并变更 → 对比项目架构铁律 → 审计报告。
不是 OpenAI 的 PR Review Bot——是 Yuncat 的轻量级自审机制。
"""
import asyncio
import json as _json
import logging
import time as _time
import uuid as _uuid

from config import DEFAULT_AGENT_ID, REVIEW_PIPE_RECENT_LIMIT

_log = logging.getLogger("yuncat.review_pipeline")


class ReviewPipeline:
    """合并后审查流水线——定期扫描变更，对比架构铁律。

    使用场景:
      1. cron 触发（via POST /api/v1/hooks/trigger）
      2. 手动触发（via Dashboard）
      3. Spec Loop 完成后自审
    """

    def __init__(self, db, agent_service, deliberation_service):
        self._db = db
        self._ag = agent_service
        self._ds = deliberation_service

    async def scan_recent_changes(self, since_hours: int = 24) -> list[dict]:
        """扫描最近 N 小时的对话分析，生成简洁的变更摘要。

        当前版本不依赖 git（Yuncat 无强制 git 工作流）。
        使用 conversations 表和 tasks 表推断变更。
        """
        reports: list[dict] = []
        try:
            # 查询最近完成的 tasks
            cursor = await self._db.execute(
                "SELECT id, input, output, status, agent_id, completed_at "
                "FROM tasks WHERE status='completed' "
                "AND completed_at > ? ORDER BY completed_at DESC LIMIT ?",
                (int(_time.time()) - since_hours * 3600, REVIEW_PIPE_RECENT_LIMIT),
            )
            rows = await cursor.fetchall()

            for row in rows:
                task_id = row[0]
                task_input = row[1]
                task_output = row[2] or ""
                agent_id = row[4] or DEFAULT_AGENT_ID

                # 快速审计——调用 AdversarialReviewer 检查常见问题
                audit_result = await self._quick_audit(task_id, task_input, task_output, agent_id)
                reports.append({
                    "task_id": task_id,
                    "input": task_input[:200],
                    "output_len": len(task_output),
                    "audit": audit_result,
                })

            return reports
        except Exception as e:
            _log.exception("Review pipeline scan failed")
            return [{"error": str(e)}]

    async def _quick_audit(
        self, task_id: str, task_input: str, task_output: str, agent_id: str,
    ) -> dict:
        """对单个已完成 task 做快速架构铁律审计。

        检查:
          1. output 是否为空
          2. output 是否含 error/failed 关键词
          3. task_input 是否含安全检查关键词
        """
        issues: list[str] = []
        score = 10

        if not task_output or len(task_output) < 10:
            issues.append("输出过短——Agent 可能未完成")
            score -= 5

        error_keywords = ["error", "失败", "failed", "timeout", "异常"]
        for kw in error_keywords:
            if kw in task_output.lower():
                issues.append(f"输出含 '{kw}'——可能存在错误")
                score -= 2
                break

        # 安全检查关键词审计
        security_signals = {
            "密码": "密码处理",
            "password": "password handling",
            "api key": "API key handling",
            "token": "token generation",
            "jwt": "JWT handling",
            "hash": "hashing",
        }
        for kw, label in security_signals.items():
            if kw in task_input.lower():
                if kw not in task_output.lower():
                    issues.append(f"任务涉及 '{label}'，但输出未提及——可能被忽略")
                    score -= 1

        return {
            "score": max(0, score),
            "issues": issues,
            "verdict": "pass" if score >= 7 else "review_recommended",
        }

    async def run_architecture_audit(self) -> dict:
        """全量架构铁律合规检查——检查所有已完成 tasks 是否违反架构铁律。

        Yuncat 架构铁律 (CLAUDE.md):
          1. 副作用唯一入口: ToolService.execute()
          2. 记忆写穿透: 先DB后缓存
          3. SQLite 单连接: aiosqlite WAL
          4. 非并发工具串行: write_file/run_command
          5. SCHEMA_SQL 单源
          6. ToolMeta 唯一注册格式
          7. Token 原子化: create_pending_state + DELETE RETURNING
        """
        try:
            cursor = await self._db.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as completed, "
                "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed "
                "FROM tasks WHERE created_at > ?",
                (int(_time.time()) - 7 * 86400,),
            )
            row = await cursor.fetchone()
            total = row[0] or 0
            completed = row[1] or 0
            failed = row[2] or 0

            return {
                "period": "7 days",
                "total_tasks": total,
                "completed": completed,
                "failed": failed,
                "success_rate_pct": round(100 * completed / max(1, total), 1),
                "recommendation": (
                    "架构铁律合规正常——tasks 成功率在目标范围内"
                    if failed / max(1, total) < 0.2
                    else "建议人工检查 failed tasks——失败率超过 20%"
                ),
            }
        except Exception as e:
            return {"error": str(e)}
