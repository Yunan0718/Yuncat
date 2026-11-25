import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { CircleHelp } from "lucide-react";
import { useTaskStore } from "../../store/taskStore";
import { useWebSocket } from "../../hooks/useWebSocket";
import { emitGlobalToast } from "../common/toast-context";
import { Button } from "../ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "../ui/dialog";
import { Textarea } from "../ui/textarea";

export function UserQuestionDialog() {
  const { t } = useTranslation();
  const question = useTaskStore((state) => state.userQuestions[0] ?? null);
  const removeQuestion = useTaskStore((state) => state.removeUserQuestion);
  const { answerUserQuestion } = useWebSocket();
  const [answer, setAnswer] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [remaining, setRemaining] = useState<number | null>(null);

  useEffect(() => {
    setAnswer("");
    setSubmitting(false);
  }, [question?.token]);

  useEffect(() => {
    if (!question?.expires_at) {
      setRemaining(null);
      return;
    }
    const token = question.token;
    let closed = false;
    let timer: number | undefined;
    const update = () => {
      if (closed) return;
      const seconds = Math.ceil((question.expires_at * 1000 - Date.now()) / 1000);
      setRemaining(Math.max(0, seconds));
      if (seconds <= 0) {
        closed = true;
        removeQuestion(token);
        emitGlobalToast(t("askUser.expired"), "info");
      }
    };
    update();
    if (!closed) timer = window.setInterval(update, 1000);
    return () => {
      closed = true;
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [question?.token, question?.expires_at, removeQuestion, t]);

  if (!question) return null;
  const canSubmit = Boolean(answer.trim())
    && (question.allow_free_text || question.options.includes(answer.trim()));
  const submit = () => {
    const value = answer.trim();
    if (!value || !canSubmit || submitting) return;
    setSubmitting(true);
    answerUserQuestion(question.token, value);
  };
  const cancel = () => {
    answerUserQuestion(question.token, "", true);
    removeQuestion(question.token);
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open) cancel(); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            <CircleHelp size={17} className="text-primary" /> {t("askUser.title")}
          </DialogTitle>
        </DialogHeader>
        <p className="text-sm whitespace-pre-wrap">{question.question}</p>
        {question.options.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            {question.options.map((option) => (
              <Button key={option} size="sm" variant={answer === option ? "default" : "outline"} onClick={() => setAnswer(option)}>
                {option}
              </Button>
            ))}
          </div>
        )}
        {question.allow_free_text && (
          <Textarea
            value={answer}
            onChange={(event) => setAnswer(event.target.value)}
            onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") submit(); }}
            placeholder={t("askUser.placeholder")}
            rows={4}
            autoFocus
          />
        )}
        <div className="flex justify-end gap-2">
          {remaining !== null && (
            <span className="mr-auto text-xs text-muted-foreground" aria-live="polite">
              {t("askUser.expiresIn", { seconds: remaining })}
            </span>
          )}
          <Button variant="outline" onClick={cancel}>{t("common.cancel")}</Button>
          <Button disabled={!canSubmit || submitting} onClick={submit}>{t("askUser.submit")}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
