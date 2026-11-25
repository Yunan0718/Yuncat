"""tests/test_harness_phase_b.py —— Supervisor–Worker Harness Phase B 验收

验收标准（docs/harness-supervisor-worker.md Phase B / path-canonicalize-design.md）：
  1. policy_engine：L0/L1 两级裁决（数据驱动）——声明路径+使命提及 → L0 放行，
     未命中 → L1 请示，越出 workspace / 工具不在白名单 → DENY（违规事件）
  2. verify_approval：Supervisor 批准后的最终硬闸——越界路径硬拒
  3. verifier：git diff + untracked + sha256 hash + 测试退出码客观取证；
     非 git 工作区 fail-closed
  4. 交叉验证：篡改 claimed_files_modified → MISMATCH（信任边界）
  5. 接线：apply_worker_report 无注入 observation 时走 verifier 取证；
     approve_permission 对越界审批硬拒
"""
import hashlib
import pathlib
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src-python"))

from context import set_app
from harness import task_manager as tm
from harness.policy_engine import evaluate, path_canonicalize, verify_approval
from harness.schemas import (
    ExecutionBudget,
    HarnessPermissions,
    PermissionRequest,
    RuntimeObservation,
    TaskPackage,
    WorkerReport,
)
from harness.supervisor_tools import approve_permission
from harness.verifier import (
    MISMATCH,
    VERIFIED,
    collect_observation,
    verify,
    verify_workspace,
)
from repositories import init_db
from repositories.harness_schema import ensure_harness_schema


def make_package(**kw) -> TaskPackage:
    base = dict(
        idempotency_key="k-b",
        mission="修复登录页按钮样式",
        acceptance_criteria=["按钮颜色正确"],
        permissions=HarnessPermissions(
            tools=["read_file", "write_file", "run_command"],
            write_paths=["src-frontend/src/components"],
        ),
        execution_budget=ExecutionBudget(max_tool_invocations=20),
    )
    base.update(kw)
    return TaskPackage(**base)


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "Button.tsx").write_text("// button", encoding="utf-8")
    (root / "docs").mkdir(parents=True)
    return root


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(str(tmp_path / "harness_b.db"))
    await ensure_harness_schema(conn)
    yield conn
    await conn.close()


@pytest.fixture
def app_ctx(db):
    app = SimpleNamespace(state=SimpleNamespace(db=db))
    set_app(app)
    yield app
    set_app(None)


# ═══════════════════════════════════════════════════════════
# 1. policy_engine L0/L1
# ═══════════════════════════════════════════════════════════

def test_l0_allow_declared_and_mentioned(ws):
    perms = HarnessPermissions(tools=["write_file"], write_paths=["src"])
    d = evaluate(
        perms, "write_file", {"path": "src/Button.tsx"}, ws,
        mission="修复 src/Button.tsx 按钮样式", acceptance_criteria=("按钮颜色正确",),
    )
    assert d.level == "L0_ALLOW"
    assert d.is_allow
    assert d.canonical_path == (ws / "src" / "Button.tsx").resolve()
    assert d.violation is False


def test_l1_declared_but_not_mentioned(ws):
    """声明路径内但使命未提及 → 仍需 Supervisor 确认（L0 需双条件）"""
    perms = HarnessPermissions(tools=["write_file"], write_paths=["src"])
    d = evaluate(perms, "write_file", {"path": "src/other.ts"}, ws, mission="随便改改")
    assert d.level == "L1_REQUEST"
    assert d.canonical_path is not None


def test_l1_undeclared_path_within_workspace(ws):
    perms = HarnessPermissions(tools=["write_file"], write_paths=["src"])
    d = evaluate(perms, "write_file", {"path": "docs/README.md"}, ws, mission="改文档")
    assert d.level == "L1_REQUEST"


def test_read_tool_l0_within_read_paths(ws):
    perms = HarnessPermissions(tools=["read_file"], read_paths=["src"])
    d = evaluate(perms, "read_file", {"path": "src/Button.tsx"}, ws, mission="读 src/Button.tsx")
    assert d.level == "L0_ALLOW"


def test_deny_tool_not_in_whitelist(ws):
    perms = HarnessPermissions(tools=["read_file"], write_paths=["src"])
    d = evaluate(perms, "run_command", {"command": "rm -rf /"}, ws)
    assert d.level == "DENY"
    assert d.violation is False


