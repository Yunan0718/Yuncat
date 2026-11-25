"""services/build_analyzer.py —— Phase 46 E.2 构建时间分析

Yuncat 的构建约束 (不是 OpenAI 的 1 分钟目标):
  - CI 验证 < 2min: pytest + tsc + lint = ~30s
  - Electron 打包: 无硬约束 (发版动作，不在 CI 内循环)
  - prebuild.js: >60s warning (趋势追踪，不阻断 CI)

本模块提供:
  - analyze_slow_build(): 解析 prebuild.js timing 日志
  - get_ci_timing(): 聚合 CI 构建时间数据
"""
import json as _json
import logging
import os as _os
import time as _time

from config import DATA_DIR

_log = logging.getLogger("yuncat.build_analyzer")

# 数据目录只有一处真源（config.DATA_DIR）
BUILD_TIMING_LOG = _os.path.join(DATA_DIR, "build_timing.jsonl")


class BuildAnalyzer:
    """构建时间分析器——分析构建图瓶颈。

    与 prebuild.js 协作: prebuild 写入 timing → Agent 通过 analyze_slow_build 工具读取。
    """

    def __init__(self, db=None):
        self.db = db

    async def analyze_slow_build(self, since_days: int = 7) -> dict:
        """分析最近 N 天的构建时间趋势。

        Returns: {total_builds, avg_seconds, max_seconds, exceeded_count, trend}
        """
        if not _os.path.exists(BUILD_TIMING_LOG):
            return {"total_builds": 0, "message": "无构建时间数据——prebuild.js 尚未启用 timing 追踪"}

        try:
            lines = []
            with open(BUILD_TIMING_LOG, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            entry = _json.loads(line)
                            lines.append(entry)
                        except Exception:
                            continue

            cutoff = _time.time() - since_days * 86400
            recent = [
                e for e in lines
                if e.get("timestamp", 0) / 1000 > cutoff
            ]
            if not recent:
                return {"total_builds": 0, "message": f"最近 {since_days} 天无构建记录"}

            elapsed_times = [e.get("elapsed", 0) for e in recent]
            threshold = 60
            exceeded = [e for e in recent if e.get("threshold", 60) > 0
                       and e.get("elapsed", 0) > e.get("threshold", 60)]

            return {
                "total_builds": len(recent),
                "avg_seconds": round(sum(elapsed_times) / len(elapsed_times), 1),
                "max_seconds": max(elapsed_times),
                "min_seconds": min(elapsed_times),
                "exceeded_count": len(exceeded),
                "threshold_seconds": threshold,
                "trend": "stable" if len(exceeded) == 0 else "degrading",
                "last_build": recent[-1].get("elapsed", 0) if recent else 0,
            }
        except Exception as e:
            return {"error": str(e)}

    async def get_ci_timing(self) -> dict:
        """获取 CI 验证时间基准。

        Yuncat CI 目标: pytest + tsc + lint < 2min。
        当前实际: 以最近一次 CI 实测为准。
        """
        return {
            "target_seconds": 120,
            "current_seconds": None,
            "components": {
                "pytest_unit": "~16s",
                "tsc_noEmit": "~8s",
                "lint_rules": "~5s",
            },
            "status": "baseline",
            "message": "CI 目标 < 2min，请以最近一次实测为准。",
        }
