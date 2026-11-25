import { useState } from "react";
import { useTranslation } from "react-i18next";
import { ModelConfigPanel } from "./ModelConfigPanel";
import { ModelTieringPanel } from "./ModelTieringPanel";
import { ConfigFileEditor } from "./ConfigFileEditor";
import { OCGatewaySection } from "./OCGatewaySection";

/* ── Models ──────────────────────────────────────── */
export function ModelsSection() {
  const { t } = useTranslation();
  const [sub, setSub] = useState<"providers" | "tiering" | "files" | "gateway">("providers");
  const subLabels: Record<typeof sub, string> = {
    providers: t("settings.providersTab"),
    tiering: t("settings.tieringTab"),
    files: t("settings.filesTab"),
    gateway: t("settings.gatewayTab"),
  };
  return (
    <div className="space-y-4">
      <div className="flex gap-1 bg-secondary rounded-lg p-0.5 w-fit">
        {(["providers", "tiering", "files", "gateway"] as const).map((s) => (
          <button
            key={s}
            onClick={() => setSub(s)}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              sub === s ? "bg-background shadow-sm text-foreground" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            {subLabels[s]}
          </button>
        ))}
      </div>
      {sub === "providers" ? (
        <ModelConfigPanel />
      ) : sub === "tiering" ? (
        <ModelTieringPanel />
      ) : sub === "files" ? (
        <ConfigFileEditor />
      ) : (
        <OCGatewaySection />
      )}
    </div>
  );
}
