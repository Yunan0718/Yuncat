import { useCallback, useEffect, useRef } from "react";
import { useAppStore } from "../store/appStore";

/**
 * P0-8 (2026-07-04): ApiError —— 携带 HTTP 状态码 + 响应体 + 友好消息。
 *
 * 旧实现 `throw new Error("API error: " + status)` 问题：
 * 1. 无状态码——调用方无法区分 401（需登录）/403（无权限）/404（不存在）/500（服务器错误）
 * 2. 无响应体——后端 FastAPI 返回 `{"detail": "具体原因"}` 的信息丢失
 * 3. 无重试判断——5xx/超时/429 应重试，4xx 不应重试
 * 4. 无网络错误区分——fetch 抛 TypeError 时无法区分 DNS 失败/连接拒绝/超时
 *
 * Task 7.18: 新增 aborted 标记——区分 abort（组件 unmount / 调用方 cancel）与真实网络错误。
 *
 * 对标主流前端 SDK（axios AxiosError / ky HTTPError / trpc TRPCClientError）。
 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly statusText: string,
    public readonly body: string,
    public readonly url: string,
    /** Task 7.18: 标记请求是否被 abort（组件 unmount 或调用方 cancel） */
    public readonly aborted?: boolean,
  ) {
    // 友好消息优先级：后端 detail > 后端 message > statusText > fallback
    let friendly = "";
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed?.detail === "string" && parsed.detail) friendly = parsed.detail;
      else if (typeof parsed?.message === "string" && parsed.message) friendly = parsed.message;
    } catch {
      // body 不是 JSON——若为纯文本则用 body
      if (body && body.length < 200) friendly = body;
    }
    super(friendly || statusText || `API error: ${status}`);
    this.name = "ApiError";
  }

  /** 5xx / 408 / 429 可重试；4xx 不应重试；0 = 网络错误可重试
   *  Batch E M25: "Backend not ready" 虽然用 status 0，但不应重试——
   *  后端未就绪时重试会导致无限循环（调用方等 isRetryable 判断为 true 后不断重试）。
   */
  get isRetryable(): boolean {
    if (this.status === 0 && this.statusText === "Backend not ready") return false;
    return this.status === 0 || this.status === 408 || this.status === 429 || this.status >= 500;
  }

  /** 401 未认证 / 403 无权限 */
  get isAuthError(): boolean {
    return this.status === 401 || this.status === 403;
  }

  /** 404 资源不存在 */
  get isNotFound(): boolean {
    return this.status === 404;
  }

  /** Task 7.18: 请求被 abort（组件 unmount 或调用方 cancel）——调用方应静默忽略 */
  get isAborted(): boolean {
    return this.aborted === true;
  }
}

/**
 * Task 7.18 / 2026-09-04: 判断错误是否为"请求被取消"。
 *
 * 背景：React StrictMode（dev）会 mount→unmount→remount 每个组件，组件卸载时
 * useApi 按设计 abort 所有在途请求（见 useApiAbort.test.ts）。这会让加载类请求的
 * catch 收到一个 isAborted=true 的 ApiError。这种 abort 不是真实故障，不应作为
 * 用户可见错误（toast / error banner）展示——否则开发预览和快速切页会出现大量
 * 误导性"加载失败 / Request was aborted"。
 *
 * 用法（对标 React Query / SWR 的 abort 静默约定）：
 *   .catch((e) => { if (isAbortedError(e)) return; toast(...); });
 */
export function isAbortedError(e: unknown): e is ApiError {
  return e instanceof ApiError && e.isAborted;
}

/**
 * API 请求 hook——自动注入 Token 和端口。
 * v64: api 用 useCallback 稳定引用，加入 useEffect deps 不触发死循环。
 * Phase 47 修复: FormData body 时不强制 Content-Type，让浏览器自动设置 multipart boundary。
 *
 * P0-8: 抛 ApiError 而非裸 Error——携带状态码、响应体、友好消息、重试判断。
 *
 * Task 7.18: AbortController 支持——组件 unmount 时自动 abort 所有在途请求。
 * 对标 React Query / SWR / axios 的请求取消机制——避免组件卸载后 Promise resolve 浪费资源。
 * 调用方也可通过 options.signal 主动取消（与 unmount abort 合并）。
 *
 * Batch 7.6: access token 过期时通过 IPC 触发主进程刷新，并重试原请求一次。
 * 2026-10-06: 403 也走同一条刷新重试。签名密钥由每次启动的静态 TOKEN 派生，
 * 后端重启后渲染进程手里的 access token 全部失效——中间件对无效 token 返 403
 * 而不是 401，只处理 401 会让 UI 持续 403（实测：同组接口 200/403 交错）。
 */
