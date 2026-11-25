import { useTranslation } from "react-i18next";
import type { ContextRef } from "./context-pills";

interface ContextPillsProps {
  refs: ContextRef[];
  onRemove: (ref: ContextRef) => void;
}

const COLOR_MAP: Record<ContextRef["type"], string> = {
  agent: "bg-entity-agent/10 text-entity-agent border-entity-agent/20",
  memory: "bg-entity-agent/10 text-entity-agent border-entity-agent/20",
  file: "bg-entity-file/10 text-entity-file border-entity-file/20",
  session: "bg-entity-workflow/10 text-entity-workflow border-entity-workflow/20",
  folder: "bg-entity-file/10 text-entity-file border-entity-file/20",
  web: "bg-entity-url/10 text-entity-url border-entity-url/20",
  git: "bg-entity-workflow/10 text-entity-workflow border-entity-workflow/20",
};

/**
 * Phase 6: @引用药丸标签组件
 *
 * 在输入框下方显示已解析的 @引用，每个药丸可点 X 删除。
 * P0-5.2: 添加 title hover 预览——显示引用类型 + value + 发送时行为说明。
 */
export function ContextPills({ refs, onRemove }: ContextPillsProps) {
  const { t } = useTranslation();
  if (refs.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1 mb-1">
      {refs.map((ref) => {
        const tipKey = `phase49.contextPills.tip.${ref.type}`;
        const tipDefault = `@${ref.type}:${ref.value}`;
        const tipText = t(tipKey, { value: ref.value, defaultValue: tipDefault });
        return (
          <span
            key={`${ref.type}-${ref.value}-${ref.startIndex}`}
            className={`inline-flex items-center gap-1 text-[10px] font-semibold
                        px-2 py-0.5 rounded-full border cursor-help ${COLOR_MAP[ref.type]}`}
            title={tipText}
          >
            {t(`phase49.contextPills.label.${ref.type}`)}: {ref.value}
            <button
              onClick={(e) => {
                e.stopPropagation();
                onRemove(ref);
              }}
              className="ml-0.5 hover:opacity-70 font-bold"
              title={t("legacy.ctxPills_48ef")}
              aria-label={t("legacy.ctxPills_48ef")}
            >
              ×
            </button>
          </span>
        );
      })}
    </div>
  );
}
