"""services/plan_parser.py —— Plan 输出结构化解析器（Phase 14→17）

从 AI 在 Plan 模式下输出的 Markdown 方案中提取结构化步骤列表。
纯函数，无外部依赖。

Phase 17: 增强 file_hint 检测 + files_affected 汇总 + diff 检测
Phase 56-P2: 解析上限按模型窗口动态计算。
"""
import re

from config import calc_context_budget, calc_list_limit


def _calc_plan_budgets(model_cfg: dict | None) -> dict[str, int]:
    """Phase 56-P2: Plan 解析各阶段上限按模型窗口动态计算。"""
    return {
        "step_text": calc_context_budget(model_cfg, fraction=0.002, min_chars=150, max_chars=600),
        "steps": calc_list_limit(model_cfg, 20, fraction_per_100k=2, max_limit=40),
        "files": calc_list_limit(model_cfg, 20, fraction_per_100k=3, max_limit=40),
        "file_hints": calc_list_limit(model_cfg, 3, fraction_per_100k=1, max_limit=6),
        "file_ops": calc_list_limit(model_cfg, 10, fraction_per_100k=2, max_limit=20),
        "commands": calc_list_limit(model_cfg, 5, fraction_per_100k=1, max_limit=10),
        "command_len": calc_context_budget(model_cfg, fraction=0.003, min_chars=300, max_chars=1200),
        "diff_files": calc_list_limit(model_cfg, 20, fraction_per_100k=2, max_limit=40),
        "changes": calc_list_limit(model_cfg, 5, fraction_per_100k=1, max_limit=10),
    }


def parse_plan(text: str, model_cfg: dict | None = None) -> dict:
    """解析 Plan 输出 → 结构化方案。

    返回格式:
    {
      "summary": "",
      "design": "",
      "steps": [{"step": 1, "task": "...", "file_hint": "..."}, ...],
      "risks": "",
      "files_affected": ["file1.py", "dir/file2.ts"],
      "raw_steps_text": "原始步骤文本"
    }
    """
    budgets = _calc_plan_budgets(model_cfg)
    result: dict = {
        "summary": _extract_section(text, r"需求理解", r"方案设计|实施步骤|风险|预估"),
        "design": _extract_section(text, r"方案设计", r"实施步骤|风险|预估"),
        "steps": [],
        "risks": _extract_section(text, r"风险与备选|风险", r"预估影响|$"),
        "files_affected": _extract_files(text, budgets),
        "raw_steps_text": "",
    }

    # 提取"实施步骤"部分
    steps_text = _extract_section(text, r"实施步骤", r"风险|预估|💡|$")
    if not steps_text:
        # 回退：全局搜索编号列表
        steps_text = text
    result["raw_steps_text"] = steps_text

    # 从步骤文本中提取编号列表项
    result["steps"] = _extract_steps(steps_text, budgets)

    # 给每个步骤补 file_hint：先从步骤文本自身提取，再从全局 files_affected 推断
    if result["steps"]:
        for s in result["steps"]:
            step_files = _extract_files(s["task"], budgets)
            if step_files:
                s["file_hint"] = ", ".join(step_files[:budgets["file_hints"]])
            else:
                s["file_hint"] = ""

    return result


def detect_file_changes(step_output: str, model_cfg: dict | None = None) -> list[str]:
    """从步骤执行输出中检测实际修改/创建的文件路径——Phase 17

    Phase 56-P2: 回退提取数量按模型窗口动态计算。
    """
    budgets = _calc_plan_budgets(model_cfg)
    patterns = [
        re.compile(r'(?:写入|保存|创建|修改|更新|生成|write|save|create|modify|update|generate)[^。\n]*?(?:文件|file)[：:\s]*`?([^\s`\n]+\.(?:py|tsx?|jsx?|rs|go|java|sql|md|html|css|yaml|yml|json))`?', re.IGNORECASE),
        re.compile(r'(?:已写入|已保存|已修改|已创建|created|saved|modified|updated)\s*[:：]\s*`?([^\s`\n]+\.(?:py|tsx?|jsx?|rs|go|java|sql|md|html|css|yaml|yml|json))`?', re.IGNORECASE),
    ]
    files = set()
    for pat in patterns:
        for m in pat.finditer(step_output):
            files.add(m.group(1))
    if not files:
        # 回退：简单提取所有代码文件路径
        files = set(_extract_files(step_output, budgets)[:budgets["file_hints"]])
    return list(files)


