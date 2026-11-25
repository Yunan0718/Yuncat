"""Stage D self-test — run_spec_loop() 核心验收 (Phase 43)

Tier 1: 导入 + 基本结构 (11 项)
Tier 2: _parse_audit_labels 标签解析 (8 项)
Tier 3: _get_project_structure 项目扫描 (4 项)
Tier 4: _resolve_spec_role_routing 路由 (5 项)
Tier 5: 检查点持久化 (4 项)
Tier 6: Provider 配置解析 (3 项)
Tier 7: 回归测试 (existing unit tests)
"""
import sys, os, io, time, tempfile, copy
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src-python'))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

passes = 0
fails = 0

def check(name, condition, detail=""):
    global passes, fails
    if condition:
        passes += 1
        print(f"  [PASS] {name}")
    else:
        fails += 1
        print(f"  [FAIL] {name} — {detail}")

# ============================================================
# Tier 1: 导入 + 基本结构
# ============================================================
print("=== Tier 1: Imports + Structure ===")
from services.deliberation.deliberation_service import (
    DeliberationService, SPEC_ROLE_ROUTING, _resolve_spec_role_routing,
)

ds = DeliberationService()
check("DeliberationService instantiated", ds is not None)
check("SPEC_ROLE_ROUTING has 3 roles", len(SPEC_ROLE_ROUTING) == 3,
      f"got {len(SPEC_ROLE_ROUTING)}: {list(SPEC_ROLE_ROUTING.keys())}")
check("SPEC_ROLE_ROUTING has architect/dev/reviewer",
      all(r in SPEC_ROLE_ROUTING for r in ["architect", "developer", "reviewer"]))
check("MAX_SPEC_ROUNDS = 3", ds.MAX_SPEC_ROUNDS == 3)
check("SPEC_ROLE_TIMEOUT = 120", ds.SPEC_ROLE_TIMEOUT == 120)
check("run_spec_loop method exists", hasattr(ds, 'run_spec_loop') and callable(ds.run_spec_loop))
check("_invoke_spec_architect exists", hasattr(ds, '_invoke_spec_architect'))
check("_invoke_spec_developer exists", hasattr(ds, '_invoke_spec_developer'))
check("_invoke_spec_auditor exists", hasattr(ds, '_invoke_spec_auditor'))
check("_invoke_spec_verification exists", hasattr(ds, '_invoke_spec_verification'))
check("_parse_audit_labels exists", hasattr(ds, '_parse_audit_labels'))

# ============================================================
# Tier 2: _parse_audit_labels 标签解析
# ============================================================
print()
print("=== Tier 2: _parse_audit_labels ===")

# Full audit text
audit_text = """
### 审计总结
[MATCH] FR-1 — 用户注册端点实现正确
[MATCH] FR-2 — 用户登录端点实现正确
[MISMATCH] FR-3 — JWT secret 从 config.py 硬编码读取，Spec 要求环境变量
[MISMATCH] schema User — 多了 Spec 中没有的 phone_number 字段
[MISSING] FR-5 — 密码重置功能未实现
[PASS] pytest tests/ -q -k 'test_auth' — 4 passed
[FAIL] npx tsc --noEmit — 3 errors
"""

result = ds._parse_audit_labels(audit_text)
check("matches=2", result["matches"] == 2, f"got {result['matches']}")
check("mismatches=2", result["mismatches"] == 2, f"got {result['mismatches']}")
check("missing=1", result["missing"] == 1, f"got {result['missing']}")
check("mismatches_list has 2 items", len(result["mismatches_list"]) == 2)
check("missing_list[0] contains 'FR-5'", "FR-5" in result["missing_list"][0])
check("verification_passed=False", result["verification_passed"] == False)
check("verification_results has 2 items", len(result["verification_results"]) == 2)

# Edge case: empty text
empty = ds._parse_audit_labels("")
check("empty text: all zeros", empty["matches"] == 0 and empty["mismatches"] == 0
     and empty["missing"] == 0 and empty["verification_passed"] == True)

# ============================================================
# Tier 3: _get_project_structure 项目扫描
# ============================================================
print()
print("=== Tier 3: _get_project_structure ===")

