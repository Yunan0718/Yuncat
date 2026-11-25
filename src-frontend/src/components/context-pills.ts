import { useApi } from "../hooks/useApi";
import { useTaskStore } from "../store/taskStore";
import { emitGlobalToast } from "./common/toast-context";

// ---- 类型 ----
export interface ContextRef {
  type: "agent" | "memory" | "file" | "session" | "folder" | "web" | "git";
  value: string;
  raw: string;
  startIndex: number;
}

// ---- 解析 ----
// Task 6.3: 新增 folder/web/git 三种引用类型（对标 Cursor / Claude Code @-mention）
// value 终止于空白或 CJK/全角标点，避免把后续中文句子吞入 value
const REF_REGEX = /@(agent|memory|file|session|folder|web|git):([^\s\u3000-\u303F\uFF00-\uFFEF]+)/g;

/** 从输入文本提取 @agent:name @memory:query @file:pattern 引用 */
export function parseReferences(text: string): ContextRef[] {
  const refs: ContextRef[] = [];
  let match;
  REF_REGEX.lastIndex = 0;
  while ((match = REF_REGEX.exec(text)) !== null) {
    refs.push({
      type: match[1] as ContextRef["type"],
      value: match[2],
      raw: match[0],
      startIndex: match.index,
    });
  }
  return refs;
}

/** 去除文本中的 @引用标记，返回纯文本 */
export function stripReferences(text: string, refs: ContextRef[]): string {
  let result = text;
  for (const ref of [...refs].sort((a, b) => b.startIndex - a.startIndex)) {
    result = result.slice(0, ref.startIndex) + result.slice(ref.startIndex + ref.raw.length);
  }
  return result.replace(/\s+/g, " ").trim();
}

/** Phase 43 @session: 解析会话引用——获取被引用会话的消息内容注入 prompt */
export async function resolveSessionRefs(
  refs: ContextRef[],
  api: ReturnType<typeof useApi>["api"],
  t: (k: string, opts?: Record<string, unknown>) => string,
): Promise<string> {
  const sessionRefs = refs.filter((r) => r.type === "session");
  if (sessionRefs.length === 0) return "";
  const results = await Promise.all(
    sessionRefs.map(async (ref) => {
      try {
        const msgs = await api<Array<{ role: string; content: string; id: string; agent_id: string }>>(
          `/api/v1/sessions/${ref.value}/messages`,
        );
        if (!msgs || msgs.length === 0) return null;
        return { ref, msgs };
      } catch (e) {
        // P0-1.9: 结构化错误 + toast（原 catch { return null } 静默吞错）
        console.warn("session ref resolve failed", ref.value, e);
        emitGlobalToast(t("phase49.contextPills.sessionResolveFail", { value: ref.value }), "error");
        return null;
      }
    }),
  );
  return results
    .filter(Boolean)
    .map((r) => {
      if (!r) return "";
      const lines = r.msgs.map(
        (m) =>
          `${m.role === "user" ? t("phase49.contextPills.user") : t("phase49.contextPills.ai")}: ${m.content.slice(0, 500)}`,
      );
      return `${t("phase49.contextPills.sessionContext", { value: r.ref.value, count: r.msgs.length })}\n${lines.join("\n")}`;
    })
    .join("\n\n---\n\n");
}

