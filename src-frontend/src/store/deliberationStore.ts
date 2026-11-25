import { create } from "zustand";

export interface RoleOutput {
  name: string;
  roleId: string;
  output: string;
  done: boolean;
  agentId?: string; // Phase 43 G1.2: 角色来源后端
}

export interface UserMessage {
  text: string;
  ts: number;
}

interface DeliberationState {
  roles: RoleOutput[];
  phase: "idle" | "proposals" | "reviews" | "consensus";
  consensus: string;
  active: boolean;
  taskId: string | null;
  /** 用户作为讨论参与者发送的消息 */
  userMessages: UserMessage[];
  /** 讨论是否暂停（用户手动暂停） */
  paused: boolean;

  initRoles: (roles: { id: string; name: string; agent_id?: string }[]) => void;
  appendChunk: (roleName: string, phase: string, data: string) => void;
  setPhase: (phase: string) => void;
  setConsensus: (text: string) => void;
  setTaskId: (taskId: string) => void;
  addUserMessage: (text: string) => void;
  setPaused: (paused: boolean) => void;
  reset: () => void;
}

export const useDeliberationStore = create<DeliberationState>((set) => ({
  roles: [],
  phase: "idle",
  consensus: "",
  active: false,
  taskId: null,
  userMessages: [],
  paused: false,

  initRoles: (roles) =>
    set({
      roles: roles.map((r) => ({
        name: r.name,
        roleId: r.id,
        output: "",
        done: false,
        // 2026-09-10: 原 fallback 为 "hermes"（Phase 43 G1.2 遗留）。Hermes 已下线，
        // 未指定 agent 时应落回原生 yuncat——否则角色会被标记成已不存在的后端。
        agentId: r.agent_id || "yuncat",
      })),
      phase: "proposals",
      active: true,
      consensus: "",
      userMessages: [],
      paused: false,
    }),

  appendChunk: (roleName, phase, data) =>
    set((s) => ({
      roles: s.roles.map((r) => (r.name === roleName ? { ...r, output: r.output + data } : r)),
      phase: (["proposals", "reviews", "consensus"] as const).includes(phase as never)
        ? (phase as DeliberationState["phase"])
        : s.phase,
    })),

  setPhase: (phase) => set({ phase: phase as DeliberationState["phase"] }),

  setConsensus: (text) => set({ consensus: text, active: false }),

  setTaskId: (taskId) => set({ taskId }),

  addUserMessage: (text) =>
    set((s) => ({ userMessages: [...s.userMessages, { text, ts: Date.now() }] })),

  setPaused: (paused) => set({ paused }),

  reset: () =>
    set({
      roles: [],
      phase: "idle",
      consensus: "",
      active: false,
      taskId: null,
      userMessages: [],
      paused: false,
    }),
}));
