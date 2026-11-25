"""skills/generate_system_skills.py —— 从现有数据源生成独立 Skill YAML（Phase 29）

运行一次：python generate_system_skills.py
产出：skills/system/ 下 ~72 个独立 Skill 目录
"""
import os, sys, yaml, re, textwrap

ROOT = os.path.dirname(os.path.abspath(__file__))
DST = os.path.join(ROOT, "system")

# 允许导入 src-python/config.py 获取当前应用版本
sys.path.insert(0, os.path.join(ROOT, ".."))
from config import APP_VERSION

# 清理旧系统 Skill（保留手动写的 yuncat-core）
for name in os.listdir(DST):
    path = os.path.join(DST, name)
    if os.path.isdir(path) and name != "yuncat-core":
        import shutil
        shutil.rmtree(path, ignore_errors=True)

count = 0

def write_skill(skill_id: str, title: str, desc: str, category: str, icon: str,
                prompt_text: str = "", scope_tasks: list = None, scope_langs: list = None,
                tags: list = None):
    global count
    d = os.path.join(DST, skill_id)
    os.makedirs(d, exist_ok=True)

    y = {
        "name": skill_id, "version": "0.3.0", "title": title,
        "description": desc, "author": "Yuncat System", "category": category,
        "icon": icon, "tags": tags or [category], "min_yuncat": APP_VERSION,
    }
    if prompt_text:
        y["prompt"] = {"file": "prompt.md", "placement": "append", "priority": 50,
                        "scope": {"tasks": scope_tasks or [], "languages": scope_langs or [],
                                  "frameworks": [], "modes": ["agent"]}}
        with open(os.path.join(d, "prompt.md"), "w", encoding="utf-8") as f:
            f.write(prompt_text.strip())

    with open(os.path.join(d, "skill.yaml"), "w", encoding="utf-8") as f:
        yaml.dump(y, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    count += 1

# ---- 1. Core (1) ----
write_skill("yuncat-core", "Yuncat 核心协议", "系统核心——工具优先级、强化规则、上下文感知", "core", "🐱",
    prompt_text=open(os.path.join(ROOT, "system", "yuncat-core", "prompt.md"), encoding="utf-8").read(),
    scope_tasks=[], scope_langs=[], tags=["core","system"])

# ---- 2. Workflow Skills (18) ----
sys.path.insert(0, os.path.join(ROOT, ".."))
from prompts.workflow_templates import BUILTIN_TEMPLATES

WORKFLOW_ICONS = {"code_review":"🔍","security_review":"🛡️","tdd_loop":"🧪","diagnose_loop":"🔬",
    "prototype":"⚡","triage":"🏥","dead_code_cleanup":"🧹","coverage_boost":"📊",
    "prp_plan":"📋","prp_pr":"📤","quality_gate":"✅","multi_model_plan":"🤖",
    "data_analysis":"📈","doc_generation":"📝","craft_fix_loop":"🔧",
    "auto_fix":"🔄","architect_editor":"🏗️","speckit_pipeline":"🚀"}

for tmpl in BUILTIN_TEMPLATES:
    name = tmpl["name"]
    desc = tmpl.get("description", name)
    icon = WORKFLOW_ICONS.get(name, "⚡")
    nodes = len(tmpl.get("graph", {}).get("nodes", []))
    write_skill(f"wf-{name}", f"工作流: {name}", f"DAG 工作流模板（{nodes} 节点）: {desc}",
                "workflow", icon, scope_tasks=[name], tags=["workflow","dag",name])

# ---- 3. Role Skills (~21+20=41) ----
# Step A: DB original 20 (from repositories/__init__.py SEED_ROLES)
sys.path.insert(0, os.path.join(ROOT, ".."))
from repositories import SEED_ROLES

ORIGINAL_20 = {"architect","security","developer","product","devops",
               "dba","reviewer","tester","coordinator","ux","performance",
               "compliance","tech_writer","data_scientist","sre",
               "ml_engineer","frontend","backend","mobile","project_manager"}

ROLE_ICONS = {"tooling":"🔧","reasoning":"💭","review":"🔍","planning":"📋",
              "security":"🛡️","optimization":"⚡","design":"🎨","engineering":"⚙️",
              "frontend":"🖼️","analysis":"📊","builtin":"🎭"}

for rid, name, cat, prompt, desc, enabled in SEED_ROLES:
    icon = ROLE_ICONS.get(cat, "🎭")
    if rid in ORIGINAL_20:
        write_skill(f"role-{rid}", f"角色: {name}", f"讨论引擎角色: {desc}",
                    "role", icon, prompt_text=prompt, scope_tasks=[],
                    tags=["role","deliberation","builtin",rid])
    else:
        # ECC extended 21 roles (Phase 28: now in SEED_ROLES)
        write_skill(f"role-{rid}", f"角色: {name}", f"讨论引擎角色: {desc}",
                    "role", icon, prompt_text=prompt, scope_tasks=[], scope_langs=[],
                    tags=["role","deliberation",rid])

# ---- 4. Command Skills (12) ----
cmd_dir = os.path.join(ROOT, "..", "commands", "builtin")
if os.path.exists(cmd_dir):
    for fname in sorted(os.listdir(cmd_dir)):
        if not fname.endswith(".yaml"):
            continue
        with open(os.path.join(cmd_dir, fname), encoding="utf-8") as f:
            cmd = yaml.safe_load(f) or {}
        name = cmd.get("name", fname[:-5])
        sc = cmd.get("slash_command", f"/{name}")
        desc = cmd.get("description", name)
        cat = cmd.get("category", "general")
        icon = "/"
        write_skill(f"cmd-{name}", f"命令: {sc}", desc, "command", icon,
                    scope_tasks=[name], tags=["command","slash",name])

print(f"Generated {count} individual skills in {DST}")