def _extract_section(text: str, start_pattern: str, end_pattern: str) -> str:
    """提取两个 Markdown 标题之间的文本"""
    # 匹配 "**start_pattern**" 或 "## start_pattern" 或 "N. **start_pattern**"
    start_re = re.compile(
        rf'(?:\d+\.\s*\*\*{start_pattern}\*\*|##\s+{start_pattern}|{start_pattern})',
        re.IGNORECASE,
    )
    end_re = re.compile(
        rf'(?:\d+\.\s*\*\*{end_pattern}\*\*|##\s+{end_pattern}|{end_pattern})',
        re.IGNORECASE,
    )

    start_m = start_re.search(text)
    if not start_m:
        return ""

    # 从 start 之后开始找 end
    rest = text[start_m.end():]
    end_m = end_re.search(rest)
    if end_m:
        return rest[:end_m.start()].strip()

    # end 是 "$" 时返回全部剩余
    if end_pattern == "$":
        return rest.strip()

    return rest.strip()


def _extract_steps(text: str, budgets: dict | None = None) -> list[dict]:
    """从 Markdown 文本中提取步骤列表。

    支持格式:
      - item (file.py)
      1. item (file.py)
      - [ ] item (file.py)
      第1步: item

    Phase 56-P2: 步骤文本长度和数量按模型窗口动态计算。
    """
    if budgets is None:
        budgets = _calc_plan_budgets(None)
    steps = []
    # 匹配各种步骤格式
    patterns = [
        # - 创建 User 模型（models/user.py）
        re.compile(r'^\s*[-*]\s+(?:\[.\]\s*)?(.+?)(?:\s*[（(]([^）)]+\.(?:py|ts|tsx|js|jsx|rs|go|java|sql|md|html|css|yaml|yml|json))[）)])?\s*$', re.MULTILINE),
        # 1. 创建 User 模型（models/user.py）
        re.compile(r'^\s*\d+[\.\)]\s+(.+?)(?:\s*[（(]([^）)]+\.(?:py|ts|tsx|js|jsx|rs|go|java|sql|md|html|css|yaml|yml|json))[）)])?\s*$', re.MULTILINE),
        # 第1步: 创建 User 模型
        re.compile(r'第\d+步[:：]\s*(.+?)(?:\s*[（(]([^）)]+\.(?:py|ts|tsx|js|jsx|rs|go|java|sql|md|html|css|yaml|yml|json))[）)])?\s*$', re.MULTILINE),
    ]

    for pattern in patterns:
        matches = pattern.findall(text)
        if matches:
            for i, m in enumerate(matches):
                task_text = m[0].strip() if isinstance(m, tuple) else m.strip()
                file_hint = m[1].strip() if isinstance(m, tuple) and len(m) > 1 and m[1] else ""
                # 清理任务文本
                task_text = re.sub(r'^[-*\d\.\)\s]+', '', task_text).strip()
                if len(task_text) > 3:  # 过滤明显的噪音
                    steps.append({
                        "step": i + 1,
                        "task": task_text[:budgets["step_text"]],
                        "file_hint": file_hint,
                    })
            if steps:
                break  # 第一个匹配到的格式就退出

    return steps[:budgets["steps"]]


def _extract_files(text: str, budgets: dict | None = None) -> list[str]:
    """从文本中提取所有文件路径引用。

    Phase 56-P2: 返回数量按模型窗口动态计算。
    """
    if budgets is None:
        budgets = _calc_plan_budgets(None)
    pattern = re.compile(
        r'(?:`([^`]+\.(?:py|tsx?|jsx?|rs|go|java|sql|md|html|css|yaml|yml|json))`'
        r'|\b([\w/.-]+\.(?:py|tsx?|jsx?|rs|go|java|sql|md|html|css|yaml|yml|json))\b)',
    )
    files = set()
    for m in pattern.finditer(text):
        f = m.group(1) or m.group(2)
        if f and len(f) > 2:
            files.add(f)
    return list(files)[:budgets["files"]]


# ---- Phase 18: 从 Plan 步骤输出中提取可执行的文件操作 ----

