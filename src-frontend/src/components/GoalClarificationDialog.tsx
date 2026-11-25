"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "./ui/dialog";
import { Button } from "./ui/button";
import type { ClarificationQuestion } from "../store/taskStore";

/**
 * Phase 78: 目标澄清弹窗
 *
 * 触发：服务端推送 `goal_clarification_request` → taskStore.setClarification()
 * 渲染：ChatView 监听 task.clarification 字段，存在则展示本组件
 * 提交：用户点击「确认目标」→ ws 发送 `goal_clarification_response` → 服务端 refine_goal
 *
 * 对齐 FileTimelineDialog 的 shadcn/ui Dialog 风格（非 confirmDialog 自定义 div 风格）。
 * 60s 无响应自动跳过（autoSkipMs=0 关闭）——首期默认开启。
 */

export interface GoalClarificationDialogProps {
  taskId: string;
  questions: ClarificationQuestion[];
  /** "initial" | "followup" | "auto_followup" | "adversarial_replan" */
  stage: string;
  /** Task 6.11: auto_followup 阶段携带的低 confidence 追问理由 */
  reason?: string;
  /** 自动跳过毫秒数；传 0 关闭自动跳过 */
  autoSkipMs?: number;
  /** 用户点击「确认目标」——提交答案给服务端提炼 */
  onSubmit: (answers: { question: string; answer: string }[]) => void;
  /** 用户点击「还需澄清」——请求服务端再生成问题 */
  onNeedMore: (answers: { question: string; answer: string }[]) => void;
  /** 用户点击「跳过」或自动跳过触发 */
  onSkip: () => void;
}

const DEFAULT_AUTO_SKIP_MS = 60_000;

