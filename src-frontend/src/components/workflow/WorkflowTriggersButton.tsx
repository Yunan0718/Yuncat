import { useState } from "react";
import { useTranslation } from "react-i18next";
import { WorkflowTriggersDialog } from "./WorkflowTriggersDialog";

/** 工具栏里的「触发器」按钮——按钮 + 弹窗 + 开关状态都收在这里，
 *  WorkflowEditor 只放一行，避免把超大页面继续撑大（尺寸棘轮会拦）。 */
export function WorkflowTriggersButton({ workflowId }: { workflowId: string | null }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="px-3 py-1 border rounded text-xs font-semibold hover:bg-secondary"
        disabled={!workflowId}
        title={t("workflowTriggers.title")}
      >
        {t("workflowTriggers.title")}
      </button>
      {workflowId && (
        <WorkflowTriggersDialog
          workflowId={workflowId}
          open={open}
          onClose={() => setOpen(false)}
        />
      )}
    </>
  );
}
