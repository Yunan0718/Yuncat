"""Phase 45 acceptance tests — LoopSupervisor + DB migration"""
import sys, os, io, time, tempfile, asyncio
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
# Tier 1: Import + data structures
# ============================================================
print("=== Phase 45-A: LoopSupervisor + Migration ===")
print("\n--- Tier 1: Imports & Data Structures ---")
from services.loop_supervisor import (
    LoopSupervisor, LoopSpec, LoopStatus, RestartStrategy
)

# RestartStrategy enum
check("RestartStrategy.PERMANENT", RestartStrategy.PERMANENT.value == "permanent")
check("RestartStrategy.TRANSIENT", RestartStrategy.TRANSIENT.value == "transient")
check("RestartStrategy.TEMPORARY", RestartStrategy.TEMPORARY.value == "temporary")

# LoopSpec defaults
spec = LoopSpec("test", lambda s: None)
check("LoopSpec default loop_type=timer", spec.loop_type == "timer")
check("LoopSpec default interval=60", spec.interval == 60)
check("LoopSpec default strategy=PERMANENT", spec.strategy == RestartStrategy.PERMANENT)
check("LoopSpec default max_restarts=3", spec.max_restarts == 3)

# LoopStatus defaults
status = LoopStatus("test")
check("LoopStatus default status=registered", status.status == "registered")
check("LoopStatus default error_count=0", status.error_count == 0)
check("LoopStatus default last_escalated_at=0", status.last_escalated_at == 0.0)

# ============================================================
# Tier 2: async tests (register + start_one + get_status)
# ============================================================
print("\n--- Tier 2: Async Operations ---")

async def _run_async_tests():
    # Create in-memory SQLite for testing
    import aiosqlite
    db = await aiosqlite.connect(":memory:")
    try:
        # Create required tables
        await db.execute("""CREATE TABLE IF NOT EXISTS loop_registry (
            loop_id TEXT PRIMARY KEY, loop_type TEXT DEFAULT 'timer',
            status TEXT DEFAULT 'registered', config TEXT DEFAULT '{}',
            state TEXT DEFAULT '{}', last_run_at INTEGER, last_ok_at INTEGER,
            restart_count INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0,
            last_error TEXT DEFAULT '', created_at INTEGER, updated_at INTEGER
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS loop_heartbeats (
            loop_id TEXT PRIMARY KEY, last_beat INTEGER, iteration_count INTEGER DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS loop_trigger_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT, loop_id TEXT, trigger_type TEXT,
            payload TEXT DEFAULT '{}', priority INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending', claimed_by TEXT, claimed_at INTEGER,
            completed_at INTEGER, created_at INTEGER
        )""")
        await db.commit()

        supervisor = LoopSupervisor(db=db)

        # Test 1: register
        called = []
        async def test_loop(state):
            called.append(1)
            state["last_value"] = len(called)

        await supervisor.register(LoopSpec("test_timer", test_loop, interval=1))
        check("register: spec stored", "test_timer" in supervisor._specs)
        check("register: status created", "test_timer" in supervisor._loops)
        check("register: status=registered",
              supervisor._loops["test_timer"].status == "registered")

        # Test 2: start_one (幂等)
        await supervisor.start_one("test_timer")
        check("start_one: status=running",
              supervisor._loops["test_timer"].status == "running")
        # Second call should be no-op
        await supervisor.start_one("test_timer")

        # Test 3: get_status
        all_status = supervisor.get_status()
        check("get_status: contains test_timer", "test_timer" in all_status)
        check("get_status: running status",
              all_status["test_timer"]["status"] == "running")

        # Test 4: shutdown
        await supervisor.shutdown_all()
        # wait for tasks to process cancellation
        await asyncio.sleep(0.15)
        check("shutdown: no exception during cancel", True)

        # Test 5: resume_after_restart — restore from DB state
        supervisor3 = LoopSupervisor(db=db)
        await supervisor3.register(LoopSpec("resume_test", test_loop, interval=1))
        # Simulate previous session: write running state to loop_registry
        await db.execute(
            "INSERT OR REPLACE INTO loop_registry(loop_id, loop_type, status, config, state, error_count) "
            "VALUES(?,?,?,?,?,?)",
            ("resume_test", "timer", "running",
             '{"interval":1,"priority":0,"strategy":"permanent","max_restarts":3}', '{}', 2),
        )
        await db.commit()
        # Initialize supervisor (sets _sem, _shutdown) before resume
        supervisor3._sem = asyncio.Semaphore(5)
        supervisor3._shutdown = asyncio.Event()
        restored = await supervisor3.resume_after_restart()
        check("resume_after_restart: restored 1 loop", restored == 1,
              f"got {restored}: {supervisor3.get_status()}")
        await supervisor3.shutdown_all()

        # Test 6: start_all with multiple loops
        supervisor2 = LoopSupervisor(db=db)
        await supervisor2.register(LoopSpec("loop_a", lambda s: None, interval=1, priority=1))
        await supervisor2.register(LoopSpec("loop_b", lambda s: None, interval=2, priority=0))
        await supervisor2.start_all()
        all2 = supervisor2.get_status()
        check("start_all: loop_a running", all2["loop_a"]["status"] == "running")
        check("start_all: loop_b running", all2["loop_b"]["status"] == "running")
        await supervisor2.shutdown_all()
    finally:
        await db.close()

asyncio.run(_run_async_tests())

# ============================================================
# Tier 3: Migration SQL validity
# ============================================================
print("\n--- Tier 3: Migration #8 SQL ---")
from repositories import MIGRATIONS
m8 = [m for m in MIGRATIONS if m[0] == 8]
check("migration #8 exists", len(m8) == 1, f"found {len(m8)}")
if m8:
    sql = m8[0][2]
    check("migration #8 has loop_registry", "CREATE TABLE IF NOT EXISTS loop_registry" in sql)
    check("migration #8 has loop_heartbeats", "CREATE TABLE IF NOT EXISTS loop_heartbeats" in sql)
    check("migration #8 has loop_trigger_queue", "CREATE TABLE IF NOT EXISTS loop_trigger_queue" in sql)

# ============================================================
print(f"\n{'='*40}")
print(f"Phase 45 Total: {passes} passed, {fails} failed out of {passes+fails}")
if fails == 0:
    print("ALL PHASE 45 CHECKS PASSED")
else:
    print(f"{fails} FAILURES — fix before continuing")
    sys.exit(1)