export function GoalClarificationDialog({
  taskId,
  questions,
  stage,
  reason,
  autoSkipMs = DEFAULT_AUTO_SKIP_MS,
  onSubmit,
  onNeedMore,
  onSkip,
}: GoalClarificationDialogProps) {
  const { t } = useTranslation();
  const [answers, setAnswers] = useState<string[]>(() => questions.map(() => ""));
  // 倒计时剩余秒数（仅用于显示；实际触发用绝对时间戳避免 drift）
  const [remaining, setRemaining] = useState<number>(autoSkipMs > 0 ? Math.ceil(autoSkipMs / 1000) : 0);
  const submittedRef = useRef(false); // 防止自动跳过与用户操作重复触发

  // 问题列表变化时重置答案（同一 taskId 下 stage 切换 initial→followup）
  useEffect(() => {
    setAnswers(questions.map(() => ""));
    submittedRef.current = false;
    // 刻意依赖 questions.length 而非 questions：questions 取自 store 里的
    // task.clarification，task 对象一旦因任何更新被重建，数组引用就会变，
    // 直接依赖 questions 会在用户答题过程中把已填内容清空。长度不变即题目未变，
    // 用长度当重置信号才是安全的——不要"顺手修"成 [questions]。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, stage, questions.length]);

  // 60s 倒计时
  useEffect(() => {
    if (autoSkipMs <= 0) return;
    const deadline = Date.now() + autoSkipMs;
    setRemaining(Math.ceil(autoSkipMs / 1000));
    const timer = setInterval(() => {
      const left = deadline - Date.now();
      if (left <= 0) {
        clearInterval(timer);
        if (!submittedRef.current) {
          submittedRef.current = true;
          onSkip();
        }
        return;
      }
      setRemaining(Math.ceil(left / 1000));
    }, 1000);
    return () => clearInterval(timer);
  }, [autoSkipMs, taskId, stage, onSkip]);

  const collected = useMemo(
    () =>
      questions.map((q, i) => ({
        question: q.question,
        answer: answers[i] ?? "",
      })),
    [questions, answers],
  );
  const hasAnyAnswer = collected.some((a) => a.answer.trim().length > 0);

  const handleSubmit = () => {
    if (submittedRef.current) return;
    if (!hasAnyAnswer) return;
    submittedRef.current = true;
    onSubmit(collected);
  };
  const handleNeedMore = () => {
    if (submittedRef.current) return;
    submittedRef.current = true;
    onNeedMore(collected);
  };
  const handleSkip = () => {
    if (submittedRef.current) return;
    submittedRef.current = true;
    onSkip();
  };

  const stageLabel = useMemo(() => {
    // Task 6.11: 支持 auto_followup / adversarial_replan 阶段标签
    switch (stage) {
      case "followup":
        return t("chatView.goalClarification.stageFollowup");
      case "auto_followup":
        return t("chatView.goalClarification.stageAutoFollowup");
      case "adversarial_replan":
        return t("chatView.goalClarification.stageAdversarialReplan");
      default:
        return t("chatView.goalClarification.stageInitial");
    }
  }, [stage, t]);

  // Task 6.11: auto_followup 阶段展示低 confidence 追问理由
  const showReason = stage === "auto_followup" && typeof reason === "string" && reason.length > 0;

  return (
    <Dialog open onOpenChange={(v) => !v && handleSkip()}>
      <DialogContent className="max-w-xl w-[90vw] max-h-[85vh] flex flex-col p-0" showCloseButton={false}>
        <DialogHeader className="px-5 py-3 border-b shrink-0">
          <div className="flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-primary" />
            <DialogTitle className="text-sm font-medium">
              {t("chatView.goalClarification.title")}
              <span className="ml-2 text-[10px] font-normal text-muted-foreground px-1.5 py-0.5 rounded bg-secondary">
                {stageLabel}
              </span>
            </DialogTitle>
          </div>
          <DialogDescription className="text-xs text-muted-foreground">
            {t("chatView.goalClarification.subtitle")}
            {autoSkipMs > 0 && (
              <span className="ml-2 text-[10px] text-warning">
                {t("chatView.goalClarification.autoSkipHint", { s: remaining })}
              </span>
            )}
          </DialogDescription>
          {showReason && (
            <div
              role="note"
              aria-live="polite"
              className="mt-2 text-[11px] leading-relaxed text-warning bg-warning/10 border border-warning/30 rounded-md px-2.5 py-1.5"
            >
              <span className="font-medium">
                {t("chatView.goalClarification.autoFollowupReasonLabel")}
              </span>
              <span className="ml-1">{reason}</span>
            </div>
          )}
        </DialogHeader>

        <div className="flex-1 overflow-y-auto p-4 space-y-3">
          {questions.length === 0 ? (
            <p className="text-xs text-muted-foreground text-center py-4">No questions</p>
          ) : (
            questions.map((q, i) => (
              <div key={q.id || i} className="border rounded-lg p-3 space-y-2">
                <div className="flex items-start gap-2">
                  <span className="text-[10px] font-mono text-muted-foreground shrink-0 mt-0.5">
                    {t("chatView.goalClarification.questionLabel", { n: i + 1 })}
                  </span>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm leading-relaxed">{q.question}</p>
                    <p className="text-[10px] text-muted-foreground mt-0.5 font-mono">[{q.type}]</p>
                  </div>
                </div>
                <textarea
                  className="w-full bg-secondary/50 border rounded-md p-2 text-xs outline-none focus:ring-1 focus:ring-primary resize-y min-h-[60px]"
                  placeholder={t("chatView.goalClarification.answerPlaceholder")}
                  value={answers[i] ?? ""}
                  onChange={(e) => {
                    const v = e.target.value;
                    setAnswers((prev) => {
                      const next = [...prev];
                      while (next.length <= i) next.push("");
                      next[i] = v;
                      return next;
                    });
                  }}
                  rows={2}
                />
              </div>
            ))
          )}
        </div>

        <DialogFooter className="px-5 py-3 border-t bg-muted/30 rounded-b-xl shrink-0">
          <Button variant="ghost" size="sm" onClick={handleSkip}>
            {t("chatView.goalClarification.skip")}
          </Button>
          <Button variant="outline" size="sm" onClick={handleNeedMore}>
            {t("chatView.goalClarification.needMore")}
          </Button>
          <Button size="sm" onClick={handleSubmit} disabled={!hasAnyAnswer}>
            {t("chatView.goalClarification.confirm")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