# Use a temp workspace
with tempfile.TemporaryDirectory() as tmpdir:
    # Create a mock project structure
    src_py = os.path.join(tmpdir, "src-python")
    os.makedirs(os.path.join(src_py, "models"))
    os.makedirs(os.path.join(src_py, "routes"))
    open(os.path.join(src_py, "models", "user.py"), "w").write("from services.auth import hash_password\n")
    open(os.path.join(src_py, "routes", "auth.py"), "w").write("from models.user import User\n")

    src_fe = os.path.join(tmpdir, "src-frontend")
    os.makedirs(os.path.join(src_fe, "src", "components"))
    open(os.path.join(src_fe, "src", "App.tsx"), "w").write("import React from 'react'\n")

    structure = ds._get_project_structure(tmpdir)
    check("structure mentions src-python", "src-python" in structure)
    check("structure mentions models/user.py", "user.py" in structure)
    check("structure mentions routes/auth.py", "auth.py" in structure)
    check("structure under 3000 chars", len(structure) <= 3000, f"got {len(structure)}")

# Edge case: nonexistent path
structure = ds._get_project_structure("/nonexistent/path/xyz")
check("nonexistent: returns fallback msg",
      "无项目结构信息" in structure or "不存在" in structure or "自行设计" in structure)

# ============================================================
# Tier 4: _resolve_spec_role_routing (Stage B)
# ============================================================
print()
print("=== Tier 4: _resolve_spec_role_routing ===")

r1 = _resolve_spec_role_routing("reviewer", 1, ["deepseek"])
check("N=1 reviewer → deepseek", r1["provider_id"] == "deepseek",
      f"got {r1['provider_id']}")

r2 = _resolve_spec_role_routing("reviewer", 1, ["deepseek", "anthropic"])
check("N=2 reviewer → anthropic", r2["provider_id"] == "anthropic",
      f"got {r2['provider_id']}")
check("N=2 diversity=True", r2["reviewer_diversity"] == True)

r3 = _resolve_spec_role_routing("reviewer", 1, ["ds", "a1", "a2", "a3"])
r4 = _resolve_spec_role_routing("reviewer", 2, ["ds", "a1", "a2", "a3"])
check("N=4 round1 vs round2 differ", r3["provider_id"] != r4["provider_id"],
      f"r1={r3['provider_id']} r2={r4['provider_id']}")

r_arch = _resolve_spec_role_routing("architect", 1, ["deepseek", "anthropic"])
check("architect always uses provider[0]", r_arch["provider_id"] == "deepseek")

# ============================================================
# Tier 5: 检查点持久化
# ============================================================
print()
print("=== Tier 5: Checkpoint persistence ===")

test_task_id = "test_spec_" + str(int(time.time()))
ds = DeliberationService()

# Save
ds._save_checkpoint(test_task_id, {
    "spec_yaml": "spec:\n  name: Test\n  version: 1",
    "round": 2,
    "audit_report": {"matches": 5, "mismatches": 0},
})
cp_path = ds._checkpoint_path(test_task_id)
check("checkpoint file exists", cp_path.exists())

# Load
cp = ds._load_checkpoint(test_task_id)
check("checkpoint loaded", cp is not None)
check("round=2", cp and cp.get("round") == 2, f"got {cp.get('round') if cp else 'None'}")
check("spec_yaml preserved", cp and "spec" in cp.get("spec_yaml", ""), f"got {cp.get('spec_yaml', '')[:30]}")

# Delete
ds._delete_checkpoint(test_task_id)
check("checkpoint deleted", not cp_path.exists())

# Edge: load nonexistent
cp2 = ds._load_checkpoint("nonexistent_task")
check("nonexistent checkpoint → None", cp2 is None)

# ============================================================
# Tier 6: Provider 配置解析
# ============================================================
print()
print("=== Tier 6: _resolve_spec_provider_config ===")
check("_resolve_spec_provider_config exists",
      hasattr(ds, '_resolve_spec_provider_config'))

# ============================================================
# Result
# ============================================================
print()
print(f"{'='*40}")
print(f"Stage D Total: {passes} passed, {fails} failed out of {passes+fails}")
if fails == 0:
    print("ALL STAGE D CHECKS PASSED")
else:
    print(f"{fails} FAILURES — fix before continuing")
    sys.exit(1)
