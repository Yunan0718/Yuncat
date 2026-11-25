import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";

/**
 * 会话级输入草稿（P3-2）。
 *
 * 2026-09-15 修复：原实现把「写草稿」的 effect 依赖数组写成 [input]，key 却取
 * currentSessionId —— 切换会话时 effect 不重跑，草稿既不存回旧会话、也不载入新会话，
 * 还会把上一个会话的输入写进新会话的 key。
 *
 * 现做法：把「当前 key」放进 ref，写盘只依赖 input；切换会话时在同一个 effect 内
 * 先存旧、再载新，顺序确定，也不需要在写盘 effect 里塞 currentSessionId。
 */
const KEY_PREFIX = "yuncat:draft:";
const keyOf = (sessionId: string | null | undefined) => KEY_PREFIX + (sessionId || "default");

export function useSessionDraft(
  sessionId: string | null | undefined,
): [string, Dispatch<SetStateAction<string>>] {
  const [input, setInput] = useState(() => {
    try {
      return localStorage.getItem(keyOf(sessionId)) || "";
    } catch {
      return "";
    }
  });
  const draftKeyRef = useRef(keyOf(sessionId));
  const latestRef = useRef(input);

  // 输入变化即写盘（写的是「当前 key」，与渲染期的 sessionId 无关）
  useEffect(() => {
    latestRef.current = input;
    try {
      if (input) localStorage.setItem(draftKeyRef.current, input);
      else localStorage.removeItem(draftKeyRef.current);
    } catch {
      // localStorage 不可用时静默降级：草稿仅存活于内存
    }
  }, [input]);

  // 会话切换：先把旧会话草稿落盘，再载入新会话草稿
  useEffect(() => {
    const nextKey = keyOf(sessionId);
    if (nextKey === draftKeyRef.current) return;
    const prevKey = draftKeyRef.current;
    draftKeyRef.current = nextKey;
    try {
      const restored = localStorage.getItem(nextKey) || "";
      if (latestRef.current) localStorage.setItem(prevKey, latestRef.current);
      else localStorage.removeItem(prevKey);
      setInput(restored);
      latestRef.current = restored;
    } catch {
      // 读不到就保留当前输入，不阻塞会话切换
    }
  }, [sessionId]);

  return [input, setInput];
}
