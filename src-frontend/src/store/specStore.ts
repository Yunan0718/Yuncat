import { create } from "zustand";

export interface AuditReport {
  matches: number;
  mismatches: number;
  missing: number;
  mismatchesList: string[];
  missingList: string[];
  verificationPassed: boolean;
  verificationResults: Array<{ passed: boolean; detail: string }>;
  auditorProvider: string;
}

export type SpecPhase =
  | "idle"
  | "checkpoint_resumed"
  | "round_start"
  | "architect"
  | "architect_done"
  | "architect_failed"
  | "developer"
  | "developer_done"
  | "auditor"
  | "auditor_done"
  | "convergence_check"
  | "escalated";

interface SpecState {
  /** 当前阶段 */
  phase: SpecPhase;
  /** 当前轮次 */
  round: number;
  /** 最大轮次 */
  maxRounds: number;
  /** Spec YAML 文本 */
  specYaml: string;
  /** Spec 名称 */
  specName: string;
  /** FR 数量 */
  frCount: number;
  /** Developer 操作的文件数 */
  filesCount: number;
  /** Developer 操作的文件列表 */
  files: string[];
  /** 审计报告 */
  auditReport: AuditReport | null;
  /** 是否已收敛 */
  converged: boolean | null;
  /** 总轮次 */
  totalRounds: number;
  /** 是否正在运行 */
  active: boolean;
  /** Task ID */
  taskId: string | null;
  /** 错误信息 */
  error: string | null;
  /** 是否从检查点恢复 */
  checkpointResumed: boolean;

  /** 设置当前阶段 */
  setPhase: (phase: SpecPhase, payload?: Record<string, unknown>) => void;
  /** 设置 Architect 完成信息 */
  setArchitectDone: (specName: string, frCount: number) => void;
  /** 设置 Developer 完成信息 */
  setDeveloperDone: (filesCount: number, files: string[]) => void;
  /** 设置 Auditor 完成信息 */
  setAuditorDone: (report: AuditReport) => void;
  /** 设置收敛结果 */
  setConvergence: (converged: boolean, mismatches: number, missing: number, verificationPassed: boolean) => void;
  /** 设置 Spec Loop 完成 */
  setLoopDone: (converged: boolean, totalRounds: number, specName: string, auditSummary: AuditReport) => void;
  /** 设置升级 */
  setEscalated: (remainingIssues: number) => void;
  /** 设置错误 */
  setError: (error: string) => void;
  /** 重置 */
  reset: () => void;
}

const initialState = {
  phase: "idle" as SpecPhase,
  round: 0,
  maxRounds: 3,
  specYaml: "",
  specName: "",
  frCount: 0,
  filesCount: 0,
  files: [] as string[],
  auditReport: null as AuditReport | null,
  converged: null as boolean | null,
  totalRounds: 0,
  active: false,
  taskId: null as string | null,
  error: null as string | null,
  checkpointResumed: false,
};

export const useSpecStore = create<SpecState>((set) => ({
  ...initialState,

  setPhase: (phase, payload) =>
    set((s) => ({
      phase,
      // Extract common payload fields
      round: (payload?.round as number) ?? s.round,
      maxRounds: (payload?.max_rounds as number) ?? s.maxRounds,
      active: phase !== "idle" && phase !== "escalated",
      taskId: (payload?.task_id as string) ?? s.taskId,
      checkpointResumed: phase === "checkpoint_resumed" || s.checkpointResumed,
      error: payload && "error" in payload ? (payload.error as string) : s.error,
    })),

  setArchitectDone: (specName, frCount) =>
    set({
      specName,
      frCount,
      phase: "architect_done",
    }),

  setDeveloperDone: (filesCount, files) =>
    set({
      filesCount,
      files,
      phase: "developer_done",
    }),

  setAuditorDone: (report) =>
    set({
      auditReport: report,
      phase: "auditor_done",
    }),

  setConvergence: (converged, mismatches, missing, verificationPassed) =>
    set({
      phase: "convergence_check",
      converged,
      auditReport: {
        matches: 0,
        mismatches,
        missing,
        mismatchesList: [],
        missingList: [],
        verificationPassed,
        verificationResults: [],
        auditorProvider: "",
      },
    }),

  setLoopDone: (converged, totalRounds, specName, auditSummary) =>
    set((s) => ({
      converged,
      totalRounds,
      specName: specName && specName.trim() ? specName : s.specName,
      auditReport: auditSummary,
      active: false,
      phase: "idle" as SpecPhase,
    })),

  setEscalated: (_remainingIssues) =>
    set({
      phase: "escalated",
      active: false,
    }),

  setError: (error) =>
    set({
      error,
      active: false,
      phase: "idle",
    }),

  reset: () => set({ ...initialState }),
}));
