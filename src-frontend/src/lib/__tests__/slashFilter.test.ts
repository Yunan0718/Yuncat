import { describe, expect, it } from "vitest";

import { rankSlashCommands, scoreSlashCommand } from "../slashFilter";

const CMDS = [
  { slash_command: "/plan", name: "plan", description: "先出计划再动手" },
  { slash_command: "/compact", name: "compact", description: "压缩当前会话上下文" },
  { slash_command: "/review", name: "review", description: "审查本次改动" },
  { slash_command: "/clear", name: "clear", description: "清空当前会话" },
];

describe("slashFilter", () => {
  it("空查询保持原目录顺序", () => {
    expect(rankSlashCommands(CMDS, "")).toEqual(CMDS);
    expect(rankSlashCommands(CMDS, "/")).toEqual(CMDS);
  });

  it("子序列能找到命令（/cpt -> /compact）", () => {
    expect(rankSlashCommands(CMDS, "/cpt")[0].slash_command).toBe("/compact");
  });

  it("大小写不敏感", () => {
    expect(rankSlashCommands(CMDS, "/PLAN")[0].slash_command).toBe("/plan");
  });

  it("精确与前缀匹配排在模糊匹配前面", () => {
    const ranked = rankSlashCommands(CMDS, "c");
    // 同为前缀匹配时名字越短排得越前（clear 比 compact 短）
    expect(ranked[0].slash_command).toBe("/clear");
    expect(ranked[1].slash_command).toBe("/compact");
  });

  it("也能用描述里的关键词搜", () => {
    expect(rankSlashCommands(CMDS, "压缩")[0].slash_command).toBe("/compact");
  });

  it("不匹配返回空列表", () => {
    expect(rankSlashCommands(CMDS, "zzzz")).toEqual([]);
  });

  it("同分时保持原来的位置（结果稳定）", () => {
    const same = [
      { slash_command: "/aa1", name: "aa1" },
      { slash_command: "/aa2", name: "aa2" },
    ];
    expect(rankSlashCommands(same, "aa").map((c) => c.slash_command)).toEqual(["/aa1", "/aa2"]);
  });

  it("scoreSlashCommand 对不匹配返回 null", () => {
    expect(scoreSlashCommand("zzz", CMDS[0])).toBeNull();
    expect(scoreSlashCommand("plan", CMDS[0])).toBe(0);
  });
});
