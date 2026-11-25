import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

class ResizeObserverStub {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}

window.ResizeObserver = ResizeObserverStub;

// Batch 4a F4: monaco-editor 本地打包后模块求值依赖 document.queryCommandSupported
// （jsdom 未实现）——stub 掉，使导入 ChatView → EditorPanel → MonacoEditor 链的测试可用
if (typeof document !== "undefined" && typeof document.queryCommandSupported !== "function") {
  Object.defineProperty(document, "queryCommandSupported", {
    writable: true,
    value: () => false,
  });
}

vi.mock("react-i18next", () => {
  // t 必须在多次渲染间保持引用稳定——真实的 react-i18next 只在语言切换时才换 t 的引用。
  // 如果像之前那样把箭头函数写在 useTranslation 的返回值里，每次渲染都是一个新函数，
  // 任何把 t 列进依赖数组的 effect/callback 都会每次渲染重建、effect 无限重跑；
  // 表现为「明明只 mock 了一次返回值，却被多消耗了一次」这类极难定位的测试失败。
  const t = (key: string, options?: Record<string, unknown>) => {
    if (options && typeof options.returnObjects === "boolean") {
      return [];
    }
    return String(key);
  };
  return {
    useTranslation: () => ({ t, i18n: { language: "zh" } }),
    initReactI18next: { type: "3rdParty", init: vi.fn() },
  };
});
