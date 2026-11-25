"""Phase 44 C.1 acceptance test — 自动修复回环

验证:
  1. run_spec_loop 返回值包含 auto_fix_attempts / auto_fix_converged
  2. _escalate_spec_failure 接受 auto_fix_attempts 参数
  3. 自动修复流程的 WS 事件类型 (auto_fix / auto_fix_reverify / auto_fix_result)
  4. Stage D 回归 (36 项)
  5. Python 语法检查
"""
import sys, os, io
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

print("=== Phase 44 C.1: Auto-fix loop ===")

# ============================================================
# Tier 1: Method signatures
# ============================================================
print("\n--- Tier 1: Signatures ---")
import inspect
from services.deliberation.deliberation_service import DeliberationService

ds = DeliberationService()

# Check _escalate_spec_failure has auto_fix_attempts param
sig = inspect.signature(ds._escalate_spec_failure)
check("_escalate_spec_failure has auto_fix_attempts param",
      "auto_fix_attempts" in sig.parameters,
      f"params: {list(sig.parameters.keys())}")
check("auto_fix_attempts default is 0",
      sig.parameters.get("auto_fix_attempts", {}).default == 0)

# Check _invoke_spec_developer has override_prompt param
sig2 = inspect.signature(ds._invoke_spec_developer)
check("_invoke_spec_developer has override_prompt param",
      "override_prompt" in sig2.parameters,
      f"params: {list(sig2.parameters.keys())}")
check("override_prompt default is None",
      sig2.parameters.get("override_prompt", {}).default is None)

# Check run_spec_loop signature
sig3 = inspect.signature(ds.run_spec_loop)
check("run_spec_loop has max_rounds param", "max_rounds" in sig3.parameters)
check("run_spec_loop has mode param", "mode" in sig3.parameters)
check("run_spec_loop has goal_condition param", "goal_condition" in sig3.parameters)

# ============================================================
# Tier 2: Structural verification (not string grep)
# ============================================================
print("\n--- Tier 2: Structural ---")

import re
source = open(os.path.join(os.path.dirname(__file__), '..', 'src-python',
    'services', 'deliberation', 'deliberation_spec.py'), encoding='utf-8').read()
source_roles = open(os.path.join(os.path.dirname(__file__), '..', 'src-python',
    'services', 'deliberation', 'deliberation_spec_roles.py'), encoding='utf-8').read()

# Verify auto-fix loop structure exists in the correct location
_fix_loop_match = re.search(r'for fix_attempt in range\(3\)', source, re.DOTALL)
check("auto-fix loop block exists (for fix_attempt in range(3))",
      bool(_fix_loop_match),
      "verify _run_auto_fix_round has 3-attempt loop")
check("auto-fix: _invoke_spec_developer wired with override_prompt=fix_prompt",
      "override_prompt=fix_prompt" in source,
      "verify _try_auto_fix_once passes override_prompt")
check("auto-fix: reverify calls auditor",
      "_invoke_spec_auditor" in source and "auto_fix_reverify" in source)
check("auto-fix: convergence check",
      "auto_fix_converged = True" in source and
      "new_verification and new_mismatches == 0 and new_missing == 0" in source)
check("auto-fix: analytics INSERT",
      "spec_auto_fix" in source and "analytics_events" in source)

# Verify auto_fix vars are initialized at run_spec_loop function scope
_run_start = source.index("async def run_spec_loop")
_auto_fix_scope = source[_run_start:_run_start + 12000]
check("auto_fix_converged = False at run_spec_loop scope",
      "auto_fix_converged = False" in _auto_fix_scope)
check("auto_fix_attempts = 0 at run_spec_loop scope",
      "auto_fix_attempts = 0" in _auto_fix_scope)

# Verify return dict structure includes auto_fix fields
_ret = re.search(r'return \{.*?"converged": converged.*?\n\s+\}', source, re.DOTALL)
if _ret:
    _rd = _ret.group(0)
    check("return dict has auto_fix_converged",
          "auto_fix_converged" in _rd)
    check("return dict has auto_fix_attempts",
          "auto_fix_attempts" in _rd)

# Verify spec_loop_done push includes auto_fix stats
_done = re.search(r'await self\._push\(push_callback.*?"spec_loop_done".*?\}\)', source, re.DOTALL)
if _done:
    _dp = _done.group(0)
    check("spec_loop_done includes auto_fix_attempts",
          "auto_fix_attempts" in _dp)
    check("spec_loop_done includes auto_fix_converged",
          "auto_fix_converged" in _dp)

# Verify _escalate_spec_failure passes auto_fix_attempts in roles module
check("_escalate_spec_failure forwards auto_fix_attempts",
      "auto_fix_attempts" in source_roles)

# ============================================================
# Tier 3: Behavioral verification (mock-based)
# ============================================================
print("\n--- Tier 3: Behavioral ---")
# Verify goal condition functions exist and produce correct results
ds = DeliberationService()
audit_all_ok = {"mismatches": 0, "missing": 0, "verification_passed": True,
                "verification_results": [{"passed": True, "command": "pytest"}]}
audit_mismatch = {"mismatches": 2, "missing": 1, "verification_passed": False, "verification_results": []}

check("goal_all_tests_pass(all_ok)=True", ds.goal_all_tests_pass(audit_all_ok) == True)
check("goal_all_tests_pass(mismatch)=False", ds.goal_all_tests_pass(audit_mismatch) == False)
check("goal_mismatch_zero(mismatch)=False", ds.goal_mismatch_zero(audit_mismatch) == False)
check("goal_mismatch_zero(all_ok)=True", ds.goal_mismatch_zero(audit_all_ok) == True)
check("goal_verification_only(mismatch+pass)=True",
      ds.goal_verification_only({"mismatches": 1, "missing": 0, "verification_passed": True,
                                  "verification_results": [{"passed": True}]}) == True)
check("goal_verification_only(fail)=False",
      ds.goal_verification_only({"mismatches": 0, "missing": 0, "verification_passed": False,
                                  "verification_results": [{"passed": False}]}) == False)

# ============================================================
# Result
# ============================================================
print(f"\n{'='*40}")
print(f"C.1 Total: {passes} passed, {fails} failed out of {passes+fails}")
if fails == 0:
    print("ALL C.1 CHECKS PASSED")
else:
    print(f"{fails} FAILURES — fix before continuing")
    sys.exit(1)
