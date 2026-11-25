"""tools/builtin/system_tools.py —— 核心内置工具 + 二阶段确认机制

薄 re-export 层：实际实现已拆分到 system_tool_*.py 子模块。
导入本模块会触发所有 @tool 装饰器注册（保持向后兼容）。

子模块：
  - system_tool_safety.py     命令安全（白名单）
  - system_tool_file_ops.py   文件操作（read/write/list/delete/diff）
  - system_tool_command.py    命令执行（run_command/_execute_command）
  - system_tool_install.py    安装工具（skill/mcp/workflow）
  - system_tool_config.py     配置/委托（yuncat_config/delegate_to_agent）
  - system_tool_search.py     搜索（search_files/codegraph_context）
  - system_tool_confirm.py    二阶段确认机制
  - system_tool_fs.py         文件系统（mkdir/reveal/rename/delete_dir）
  - system_tool_context.py    上下文/度量（docs/thinking/metrics/cache）
  - system_tool_checkpoint.py 检查点（list/restore）
  - system_tool_advanced.py   高级工具（git/edit_file/best_of_n/mcp_server）
  - system_tool_vision.py     视觉分析（vision_analyze 委托 OC）
  - system_tool_browser.py  ###Electron ## Chromium + CDP ###AI #########
"""
# ---- safety ----
from .system_tool_safety import (
    SAFE_COMMAND_WHITELIST,
    SAFE_INTERPRETERS,
    FORBIDDEN_INTERPRETER_FLAGS,
    is_safe_command,
)

# ---- file_ops ----
from .system_tool_file_ops import (
    read_file,
    list_dir,
    write_file,
    delete_file,
    diff,
    _generate_unified_diff,
    _compute_diff_stats,
    _handle_delete,
)

# ---- command ----
from .system_tool_command import (
    run_command,
    _execute_command,
    _get_workspace_path,
)

# ---- install ----
from .system_tool_install import (
    install_skill_from_url,
    configure_mcp,
    install_workflow_from_url,
)

# ---- config ----
from .system_tool_config import (
    yuncat_config,
    delegate_to_agent,
    _safe_truncate,
)

# ---- search ----
from .system_tool_search import (
    search_files,
    codegraph_context,
)

# ---- confirm ----
from .system_tool_confirm import (
    create_pending_state,
    create_pending_state_with_expiry,
    _pop_pending,
    _cleanup_expired_states,
    _handle_write,
    _handle_command,
    _handle_rename,
    _handle_delete_dir,
    dispatch_confirm,
    CONFIRM_HANDLERS,
)

# ---- fs ----
from .system_tool_fs import (
    mkdir,
    reveal_in_explorer,
    rename_file,
    delete_dir,
)

# ---- context ----
from .system_tool_context import (
    _TOOL_RESULT_CACHE,
    _cache_tool_result,
    request_more_context,
    get_own_metrics,
    sequential_thinking,
    docs_lookup,
)
# ---- interaction ----
from .system_tool_ask_user import ask_user
from .system_tool_todo import todo_write
from .system_tool_present import present

# ---- checkpoint ----
from .system_tool_checkpoint import (
    checkpoint_list,
    checkpoint_restore,
)

# ---- advanced ----
from .system_tool_advanced import (
    GIT_WHITELIST,
    git,
    edit_file,
    best_of_n,
    create_mcp_server,
)

# ---- memory ----
# P0-1.12: 记忆工具——对标 Letta/MemGPT 记忆工具化，让 Agent 主动检索/写入记忆
from .memory_tool import (
    search_memory,
    store_memory,
)

# ---- vision ----
# Task 6.2: 视觉分析工具——让 Agent 主动分析图片（委托 OC openclaw_vision）
from .system_tool_vision import (
    vision_analyze,
)

# ---- browser ----
# 2026-09-16: 内置浏览器工具。窗口住在 Electron 主进程，这里只发白名单动作，
# 人类和 AI 操作的是同一批标签页（见 services/host_bridge.py）。
from .system_tool_browser import (
    browser_open,
    browser_navigate,
    browser_snapshot,
    browser_click,
    browser_type,
    browser_press,
    browser_scroll,
    browser_read_page,
    browser_screenshot,
    browser_wait_for,
    browser_tabs,
    browser_history,
    browser_eval,
)
