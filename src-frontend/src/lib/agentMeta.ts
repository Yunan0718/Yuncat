import { Bot, Sparkles, Zap, type LucideIcon } from "lucide-react";

export type AgentKind = "native" | "external" | "cli";

export interface AgentMeta {
  name: string;
  kind: AgentKind;
  icon: LucideIcon;
  /** GitHub repo (for external agents) — surfaced as a setup hint. */
  repo?: string;
  /** Launch command template; `{port}` is replaced at render time. */
  cliEntry?: string;
  /** i18n key for a one-line description. */
  descKey?: string;
  /** i18n key for an agent-specific port note (external gateways). */
  portDescKey?: string;
  /** i18n key for an agent-specific backend note. */
  backendDescKey?: string;
}

/**
 * Data-driven agent metadata.
 *
 * The UI used to special-case `openclaw` / `hermes` in 5+ places (status dots,
 * name fallbacks, setup copy). That silently breaks if the backend renames an
 * agent id. This table is the one place that maps an agent id to its display
 * name, icon and (for external agents) setup instructions.
 */
export const AGENT_META: Record<string, AgentMeta> = {
  yuncat: { name: "Yuncat", kind: "native", icon: Bot },
  hermes: {
    name: "Hermes",
    kind: "external",
    icon: Sparkles,
    repo: "github.com/NousResearch/hermes-agent",
    // hermes-agent 没有 `api-server` 子命令；API server 是 gateway 的一个 platform。
    // API_SERVER_KEY 是必填项（服务端拒绝无 key 启动），端口默认 8642。
    cliEntry: "$env:API_SERVER_ENABLED='true'; $env:API_SERVER_KEY='<随机密钥>'; $env:API_SERVER_PORT='{port}'; hermes gateway run",
    descKey: "agentGuide.hermesDesc",
    backendDescKey: "settings.hermesBackendDesc",
  },
  openclaw: {
    name: "OpenClaw",
    kind: "external",
    icon: Sparkles,
    repo: "github.com/openclaw/openclaw",
    cliEntry: "openclaw gateway start --port {port}",
    descKey: "agentGuide.openclawDesc",
    portDescKey: "settings.ocPortDesc",
  },
  codex: { name: "Codex", kind: "cli", icon: Zap },
  claude: { name: "Claude", kind: "cli", icon: Zap },
};

export function agentMeta(id: string): AgentMeta | undefined {
  return AGENT_META[id];
}

export function agentDisplayName(id: string): string {
  return AGENT_META[id]?.name ?? id;
}