def extract_file_operations(step_output: str, model_cfg: dict | None = None) -> list[dict]:
    """从 LLM 步骤输出中提取文件写入/创建操作。

    识别模式:
    - ```language:filepath ... ``` 代码块
    - "写入/创建/保存 文件 xxx" 后续的代码
    - ```{filepath} ... ```

    返回: [{"path": "shared/xxx.py", "content": "..."}, ...]

    Phase 56-P2: 返回数量按模型窗口动态计算。
    """
    budgets = _calc_plan_budgets(model_cfg)
    ops = []
    # 模式 1: 代码块标记含语言和文件路径 如 ```python:utils.py
    pat1 = re.compile(
        r'```[\w#]*\s*[:#]\s*([^\s\n`]+\.(?:py|tsx?|jsx?|rs|go|java|sql|md|html|css|yaml|yml|json))\s*\n(.*?)```',
        re.DOTALL,
    )
    for m in pat1.finditer(step_output):
        path = m.group(1).strip()
        content = m.group(2).strip()
        if len(content) > 10:  # 实质性内容
            ops.append({"path": f"shared/{path}", "content": content})

    # 模式 2: 文件路径标题 + 代码块 如 ## file: utils.py \n ```python...```
    pat2 = re.compile(
        r'#*\s*(?:文件|file)[：:\s]*`?([^\s\n`]+\.(?:py|tsx?|jsx?|rs|go|java|sql|md|html|css|yaml|yml|json))`?\s*\n\s*```[\w]*\s*\n(.*?)```',
        re.DOTALL,
    )
    for m in pat2.finditer(step_output):
        path = m.group(1).strip()
        content = m.group(2).strip()
        if len(content) > 10:
            ops.append({"path": f"shared/{path}", "content": content})

    # 去重——同一文件只保留最后一次写入
    seen = {}
    for op in ops:
        seen[op["path"]] = op
    return list(seen.values())[:budgets["file_ops"]]


def extract_commands(step_output: str, model_cfg: dict | None = None) -> list[str]:
    """从步骤输出中提取可执行命令。识别模式:
    - `$ command`
    - ```bash ... command ... ```
    - "运行/执行: command"

    Phase 56-P2: 命令长度和数量按模型窗口动态计算。
    """
    budgets = _calc_plan_budgets(model_cfg)
    max_len = budgets["command_len"]
    cmds = []
    # $ command
    for m in re.finditer(r'\$\s+(.+)', step_output):
        cmd = m.group(1).strip()
        if 2 < len(cmd) < max_len:
            cmds.append(cmd)
    # ```bash/sh block
    pat = re.compile(r'```(?:ba)?sh\s*\n(.*?)```', re.DOTALL)
    for m in pat.finditer(step_output):
        for line in m.group(1).strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#") and 2 < len(line) < max_len:
                cmds.append(line)
    return cmds[:budgets["commands"]]


# ---- Phase 20: Diff 式编辑解析 ----

def parse_diff_output(response: str) -> list[dict]:
    """从 LLM 响应中提取 unified diff 块 → 结构化文件操作。

    借鉴 Codex CLI 的 apply_patch 模式——大文件场景下 token 节省 80%+。

    返回: [{"path": "x.py", "diff": "...", "additions": 3, "deletions": 1}, ...]
    """
    # 匹配 ```diff ... ``` 代码块
    diff_pat = re.compile(r'```diff\s*\n(.*?)```', re.DOTALL)
    result = []
    for block in diff_pat.finditer(response):
        content = block.group(1)
        # 解析 --- a/path 和 +++ b/path
        file_match = re.search(r'\+\+\+\s+b/(.+)', content)
        if not file_match:
            continue
        path = file_match.group(1).strip()
        # 统计增删行
        additions = len(re.findall(r'^\+(?!\+\+)', content, re.MULTILINE))
        deletions = len(re.findall(r'^-(?!---)', content, re.MULTILINE))
        result.append({
            "path": path,
            "diff": content.strip(),
            "additions": additions,
            "deletions": deletions,
        })
    return result


def extract_diff_edits_from_any(text: str) -> list[dict]:
    """宽松模式——匹配任意位置的 unified diff（不限于 ```diff 代码块）。

    正则: ^--- a/path\\n^\\+\\+\\+ b/path\\n^@@
    """
    any_pat = re.compile(
        r'^---\s+a/(.+?)\n\+\+\+\s+b/(.+?)\n@@\s+-(\d+),?(\d*)\s+\+(\d+),?(\d*)\s+@@(.*?)(?=^---|\Z)',
        re.MULTILINE | re.DOTALL,
    )
    result = []
    for m in any_pat.finditer(text):
        path_a = m.group(1).strip()
        path_b = m.group(2).strip()
        hunk = m.group(7)
        additions = len(re.findall(r'^\+(?!\+\+)', hunk, re.MULTILINE))
        deletions = len(re.findall(r'^-(?!---)', hunk, re.MULTILINE))
        result.append({
            "path": path_b,  # 使用目标路径
            "path_old": path_a,
            "diff": m.group(0).strip(),
            "additions": additions,
            "deletions": deletions,
        })
    return result
