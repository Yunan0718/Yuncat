"""tests/test_harness_phase_d.py —— Supervisor–Worker Harness Phase D 验收

验收标准（docs/phase-d-scheduler-isolation.md §6）：
  - D-01~D-05  scheduler 纯函数（write_paths 冲突 / 祖先-后代 / can_run / DAG 推导）
  - D-06~D-07  acquire_task 领取门（冲突保持 QUEUED / 无冲突 RUNNING）
  - D-08~D-10  isolation 浅拷贝（副本范围 / COMPLETED 合并 / 非 COMPLETED 丢弃）
  - D-11~D-13  HITL（human_resolve retry / cancel / 状态机回归）
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src-python"))

from harness import scheduler as sch
from harness import task_manager as tm
from harness.schemas import ExecutionBudget, HarnessPermissions, TaskPackage
from repositories import init_db
from repositories.harness_schema import ensure_harness_schema


# ============================================================
# fixtures
# ============================================================

@pytest.fixture
async def db(tmp_path):
    conn = await init_db(str(tmp_path / "harness_d.db"))
    await ensure_harness_schema(conn)
    yield conn
    await conn.close()


def _pkg(idem: str, *, write=None, read=None, status: str = "PENDING", task_id: str | None = None,
         max_attempts: int = 3) -> TaskPackage:
    return TaskPackage(
        id=task_id or f"ht_{idem}",
        idempotency_key=idem,
        mission="phase d",
        permissions=HarnessPermissions(write_paths=write or [], read_paths=read or []),
        status=status,
        max_attempts=max_attempts,
    )


async def _mk_task(db, idem: str, *, write=None, read=None, status: str = "PENDING") -> dict:
    """落库 + 按 status 走合法迁移链（PENDING→QUEUED→RUNNING→WAITING_PERMISSION）。"""
    task = await tm.create_task(db, _pkg(idem, write=write, read=read))
    if status == "PENDING":
        return task
    task = await tm.transition(db, task["id"], tm.TaskStatus.QUEUED, "queue")
    if status == "QUEUED":
        return task
    task = await tm.transition(db, task["id"], tm.TaskStatus.RUNNING, "start")
    if status == "RUNNING":
        return task
    return await tm.transition(db, task["id"], tm.TaskStatus.WAITING_PERMISSION, "l1")


# ============================================================
# D-01 ~ D-05：scheduler 纯函数
# ============================================================

def test_d01_write_paths_equal_conflict(tmp_path):
    """D-01: 两任务 write_paths 相同文件 → 冲突（write_conflict 对称）。"""
    a = _pkg("a", write=["src/a.py"])
    b = _pkg("b", write=["src/a.py"])
    a_p = sch.normalize_paths(["src/a.py"], tmp_path)
    b_p = sch.normalize_paths(["src/a.py"], tmp_path)
    assert sch.write_conflict(a_p[0], b_p[0]) is True
    assert sch.write_conflict(b_p[0], a_p[0]) is True  # 对称性
    # 对称性（祖先-后代、子→父顺序）：child→parent 与 parent→child 结论一致
    parent_p = sch.normalize_paths(["src"], tmp_path)
    child_p = sch.normalize_paths(["src/util.py"], tmp_path)
    assert sch.write_conflict(child_p[0], parent_p[0]) is True  # 依赖 b in a.parents 分支
    assert sch.write_conflict(parent_p[0], child_p[0]) is True  # 依赖 a in b.parents 分支
    # can_run 层面：a 在 RUNNING → b 不可运行
    a.status = tm.TaskStatus.RUNNING
    ok, conflicts = sch.can_run(b, [a], tmp_path)
    assert ok is False
    assert conflicts[0]["kind"] == sch.KIND_WRITE_WRITE
    assert conflicts[0]["other_task_id"] == a.id


def test_d02_ancestor_descendant_conflict(tmp_path):
    """D-02: 祖先/后代目录冲突——写 dir 与写 dir/file 冲突；写 dir1 与写 dir2 不冲突。"""
    a = _pkg("a", write=["src"])
    b = _pkg("b", write=["src/util.py"])
    a_p = sch.normalize_paths(["src"], tmp_path)
    b_p = sch.normalize_paths(["src/util.py"], tmp_path)
    assert sch.write_conflict(a_p[0], b_p[0]) is True  # src 是 src/util.py 的祖先
    # 互不相干目录不冲突
    c = _pkg("c", write=["docs"])
    c_p = sch.normalize_paths(["docs"], tmp_path)
    assert sch.write_conflict(a_p[0], c_p[0]) is False


def test_d03_can_run_reports_running_occupation(tmp_path):
    """D-03: can_run 对 RUNNING 占用返回 (False, conflicts) 且明细含 other_task_id/命中路径。"""
    a = _pkg("a", write=["src/a.py"], status=tm.TaskStatus.RUNNING)
    b = _pkg("b", write=["src/a.py"])
    ok, conflicts = sch.can_run(b, [a], tmp_path)
    assert ok is False
    assert len(conflicts) == 1
    assert conflicts[0]["other_task_id"] == a.id
    assert conflicts[0]["paths"] == ["src/a.py"]
    # WAITING_PERMISSION 同样占用（暂停审批仍持有写入权）
    a.status = tm.TaskStatus.WAITING_PERMISSION
    ok, conflicts = sch.can_run(b, [a], tmp_path)
    assert ok is False


def test_d04_terminal_releases_occupation(tmp_path):
    """D-04: 终态任务不占用——COMPLETED/REJECTED 后 can_run 放行。"""
    a = _pkg("a", write=["src/a.py"], status=tm.TaskStatus.COMPLETED)
    b = _pkg("b", write=["src/a.py"])
    ok, conflicts = sch.can_run(b, [a], tmp_path)
    assert ok is True and conflicts == []
    a.status = tm.TaskStatus.REJECTED
    ok, _ = sch.can_run(b, [a], tmp_path)
    assert ok is True


def test_d05_dependency_graph_read_write(tmp_path):
    """D-05: read∩write 推导 DAG——B 读 A 写 → B 依赖 A；方向正确、无自依赖。"""
    a = _pkg("a", write=["data/out.json"])
    b = _pkg("b", read=["data/out.json"], write=["report.md"])
    c = _pkg("c", write=["other.md"])
    dep = sch.build_dependency_graph([a, b, c], tmp_path)
    assert dep[b.id] == {a.id}      # B 依赖 A（A 写、B 读）
    assert dep[a.id] == set()       # A 无依赖
    assert dep[c.id] == set()       # C 与 A/B 无读写交集
    # 读-读不产生依赖（两个任务都只读同一文件，无写方）
    d = _pkg("d", read=["data/out.json"])
    e = _pkg("e", read=["data/out.json"])
    dep2 = sch.build_dependency_graph([d, e], tmp_path)
    assert dep2[d.id] == set() and dep2[e.id] == set()


# ============================================================
# D-06 ~ D-07：acquire_task 领取门
# ============================================================

async def test_d06_acquire_conflict_keeps_queued(db, tmp_path):
    """D-06: 冲突领取 → 保持 QUEUED、不落审计、返回 conflicts。"""
    await _mk_task(db, "d6a", write=["src/a.py"], status="RUNNING")
    b = await _mk_task(db, "d6b", write=["src/a.py"], status="QUEUED")

    res = await sch.acquire_task(db, b["id"], workspace=tmp_path)
    assert res["acquired"] is False
    assert len(res["conflicts"]) == 1
    assert res["conflicts"][0]["kind"] == sch.KIND_WRITE_WRITE
    # 状态保持 QUEUED，且审计中无 RUNNING 迁移
    updated = await tm.get_task(db, b["id"])
    assert updated["status"] == tm.TaskStatus.QUEUED
    transitions = await tm.get_state_transitions(db, b["id"])
    assert transitions[-1]["to_status"] == tm.TaskStatus.QUEUED


async def test_d07_acquire_no_conflict_runs(db, tmp_path):
    """D-07: 无冲突领取 → RUNNING + attempt_id，审计含 QUEUED→RUNNING。"""
    b = await _mk_task(db, "d7", write=["src/b.py"], status="QUEUED")
    res = await sch.acquire_task(db, b["id"], workspace=tmp_path)
    assert res["acquired"] is True
    assert res["task"]["status"] == tm.TaskStatus.RUNNING
    assert res["attempt_id"].startswith("att_")
    transitions = await tm.get_state_transitions(db, b["id"])
    assert transitions[-1]["to_status"] == tm.TaskStatus.RUNNING


async def test_d07b_acquire_rejects_non_queued(db, tmp_path):
    """D-07b: 非 QUEUED 状态领取被拒绝（fail-closed）；不存在任务同理。"""
    t = await _mk_task(db, "d7b", status="PENDING")
    res = await sch.acquire_task(db, t["id"], workspace=tmp_path)
    assert res["acquired"] is False and "仅 QUEUED" in res["error"]
    res2 = await sch.acquire_task(db, "ht_nope", workspace=tmp_path)
    assert res2["acquired"] is False and "不存在" in res2["error"]


# ============================================================
# D-08 ~ D-10：isolation 浅拷贝
# ============================================================

def _seed_workspace(ws: Path) -> None:
    (ws / "src").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)
    (ws / "secret").mkdir(parents=True)
    (ws / "src" / "a.py").write_text("v1", encoding="utf-8")
    (ws / "src" / "b.py").write_text("keep", encoding="utf-8")
    (ws / "docs" / "readme.md").write_text("read", encoding="utf-8")
    (ws / "secret" / "key.txt").write_text("top-secret", encoding="utf-8")


def test_d08_copy_only_declared_paths(tmp_path):
    """D-08: 副本仅含 write_paths ∪ read_paths（相对结构保留）；未声明路径/内部目录不入副本。"""
    from harness.isolation import WorkspaceIsolation

    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_workspace(ws)
    iso = WorkspaceIsolation(ws)
    pkg = _pkg("d8", write=["src/a.py"], read=["docs"])
    copy_root = iso.create(pkg, "att_d8")

    assert (copy_root / "src" / "a.py").read_text(encoding="utf-8") == "v1"  # write 复制
    assert (copy_root / "docs" / "readme.md").read_text(encoding="utf-8") == "read"  # read 复制
    assert not (copy_root / "src" / "b.py").exists()      # 未声明文件不入副本
    assert not (copy_root / "secret").exists()            # 未声明目录不入副本
    assert not (copy_root / ".harness").exists()          # 副本内不出现内部目录


def test_d09_merge_back_syncs_write_scope(tmp_path):
    """D-09: COMPLETED 后 merge_back 同步 write 范围内新增/修改/删除。"""
    from harness.isolation import WorkspaceIsolation

    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_workspace(ws)
    (ws / "src" / "delete.py").write_text("todelete", encoding="utf-8")
    iso = WorkspaceIsolation(ws)
    pkg = _pkg("d9", write=["src"])
    copy_root = iso.create(pkg, "att_d9")

    # Worker 在副本：改 a.py、新增 new.py、删除 delete.py
    (copy_root / "src" / "a.py").write_text("v2-changed", encoding="utf-8")
    (copy_root / "src" / "new.py").write_text("new-file", encoding="utf-8")
    (copy_root / "src" / "delete.py").unlink()

    res = iso.merge_back(pkg, "att_d9")
    assert (ws / "src" / "a.py").read_text(encoding="utf-8") == "v2-changed"  # 修改回流
    assert (ws / "src" / "new.py").read_text(encoding="utf-8") == "new-file"  # 新增回流
    assert not (ws / "src" / "delete.py").exists()                            # 删除同步
    assert "src/a.py" in res["merged"] and "src/new.py" in res["merged"]
    assert "src/delete.py" in res["deleted"]
    assert res["conflicts"] == []
    # 未声明范围零接触
    assert (ws / "docs" / "readme.md").read_text(encoding="utf-8") == "read"


def test_d09b_merge_back_external_conflict(tmp_path):
    """D-09b: Worker 执行期间主工作区被外部修改 → merge_back 拒绝覆盖（不静默）。"""
    from harness.isolation import WorkspaceIsolation

    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_workspace(ws)
    iso = WorkspaceIsolation(ws)
    pkg = _pkg("d9b", write=["src/a.py"])
    copy_root = iso.create(pkg, "att_d9b")

    (ws / "src" / "a.py").write_text("external-edit", encoding="utf-8")  # 外部修改
    (copy_root / "src" / "a.py").write_text("worker-edit", encoding="utf-8")

    res = iso.merge_back(pkg, "att_d9b")
    assert "src/a.py" in res["conflicts"] and "src/a.py" not in res["merged"]
    assert (ws / "src" / "a.py").read_text(encoding="utf-8") == "external-edit"  # 外部版本保留


def test_d10_cleanup_discards_and_read_not_merged(tmp_path):
    """D-10: 非 COMPLETED cleanup 丢弃副本、主工作区零接触；read 范围只读不回流。"""
    from harness.isolation import WorkspaceIsolation

    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_workspace(ws)
    iso = WorkspaceIsolation(ws)
    pkg = _pkg("d10", write=["src/a.py"], read=["docs"])
    copy_root = iso.create(pkg, "att_d10")

    (copy_root / "src" / "a.py").write_text("worker-touched", encoding="utf-8")
    (copy_root / "docs" / "readme.md").write_text("read-touched", encoding="utf-8")

    # cleanup：副本删除，主工作区零接触
    assert iso.cleanup(pkg.id, "att_d10") is True
    assert not copy_root.exists()
    assert (ws / "src" / "a.py").read_text(encoding="utf-8") == "v1"
    assert (ws / "docs" / "readme.md").read_text(encoding="utf-8") == "read"
    # 幂等：重复 cleanup no-op
    assert iso.cleanup(pkg.id, "att_d10") is False

    # read 范围只读不回流：副本内改 read 文件 → merge_back 不合并
    copy_root2 = iso.create(pkg, "att_d10b")
    (copy_root2 / "docs" / "readme.md").write_text("read-touched", encoding="utf-8")
    res = iso.merge_back(pkg, "att_d10b")
    assert "docs/readme.md" not in res["merged"]
    assert (ws / "docs" / "readme.md").read_text(encoding="utf-8") == "read"


# ============================================================
# D-11 ~ D-13：HITL 人工裁决
# ============================================================

@pytest.fixture
def app_ctx(db):
    app = SimpleNamespace(state=SimpleNamespace(db=db))
    from context import set_app

    set_app(app)
    yield app
    set_app(None)


async def _to_blocked(db, idem: str) -> dict:
    """PENDING → QUEUED → RUNNING → TIMEOUT → (increment_attempt 熔断) BLOCKED。"""
    task = await tm.create_task(db, _pkg(idem, max_attempts=1))
    await tm.transition(db, task["id"], tm.TaskStatus.QUEUED, "q")
    await tm.transition(db, task["id"], tm.TaskStatus.RUNNING, "r")
    await tm.transition(db, task["id"], tm.TaskStatus.TIMEOUT, "t")
    return await tm.increment_attempt(db, task["id"])  # attempt 0→1 >= max_attempts=1 → BLOCKED


async def test_d11_human_resolve_retry(app_ctx, db):
    """D-11: human_resolve(retry) BLOCKED → QUEUED 且 attempt 归零（审计含 reason）。"""
    from harness.supervisor_tools import human_resolve

    task = await _to_blocked(db, "d11")
    assert task["status"] == tm.TaskStatus.BLOCKED
    assert task["attempt"] == 1

    res = await human_resolve(task["id"], "retry", "人工复核后允许重试")
    assert res["success"] is True
    assert res["data"]["status"] == tm.TaskStatus.QUEUED
    assert res["data"]["attempt"] == 0  # 熔断计数归零
    updated = await tm.get_task(db, task["id"])
    assert updated["status"] == tm.TaskStatus.QUEUED and updated["attempt"] == 0
    transitions = await tm.get_state_transitions(db, task["id"])
    assert transitions[-1]["to_status"] == tm.TaskStatus.QUEUED
    assert "人工裁决重试" in transitions[-1]["reason"]


async def test_d12_human_resolve_cancel(app_ctx, db):
    """D-12: human_resolve(cancel) BLOCKED → CANCELLED。"""
    from harness.supervisor_tools import human_resolve

    task = await _to_blocked(db, "d12")
    res = await human_resolve(task["id"], "cancel", "人工决定不做")
    assert res["success"] is True
    assert res["data"]["status"] == tm.TaskStatus.CANCELLED
    assert (await tm.get_task(db, task["id"]))["status"] == tm.TaskStatus.CANCELLED


async def test_d12b_human_resolve_guards(app_ctx, db):
    """D-12b: 非 BLOCKED 状态 / 非法 action / 不存在任务 → 拒绝（fail-closed）。"""
    from harness.supervisor_tools import human_resolve

    t = await _mk_task(db, "d12b", status="QUEUED")
    res = await human_resolve(t["id"], "retry", "x")
    assert res["success"] is False and "仅 BLOCKED" in res["error"]
    assert (await tm.get_task(db, t["id"]))["status"] == tm.TaskStatus.QUEUED  # 状态未被改动
    res2 = await human_resolve(t["id"], "bogus", "x")
    assert res2["success"] is False and "retry|cancel" in res2["error"]
    res3 = await human_resolve("ht_nope", "retry", "x")
    assert res3["success"] is False and "不存在" in res3["error"]


def test_d13_blocked_state_transition_regression():
    """D-13: 状态机回归——BLOCKED 新边（QUEUED/CANCELLED）放行；非法迁移仍拒绝。"""
    tm.validate_transition(tm.TaskStatus.BLOCKED, tm.TaskStatus.QUEUED)
    tm.validate_transition(tm.TaskStatus.BLOCKED, tm.TaskStatus.CANCELLED)
    with pytest.raises(ValueError):
        tm.validate_transition(tm.TaskStatus.BLOCKED, tm.TaskStatus.RUNNING)
    with pytest.raises(ValueError):
        tm.validate_transition(tm.TaskStatus.BLOCKED, tm.TaskStatus.COMPLETED)
    # 既有边回归：BUDGET_EXCEEDED → BLOCKED 仍合法
    tm.validate_transition(tm.TaskStatus.BUDGET_EXCEEDED, tm.TaskStatus.BLOCKED)
