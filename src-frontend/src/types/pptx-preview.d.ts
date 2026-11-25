/**
 * pptx-preview 没有自带类型声明（package.json 里没有 types 字段），
 * 这里只声明我们真正用到的那一个入口，避免整仓退回 any。
 */
declare module "pptx-preview" {
  export interface PptxPreviewer {
    preview(data: ArrayBuffer): Promise<unknown> | unknown;
  }
  export function init(
    element: HTMLElement,
    options: { width: number; height: number },
  ): PptxPreviewer;
}
