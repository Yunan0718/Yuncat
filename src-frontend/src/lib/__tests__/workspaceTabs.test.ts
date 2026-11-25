import { describe, expect, it } from "vitest";

import { toWorkspacePath, toWorkspaceRelative } from "../workspaceTabs";

/**
 * 2026-09-15 回归锚定：文件树路径约定。
 *
 * 曾经的实现按 "/shared/" 切分，造成两个线上可见的故障：
 *  - `shared` 根节点（路径以 /shared 结尾、无尾斜杠）会退回**整条绝对路径** →
 *    请求变成 dir=shared/C:/… → 后端 500 → 目录永远显示「空」；
 *  - 文件会丢掉 `shared/` 前缀 → 后端按工作区根解析 → 404 → 文件树里打不开任何文件。
 * 这里把「工作区相对路径」的取值钉死。
 */
const ROOT = "C:\\Users\\me\\YuncatWorkspace";

describe("toWorkspaceRelative", () => {
  it("shared 根节点 → shared（不是空串，也不是绝对路径）", () => {
    expect(toWorkspaceRelative(`${ROOT}\\shared`, ROOT)).toBe("shared");
  });

  it("shared 下的一级文件 → shared/文件", () => {
    expect(toWorkspaceRelative(`${ROOT}\\shared\\hello.py`, ROOT)).toBe("shared/hello.py");
  });

  it("shared 下的嵌套文件保留完整工作区相对路径", () => {
    expect(toWorkspaceRelative(`${ROOT}\\shared\\pkg\\a.py`, ROOT)).toBe("shared/pkg/a.py");
  });

  it("agents 目录同样按工作区相对处理（此前会漏掉 agents/ 前缀）", () => {
    expect(toWorkspaceRelative(`${ROOT}\\agents\\sess-1\\x.py`, ROOT)).toBe("agents/sess-1/x.py");
  });

  it("工作区根自身 → 空串", () => {
    expect(toWorkspaceRelative(ROOT, ROOT)).toBe("");
  });

  it("传入正斜杠路径也能处理（后端返回的 path 混用分隔符）", () => {
    expect(toWorkspaceRelative(`${ROOT}/shared/hello.py`, ROOT)).toBe("shared/hello.py");
  });

  it("拿不到工作区根时按 /shared 标记推断，仍返回工作区相对形态", () => {
    expect(toWorkspaceRelative("C:/x/YuncatWorkspace/shared/a.py", "")).toBe("shared/a.py");
    expect(toWorkspaceRelative("C:/x/YuncatWorkspace/shared", "")).toBe("shared");
  });
});

describe("toWorkspacePath", () => {
  it("绝对路径原样透传（后端用 validate_path 校验，且接受绝对路径）", () => {
    expect(toWorkspacePath(`${ROOT}\\shared\\hello.py`)).toBe("C:/Users/me/YuncatWorkspace/shared/hello.py");
  });
});