/** Phase 17: 解析 @file 引用——搜索文件名，读取文件内容插入 prompt */
export async function resolveFileRefs(
  refs: ContextRef[],
  api: ReturnType<typeof useApi>["api"],
  t: (k: string, opts?: Record<string, unknown>) => string,
): Promise<string> {
  const fileRefs = refs.filter((r) => r.type === "file");
  if (fileRefs.length === 0) return "";

  const results = await Promise.all(
    fileRefs.map(async (ref) => {
      try {
        // 搜索匹配的文件
        const files = await api<Array<{ name: string; path: string }>>(
          `/api/v1/workspace/files/search-by-name?q=${encodeURIComponent(ref.value)}`,
        );
        if (files.length === 0) return null;
        // 读取匹配文件内容（最多读 2 个，避免 token 爆炸）
        const contents = await Promise.all(
          files.slice(0, 2).map(async (f) => {
            try {
              const res = await api<{ content: string }>(
                `/api/v1/workspace/files/read?path=${encodeURIComponent(f.path)}`,
              );
              return { name: f.name, path: f.path, content: res.content.slice(0, useTaskStore.getState().contextUsage.budgets?.tool_result_limit || 3000) };
            } catch {
              return null;
            }
          }),
        );
        const valid = contents.filter((c): c is NonNullable<typeof c> => c != null);
        if (valid.length === 0) return null;
        return { ref, files: valid };
      } catch (e) {
        // P0-1.9: 结构化错误 + toast（原 catch { return null } 静默吞错）
        console.warn("file ref resolve failed", ref.value, e);
        emitGlobalToast(t("phase49.contextPills.fileResolveFail", { value: ref.value }), "error");
        return null;
      }
    }),
  );

  return results
    .filter(Boolean)
    .map((r) => {
      if (!r) return "";
      const blocks = r.files.map(
        (f) => `${t("phase49.contextPills.fileHeader", { path: f.path })}\n\`\`\`\n${f.content}\n\`\`\``,
      );
      return `${t("phase49.contextPills.fileContext", { value: r.ref.value })}\n${blocks.join("\n\n")}`;
    })
    .join("\n\n");
}

/** Phase 6: 解析 @memory 引用：并行搜索所有引用，返回上下文文本 */
export async function resolveMemoryRefs(
  refs: ContextRef[],
  api: ReturnType<typeof useApi>["api"],
  t: (k: string, opts?: Record<string, unknown>) => string,
): Promise<string> {
  const memoryRefs = refs.filter((r) => r.type === "memory");
  if (memoryRefs.length === 0) return "";

  const results = await Promise.all(
    memoryRefs.map(async (ref) => {
      try {
        const items = await api<Array<{ content: string; confidence: number }>>("/api/v1/memories/search", {
          method: "POST",
          body: JSON.stringify({ query: ref.value, limit: 5 }),
        });
        return { ref, items };
      } catch (e) {
        // P0-1.9: 结构化错误 + toast（原 catch { return null } 静默吞错）
        console.warn("memory ref resolve failed", ref.value, e);
        emitGlobalToast(t("phase49.contextPills.memoryResolveFail", { value: ref.value }), "error");
        return null;
      }
    }),
  );

  return results
    .filter(Boolean)
    .map((r) => {
      const lines = r!.items.map(
        (m) => `${t("phase49.contextPills.confidence", { pct: Math.round(m.confidence * 100) })} ${m.content}`,
      );
      return `${t("phase49.contextPills.memoryContext", { value: r!.ref.value })}\n${lines.join("\n")}`;
    })
    .join("\n\n");
}

/** Task 6.3: 解析 @folder 引用——列出文件夹下文件树，注入结构（不读内容）
 * 对标 Cursor @folder——让 Agent 了解目录结构而不过载 token
 */
export async function resolveFolderRefs(
  refs: ContextRef[],
  api: ReturnType<typeof useApi>["api"],
  t: (k: string, opts?: Record<string, unknown>) => string,
): Promise<string> {
  const folderRefs = refs.filter((r) => r.type === "folder");
  if (folderRefs.length === 0) return "";

  const results = await Promise.all(
    folderRefs.map(async (ref) => {
      try {
        const entries = await api<
          Array<{ name: string; path: string; is_dir: boolean; size: number; modified: number }>
        >(`/api/v1/workspace/files?dir=${encodeURIComponent(ref.value)}`);
        if (!entries || entries.length === 0) return null;
        return { ref, entries };
      } catch (e) {
        console.warn("folder ref resolve failed", ref.value, e);
        emitGlobalToast(t("phase49.contextPills.folderResolveFail", { value: ref.value }), "error");
        return null;
      }
    }),
  );

  return results
    .filter(Boolean)
    .map((r) => {
      if (!r) return "";
      const tree = r.entries
        .map((e) => `${e.is_dir ? "📁" : "📄"} ${e.name}${e.is_dir ? "/" : ""}`)
        .join("\n");
      return `${t("phase49.contextPills.folderContext", { value: r.ref.value, count: r.entries.length })}\n${tree}`;
    })
    .join("\n\n");
}

