/**
 * mammoth 的浏览器构建没有类型声明，这里只声明我们用到的入口。
 *
 * 为什么要直接引 `mammoth/mammoth.browser` 而不是 `mammoth`：
 * 包默认入口是 Node 版（用 fs 打开 zip），浏览器版才接受 `{ arrayBuffer }`。
 * 靠打包器的 browser 字段映射"碰运气"不如显式依赖——真实 docx 在浏览器里
 * 解析不出来的话，用户看到的就是一句"打不开"。
 */
declare module "mammoth/mammoth.browser" {
  export interface MammothResult {
    value: string;
    messages: Array<{ type: string; message: string }>;
  }
  export function convertToHtml(
    input: { arrayBuffer: ArrayBuffer },
    options?: { styleMap?: string | string[]; includeDefaultStyleMap?: boolean },
  ): Promise<MammothResult>;
  export function extractRawText(input: { arrayBuffer: ArrayBuffer }): Promise<MammothResult>;
}
