import { Check, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "../ui/button";
import type { OptimizeResult } from "../../hooks/usePromptOptimize";

interface OptimizedPromptPreviewProps {
  rawPrompt: string;
  result: OptimizeResult;
  onApply: (optimized: string) => void;
  onDiscard: () => void;
}

/**
 * Batch 5.2: 优化结果预览
 *
 * 展示：
 * - 多个候选按钮（带分数），可切换当前查看的候选
 * - 左右对比：原始 prompt vs 当前候选优化后文本
 * - 当前候选的改动点列表
 * - 应用 / 放弃 按钮
 */
export function OptimizedPromptPreview({ rawPrompt, result, onApply, onDiscard }: OptimizedPromptPreviewProps) {
  const { t } = useTranslation();
  const [selectedIndex, setSelectedIndex] = useState(0);

  useEffect(() => {
    setSelectedIndex(0);
  }, [result]);

  const selected = result.candidates[selectedIndex] || result;

  return (
    <div
      className="border rounded-md p-3 mt-2 bg-muted/30"
      data-testid="optimize-preview"
    >
      <div className="text-xs font-medium mb-2 text-muted-foreground">
        {t("chat.optimizePrompt.title")}
      </div>

      {result.candidates.length > 1 && (
        <div className="flex flex-wrap gap-1.5 mb-3" data-testid="optimize-candidates">
          {result.candidates.map((candidate, idx) => (
            <Button
              key={idx}
              variant={idx === selectedIndex ? "default" : "outline"}
              size="sm"
              className="h-6 text-[10px] px-2"
              onClick={() => setSelectedIndex(idx)}
              data-testid={`optimize-candidate-${idx}`}
            >
              {t("chat.optimizePrompt.candidateLabel", { n: idx + 1 })}
              {" "}
              <span className="opacity-80 ml-1">
                {Math.round(candidate.score * 100)}%
              </span>
            </Button>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-2 mb-3">
        <div className="border rounded p-2 bg-background/50">
          <div className="text-[10px] text-muted-foreground mb-1">
            {t("chat.optimizePrompt.originalLabel")}
          </div>
          <div className="text-sm whitespace-pre-wrap break-words text-muted-foreground">
            {rawPrompt}
          </div>
        </div>
        <div className="border rounded p-2 bg-background">
          <div className="text-[10px] text-muted-foreground mb-1">
            {t("chat.optimizePrompt.optimizedLabel")}
          </div>
          <div className="text-sm whitespace-pre-wrap break-words">
            {selected.optimized}
          </div>
        </div>
      </div>

      {selected.changes.length > 0 && (
        <div className="mb-3">
          <div className="text-xs text-muted-foreground mb-1">
            {t("chat.optimizePrompt.changesLabel")}
          </div>
          <ul className="text-xs space-y-0.5 list-disc list-inside">
            {selected.changes.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex gap-2 justify-end">
        <Button variant="ghost" size="sm" onClick={onDiscard} data-testid="optimize-discard">
          <X className="h-3 w-3 mr-1" />
          {t("chat.optimizePrompt.discard")}
        </Button>
        <Button
          variant="default"
          size="sm"
          onClick={() => onApply(selected.optimized)}
          data-testid="optimize-apply"
        >
          <Check className="h-3 w-3 mr-1" />
          {t("chat.optimizePrompt.apply")}
        </Button>
      </div>
    </div>
  );
}
