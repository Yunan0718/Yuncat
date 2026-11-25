/** components/SkillMarket.tsx — Phase 51-C/D: Skill 市场增强（Git/市场安装 + MCP 工具注册入口）

已安装 Skill 列表 + 外部市场搜索 + 安全审查 + 冲突检测 + 5星评分 + 创作向导
*/
import { useState, useEffect, useCallback } from "react";
import { useApi } from "../hooks/useApi";
import { useTranslation } from "react-i18next";
import { Button } from "./ui/button";
import { Input } from "./ui/input";
import { Badge } from "./ui/badge";

interface SkillInfo {
  id: string;
  version: string;
  title: string;
  description: string;
  author: string;
  tags: string[];
  category: string;
  icon: string;
  enabled: boolean;
  is_system: boolean;
  prompts_count: number;
  workflows_count: number;
  commands_count: number;
  roles_count: number;
  permissions?: Record<string, boolean>;
}

interface SecurityWarning {
  type: string;
  detail: string;
  severity: string;
}
interface Conflict {
  type: string;
  module: string;
  detail: string;
  new_skill?: string;
  existing_skill?: string;
}

interface CommunitySkill {
  id?: string;
  name?: string;
  title?: string;
  description?: string;
  url?: string;
  source?: string;
  category?: string;
  avg_rating?: number;
  stars?: number;
}

interface RatingInfo {
  avg_rating: number;
  total_ratings: number;
}

// 筛选用哨兵值——不要用会随语言变化的字面量（显示时再走 t("skillMarket.all")）
const ALL = "__all__";
const CATEGORIES = [ALL, "core", "workflow", "role", "command", "review", "engineering", "security", "custom"];
const RATING_STARS = [1, 2, 3, 4, 5];

function StarRating({
  value,
  onRate,
  size = "text-[10px]",
}: {
  value: number;
  onRate?: (n: number) => void;
  size?: string;
}) {
  return (
    <span className={`inline-flex items-center ${onRate ? "cursor-pointer" : ""}`}>
      {RATING_STARS.map((star) => (
        <span
          key={star}
          className={`${size} ${star <= Math.round(value) ? "text-warning" : "text-muted-foreground"}`}
          onClick={() => onRate?.(star)}
        >
          ★
        </span>
      ))}
    </span>
  );
}

