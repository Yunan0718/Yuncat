import { useCallback, useState } from "react";
import { useApi } from "./useApi";

/**
 * Phase 73-A / Batch 5.2: 输入框 ✨ 优化——封装 POST /api/v1/prompt-agent/optimize
 *
 * 设计：
 * - 复用 useApi（自动注入 Token + port）
 * - 导出纯函数 buildOptimizeRequest 便于 Vitest
 * - loading / error / result 三态
 * - result 包含多候选列表，便于前端做前后对比
 */

/** Batch 5.2: 单个优化候选 */
export interface Candidate {
  optimized: string;
  changes: string[];
  score: number;
}

/** Phase 73-A: 优化结果（Batch 5.2 新增 candidates 数组） */
export interface OptimizeResult extends Candidate {
  candidates: Candidate[];
  /** Task 6.17: 失败时后端返回 error 字段 */
  error?: string;
}

/** Phase 73-A: 构建优化请求 body——纯函数 */
export function buildOptimizeRequest(raw_prompt: string, agent_id: string = "") {
  return { raw_prompt, agent_id };
}

/** Batch 5.2: 把单结果/无 candidates 的后端响应回填为完整 OptimizeResult */
export function normalizeOptimizeResult(res: Partial<OptimizeResult> & Pick<OptimizeResult, "optimized" | "changes">): OptimizeResult {
  const base: Candidate = {
    optimized: res.optimized,
    changes: res.changes || [],
    score: 1,
  };
  const candidates =
    res.candidates && res.candidates.length > 0
      ? res.candidates
      : [base];
  return {
    ...base,
    candidates,
  };
}

/** Phase 73-A / Batch 5.2: usePromptOptimize hook */
export function usePromptOptimize() {
  const { api } = useApi();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<OptimizeResult | null>(null);

  const optimize = useCallback(
    async (raw_prompt: string, agent_id: string = ""): Promise<OptimizeResult | null> => {
      if (!raw_prompt.trim()) {
        setError("empty");
        return null;
      }
      setLoading(true);
      setError(null);
      try {
        const res = await api<OptimizeResult>("/api/v1/prompt-agent/optimize", {
          method: "POST",
          body: JSON.stringify(buildOptimizeRequest(raw_prompt, agent_id)),
        });
        // Task 6.17: 后端失败时返回 {error, optimized, changes}
        if (res.error) {
          setError(res.error);
          return null;
        }
        const normalized = normalizeOptimizeResult(res);
        setResult(normalized);
        return normalized;
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
        return null;
      } finally {
        setLoading(false);
      }
    },
    [api],
  );

  const reset = useCallback(() => {
    setResult(null);
    setError(null);
    setLoading(false);
  }, []);

  return { optimize, loading, error, result, reset };
}
