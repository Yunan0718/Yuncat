import { useEffect, useRef, useState } from "react";

/**
 * 高频更新值的节流（leading + trailing）。
 *
 * 契约：
 * - 距上次产出 >= minIntervalMs → 立即产出（leading；首次渲染必然直通）
 * - 否则排一个 trailing 定时器，窗口末尾补产出**当时最新**的值
 * - 产出频率上限 ≈ 1 / minIntervalMs，**与上游更新频率无关**（这是本 hook 的全部意义）
 *
 * 实现取舍（两种写法行为等价，这里取定时器抖动更少的那种）：
 * 常见写法把 `clearTimeout` 放进该 effect 的 cleanup，于是每个新值都会
 * clear + 重排一次定时器。由于 `elapsed` 按真实时间累积，那种写法到点同样会
 * 走 leading 分支出帧，**并不会**退化成防抖——只是定时器抖动更多。
 * 本实现改为「已有 pending 就不再重排」，代价是定时器回调必须读 `latest` ref
 * （闭包捕获的是排程那一刻的旧值），换来每窗口只操作一次定时器。
 *
 * 注意：cleanup 只在卸载时执行。若放进上面的 effect，每次值变化都会清掉
 * trailing 定时器，最后一帧就只能靠下一次更新触发。
 *
 * 用途：流式文本每来一个 token 就全量 `marked.parse + hljs + DOMPurify` 是
 * O(tokens × n) 的开销；节流后解析次数与**时长**成正比，与 token 数无关。
 */
export function useThrottledValue<T>(value: T, minIntervalMs = 120): T {
  const [throttled, setThrottled] = useState(value);
  const lastAt = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 定时器回调必须读到最新值——闭包捕获的会是排程那一刻的旧值
  const latest = useRef(value);
  latest.current = value;

  useEffect(() => {
    const now = Date.now();
    const elapsed = now - lastAt.current;

    if (elapsed >= minIntervalMs) {
      lastAt.current = now;
      setThrottled(value);
      return;
    }

    // 已有 pending 定时器：它会用 latest 产出最新值，无需重复排程
    if (timer.current !== null) return;

    timer.current = setTimeout(() => {
      timer.current = null;
      lastAt.current = Date.now();
      setThrottled(latest.current);
    }, minIntervalMs - elapsed);
  }, [value, minIntervalMs]);

  // 仅在卸载时清理——不能放进上面的 effect，否则每次值变化都会取消 trailing
  useEffect(() => {
    return () => {
      if (timer.current !== null) {
        clearTimeout(timer.current);
        timer.current = null;
      }
    };
  }, []);

  return throttled;
}
