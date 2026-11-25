import { useEffect, useState, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { useApi } from "../hooks/useApi";
import { Shield, RefreshCw, CheckCircle2, XCircle, Layers } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "./ui/card";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { LoadingCurve } from "./common/LoadingCurve";
import { EmptyState } from "./common/EmptyState";

interface ToolPolicyData {
  profile: string;
  allow: string[];
  deny: string[];
  byProvider: Record<string, { profile: string; allow: string[]; deny: string[] }>;
  groups: Record<string, string[]>;
}

export function ToolPolicyPanel() {
  const { t } = useTranslation();
  const { api } = useApi();
  const [policy, setPolicy] = useState<ToolPolicyData | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloading, setReloading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api<ToolPolicyData>("/api/v1/tools/policy");
      if (data) setPolicy(data);
    } catch {
      /* ignore */
    }
    setLoading(false);
  }, [api]);

  useEffect(() => {
    load();
  }, [api, load]);

  const handleReload = async () => {
    setReloading(true);
    try {
      await api("/api/v1/tools/policy/reload", { method: "POST" });
      await load();
    } catch {
      /* ignore */
    }
    setReloading(false);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 gap-3 text-muted-foreground">
        <LoadingCurve variant="lissajous" size={32} particleCount={8} />
        <span className="text-sm">{t("toolPolicy.loading")}</span>
      </div>
    );
  }

  if (!policy) {
    return (
      <EmptyState icon={<Shield size={24} />} title={t("toolPolicy.empty")} description={t("toolPolicy.emptyDesc")} />
    );
  }

  const groupNames = Object.keys(policy.groups || {});

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-semibold flex items-center gap-2">
            <Shield size={16} className="text-primary" />
            {t("toolPolicy.title")}
          </h3>
          <p className="text-xs text-muted-foreground">{t("toolPolicy.subtitle")}</p>
        </div>
        <Button variant="outline" size="sm" onClick={handleReload} disabled={reloading} className="gap-1 text-xs">
          {reloading ? <RefreshCw size={12} className="animate-spin" /> : <RefreshCw size={12} />}
          {t("toolPolicy.reload")}
        </Button>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm">{t("toolPolicy.defaultPolicy")}</CardTitle>
            <Badge variant="secondary" className="text-[10px]">
              {policy.profile}
            </Badge>
          </div>
          <CardDescription className="text-xs">{t("toolPolicy.defaultDesc")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-xs">
          <PolicyRuleList icon={CheckCircle2} label={t("toolPolicy.allowed")} rules={policy.allow} variant="allowed" />
          <PolicyRuleList icon={XCircle} label={t("toolPolicy.denied")} rules={policy.deny} variant="denied" />
        </CardContent>
      </Card>

      {Object.keys(policy.byProvider).length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">{t("toolPolicy.byProvider")}</CardTitle>
            <CardDescription className="text-xs">{t("toolPolicy.byProviderDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {Object.entries(policy.byProvider).map(([provider, p]) => (
              <div key={provider} className="border rounded-lg p-3 space-y-2">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium">{provider}</span>
                  <Badge variant="outline" className="text-[10px]">
                    {p.profile}
                  </Badge>
                </div>
                <PolicyRuleList icon={CheckCircle2} label={t("toolPolicy.allowed")} rules={p.allow} variant="allowed" />
                <PolicyRuleList icon={XCircle} label={t("toolPolicy.denied")} rules={p.deny} variant="denied" />
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {groupNames.length > 0 && (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm flex items-center gap-2">
              <Layers size={14} />
              {t("toolPolicy.groups")}
            </CardTitle>
            <CardDescription className="text-xs">{t("toolPolicy.groupsDesc")}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {groupNames.map((group) => (
              <div key={group} className="flex items-start gap-2 text-xs">
                <Badge variant="secondary" className="text-[10px] shrink-0 mt-0.5">
                  {group}
                </Badge>
                <span className="text-muted-foreground">{(policy.groups[group] || []).join(", ")}</span>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function PolicyRuleList({
  icon: Icon,
  label,
  rules,
  variant,
}: {
  icon: typeof CheckCircle2;
  label: string;
  rules: string[];
  variant: "allowed" | "denied";
}) {
  if (!rules || rules.length === 0) return null;
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1.5 text-muted-foreground">
        <Icon size={12} className={variant === "allowed" ? "text-success" : "text-destructive"} />
        <span className="font-medium">{label}</span>
      </div>
      <div className="flex flex-wrap gap-1 pl-5">
        {rules.map((rule) => (
          <Badge key={rule} variant={variant === "allowed" ? "default" : "destructive"} className="text-[10px] h-5">
            {rule}
          </Badge>
        ))}
      </div>
    </div>
  );
}