/** Task 6.3: 解析 @git 引用——注入 git log/diff/status
 * 支持: @git:log（最近提交）/ @git:diff（未提交更改）/ @git:status（git 状态）
 * 默认 log（未知 value 也按 log 处理）
 */
export async function resolveGitRefs(
  refs: ContextRef[],
  api: ReturnType<typeof useApi>["api"],
  t: (k: string, opts?: Record<string, unknown>) => string,
): Promise<string> {
  const gitRefs = refs.filter((r) => r.type === "git");
  if (gitRefs.length === 0) return "";

  const results = await Promise.all(
    gitRefs.map(async (ref) => {
      try {
        const subType = ref.value.toLowerCase();
        if (subType === "diff") {
          const res = await api<{ diff: string }>("/api/v1/git/diff");
          if (!res.diff || !res.diff.trim()) return null;
          return { ref, content: res.diff };
        } else if (subType === "status") {
          const res = await api<unknown>("/api/v1/git/status");
          return { ref, content: JSON.stringify(res, null, 2) };
        }
        // 默认 log（包括 @git:log 或未知值）
        const res = await api<unknown>("/api/v1/git/log?count=20");
        return { ref, content: JSON.stringify(res, null, 2) };
      } catch (e) {
        console.warn("git ref resolve failed", ref.value, e);
        emitGlobalToast(t("phase49.contextPills.gitResolveFail", { value: ref.value }), "error");
        return null;
      }
    }),
  );

  return results
    .filter(Boolean)
    .map((r) => {
      if (!r) return "";
      return `${t("phase49.contextPills.gitContext", { value: r.ref.value })}\n\`\`\`\n${r.content}\n\`\`\``;
    })
    .join("\n\n");
}

/** Task 6.3: 解析 @web 引用——web 搜索注入（委托 OC web_search，不可用时回退 URL hint）
 * 对标 Cursor @web——让用户指定搜索查询，结果注入上下文
 */
export async function resolveWebRefs(
  refs: ContextRef[],
  api: ReturnType<typeof useApi>["api"],
  t: (k: string, opts?: Record<string, unknown>) => string,
): Promise<string> {
  const webRefs = refs.filter((r) => r.type === "web");
  if (webRefs.length === 0) return "";

  const results = await Promise.all(
    webRefs.map(async (ref) => {
      try {
        const res = await api<{
          ok: boolean;
          results: Array<{ title?: string; url?: string; snippet?: string }>;
          source: string;
          url_hint?: Record<string, string>;
        }>(`/api/v1/web/search?q=${encodeURIComponent(ref.value)}`);
        if (!res.ok && (!res.results || res.results.length === 0) && !res.url_hint) return null;
        return { ref, res };
      } catch (e) {
        console.warn("web ref resolve failed", ref.value, e);
        emitGlobalToast(t("phase49.contextPills.webResolveFail", { value: ref.value }), "error");
        return null;
      }
    }),
  );

  return results
    .filter(Boolean)
    .map((r) => {
      if (!r) return "";
      const { res } = r;
      // OC 成功——注入真实搜索结果
      if (res.source === "openclaw" && res.results && res.results.length > 0) {
        const lines = res.results
          .map(
            (item, i) =>
              `${i + 1}. ${item.title || "Untitled"}\n   ${item.url || ""}\n   ${item.snippet || ""}`,
          )
          .join("\n");
        return `${t("phase49.contextPills.webContext", { value: r.ref.value, count: res.results.length })}\n${lines}`;
      }
      // 降级——URL hint
      if (res.url_hint) {
        const urls = Object.entries(res.url_hint)
          .map(([engine, url]) => `- ${engine}: ${url}`)
          .join("\n");
        return `${t("phase49.contextPills.webHint", { value: r.ref.value })}\n${urls}`;
      }
      return "";
    })
    .join("\n\n");
}
