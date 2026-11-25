"""Fix all stale imports after config.py cleanup."""
import re, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.join(ROOT, "src-python"))

fixes = {
    # Remove deleted constant from 'from config import' lines
    "from config import SUPERVISOR_SHUTDOWN_TIMEOUT": "",
    "from config import PAGE_DEFAULT": "",
    "from config import PAGE_LARGE": "",
    "from config import PAGE_SMALL": "",
    "from config import BULK_EXPORT": "",
    "from config import BULK_STATS": "",
    "from config import BULK_LOGS": "",
    "from config import LLM_TINY": "",
    "from config import LLM_SMALL": "",
    "from config import LLM_MEDIUM": "",
    "from config import LLM_LARGE": "",
    "from config import MAX_EVENT_LOG_ENTRIES": "",
    "from config import MEMORY_SAMPLE_LIMIT": "",
    "from config import MEMORY_INJECT_LIMIT": "",
    "from config import REVIEW_SAMPLE_LIMIT": "",
    "from config import HITL_DEFAULT_TIMEOUT": "",
    "from config import DREAM_FREQUENCY_PRESETS": "",
    "from config import DREAM_CHECK_INTERVAL": "",
    "from config import DREAM_TIME_GATE": "",
    "from config import DREAM_MIN_TURNS": "",
    "from config import SPEC_DEVELOPER_TIMEOUT": "",
    "from config import SPEC_AUDITOR_TIMEOUT": "",
    "from config import ROLE_PROPOSE_TIMEOUT": "",
    "from config import VERIFY_CMD_TIMEOUT": "",
    "from config import DELEGATE_TIMEOUT": "",
    "from config import MCP_START_TIMEOUT": "",
    "from config import ADAPTER_EXECUTE_TOOL_TIMEOUT": "",
    "from config import DEFAULT_MAX_TOKENS": "",
    "from config import MAX_TOOLS_PER_ROUND": "",
    "from config import TOOL_RESULT_MAX_CHARS": "",
    "from config import ASSISTANT_MSG_MAX_CHARS": "",
    "from config import REASONING_CONTENT_MAX_CHARS": "",
    "from config import CONTEXT_REATTACH_FILE_MAX_CHARS": "",
    "from config import TOKEN, PAGE_SMALL": "from config import TOKEN",
    "from config import PAGE_DEFAULT, PAGE_LARGE, BULK_EXPORT": "",
    "from config import PAGE_DEFAULT, PAGE_LARGE": "",
    "from config import BULK_EXPORT, BULK_STATS, BULK_LOGS, PAGE_LARGE": "",
    "from config import LLM_TINY, PAGE_SMALL, PAGE_DEFAULT, PAGE_LARGE": "",
    "from config import PAGE_DEFAULT, MEMORY_SAMPLE_LIMIT, MEMORY_INJECT_LIMIT, REVIEW_SAMPLE_LIMIT": "",
    "from config import INTERNAL_LIMIT, SUPERVISOR_SHUTDOWN_TIMEOUT": "from config import INTERNAL_LIMIT",
    "from config import INTERNAL_LIMIT, MEMORY_SAMPLE_LIMIT, MEMORY_INJECT_LIMIT": "from config import INTERNAL_LIMIT",
}

# Clean up import lines with multiple constants where some were removed
def clean_import_line(line):
    """Remove empty tuples, trailing commas, etc."""
    line = line.strip()
    if not line:
        return ""
    # from config import XXX, -> becomes empty if only one was removed
    if line.startswith("from config import ("):
        # Multi-line import - skip for now
        return line
    # Remove trailing commas
    line = re.sub(r',\s*$', '', line)
    # Remove "from config import " with nothing after
    if re.match(r'^from config import\s*$', line):
        return ""
    return line + "\n"

# Fix var definitions in lines (like "PAGE_LARGE = 1000" leftover)
def_fixes = {
    "PAGE_LARGE = 1000": "PAGE_LARGE = 1000",
    "PAGE_DEFAULT = 1000": "PAGE_DEFAULT = 1000",
    "PAGE_SMALL = 1000": "PAGE_SMALL = 1000",
    "BULK_EXPORT = 10000": "BULK_EXPORT = 10000",
    "HITL_DEFAULT_TIMEOUT = 3600": "HITL_DEFAULT_TIMEOUT = 3600",
}

count = 0
for dirpath, dirnames, filenames in os.walk("."):
    for fn in filenames:
        if not fn.endswith(".py"):
            continue
        if "test_" in fn or "__pycache__" in dirpath:
            continue
        fpath = os.path.join(dirpath, fn)
        with open(fpath, encoding="utf-8") as f:
            content = f.read()

        modified = False
        for old, new in fixes.items():
            if old in content:
                content = content.replace(old, new)
                modified = True
                count += 1

        if modified:
            # Clean up the import lines
            lines = content.split("\n")
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("from config import") or stripped.startswith("from config import ("):
                    cleaned = clean_import_line(stripped)
                    if cleaned:
                        new_lines.append(line.replace(stripped, cleaned.rstrip()))
                    # If empty, skip the line
                else:
                    new_lines.append(line)
            content = "\n".join(new_lines)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)

print(f"Fixed {count} stale import references across files")
