import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../hooks/useApi";
import { useAppStore } from "../store/appStore";
import { Button } from "../components/ui/button";
import { Textarea } from "../components/ui/textarea";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { LoadingCurve } from "../components/common/LoadingCurve";
import { ErrorState } from "../components/common/ErrorState";
import { ArenaBattlePanel, RevealedInfo } from "../components/arena/ArenaBattlePanel";
import { Swords, BarChart3, RefreshCw, Trophy, AlertCircle } from "lucide-react";

interface ArenaModel {
  provider: string;
  model: string;
  display_name: string;
}

interface ArenaBattle {
  battle_id: string;
  status: string;
  response_a: string;
  response_b: string;
  created_at: number;
}

interface LeaderboardEntry {
  provider: string;
  model: string;
  battles: number;
  wins: number;
  win_rate: number;
}

type PageState = "idle" | "loading" | "voting" | "revealed";

export function ArenaPage() {
  const { t } = useTranslation();
  const { api } = useApi();
  const backendReady = useAppStore((s) => s.backendReady);

  const [prompt, setPrompt] = useState("");
  const [state, setState] = useState<PageState>("idle");
  const [battle, setBattle] = useState<ArenaBattle | null>(null);
  const [revealed, setRevealed] = useState<RevealedInfo | undefined>();
  const [voted, setVoted] = useState(false);
  const [error, setError] = useState("");
  const [models, setModels] = useState<ArenaModel[]>([]);
  const [leaderboard, setLeaderboard] = useState<LeaderboardEntry[]>([]);
  const [modelA, setModelA] = useState("");
  const [modelB, setModelB] = useState("");

  // 用 useCallback 固定引用：它既在下面的 effect 里调用，也是「提交投票」后的
  // 刷新入口（第 141 行附近）。原先是普通函数且声明在 effect 之后，effect 只能靠
  // [backendReady, api] 间接命中它的真实依赖——规则校验不到，将来给它加状态时易漏。
  const loadLeaderboard = useCallback(async () => {
    try {
      const data = await api<{ entries: LeaderboardEntry[] }>("/api/v1/arena/leaderboard");
      setLeaderboard(data.entries || []);
    } catch {
      setLeaderboard([]);
    }
  }, [api]);

  useEffect(() => {
    if (!backendReady) return;
    api<ArenaModel[]>("/api/v1/arena/models")
      .then((data) => {
        if (Array.isArray(data)) {
          setModels(data);
        } else {
          // 后端返回 {models: [...]}
          const list = (data as { models?: ArenaModel[] }).models;
          if (list) setModels(list);
        }
      })
      .catch(() => setModels([]));

    loadLeaderboard();
  }, [backendReady, api, loadLeaderboard]);

  const startBattle = async () => {
    if (!prompt.trim()) return;
    setState("loading");
    setError("");
    setRevealed(undefined);
    setVoted(false);

    const body: Record<string, unknown> = { prompt: prompt.trim() };
    if (modelA && modelB) {
      const a = models.find((m) => `${m.provider}/${m.model}` === modelA);
      const b = models.find((m) => `${m.provider}/${m.model}` === modelB);
      if (a) body.model_a = a;
      if (b) body.model_b = b;
    }

    try {
      const data = await api<ArenaBattle>("/api/v1/arena/battle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setBattle(data);
      setState("voting");
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg || t("arena.battleFailed"));
      setState("idle");
    }
  };

  const handleVote = async (choice: "a" | "b" | "tie") => {
    if (!battle) return;
    if (choice === "tie") {
      setVoted(true);
      return;
    }
    try {
      await api(`/api/v1/arena/battle/${battle.battle_id}/vote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ choice }),
      });
      setVoted(true);
      await handleReveal();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    }
  };

  const handleReveal = async () => {
    if (!battle) return;
    try {
      const data = await api<{
        model_a: ArenaModel;
        model_b: ArenaModel;
      }>(`/api/v1/arena/battle/${battle.battle_id}/reveal`);
      setRevealed({
        modelA: `${data.model_a.display_name} (${data.model_a.provider})`,
        modelB: `${data.model_b.display_name} (${data.model_b.provider})`,
      });
      setState("revealed");
      await loadLeaderboard();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setError(msg);
    }
  };

  const reset = () => {
    setPrompt("");
    setState("idle");
    setBattle(null);
    setRevealed(undefined);
    setVoted(false);
    setError("");
  };

  if (!backendReady) {
    return (
      <div className="h-full flex items-center justify-center text-muted-foreground">
        {t("chat.startup")}
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="page-shell space-y-6">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold tracking-tight flex items-center gap-2">
              <Swords size={24} className="text-primary" />
              {t("arena.title")}
            </h1>
            <p className="text-sm text-muted-foreground mt-1">{t("arena.subtitle")}</p>
          </div>
          <Button variant="outline" size="sm" onClick={reset} className="gap-2">
            <RefreshCw size={16} />
            {t("arena.newBattle")}
          </Button>
        </div>

        {state === "idle" && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t("arena.promptPlaceholder")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <Textarea
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder={t("arena.promptPlaceholder")}
                rows={5}
              />

              {models.length >= 2 && (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <ModelSelect
                    label={`${t("arena.modelA")} (${t("arena.autoPick")})`}
                    value={modelA}
                    onChange={setModelA}
                    models={models}
                    exclude={modelB}
                  />
                  <ModelSelect
                    label={`${t("arena.modelB")} (${t("arena.autoPick")})`}
                    value={modelB}
                    onChange={setModelB}
                    models={models}
                    exclude={modelA}
                  />
                </div>
              )}

              {models.length < 2 && (
                <p className="text-sm text-destructive">{t("arena.needTwoModels")}</p>
              )}

              {error && (
                <ErrorState icon={<AlertCircle size={18} />} title={t("common.error")} message={error} compact />
              )}

              <Button onClick={startBattle} disabled={!prompt.trim() || models.length < 2}>
                {t("arena.startBattle")}
              </Button>
            </CardContent>
          </Card>
        )}

        {state === "loading" && (
          <div className="h-96 flex flex-col items-center justify-center gap-4 text-muted-foreground">
            <LoadingCurve variant="lissajous" size={48} particleCount={12} />
            <p>{t("chat.executing")}</p>
          </div>
        )}

        {(state === "voting" || state === "revealed") && battle && (
          <>
            <div className="h-[60vh]">
              <ArenaBattlePanel
                responseA={battle.response_a}
                responseB={battle.response_b}
                onVote={handleVote}
                revealed={revealed}
                voted={voted}
              />
            </div>
            {state === "voting" && voted && !revealed && (
              <div className="flex justify-center">
                <Button onClick={handleReveal} className="gap-2">
                  <Trophy size={16} />
                  {t("arena.reveal")}
                </Button>
              </div>
            )}
          </>
        )}

        {leaderboard.length > 0 && (
          <Card>
            <CardHeader>
              <CardTitle className="text-base flex items-center gap-2">
                <BarChart3 size={18} className="text-primary" />
                {t("arena.leaderboard")}
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b text-muted-foreground">
                      <th className="text-left py-2 font-medium">{t("arena.rank")}</th>
                      <th className="text-left py-2 font-medium">{t("arena.model")}</th>
                      <th className="text-left py-2 font-medium">{t("arena.provider")}</th>
                      <th className="text-right py-2 font-medium">{t("arena.battles")}</th>
                      <th className="text-right py-2 font-medium">{t("arena.wins")}</th>
                      <th className="text-right py-2 font-medium">{t("arena.winRate")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {leaderboard.map((entry, idx) => (
                      <tr key={`${entry.provider}-${entry.model}`} className="border-b last:border-0">
                        <td className="py-2">{idx + 1}</td>
                        <td className="py-2 font-medium">{entry.model}</td>
                        <td className="py-2 text-muted-foreground">{entry.provider}</td>
                        <td className="py-2 text-right">{entry.battles}</td>
                        <td className="py-2 text-right">{entry.wins}</td>
                        <td className="py-2 text-right">
                          {Math.round(entry.win_rate * 100)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

function ModelSelect({
  label,
  value,
  onChange,
  models,
  exclude,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  models: ArenaModel[];
  exclude: string;
}) {
  return (
    <div className="space-y-1">
      <label className="text-xs text-muted-foreground">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full h-9 rounded-md border border-input bg-background px-3 text-sm"
      >
        <option value="">Auto</option>
        {models
          .filter((m) => `${m.provider}/${m.model}` !== exclude)
          .map((m) => (
            <option key={`${m.provider}/${m.model}`} value={`${m.provider}/${m.model}`}>
              {m.display_name} ({m.provider})
            </option>
          ))}
      </select>
    </div>
  );
}
