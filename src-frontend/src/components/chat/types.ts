/**
 * ChatMode —— 聊天界面模式。
 *
 * 用户可选（ModeButtons 显示）: agent | ask | long_task
 * 仅隐藏（后端仍处理，UI 不显示）: plan
 * 内部自动触发（用户不可选，由 AI 判断后切换）: spec | deliberation
 */
export type ChatMode = "agent" | "plan" | "ask" | "spec" | "deliberation" | "long_task";

export interface ChatAttachment {
  name: string;
  data: string;
  type: string;
}

export interface ChatTab {
  key: number;
  path: string;
  name: string;
  content: string;
  originalContent: string;
  loading: boolean;
}

/** P0-7: 消息右键上下文菜单状态——抽取到 MessageContextMenu 组件用 */
export interface CtxMenuState {
  x: number;
  y: number;
  taskId: string;
  prompt: string;
  seq?: number;
}
