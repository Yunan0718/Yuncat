import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * v67: 头尾截断度量。
 *
 * 借鉴 DeepSeek harness（`packages/client/ui-primitives/src/head-tail-cap.ts`）。
 * 长文本若只保留头部，会丢掉往往最关键的结尾（报错信息、结果摘要、退出码）。
 * 因此保留「头 + 尾」，把中间折叠为一个「N 已隐藏」的提示。
 *
 * 纯算术、不做切片——调用方自行按 headCount/tailCount 切分，便于各自叠加
 * 额外逻辑（例如命中行保留所属文件头）。
 *
 * @param total - 总长度（字符数或行数均可，与 max 同单位）
 * @param max - 折叠态下保留的上限
 * @param expanded - 是否已展开（展开则不做截断）
 */
export interface HeadTailCap {
  /** 被隐藏的数量（total − max）；≤ 0 表示无隐藏 */
  hidden: number;
  /** 是否处于折叠截断态 */
  capped: boolean;
  /** 头部保留量：ceil(max / 2) */
  headCount: number;
  /** 尾部保留量：max − headCount */
  tailCount: number;
}

export function headTailCap(total: number, max: number, expanded: boolean): HeadTailCap {
  const hidden = total - max;
  const headCount = Math.ceil(max / 2);
  return { hidden, capped: hidden > 0 && !expanded, headCount, tailCount: max - headCount };
}

/**
 * v67: 单行文本的中段省略——保留开头与结尾，中间以 `…` 连接。
 *
 * 适用于工具结果预览这类单行展示：只截头部会让用户看不到结尾的报错或结果。
 * @param text - 原文
 * @param max - 折叠态下保留的最大字符数（不含省略号）
 */
export function truncateMiddle(text: string, max: number): string {
  if (max <= 0) return "";
  if (text.length <= max) return text;
  const { headCount, tailCount } = headTailCap(text.length, max, false);
  return `${text.slice(0, headCount)}…${text.slice(text.length - tailCount)}`;
}
