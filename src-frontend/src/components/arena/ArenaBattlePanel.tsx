import { useTranslation } from "react-i18next";
import { Button } from "../ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../ui/card";
import { ResultRenderer } from "../ResultRenderer";
import { Trophy, Equal } from "lucide-react";

export interface RevealedInfo {
  modelA: string;
  modelB: string;
}

interface ArenaBattlePanelProps {
  responseA: string;
  responseB: string;
  onVote: (choice: "a" | "b" | "tie") => void;
  revealed?: RevealedInfo;
  voted?: boolean;
}

export function ArenaBattlePanel({
  responseA,
  responseB,
  onVote,
  revealed,
  voted,
}: ArenaBattlePanelProps) {
  const { t } = useTranslation();
  const canReveal = voted || revealed;

  return (
    <div className="flex flex-col h-full gap-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 flex-1 min-h-0">
        <ResponseCard
          label={t("arena.modelA")}
          response={responseA}
          revealed={canReveal ? revealed?.modelA : undefined}
        />
        <ResponseCard
          label={t("arena.modelB")}
          response={responseB}
          revealed={canReveal ? revealed?.modelB : undefined}
        />
      </div>

      {!revealed && (
        <div className="flex items-center justify-center gap-3 pt-2">
          <Button
            variant="outline"
            className="gap-2 min-w-[120px]"
            onClick={() => onVote("a")}
            disabled={voted}
          >
            <Trophy size={16} />
            {t("arena.voteA")}
          </Button>
          <Button
            variant="outline"
            className="gap-2 min-w-[120px]"
            onClick={() => onVote("tie")}
            disabled={voted}
          >
            <Equal size={16} />
            {t("arena.tie")}
          </Button>
          <Button
            variant="outline"
            className="gap-2 min-w-[120px]"
            onClick={() => onVote("b")}
            disabled={voted}
          >
            <Trophy size={16} />
            {t("arena.voteB")}
          </Button>
        </div>
      )}
    </div>
  );
}

function ResponseCard({
  label,
  response,
  revealed,
}: {
  label: string;
  response: string;
  revealed?: string;
}) {
  return (
    <Card className="flex flex-col h-full overflow-hidden">
      <CardHeader className="py-3 px-4 border-b shrink-0">
        <CardTitle className="text-sm font-semibold">{label}</CardTitle>
        {revealed && (
          <p className="text-xs text-muted-foreground mt-1">{revealed}</p>
        )}
      </CardHeader>
      <CardContent className="flex-1 overflow-y-auto p-4 min-h-0">
        <ResultRenderer content={response} />
      </CardContent>
    </Card>
  );
}