export function useApi() {
  const port = useAppStore((s) => s.port);
  const token = useAppStore((s) => s.token);
  const setToken = useAppStore((s) => s.setToken);

  // Task 7.18: 跟踪所有在途 AbortController——unmount 时全部 abort
  // 使用 Set 而非单个 ref——支持多个并发请求各自独立 abort
  const controllersRef = useRef<Set<AbortController>>(new Set());

  // Batch 7.6: 防止并发 401 触发多次刷新——共享单个刷新 Promise
  const refreshPromiseRef = useRef<Promise<{ ok: boolean; token?: string; error?: string }> | null>(null);

  // Cleanup: 组件 unmount 时 abort 所有在途请求
  //
  // 这里捕获 Set 本身而非在 cleanup 里读 `controllersRef.current`：
  // exhaustive-deps 会警告 ref.current 在 cleanup 运行时可能已变——那条警告针对的是
  // `ref.current` 被**重新赋值**的情况。而本文件的 controllersRef.current 只在
  // 第 85 行初始化一次（add/delete 是原地增删，不改引用），Set 身份在整个生命周期稳定。
  // 把它提到局部常量，既让这个不变量显式化，也让依赖数组是真实且稳定的。
  const controllers = controllersRef.current;
  useEffect(
    () => () => {
      controllers.forEach((c) => c.abort());
      controllers.clear();
    },
    [controllers],
  );

  const doFetch = useCallback(
    async (url: string, options?: RequestInit, accessToken?: string): Promise<Response> => {
      const authToken = accessToken || token;
      const isFormData =
        typeof FormData !== "undefined" && options?.body instanceof FormData;

      // Task 7.18: 为每个请求创建独立 AbortController
      const controller = new AbortController();
      controllersRef.current.add(controller);

      // 合并调用方提供的 signal（如有）——调用方 abort → 我们的 controller 也 abort
      const callerSignal = options?.signal;
      if (callerSignal) {
        if (callerSignal.aborted) {
          controller.abort();
        } else {
          callerSignal.addEventListener("abort", () => controller.abort(), { once: true });
        }
      }

      let res: Response;
      try {
        res = await fetch(url, {
          ...options,
          signal: controller.signal,
          headers: {
            Authorization: `Bearer ${authToken}`,
            // FormData 时由浏览器自动设置 Content-Type（含 boundary），不显式覆盖
            ...(isFormData ? {} : { "Content-Type": "application/json" }),
            ...options?.headers,
          },
        });
      } catch (err) {
        // Task 7.18: 区分 abort 与网络错误
        if (controller.signal.aborted) {
          throw new ApiError(0, "Aborted", "Request was aborted", url, true);
        }
        // 网络错误——fetch 抛 TypeError（DNS/连接拒绝/超时）
        throw new ApiError(0, "Network error", String(err), url);
      } finally {
        // 请求完成（成功或失败）——从跟踪集合移除，避免泄漏
        controllersRef.current.delete(controller);
      }
      return res;
    },
    [token],
  );

  const refreshTokenOnce = useCallback(async (): Promise<{ ok: boolean; token?: string }> => {
    if (!window.yuncat?.refreshAccessToken) {
      return { ok: false };
    }
    if (refreshPromiseRef.current) {
      return refreshPromiseRef.current as Promise<{ ok: boolean; token?: string }>;
    }
    const promise = window.yuncat.refreshAccessToken();
    refreshPromiseRef.current = promise;
    try {
      const result = await promise;
      if (result.ok && result.token) {
        setToken(result.token);
        return { ok: true, token: result.token };
      }
      return { ok: false };
    } finally {
      refreshPromiseRef.current = null;
    }
  }, [setToken]);

  const api = useCallback(
    async <T>(path: string, options?: RequestInit): Promise<T> => {
      if (!port || !token) {
        // 后端未就绪——用 status 0 表示（调用方可检查 isRetryable 重试）
        throw new ApiError(0, "Backend not ready", "", path);
      }
      const url = `http://127.0.0.1:${port}${path}`;

      let res = await doFetch(url, options);

      // Batch 7.6: 401 时尝试刷新 access token 并重试一次
      if (res.status === 401 || res.status === 403) {
        const refreshResult = await refreshTokenOnce();
        if (refreshResult.ok && refreshResult.token) {
          res = await doFetch(url, options, refreshResult.token);
        }
      }

      if (!res.ok) {
        const body = await res.text().catch(() => "");
        throw new ApiError(res.status, res.statusText, body, url);
      }
      try {
        return await res.json();
      } catch (err) {
        // JSON 解析失败——响应体不是合法 JSON
        throw new ApiError(res.status, "Invalid JSON response", String(err), url);
      }
    },
    [port, token, doFetch, refreshTokenOnce],
  );

  /** 取二进制响应（图片预览等）——鉴权与 401 刷新重试与 api() 一致。 */
  const apiBlob = useCallback(
    async (path: string, options?: RequestInit): Promise<Blob> => {
      if (!port || !token) {
        throw new ApiError(0, "Backend not ready", "", path);
      }
      const url = `http://127.0.0.1:${port}${path}`;
      let res = await doFetch(url, options);
      if (res.status === 401 || res.status === 403) {
        const refreshResult = await refreshTokenOnce();
        if (refreshResult.ok && refreshResult.token) {
          res = await doFetch(url, options, refreshResult.token);
        }
      }
      if (!res.ok) {
        const body = await res.text().catch(() => "");
        throw new ApiError(res.status, res.statusText, body, url);
      }
      return res.blob();
    },
    [port, token, doFetch, refreshTokenOnce],
  );

  return { api, apiBlob };
}
