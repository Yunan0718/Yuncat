/** hooks/useWatchFiles.ts — Phase 20: Watch Files 模式

借鉴 Aider——监控 workspace 文件中的 `# AI:` / `// AI:` 注释标记，
开发者写注释即可自动触发 Agent 处理。
去重：同一文件同一行只触发一次。
*/
import { useEffect, useRef } from "react";
import { useApi } from "./useApi";

export interface WatchTrigger {
  file: string;
  line: number;
  comment: string;
  timestamp: number;
}

type SubmitFn = (prompt: string, agentId?: string, sessionId?: string, mode?: string) => void;

/** 检测文件内容中的 AI 注释 */
function scanForAiComments(filePath: string, content: string): WatchTrigger[] {
  const triggers: WatchTrigger[] = [];
  const lines = content.split("\n");
  const patterns = [
    /^\s*#\s*AI:\s*(.+)/i, // Python/Ruby/Shell
    /^\s*\/\/\s*AI:\s*(.+)/i, // JS/TS/Go/Rust
    /^\s*--\s*AI:\s*(.+)/i, // SQL/Lua
  ];
  for (let i = 0; i < lines.length; i++) {
    for (const pat of patterns) {
      const m = pat.exec(lines[i]);
      if (m) {
        triggers.push({
          file: filePath,
          line: i + 1,
          comment: m[1].trim(),
          timestamp: Date.now(),
        });
        break; // 一行只匹配一个模式
      }
    }
  }
  return triggers;
}

export function useWatchFiles(
  submitTask: SubmitFn,
  enabled: boolean,
  workspacePath?: string,
  pollIntervalMs: number = 3000,
) {
  const triggeredRef = useRef<Set<string>>(new Set());
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Batch D C4: 使用 useApi 替代直接 fetch——统一 base URL + token 注入
  const { api } = useApi();

  useEffect(() => {
    if (!enabled) {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      return;
    }

    const wsPath = workspacePath || "";
    if (!wsPath) return;

    const poll = async () => {
      try {
        // Batch D C4: 通过 useApi 统一走 baseURL + token 注入
        const data = await api<{ files?: Array<{ path: string }> }>(
          `/api/v1/workspace/files?dir=${encodeURIComponent(wsPath)}`
        );

        const files: Array<{ path: string }> = data.files || [];
        for (const f of files.slice(0, 50)) {
          // 限制每次最多扫描 50 个文件
          // 跳过二进制文件和超大文件
          if (/\.(png|jpg|jpeg|gif|ico|woff|ttf|exe|dll|db|sqlite|bin)$/i.test(f.path)) continue;

          try {
            const fd = await api<{ content?: string }>(
              `/api/v1/workspace/files/read?path=${encodeURIComponent(f.path)}`
            );
            const content = fd.content || "";

            const triggers = scanForAiComments(f.path, content);
            for (const t of triggers) {
              const dedupKey = `${t.file}:${t.line}`;
              if (triggeredRef.current.has(dedupKey)) continue;
              triggeredRef.current.add(dedupKey);
              submitTask(t.comment, undefined, undefined, "agent");
            }
          } catch {
            // 跳过读取失败的文件
          }
        }

        // 限制去重集合大小
        if (triggeredRef.current.size > 500) {
          const entries = Array.from(triggeredRef.current);
          triggeredRef.current = new Set(entries.slice(-250));
        }
      } catch {
        // 静默处理轮询错误
      }
    };

    pollTimerRef.current = setInterval(poll, pollIntervalMs);
    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    };
  }, [enabled, workspacePath, pollIntervalMs, submitTask, api]);
}
