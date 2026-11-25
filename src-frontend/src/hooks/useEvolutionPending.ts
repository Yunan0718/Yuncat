import { useCallback } from "react";
import { useApi } from "./useApi";

export interface EvolutionPendingItem {
  id: string;
  pattern_signature: string;
  task_type: string;
  workflow_id: string;
  skill_dir: string;
  examples: string[];
  sandbox_result: {
    completion_rate?: number;
    node_success_rate?: number;
    total_nodes?: number;
    total_error_nodes?: number;
    runs?: Array<{ input: string; status: string; error_nodes: string[]; final_output: string }>;
  };
  ab_result: {
    wins?: number;
    losses?: number;
    ties?: number;
    win_rate?: number;
    baseline_completion_rate?: number;
    new_completion_rate?: number;
  };
  evaluation: {
    passed?: boolean;
    score?: number;
    reason?: string;
  };
  status: "pending" | "approved" | "rejected" | "auto_rejected";
  notification_sent: boolean;
  created_at: number;
  updated_at: number;
}

interface ListResponse {
  ok: boolean;
  items: EvolutionPendingItem[];
}

interface DetailResponse {
  ok: boolean;
  item: EvolutionPendingItem;
}

interface ActionResponse {
  ok: boolean;
  skill?: Record<string, unknown>;
}

export function useEvolutionPending() {
  const { api } = useApi();

  const listPending = useCallback(
    async (status?: string) => {
      const query = status ? `?status=${encodeURIComponent(status)}` : "";
      return api<ListResponse>(`/api/v1/evolution/pending${query}`);
    },
    [api],
  );

  const getPending = useCallback(
    async (id: string) => {
      return api<DetailResponse>(`/api/v1/evolution/pending/${encodeURIComponent(id)}`);
    },
    [api],
  );

  const approve = useCallback(
    async (id: string) => {
      return api<ActionResponse>(`/api/v1/evolution/pending/${encodeURIComponent(id)}/approve`, {
        method: "POST",
      });
    },
    [api],
  );

  const reject = useCallback(
    async (id: string) => {
      return api<ActionResponse>(`/api/v1/evolution/pending/${encodeURIComponent(id)}/reject`, {
        method: "POST",
      });
    },
    [api],
  );

  return { listPending, getPending, approve, reject };
}
