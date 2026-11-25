import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ToolConfirmDialog, ToolConfirmPreview } from "../ToolConfirmDialog";

describe("ToolConfirmPreview", () => {
  it("渲染 write_file 工具的 diff 预览", () => {
    render(
      <ToolConfirmPreview
        toolName="write_file"
        preview={{
          path: "src/app.ts",
          unified_diff: "@@ -1 +1 @@\n-old\n+new",
          diff_stats: { summary: "1→1 行" },
        }}
        lang="zh"
      />,
    );
    expect(screen.getByText("1→1 行")).toBeInTheDocument();
    expect(screen.getByText(/-old/)).toBeInTheDocument();
    expect(screen.getByText(/\+new/)).toBeInTheDocument();
  });

  it("渲染 delete_file 工具的删除预览", () => {
    render(
      <ToolConfirmPreview
        toolName="delete_file"
        preview={{
          path: "src/legacy.ts",
          size: 1024,
          preview: "old content",
        }}
        lang="zh"
      />,
    );
    expect(screen.getByText(/src\/legacy\.ts/)).toBeInTheDocument();
    expect(screen.getByText(/1,024 bytes/)).toBeInTheDocument();
    expect(screen.getByText("old content")).toBeInTheDocument();
  });

  it("渲染 run_command 工具的命令预览", () => {
    render(
      <ToolConfirmPreview
        toolName="run_command"
        preview={{
          command: "npm run test",
          timeout_sec: 60,
        }}
        lang="zh"
      />,
    );
    expect(screen.getByText("npm run test")).toBeInTheDocument();
    // H1: 默认 LOW 风险——不显示警告横幅
    expect(screen.queryByText(/含中风险关键词/)).not.toBeInTheDocument();
  });

  // ─── H1: 危险命令确认 UI 测试 ───
  // 注意: vitest setup mock 了 useTranslation，t(key) 直接返回 key 字符串

  it("H1: LOW 风险命令不显示警告横幅但仍显示风险徽章", () => {
    render(
      <ToolConfirmPreview
        toolName="run_command"
        preview={{
          command: "ls -la",
          cwd: "/home/user",
          timeout_sec: 30,
          risk_level: "LOW",
          risk_reason: "未匹配任何风险模式",
        }}
        lang="zh"
      />,
    );
    expect(screen.getByText("ls -la")).toBeInTheDocument();
    // LOW 风险徽章应显示（mock t 返回 key）
    expect(screen.getByText("tool.riskLow")).toBeInTheDocument();
    // LOW 风险不显示警告横幅（risk_reason 文本不应出现）
    expect(screen.queryByText(/未匹配任何风险模式/)).not.toBeInTheDocument();
    // 工作目录与超时应展示
    expect(screen.getByText(/\/home\/user/)).toBeInTheDocument();
    expect(screen.getByText(/30tool\.seconds/)).toBeInTheDocument();
  });

  it("H1: MEDIUM 风险命令显示黄色警告横幅 + 风险原因", () => {
    render(
      <ToolConfirmPreview
        toolName="run_command"
        preview={{
          command: "sudo apt-get install foo",
          cwd: "/root",
          timeout_sec: 120,
          risk_level: "MEDIUM",
          risk_reason: "含中风险关键词: sudo",
        }}
        lang="zh"
      />,
    );
    expect(screen.getByText("sudo apt-get install foo")).toBeInTheDocument();
    // MEDIUM 风险徽章
    expect(screen.getByText("tool.riskMedium")).toBeInTheDocument();
    // 警告横幅显示风险原因
    expect(screen.getByText(/含中风险关键词: sudo/)).toBeInTheDocument();
    // 工作目录与超时
    expect(screen.getByText(/\/root/)).toBeInTheDocument();
    expect(screen.getByText(/120tool\.seconds/)).toBeInTheDocument();
    // 执行警告（横幅前缀 + t key）
    expect(screen.getByText(/tool\.execWarning/)).toBeInTheDocument();
  });

  it("H1: HIGH 风险命令防御性渲染红色 destructive 横幅", () => {
    render(
      <ToolConfirmPreview
        toolName="run_command"
        preview={{
          command: "rm -rf /",
          cwd: "/",
          risk_level: "HIGH",
          risk_reason: "匹配高风险模式: rm -rf /",
        }}
        lang="zh"
      />,
    );
    expect(screen.getByText("rm -rf /")).toBeInTheDocument();
    // HIGH 风险徽章
    expect(screen.getByText("tool.riskHigh")).toBeInTheDocument();
    // 警告横幅显示风险原因
    expect(screen.getByText(/匹配高风险模式: rm -rf \//)).toBeInTheDocument();
  });

  it("H1: 缺失 risk_level 字段时默认按 LOW 处理", () => {
    render(
      <ToolConfirmPreview
        toolName="run_command"
        preview={{ command: "echo hello" }}
        lang="zh"
      />,
    );
    // 默认 LOW 风险徽章
    expect(screen.getByText("tool.riskLow")).toBeInTheDocument();
    // 不显示警告横幅（无 ⚠ 标记的横幅）
    expect(screen.queryByText(/⚠/)).not.toBeInTheDocument();
  });

  it("H1: 缺失 risk_reason 时 MEDIUM 风险不显示横幅（但徽章仍显示）", () => {
    render(
      <ToolConfirmPreview
        toolName="run_command"
        preview={{
          command: "sudo ls",
          risk_level: "MEDIUM",
          // risk_reason 故意缺失
        }}
        lang="zh"
      />,
    );
    expect(screen.getByText("sudo ls")).toBeInTheDocument();
    // MEDIUM 风险徽章应显示
    expect(screen.getByText("tool.riskMedium")).toBeInTheDocument();
    // risk_reason 缺失——横幅不显示（无 ⚠ 标记）
    expect(screen.queryByText(/⚠/)).not.toBeInTheDocument();
  });

  it("H1: 缺失 cwd/timeout_sec 时不渲染元信息行", () => {
    const { container } = render(
      <ToolConfirmPreview
        toolName="run_command"
        preview={{ command: "echo hi", risk_level: "LOW" }}
        lang="zh"
      />,
    );
    expect(screen.getByText("echo hi")).toBeInTheDocument();
    // 不应出现 "工作目录:" 或 "超时:" 标签（mock t 返回 key）
    expect(screen.queryByText(/tool\.workingDir/)).not.toBeInTheDocument();
    expect(screen.queryByText(/tool\.timeout/)).not.toBeInTheDocument();
    // 但执行警告仍显示
    expect(screen.getByText(/tool\.execWarning/)).toBeInTheDocument();
    expect(container.firstChild).not.toBeNull();
  });

  it("未知工具类型返回 null", () => {
    const { container } = render(<ToolConfirmPreview toolName="unknown_tool" preview={{}} lang="zh" />);
    expect(container.firstChild).toBeNull();
  });

  // ─── H2: 工作区安全边界条测试 ───
  // 注意: vitest setup mock 了 useTranslation，t(key) 直接返回 key 字符串

  it("H2: write_file 含 workspace_root 时渲染安全边界条", () => {
    render(
      <ToolConfirmPreview
        toolName="write_file"
        preview={{
          path: "shared/app.ts",
          workspace_root: "/home/user/YuncatWorkspace",
          unified_diff: "@@ -1 +1 @@\n-old\n+new",
          diff_stats: { summary: "1→1 行" },
        }}
        lang="zh"
      />,
    );
    // 边界条标签
    expect(screen.getByText(/tool\.workspaceRoot/)).toBeInTheDocument();
    expect(screen.getByText(/tool\.relativePath/)).toBeInTheDocument();
    // 工作区根路径值
    expect(screen.getByText("/home/user/YuncatWorkspace")).toBeInTheDocument();
    // 相对路径值（路径可能在边界条和 InlineDiff 中多处渲染，用 getAllByText）
    expect(screen.getAllByText("shared/app.ts").length).toBeGreaterThan(0);
  });

  it("H2: write_file 缺少 workspace_root 时不渲染安全边界条", () => {
    render(
      <ToolConfirmPreview
        toolName="write_file"
        preview={{
          path: "shared/app.ts",
          unified_diff: "@@ -1 +1 @@\n-old\n+new",
        }}
        lang="zh"
      />,
    );
    // 不应出现边界条标签
    expect(screen.queryByText(/tool\.workspaceRoot/)).not.toBeInTheDocument();
    expect(screen.queryByText(/tool\.relativePath/)).not.toBeInTheDocument();
    // 但 diff 预览仍正常渲染
    expect(screen.getByText(/-old/)).toBeInTheDocument();
  });

  it("H2: edit_file 含 workspace_root 时渲染安全边界条", () => {
    render(
      <ToolConfirmPreview
        toolName="edit_file"
        preview={{
          path: "shared/edit.ts",
          workspace_root: "C:\\Users\\test\\YuncatWorkspace",
          unified_diff: "@@ -1 +1 @@\n-foo\n+bar",
          matches: 1,
          total_edits: 1,
        }}
        lang="zh"
      />,
    );
    expect(screen.getByText(/tool\.workspaceRoot/)).toBeInTheDocument();
    expect(screen.getByText("C:\\Users\\test\\YuncatWorkspace")).toBeInTheDocument();
    // 相对路径值（路径可能在边界条和 InlineDiff 中多处渲染，用 getAllByText）
    expect(screen.getAllByText("shared/edit.ts").length).toBeGreaterThan(0);
  });

  it("H2: delete_file 含 workspace_root 时渲染安全边界条", () => {
    render(
      <ToolConfirmPreview
        toolName="delete_file"
        preview={{
          path: "shared/old.ts",
          workspace_root: "/tmp/ws",
          size: 512,
          preview: "content here",
        }}
        lang="zh"
      />,
    );
    expect(screen.getByText(/tool\.workspaceRoot/)).toBeInTheDocument();
    expect(screen.getByText("/tmp/ws")).toBeInTheDocument();
    // 删除预览仍正常
    expect(screen.getByText(/512/)).toBeInTheDocument();
  });

  it("H2: delete_file 缺少 workspace_root 时不渲染边界条但保留删除预览", () => {
    render(
      <ToolConfirmPreview
        toolName="delete_file"
        preview={{
          path: "shared/old.ts",
          size: 256,
        }}
        lang="zh"
      />,
    );
    expect(screen.queryByText(/tool\.workspaceRoot/)).not.toBeInTheDocument();
    // 删除预览仍正常
    expect(screen.getByText(/shared\/old\.ts/)).toBeInTheDocument();
  });

describe("ToolConfirmDialog 提交状态", () => {
  const props = {
    open: true,
    toolName: "run_command",
    preview: { command: "echo hi" },
  };

  it("初始焦点落在安全的拒绝按钮", () => {
    render(<ToolConfirmDialog {...props} onConfirm={vi.fn()} />);
    expect(screen.getByText("tool.reject")).toHaveFocus();
  });

  it("审批请求失败时保留弹窗、展示错误并解除提交锁", async () => {
    const onConfirm = vi.fn().mockRejectedValue(new Error("409 conflict"));
    render(<ToolConfirmDialog {...props} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByText("tool.confirmExec"));
    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("toolConfirm.failed");
    });
    expect(screen.getByText("tool.confirmExec")).not.toBeDisabled();
    expect(screen.getByText("tool.reject")).toBeInTheDocument();
  });
});});
