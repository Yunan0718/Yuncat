"""健康检查脚本 — 验证所有 Phase 43-46 后端组件的完整性"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src-python'))

errors = []

def check(name, condition, detail=""):
    if condition:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name} - {detail}")
        errors.append(name)

# 1. 导入
print("1. 导入检查")
mods = [
    ('loop_supervisor', 'services.loop_supervisor'),
    ('gate_manager', 'services.gate_manager'),
    ('build_analyzer', 'services.build_analyzer'),
    ('review_pipeline', 'services.review_pipeline'),
    ('spec_orchestrator', 'services.spec_orchestrator'),
    ('spec_schema', 'prompts.spec_schema'),
    ('spec_prompts', 'prompts.spec_prompts'),
    ('spec_parser', 'services.spec_parser'),
    ('model_config_service', 'services.model_config_service'),
    # 2026-10-22：模块分层后路径变了（agent/ memory/ deliberation/ 子包），脚本一直
    # 停在旧路径上跑不起来——修好它，它才是有效的冒烟检查。
    ('deliberation_service', 'services.deliberation.deliberation_service'),
    ('agent_service', 'services.agent.agent_service'),
    ('memory_service', 'services.memory.memory_service'),
]
for name, path in mods:
    try:
        __import__(path)
        check(f"import {name}", True)
    except Exception as e:
        check(f"import {name}", False, str(e)[:200])

# 2. DB Migration
print("\n2. Migration 检查")
from repositories import MIGRATIONS
check("MIGRATIONS list exists", len(MIGRATIONS) > 0)
check("Migration #8 (loop tables) exists", any(m[0] == 8 for m in MIGRATIONS),
      f"MIGRATIONS versions: {[m[0] for m in MIGRATIONS]}")

# 3. lifespan parse
print("\n3. lifespan.py 语法")
try:
    import ast
    ast.parse(open(os.path.join(os.path.dirname(__file__), '..', 'src-python', 'lifespan.py'), encoding='utf-8').read())
    check("lifespan syntax", True)
except SyntaxError as e:
    check("lifespan syntax", False, str(e))

# 4. 主路由
print("\n4. 路由注册")
import main
check("main.py loaded", True)
router_count = sum(1 for r in main.app.routes if hasattr(r, 'methods'))
check(f"routes registered", router_count > 10, f"{router_count} routes")

# 5. 异步组件功能
import asyncio
async def async_checks():
    print("\n5. 异步组件")
    from services.model_config_service import ModelConfigService
    svc = ModelConfigService()
    providers = await svc.list_providers()
    check(f"list_providers() -> {len(providers)} providers", isinstance(providers, list) and len(providers) > 0,
          f"type={type(providers).__name__}")

    # Get health endpoint data
    from services.deliberation.deliberation_service import DeliberationService
    ds = DeliberationService()
    check("DeliberationService created", ds is not None)
    check("run_spec_loop exists", hasattr(ds, 'run_spec_loop'))
    check("goal_all_tests_pass", hasattr(ds, 'goal_all_tests_pass'))
    check("ghost_library_verify", hasattr(ds, 'ghost_library_verify'))

    # LoopSupervisor
    from services.loop_supervisor import LoopSupervisor, LoopSpec, RestartStrategy
    sup = LoopSupervisor(db=None)
    spec = LoopSpec("test", lambda s: None, interval=1)
    sup.register_sync(spec)
    check("register_sync", "test" in sup._specs)

asyncio.run(async_checks())

# 结果
print(f"\n{'='*40}")
if errors:
    print(f"FAILED {len(errors)} checks: {errors}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED - Backend is healthy")
