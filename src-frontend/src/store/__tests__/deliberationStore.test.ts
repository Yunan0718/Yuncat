import { describe, it, expect, beforeEach } from "vitest";
import { useDeliberationStore } from "../deliberationStore";

describe("deliberationStore", () => {
  beforeEach(() => {
    useDeliberationStore.setState({
      roles: [],
      phase: "idle",
      consensus: "",
      active: false,
      taskId: null,
    });
  });

  it("initRoles: sets roles from input and enters proposals phase", () => {
    const roles = [
      { id: "r1", name: "架构师" },
      { id: "r2", name: "安全专家" },
    ];
    useDeliberationStore.getState().initRoles(roles);
    const s = useDeliberationStore.getState();
    expect(s.roles).toHaveLength(2);
    expect(s.roles[0].name).toBe("架构师");
    expect(s.roles[0].roleId).toBe("r1");
    expect(s.roles[0].output).toBe("");
    expect(s.roles[0].done).toBe(false);
    expect(s.phase).toBe("proposals");
    expect(s.active).toBe(true);
    expect(s.consensus).toBe("");
  });

  it("appendChunk: appends data to matching role by name", () => {
    useDeliberationStore.getState().initRoles([{ id: "r1", name: "架构师" }]);
    useDeliberationStore.getState().appendChunk("架构师", "proposals", "我建议");
    useDeliberationStore.getState().appendChunk("架构师", "proposals", "使用微服务");

    const s = useDeliberationStore.getState();
    expect(s.roles[0].output).toBe("我建议使用微服务");
    expect(s.phase).toBe("proposals");
  });

  it("appendChunk: does nothing when role name doesn't match", () => {
    useDeliberationStore.getState().initRoles([{ id: "r1", name: "架构师" }]);
    useDeliberationStore.getState().appendChunk("不存在的角色", "proposals", "data");

    const s = useDeliberationStore.getState();
    expect(s.roles[0].output).toBe("");
  });

  it("setPhase: updates phase correctly", () => {
    useDeliberationStore.getState().setPhase("reviews");
    expect(useDeliberationStore.getState().phase).toBe("reviews");

    useDeliberationStore.getState().setPhase("consensus");
    expect(useDeliberationStore.getState().phase).toBe("consensus");
  });

  it("setConsensus: sets consensus text and marks inactive", () => {
    useDeliberationStore.getState().initRoles([{ id: "r1", name: "架构师" }]);
    useDeliberationStore.getState().setConsensus("最终方案：使用 gRPC");

    const s = useDeliberationStore.getState();
    expect(s.consensus).toBe("最终方案：使用 gRPC");
    expect(s.active).toBe(false);
  });

  it("reset: clears all state", () => {
    useDeliberationStore.getState().initRoles([{ id: "r1", name: "架构师" }]);
    useDeliberationStore.getState().appendChunk("架构师", "proposals", "data");
    useDeliberationStore.getState().reset();

    const s = useDeliberationStore.getState();
    expect(s.roles).toHaveLength(0);
    expect(s.phase).toBe("idle");
    expect(s.consensus).toBe("");
    expect(s.active).toBe(false);
    expect(s.taskId).toBeNull();
  });
});