export function SkillMarket() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [installed, setInstalled] = useState<SkillInfo[]>([]);
  const [category, setCategory] = useState(ALL);
  const [installSource, setInstallSource] = useState("");
  const [statusMsg, setStatusMsg] = useState("");
  const [conflicts, setConflicts] = useState<Conflict[]>([]);
  const [securityWarnings, setSecurityWarnings] = useState<SecurityWarning[]>([]);
  const [showCreateWizard, setShowCreateWizard] = useState(false);
  const [ratingSkillId, setRatingSkillId] = useState("");
  const [activeTab, setActiveTab] = useState<"installed" | "community">("installed");

  const [gitUrl, setGitUrl] = useState("");
  const [gitBranch, setGitBranch] = useState("");
  const [installingGit, setInstallingGit] = useState(false);

  const [communityQuery, setCommunityQuery] = useState("");
  const [communityResults, setCommunityResults] = useState<CommunitySkill[]>([]);
  const [communitySearching, setCommunitySearching] = useState(false);
  const [communityCategory, setCommunityCategory] = useState(ALL);
  const [communitySource, setCommunitySource] = useState(ALL);
  const [minRating, setMinRating] = useState(0);
  const [resultRatings, setResultRatings] = useState<Record<string, RatingInfo>>({});

  const [wizardStep, setWizardStep] = useState(1);
  const [wizardTitle, setWizardTitle] = useState("");
  const [wizardDesc, setWizardDesc] = useState("");
  const [wizardCategory, setWizardCategory] = useState("custom");
  const [wizardPrompt, setWizardPrompt] = useState("");
  const [wizardSlashCmd, setWizardSlashCmd] = useState("");
  const [wizardCmdDesc, setWizardCmdDesc] = useState("");
  const [wizardIcon, setWizardIcon] = useState("Puzzle");

  const loadInstalled = useCallback(() => {
    api<SkillInfo[]>("/api/v1/skills")
      .then(setInstalled)
      .catch((e) => console.warn("SkillMarket: installed skills load failed:", e));
  }, [api]);
  useEffect(() => {
    loadInstalled();
  }, [loadInstalled]);

  const setMsg = (key: string) => setStatusMsg(t(`legacy.skill_${key}` as string));

  const doInstall = async (force?: boolean) => {
    if (!installSource) return;
    setMsg("0410");
    setConflicts([]);
    setSecurityWarnings([]);

    try {
      const d = await api<{ warnings?: SecurityWarning[]; safe?: boolean }>("/api/v1/skills/check-security", {
        method: "POST",
        body: JSON.stringify({ source_dir: installSource }),
      });
      if (d.warnings?.length) setSecurityWarnings(d.warnings);
      if (!d.safe) {
        setMsg("97be");
        return;
      }
    } catch {
      setMsg("7f0c");
      return;
    }

    // 2.13 修复: force=true 时跳过冲突检测（用户已确认要覆盖）
    if (!force) {
      try {
        const d = await api<{ conflicts?: Conflict[] }>("/api/v1/skills/check-conflicts", {
          method: "POST",
          body: JSON.stringify({ source_dir: installSource }),
        });
        if (d.conflicts?.length) {
          setConflicts(d.conflicts);
          setStatusMsg(t("skillMarket.conflictsDetected", { count: d.conflicts.length }));
          return;
        }
      } catch {
        setMsg("ecc3");
        return;
      }
    }

    setMsg("463c");
    const d = await api<{ ok?: boolean; skill?: { title?: string }; error?: string }>("/api/v1/skills/install", {
      method: "POST",
      body: JSON.stringify({ source_dir: installSource }),
    });
    if (d.ok) {
      setStatusMsg(t("skillMarket.installedWithName", { name: d.skill?.title || installSource }));
      setInstallSource("");
      loadInstalled();
    } else setStatusMsg(t("skillMarket.installFailedWithName", { error: d.error }));
  };

  const doInstallGit = async () => {
    if (!gitUrl) return;
    setInstallingGit(true);
    setStatusMsg(t("skillMarket.installing"));
    const d = await api<{ ok?: boolean; skill?: { title?: string }; error?: string }>("/api/v1/skills/install-git", {
      method: "POST",
      body: JSON.stringify({ url: gitUrl, branch: gitBranch || undefined }),
    });
    setInstallingGit(false);
    if (d.ok) {
      setStatusMsg(`${t("skillMarket.installed")}: ${d.skill?.title || gitUrl}`);
      setGitUrl("");
      setGitBranch("");
      loadInstalled();
    } else {
      setStatusMsg(`${t("skillMarket.error")}: ${d.error}`);
    }
  };

  const doInstallMarketplace = async (skillId: string, source = "agentskills.io") => {
    setStatusMsg(t("skillMarket.installing"));
    const d = await api<{ ok?: boolean; skill?: { title?: string }; error?: string }>(
      "/api/v1/skills/install-marketplace",
      {
        method: "POST",
        body: JSON.stringify({ skill_id: skillId, source }),
      },
    );
    if (d.ok) {
      setStatusMsg(`${t("skillMarket.installed")}: ${d.skill?.title || skillId}`);
      loadInstalled();
    } else setStatusMsg(`${t("skillMarket.error")}: ${d.error}`);
  };

  const doToggle = async (id: string) => {
    await api(`/api/v1/skills/${id}/toggle`, { method: "PUT" });
    loadInstalled();
  };
  const doUninstall = async (id: string) => {
    await api(`/api/v1/skills/${id}`, { method: "DELETE" });
    loadInstalled();
  };
  const doRate = async (id: string, rating: number) => {
    await api(`/api/v1/skills/${id}/rate`, {
      method: "POST",
      body: JSON.stringify({ rating }),
    });
    setRatingSkillId("");
  };

  const doCreate = async () => {
    setMsg("b261");
    const body: Record<string, unknown> = {
      title: wizardTitle,
      description: wizardDesc,
      category: wizardCategory,
      icon: wizardIcon,
      prompt_text: wizardPrompt,
    };
    if (wizardSlashCmd) {
      body.slash_command = wizardSlashCmd;
      body.command_description = wizardCmdDesc;
    }
    const d = await api<{ ok?: boolean; skill?: { title?: string }; error?: string }>("/api/v1/skills/create", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (d.ok) {
      setStatusMsg(t("skillMarket.created", { name: d.skill?.title }));
      setShowCreateWizard(false);
      setWizardStep(1);
      loadInstalled();
    } else setStatusMsg(t("skillMarket.createFailed", { error: d.error }));
  };

  const searchCommunity = useCallback(() => {
    if (!communityQuery.trim()) return;
    setCommunitySearching(true);
    api<{ results: CommunitySkill[] }>(`/api/v1/skills/search/external?q=${encodeURIComponent(communityQuery)}`)
      .then((r) => {
        const mapped = (r.results || []).map((item) => ({
          ...item,
          id: item.id || item.url || `${item.name || item.title}`,
          name: item.name || item.title || "Unnamed",
          source: item.source || "community",
          category: item.category || "general",
          avg_rating: item.avg_rating || item.stars || 0,
        }));
        setCommunityResults(mapped);
      })
      .catch(() => setCommunityResults([]))
      .finally(() => setCommunitySearching(false));
  }, [api, communityQuery]);

  useEffect(() => {
    const ids = communityResults.map((r) => r.id).filter(Boolean) as string[];
    if (!ids.length) return;
    ids.forEach((id) => {
      api<RatingInfo>(`/api/v1/skills/${id}/rating`)
        .then((r) => setResultRatings((prev) => ({ ...prev, [id!]: r })))
        .catch((e) => console.warn(`SkillMarket: rating load failed for ${id}:`, e));
    });
  }, [communityResults, api]);

  const sources = [ALL, ...Array.from(new Set(communityResults.map((r) => r.source).filter(Boolean)))];

  const filteredResults = communityResults.filter((r) => {
    const text = `${r.name || ""} ${r.description || ""}`.toLowerCase();
    const matchesQuery = !communityQuery || text.includes(communityQuery.toLowerCase());
    const matchesCategory = communityCategory === ALL || r.category === communityCategory;
    const matchesSource = communitySource === ALL || r.source === communitySource;
    const rating = resultRatings[r.id || ""]?.avg_rating || r.avg_rating || 0;
    const matchesRating = rating >= minRating;
    return matchesQuery && matchesCategory && matchesSource && matchesRating;
  });

  const filtered = installed.filter((s) => category === ALL || s.category === category);

  return (
    <div className="h-full overflow-y-auto p-6 max-w-4xl mx-auto space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">{t("skillMarket.title")}</h1>
        <button
          onClick={() => setShowCreateWizard(!showCreateWizard)}
          className="px-4 py-1.5 bg-primary text-white rounded text-sm"
        >
          {showCreateWizard ? t("common.cancel") : `+ ${t("skillMarket.create")}`}
        </button>
      </div>

      {showCreateWizard && (
        <div className="card p-4 space-y-3">
          <h2 className="text-sm font-semibold">{t("skillMarket.createWizardTitle", { step: wizardStep, total: 5 })}</h2>
          {wizardStep === 1 && (
            <>
              <input
                value={wizardTitle}
                onChange={(e) => setWizardTitle(e.target.value)}
                placeholder={t("skillMarket.skillName")}
                className="w-full px-3 py-1.5 border rounded text-sm"
              />
              <textarea
                value={wizardDesc}
                onChange={(e) => setWizardDesc(e.target.value)}
                placeholder={t("common.description")}
                rows={2}
                className="w-full px-3 py-1.5 border rounded text-sm"
              />
              <select
                value={wizardCategory}
                onChange={(e) => setWizardCategory(e.target.value)}
                className="w-full px-3 py-1.5 border rounded text-sm"
              >
                {CATEGORIES.filter((c) => c !== ALL).map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </>
          )}
          {wizardStep === 2 && (
            <textarea
              value={wizardPrompt}
              onChange={(e) => setWizardPrompt(e.target.value)}
              placeholder={t("skillMarket.wizardPromptPlaceholder")}
              rows={6}
              className="w-full px-3 py-1.5 border rounded text-sm font-mono"
            />
          )}
          {wizardStep === 3 && (
            <textarea
              value={wizardPrompt}
              onChange={(e) => setWizardPrompt(e.target.value)}
              placeholder={t("skillMarket.wizardDagPlaceholder")}
              rows={8}
              className="w-full px-3 py-1.5 border rounded text-sm font-mono"
            />
          )}
          {wizardStep === 4 && (
            <>
              <input
                value={wizardSlashCmd}
                onChange={(e) => setWizardSlashCmd(e.target.value)}
                placeholder={t("skillMarket.wizardSlashPlaceholder")}
                className="w-full px-3 py-1.5 border rounded text-sm"
              />
              <input
                value={wizardCmdDesc}
                onChange={(e) => setWizardCmdDesc(e.target.value)}
                placeholder={t("legacy.skill_bf91")}
                className="w-full px-3 py-1.5 border rounded text-sm"
              />
            </>
          )}
          {wizardStep === 5 && (
            <input
              value={wizardIcon}
              onChange={(e) => setWizardIcon(e.target.value)}
              placeholder={t("legacy.skill_76cb")}
              className="w-full px-3 py-1.5 border rounded text-sm"
            />
          )}
          <div className="flex gap-2">
            {wizardStep > 1 && (
              <button onClick={() => setWizardStep((s) => s - 1)} className="px-3 py-1.5 border rounded text-sm">
                {t("skillMarket.back")}
              </button>
            )}
            {wizardStep < 5 && (
              <button
                onClick={() => setWizardStep((s) => s + 1)}
                className="px-3 py-1.5 bg-primary text-primary-foreground rounded text-sm"
              >
                {t("skillMarket.next")}
              </button>
            )}
            {wizardStep === 5 && (
              <button onClick={doCreate} className="px-4 py-1.5 bg-success-solid text-success-foreground rounded text-sm">
                {t("skillMarket.create")}
              </button>
            )}
          </div>
        </div>
      )}

      <div className="flex gap-1 bg-secondary rounded-lg p-0.5 w-fit">
        {(["installed", "community"] as const).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              activeTab === tab
                ? "bg-background shadow-sm text-foreground"
                : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {tab === "installed" ? t("skillMarket.installed") : t("skillMarket.community")}
          </button>
        ))}
      </div>

      {activeTab === "community" && (
        <div className="card p-4 space-y-3">
          <h2 className="text-sm font-semibold">{t("skillMarket.community")}</h2>
          <div className="flex gap-2">
            <input
              value={communityQuery}
              onChange={(e) => setCommunityQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") searchCommunity();
              }}
              placeholder={t("skillMarket.searchPlaceholder")}
              className="flex-1 px-3 py-1.5 border rounded text-sm"
            />
            <button
              disabled={communitySearching || !communityQuery.trim()}
              onClick={searchCommunity}
              className="px-4 py-1.5 bg-primary text-primary-foreground rounded text-sm disabled:opacity-40"
            >
              {communitySearching ? t("legacy.skill_9bc4") : t("legacy.skill_e5f7")}
            </button>
          </div>

          <div className="flex flex-wrap gap-2">
            <select
              value={communityCategory}
              onChange={(e) => setCommunityCategory(e.target.value)}
              className="px-2 py-1 border rounded text-xs"
            >
              {CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c === ALL ? t("skillMarket.all") : c}
                </option>
              ))}
            </select>
            <select
              value={communitySource}
              onChange={(e) => setCommunitySource(e.target.value)}
              className="px-2 py-1 border rounded text-xs"
            >
              {sources.map((s) => (
                <option key={s} value={s}>
                  {s === ALL ? t("skillMarket.all") : s}
                </option>
              ))}
            </select>
            <select
              value={minRating}
              onChange={(e) => setMinRating(Number(e.target.value))}
              className="px-2 py-1 border rounded text-xs"
            >
              <option value={0}>{t("skillMarket.minRating")}: 0</option>
              {[1, 2, 3, 4, 5].map((n) => (
                <option key={n} value={n}>
                  {n}+
                </option>
              ))}
            </select>
          </div>

          {statusMsg && <div className="text-xs text-muted-foreground">{statusMsg}</div>}

          {filteredResults.length > 0 ? (
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {filteredResults.map((r) => {
                const rating = resultRatings[r.id || ""]?.avg_rating || r.avg_rating || 0;
                const total = resultRatings[r.id || ""]?.total_ratings || 0;
                return (
                  <div key={r.id || r.url} className="flex items-center justify-between border rounded-lg p-3">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium truncate">{r.name}</span>
                        {r.category && (
                          <Badge variant="secondary" className="text-[9px]">
                            {r.category}
                          </Badge>
                        )}
                        {r.source && (
                          <Badge variant="outline" className="text-[9px]">
                            {r.source}
                          </Badge>
                        )}
                      </div>
                      <div className="text-[10px] text-muted-foreground line-clamp-1">{r.description}</div>
                      <div className="flex items-center gap-2 mt-1">
                        <StarRating value={rating} size="text-[9px]" />
                        {total > 0 && <span className="text-[9px] text-muted-foreground">({total})</span>}
                      </div>
                    </div>
                    <button
                      onClick={() => {
                        if (r.source === "agentskills.io" && r.id) doInstallMarketplace(r.id, r.source);
                        else if (r.url) {
                          setGitUrl(r.url);
                          doInstallGit();
                        }
                      }}
                      className="ml-3 px-3 py-1 text-[10px] bg-primary text-white rounded shrink-0"
                    >
                      {t("skillMarket.install")}
                    </button>
                  </div>
                );
              })}
            </div>
          ) : communityResults.length > 0 ? (
            <div className="text-xs text-muted-foreground">{t("skillMarket.noResults")}</div>
          ) : null}
        </div>
      )}

      {activeTab === "installed" && (
        <>
          <div className="card p-4 space-y-3">
            <h2 className="text-sm font-semibold">{t("skillMarket.installFromGit")}</h2>
            <div className="flex gap-2">
              <Input
                value={gitUrl}
                onChange={(e) => setGitUrl(e.target.value)}
                placeholder={t("skillMarket.gitUrl")}
                className="flex-1 text-sm"
              />
              <Input
                value={gitBranch}
                onChange={(e) => setGitBranch(e.target.value)}
                placeholder={t("skillMarket.gitBranch")}
                className="w-32 text-sm"
              />
              <Button onClick={doInstallGit} disabled={installingGit || !gitUrl.trim()} size="sm">
                {installingGit ? t("skillMarket.installing") : t("skillMarket.install")}
              </Button>
            </div>
          </div>

          <div className="card p-4">
            <h2 className="text-sm font-semibold mb-2">{t("skillMarket.localDir")}</h2>
            <div className="flex gap-2">
              <input
                value={installSource}
                onChange={(e) => setInstallSource(e.target.value)}
                placeholder={t("skillMarket.localDirPlaceholder")}
                className="flex-1 px-3 py-1.5 border rounded text-sm"
              />
              <button
                onClick={() => doInstall()}
                disabled={!installSource}
                className="px-4 py-1.5 bg-primary text-primary-foreground rounded text-sm disabled:opacity-40"
              >
                {t("skillMarket.install")}
              </button>
            </div>
            {statusMsg && <div className="mt-2 text-xs text-muted-foreground">{statusMsg}</div>}

            {securityWarnings.length > 0 && (
              <div className="mt-3 p-3 rounded bg-warning/10 border border-warning/20">
                <div className="text-xs font-semibold text-warning mb-1">{t("skillMarket.securityReview")}</div>
                {securityWarnings.map((w) => (
                  <div key={w.detail} className="text-[10px] text-warning/90">
                    {w.severity === "high" ? "RED" : "YLW"} {w.detail}
                  </div>
                ))}
              </div>
            )}

            {conflicts.length > 0 && (
              <div className="mt-3 p-3 rounded bg-destructive/10 border border-destructive/20">
                <div className="text-xs font-semibold text-destructive mb-1">{t("skillMarket.conflictCheck")}</div>
                {conflicts.map((c) => (
                  <div key={c.module + ":" + c.detail} className="text-[10px] text-destructive/90">
                    [{c.module}] {c.detail}
                  </div>
                ))}
                <button
                  onClick={() => {
                    setConflicts([]);
                    doInstall(true);
                  }}
                  className="mt-2 text-[10px] text-primary hover:underline"
                >
                  {t("skillMarket.forceInstall")}
                </button>
              </div>
            )}
          </div>

          <section>
            <h2 className="text-sm font-semibold mb-2">
              {t("skillMarket.installed")} ({filtered.length}/{installed.length})
            </h2>
            <div className="flex gap-1.5 mb-3 flex-wrap">
              {CATEGORIES.map((c) => (
                <button
                  key={c}
                  onClick={() => setCategory(c)}
                  className={`px-2 py-0.5 rounded text-[10px] border ${category === c ? "bg-primary text-primary-foreground border-primary" : "border-border text-muted-foreground hover:bg-secondary"}`}
                >
                  {c === ALL ? t("skillMarket.all") : c}
                </button>
              ))}
            </div>

            <div className="grid grid-cols-2 gap-3">
              {filtered.map((s) => (
                <div key={s.id} className="card p-3" style={{ opacity: s.enabled ? 1 : 0.5 }}>
                  <div className="flex items-start gap-2">
                    <span className="text-lg">{s.icon || "box"}</span>
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium truncate">{s.title}</span>
                        {s.is_system && (
                          <span className="text-[8px] px-1 rounded bg-muted text-muted-foreground">
                            {t("settings.system")}
                          </span>
                        )}
                        {!s.enabled && (
                          <span className="text-[8px] px-1 rounded bg-warning/10 text-warning">
                            {t("legacy.skill_710a")}
                          </span>
                        )}
                      </div>
                      <div className="text-[10px] text-muted-foreground mt-0.5 line-clamp-2">{s.description}</div>
                      <div className="flex items-center gap-2 mt-2">
                        <span className="text-[9px] text-muted-foreground">v{s.version}</span>
                        {s.prompts_count > 0 && (
                          <span className="text-[9px] text-muted-foreground">P:{s.prompts_count}</span>
                        )}
                        {s.workflows_count > 0 && (
                          <span className="text-[9px] text-muted-foreground">W:{s.workflows_count}</span>
                        )}
                        {s.commands_count > 0 && (
                          <span className="text-[9px] text-muted-foreground">/{s.commands_count}</span>
                        )}
                        <div className="flex-1" />
                        {ratingSkillId === s.id ? (
                          RATING_STARS.map((star) => (
                            <button
                              key={star}
                              onClick={() => doRate(s.id, star)}
                              className="text-[10px] hover:text-warning"
                            >
                              ★
                            </button>
                          ))
                        ) : (
                          <button
                            onClick={() => setRatingSkillId(s.id)}
                            className="text-[10px] text-muted-foreground hover:text-warning"
                          >
                            {t("skillMarket.rating")}
                          </button>
                        )}
                        {s.permissions?.allow_command_exec && <span className="text-[8px] text-warning">cmd</span>}
                      </div>
                      <div className="flex items-center gap-2 mt-1.5">
                        {!s.is_system && (
                          <button
                            onClick={() => doUninstall(s.id)}
                            className="text-[9px] text-destructive hover:underline"
                          >
                            {t("common.delete")}
                          </button>
                        )}
                        <button onClick={() => doToggle(s.id)} className="text-[9px] text-primary hover:underline">
                          {s.enabled ? t("common.disable") : t("common.enable")}
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </>
      )}
    </div>
  );
}
