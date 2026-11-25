import { useTranslation } from "react-i18next";

/** 工具名本地化 — 根据当前语言显示本地化工具名 */
export function useToolName(toolName: string): string {
  const { t } = useTranslation();
  return t(`tools.${toolName}`, toolName);
}