def test_non_path_tool_l1_request(ws):
    """run_command 在白名单内但无路径参数 → L1（命令级安全由 ToolService 兜底）"""
    perms = HarnessPermissions(tools=["run_command"])
    d = evaluate(perms, "run_command", {"command": "pytest -q"}, ws)
    assert d.level == "L1_REQUEST"


def test_deny_dotdot_escape_violation(ws):
    """验收：../ 绕过被拦截（violation=True，Supervisor 也救不回）"""
    perms = HarnessPermissions(tools=["write_file"], write_paths=["src"])
    d = evaluate(perms, "write_file", {"path": "../outside.txt"}, ws, mission="x")
    assert d.level == "DENY"
    assert d.violation is True


def test_deny_absolute_outside_violation(ws, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    perms = HarnessPermissions(tools=["write_file"], write_paths=["src"])
    d = evaluate(perms, "write_file", {"path": str(outside)}, ws, mission="x")
    assert d.level == "DENY"
    assert d.violation is True


def test_deny_symlink_escape_violation(ws, monkeypatch, tmp_path):
    """验收：符号链接绕过被拦截（mock resolve 模拟 symlink 指向 workspace 外）"""
    (ws / "evil").write_text("x")
    original_resolve = pathlib.Path.resolve

    def mock_resolve(self, *args, **kwargs):
        result = original_resolve(self, *args, **kwargs)
        if self.name == "evil":
            return tmp_path / "outside" / "pwn"
        return result

    monkeypatch.setattr(pathlib.Path, "resolve", mock_resolve)
    perms = HarnessPermissions(tools=["write_file"], write_paths=["src"])
    d = evaluate(perms, "write_file", {"path": "evil"}, ws, mission="x")
    assert d.level == "DENY"
    assert d.violation is True


def test_path_canonicalize_shared_with_sandbox(ws):
    """path_canonicalize 与 security.sandbox.canonicalize 同一实现（单一事实源）"""
    from security.sandbox import canonicalize
    assert path_canonicalize("src/Button.tsx", ws, ws) == canonicalize("src/Button.tsx", ws, ws)
    with pytest.raises(PermissionError):
        path_canonicalize("../outside", ws, ws)


# ═══════════════════════════════════════════════════════════
# 2. verify_approval 最终硬闸
# ═══════════════════════════════════════════════════════════

def test_verify_approval_none_passes():
    ok, _ = verify_approval(None, Path("C:/ws"))
    assert ok


def test_verify_approval_inside_ok(ws):
    ok, _ = verify_approval("src/Button.tsx", ws)
    assert ok


def test_verify_approval_outside_rejected(ws, tmp_path):
    outside = tmp_path / "out.txt"
    outside.write_text("x")
    ok, why = verify_approval(str(outside), ws)
    assert ok is False
    assert "越界" in why


def test_verify_approval_dotdot_rejected(ws):
    ok, why = verify_approval("../../etc/passwd", ws)
    assert ok is False


# ═══════════════════════════════════════════════════════════
# 3. verifier 取证
# ═══════════════════════════════════════════════════════════

class FakeGit:
    """可编程 git runner——单测不依赖真实 git。"""

    def __init__(self, *, worktree=True, diff=(), untracked=(), test_rc=0):
        self._worktree = worktree
        self._diff = list(diff)
        self._untracked = list(untracked)
        self._test_rc = test_rc
        self.calls: list[list[str]] = []

    async def __call__(self, cmd, cwd):
        self.calls.append(list(cmd))
        if cmd[0] == "rev-parse":
            return (0, "true\n", "") if self._worktree else (1, "", "not a git repo")
        if cmd[0] == "diff":
            return 0, ("\n".join(self._diff) + "\n") if self._diff else "", ""
        if cmd[0] == "ls-files":
            return 0, ("\n".join(self._untracked) + "\n") if self._untracked else "", ""
        if cmd[0] == "pytest":
            return self._test_rc, "", ""
        return 0, "", ""


async def test_collect_observation_merges_diff_and_untracked(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_text("hello", encoding="utf-8")
    (ws / "b.txt").write_text("world", encoding="utf-8")
    runner = FakeGit(diff=["a.txt"], untracked=["b.txt"])
    obs = await collect_observation(ws, runner=runner)
    assert obs.actual_files_modified == ["a.txt", "b.txt"]
    assert obs.file_hashes["a.txt"] == hashlib.sha256(b"hello").hexdigest()
    assert obs.test_exit_code is None
    assert obs.error_message is None
    assert runner.calls[0] == ["rev-parse", "--is-inside-work-tree"]


async def test_collect_observation_non_git_fail_closed(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    obs = await collect_observation(ws, runner=FakeGit(worktree=False))
    assert obs.actual_files_modified == []
    assert obs.error_message and "fail-closed" in obs.error_message


async def test_collect_observation_captures_test_exit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").write_text("x")
    obs = await collect_observation(ws, test_command=["pytest", "-q"], runner=FakeGit(diff=["a"], test_rc=2))
    assert obs.test_exit_code == 2


# ═══════════════════════════════════════════════════════════
# 4. verify 交叉验证（信任边界）
# ═══════════════════════════════════════════════════════════

def test_verify_verified_on_match():
    report = WorkerReport(task_id="t", attempt_id="a", claimed_files_modified=["x.ts"], test_exit_code=0)
    obs = RuntimeObservation(actual_files_modified=["x.ts"], test_exit_code=0)
    assert verify(report, obs) == VERIFIED


def test_verify_mismatch_on_tampered_claim():
    """验收：篡改 claimed_files_modified（多报/漏报）→ MISMATCH"""
    report = WorkerReport(task_id="t", attempt_id="a", claimed_files_modified=["x.ts", "sneaky.ts"])
    obs = RuntimeObservation(actual_files_modified=["x.ts"], test_exit_code=0)
    assert verify(report, obs) == MISMATCH


def test_verify_mismatch_on_missing_claim():
    report = WorkerReport(task_id="t", attempt_id="a", claimed_files_modified=[])
    obs = RuntimeObservation(actual_files_modified=["x.ts"], test_exit_code=0)
    assert verify(report, obs) == MISMATCH


def test_verify_mismatch_on_partial_claim():
    """验收：claimed 是 actual 的真子集（少报文件）且退出码一致 → MISMATCH

    变异锚点：verify 若用「claimed ⊆ actual」子集比较（而非集合相等），
    少报文件会被误判 VERIFIED——本用例锁死该信任边界。
    """
    report = WorkerReport(task_id="t", attempt_id="a", claimed_files_modified=["a.ts"], test_exit_code=0)
    obs = RuntimeObservation(actual_files_modified=["a.ts", "b.ts"], test_exit_code=0)
    assert verify(report, obs) == MISMATCH


def test_verify_mismatch_on_exit_code_diff():
    report = WorkerReport(task_id="t", attempt_id="a", claimed_files_modified=["x.ts"], test_exit_code=0)
    obs = RuntimeObservation(actual_files_modified=["x.ts"], test_exit_code=1)
    assert verify(report, obs) == MISMATCH


def test_verify_exit_skipped_when_observation_none():
    """未跑测试（exit None）→ 不校验退出码"""
    report = WorkerReport(task_id="t", attempt_id="a", claimed_files_modified=["x.ts"], test_exit_code=0)
    obs = RuntimeObservation(actual_files_modified=["x.ts"], test_exit_code=None)
    assert verify(report, obs) == VERIFIED


async def test_verify_workspace_convenience(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fixed.ts").write_text("x")
    report = WorkerReport(task_id="t", attempt_id="a", claimed_files_modified=["fixed.ts"], test_exit_code=0)
    decision, obs = await verify_workspace(ws, report, runner=FakeGit(diff=["fixed.ts"], test_rc=0))
    assert decision == VERIFIED
    assert obs.verifier_decision == "UNVERIFIED"  # 决策由上层写入


# ═══════════════════════════════════════════════════════════
# 5. 接线：task_manager 走 verifier + approve_permission 硬闸
# ═══════════════════════════════════════════════════════════

async def test_apply_worker_report_requires_observation_or_workspace(db):
    task = await tm.create_task(db, make_package(idempotency_key="k-need"))
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED)
    await tm.transition(db, task["id"], tm.TaskStatus.RUNNING)
    attempt_id = await tm.record_attempt(db, task["id"], 1)
    report = WorkerReport(task_id=task["id"], attempt_id=attempt_id, claimed_files_modified=["x.ts"])
    with pytest.raises(ValueError, match="取证来源"):
        await tm.apply_worker_report(db, report)


async def test_apply_worker_report_verifier_collects(db, tmp_path):
    """observation 缺失时经 verifier 取证（fake git）→ VERIFIED"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "fixed.ts").write_text("x", encoding="utf-8")
    task = await tm.create_task(db, make_package(idempotency_key="k-vb"))
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED)
    await tm.transition(db, task["id"], tm.TaskStatus.RUNNING)
    attempt_id = await tm.record_attempt(db, task["id"], 1)
    report = WorkerReport(
        task_id=task["id"], attempt_id=attempt_id,
        claimed_files_modified=["fixed.ts"], test_exit_code=0,
    )
    result = await tm.apply_worker_report(db, report, workspace=ws, runner=FakeGit(diff=["fixed.ts"], test_rc=0))
    assert result["verifier_decision"] == VERIFIED
    task = await tm.get_task(db, task["id"])
    assert task["status"] == tm.TaskStatus.VERIFIED


async def test_apply_worker_report_tampered_claim_mismatch(db, tmp_path):
    """接线验收：Worker 谎报改文件 → verifier 取证后 MISMATCH"""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "real.ts").write_text("x", encoding="utf-8")
    task = await tm.create_task(db, make_package(idempotency_key="k-tamper"))
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED)
    await tm.transition(db, task["id"], tm.TaskStatus.RUNNING)
    attempt_id = await tm.record_attempt(db, task["id"], 1)
    report = WorkerReport(
        task_id=task["id"], attempt_id=attempt_id,
        claimed_files_modified=["fake.ts"],  # 声称改了 fake.ts，实际只动了 real.ts
    )
    result = await tm.apply_worker_report(db, report, workspace=ws, runner=FakeGit(diff=["real.ts"]))
    assert result["verifier_decision"] == MISMATCH


async def test_approve_permission_hard_deny_out_of_bounds(app_ctx, tmp_path, monkeypatch):
    """接线验收：approve 越界路径 → 硬性拒绝（违规事件），任务留在 WAITING_PERMISSION"""
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "ws"))
    db = app_ctx.state.db
    task = await tm.create_task(db, make_package(idempotency_key="k-deny"))
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED)
    await tm.transition(db, task["id"], tm.TaskStatus.RUNNING)
    attempt_id = await tm.record_attempt(db, task["id"], 1)
    req = PermissionRequest(
        task_id=task["id"], attempt_id=attempt_id, tool_name="write_file",
        arguments={"path": "src/evil.ts"},
        path_canonical=str(tmp_path / "outside" / "pwn.txt"),  # 越出 workspace
        level=3,
    )
    recorded = await tm.record_permission_request(db, req)
    await tm.transition(db, task["id"], tm.TaskStatus.WAITING_PERMISSION, "越权请求")

    result = await approve_permission(permission_request_id=recorded["id"], allow=True, reason="业务需要")
    assert result["success"] is False
    assert "硬性拒绝" in result["error"]
    task = await tm.get_task(db, task["id"])
    assert task["status"] == tm.TaskStatus.WAITING_PERMISSION  # 未批准，未回 RUNNING


# ═══════════════════════════════════════════════════════════
# 6. 真实 git 集成（git 可用时）
# ═══════════════════════════════════════════════════════════

@pytest.mark.skipif(shutil.which("git") is None, reason="git 不可用")
async def test_verifier_real_git_repo(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()

    def run(*args):
        return subprocess.run(["git", "-C", str(ws), *args], capture_output=True, text=True)

    run("init", "-q")
    run("config", "user.email", "t@test.local")
    run("config", "user.name", "tester")
    (ws / "keep.txt").write_text("v1", encoding="utf-8")
    run("add", ".")
    run("commit", "-qm", "init")

    (ws / "keep.txt").write_text("v2", encoding="utf-8")  # 修改
    (ws / "new.txt").write_text("n", encoding="utf-8")    # 未跟踪

    obs = await collect_observation(ws)
    assert "keep.txt" in obs.actual_files_modified
    assert "new.txt" in obs.actual_files_modified
    assert obs.file_hashes["keep.txt"] == hashlib.sha256(b"v2").hexdigest()
    assert obs.error_message is None
