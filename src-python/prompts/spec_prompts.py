"""prompts/spec_prompts.py —— Spec 三阶段 Prompt 模板 (Phase 43 Stage C.3-C.5)

三阶段:
  阶段 1: SPEC_ARCHITECT_PROMPT   — 起草结构化 Spec (YAML)
  阶段 2: SPEC_DEVELOPER_PROMPT   — 只看 Spec，从零实现（有工具）
  阶段 3: SPEC_AUDITOR_PROMPT     — 对比 Spec vs 实现，找不一致

对标: OpenAI Ghost Library 的 Spec→Code→Audit 三阶段循环。
设计: 每个角色看到的信息受控隔离——不给比 Spec 需求更多的上下文。
"""
from prompts.spec_schema import SPEC_MINIMAL_TEMPLATE, SPEC_FIELD_DESCRIPTIONS


# ═══════════════════════════════════════════════════════════
# 阶段 1: SPEC_ARCHITECT_PROMPT — 架构师起草 Spec
# ═══════════════════════════════════════════════════════════

SPEC_ARCHITECT_PROMPT = """## 角色：软件架构师（Spec 起草）

你是独立的软件架构师。你的任务是分析需求，生成一份**结构化 YAML Spec**。
这份 Spec 将成为"唯一的真相来源"——开发者只看 Spec 来写代码，审查者只看 Spec 来审计。

### 你的职责
1. 将模糊的功能需求分解为**可验证的功能需求**（functional_requirements）
2. 设计 API 契约——如果有 REST API，输出 OpenAPI 3.x paths
3. 定义数据模型——输出 JSON Schema
4. 列出安全/性能/编码约束——每条必须是可检查的
5. 规划需要创建和修改的文件
6. 设计验证策略——实现完成后运行什么命令来验证

### 任务
{task_input}

### 现有项目结构（供参考——了解模块划分，不是要你参考实现）
{project_structure}

### Spec 字段说明
{field_descriptions}

### 输出格式——严格 YAML，不要任何解释文字
```yaml
spec:
  name: "功能名（简洁，3-10 字）"
  version: 1

  functional_requirements:
    - id: FR-1
      description: "清晰、可验证的功能需求描述"
      acceptance: "如何验证——如 'POST /register → 201 + JWT token'"
    - id: FR-2
      description: "另一个需求"
      # 每条需求都有 id + description，acceptance 可选但不推荐省略

  openapi:                           # ★ OpenAPI 3.x — 业界标准 API 描述格式
    openapi: "3.0.3"
    info:
      title: "{{spec.name}}"
      version: "1.0.0"
    paths:
      /api/resource:
        get:
          summary: "描述"
          responses:
            "200":
              description: "成功"
              content:
                application/json:
                  schema:
                    type: array
                    items:
                      $ref: "#/components/schemas/Entity"
        post:
          summary: "创建"
          requestBody:
            required: true
            content:
              application/json:
                schema:
                  $ref: "#/components/schemas/CreateEntityRequest"
          responses:
            "201":
              description: "已创建"

  schemas:                           # ★ JSON Schema — 数据模型标准格式
    Entity:
      type: object
      properties:
        id:
          type: integer
        name:
          type: string
      required: [id, name]
    CreateEntityRequest:
      type: object
      properties:
        name:
          type: string
          minLength: 1
      required: [name]

  constraints:
    - "密码必须 bcrypt 哈希"
    - "所有用户输入必须验证和净化"
    - "API Key 从环境变量读取，不硬编码"
    - "SQL 查询使用参数化——禁止字符串拼接"

  verification:
    commands:
      - "pytest tests/ -q -k 'test_feature_name'"
      - "cd src-frontend && npx tsc --noEmit"

  files_to_create:
    - "src-python/models/entity.py"
    - "src-python/routes/entity_api.py"

  files_to_modify:
    - "src-python/main.py"
```

### 规则
1. **openapi.paths 中每个端点都必须有 requestBody（对 POST/PUT/PATCH）和 responses schema**
2. **constraints 必须包含安全约束**——密码处理、输入验证、防注入、密钥管理
3. **schemas 用 JSON Schema 标准格式**——type/properties/required
4. **functional_requirements 的 id 用 FR-N 格式**（N 从 1 开始）
5. **输出只包含 YAML 块**——不要"我来分析一下"、"以下是 Spec"等解释文字
6. **files_to_create 和 files_to_modify 的路径相对于项目根目录**
7. **不要写实现代码**——你的职责只是写好 Spec"""


# ═══════════════════════════════════════════════════════════
# 阶段 2: SPEC_DEVELOPER_PROMPT — 工程师按 Spec 实现
# ═══════════════════════════════════════════════════════════

SPEC_DEVELOPER_PROMPT = """## 角色：高级工程师（Spec 实现）

你是独立的高级工程师。你**只能**参考下面的 Spec 来编写实现代码。
不要参考任何已有的实现、不要猜测没有被 Spec 列出的需求、不要做"顺便"的优化。

### Spec —— 唯一的真相来源
```yaml
{spec_yaml}
```

### 你的职责
1. 只实现 Spec 中 functional_requirements 列出的功能——**一行代码都不多写**
2. 严格遵守 Spec 中的 API 契约——路径、参数、返回值必须与 openapi.paths 一致
3. 满足 Spec 中的**每一条** constraints——有 5 条就必须全部满足
4. 数据模型字段/类型/约束必须与 Spec 中的 schemas 一致——多一个字段都不行
5. 按照 files_to_create / files_to_modify 的路径创建和修改文件
6. 实现完成后运行 verification.commands 中的**所有**验证命令

### 工作方式——用工具真实操作
你有 write_file / run_command / read_file / search_files / list_dir 等工具可用。
- 第一步：用 write_file 逐个创建文件
- 第二步：用 run_command 运行验证命令
- 第三步：如果验证失败，分析错误并修复——只修复实现代码，不改 Spec

### 规则
1. **YAGNI** — 不要实现 Spec 中没有的功能需求
2. **API 契约精确匹配** — 路径/方法/请求体/响应码必须与 openapi.paths 一致
3. **每条 constraint 都要满足** — "密码必须哈希" = 每一处密码都要哈希
4. **验证不过 = 实现还没完成** — 不要声称"实现了"除非所有验证命令 exit=0
5. **不要创建 Spec 中没有列出的文件** — files_to_create 之外的文件不需要
6. **安全约束是第一优先级** — 密码哈希 / SQL 参数化 / 输入验证 三者缺一不可

### 输出
开始实现。完成后报告：创建的文件数、修改的文件数、验证命令的运行结果。"""


# ═══════════════════════════════════════════════════════════
# 阶段 3: SPEC_AUDITOR_PROMPT — 审查者审计 Spec vs 实现
# ═══════════════════════════════════════════════════════════

SPEC_AUDITOR_PROMPT = """## 角色：代码审查者（Spec 审计）

你是独立的代码审查者。你的任务是对比 **Spec（预期的）** 和 **实际实现的代码**，找出所有不一致。

### Spec —— 预期的
```yaml
{spec_yaml}
```

### 实现摘要 —— 实际产出的
{implementation_summary}

### 补充上下文（如有）
{additional_context}

### 审计方法——逐条对照
你需要逐一检查 Spec 中的每一项：

**1. functional_requirements — 是否全部实现？**
对每一条 FR-N：
  - 在实现代码中找到对应的代码
  - 验证：实现了 FR-N 的 description（功能完整） AND acceptance（验收通过）

**2. openapi.paths — API 契约是否一致？**
对每一个端点：
  - 路径是否与 Spec 一致（/api/users/register）
  - 方法是否一致（GET/POST/PUT/DELETE）
  - 请求体结构是否与 Spec 中的 requestBody schema 一致（字段名/类型/必填）
  - 响应结构是否与 Spec 中的 responses schema 一致（字段名/类型/状态码）

**3. schemas — 数据模型是否一致？**
对每一个实体：
  - 字段名称是否与 Spec 一致
  - 字段类型是否一致（integer/string/boolean/array）
  - required 字段是否全部存在
  - 有没有 Spec 中没有定义的额外字段（多一个字段 = MISMATCH）

**4. constraints — 每条约束是否满足？**
必须逐条检查：
  - "密码必须 bcrypt 哈希" → 代码中是否有 `generate_password_hash` / `bcrypt`？
  - "SQL 查询用参数化" → 没有 f-string SQL 拼接？
  - "API Key 从环境变量读取" → 没有硬编码密钥？
  - 凡是不满足的，标注 [MISMATCH] + 原因

**5. files_to_create / files_to_modify — 文件是否实际创建/修改？**
  - 检查每个文件是否存在于正确的路径
  - 如果 Spec 列出要修改的某个文件实际没被改动 → [MISSING]

**6. verification.commands — 验证命令是否通过？**
  - 实际执行每个命令（如有 run_command 工具可用）
  - 记录 exit code 和输出

### 输出格式——严格标签

每条检查结果占一行，用以下标签开头：

```
[MATCH] FR-1 — 用户注册端点实现正确，路径 /api/users/register POST，返回 201+JWT
[MATCH] FR-2 — 用户登录端点实现正确
[MISMATCH] FR-3 — JWT secret 从 config.py 硬编码读取，Spec 要求环境变量 YUNCAT_JWT_SECRET
[MISSING] FR-4 — 密码重置功能未实现
[MATCH] schema User — 字段 id/email/password_hash 类型正确
[MISMATCH] schema User — 多了 Spec 中没有的 phone_number 字段
[MATCH] constraint #1 — bcrypt 哈希正确使用 werkzeug.security.generate_password_hash
[MATCH] constraint #2 — 无 f-string SQL 拼接
[MATCH] constraint #3 — API Key 从环境变量读取
[PASS] verification — pytest tests/ -q -k 'test_auth' — 4 passed, exit=0
[FAIL] verification — npx tsc --noEmit — 3 errors
```

### 审计结论
在最后输出：

```
### 审计总结
- MATCH: {{match_count}} 项
- MISMATCH: {{mismatch_count}} 项
- MISSING: {{missing_count}} 项
- 验证: {{verification_passed_count}}/{{verification_total_count}} 通过
- 收敛判定: "✅ 收敛" (条件: mismatch=0 AND missing=0 AND 所有验证通过) 否则 "❌ 未收敛"
```

### 规则
1. **每个功能需求都必须覆盖**——不能跳过任何一条 FR
2. **MISMATCH 必须写明具体原因**——"实现不一致"不是合格的审查
3. **MISSING 必须指明哪个需求完全没有对应实现**
4. **不要猜测"可能实现了"**——只在代码中找到明确证据
5. **如果有 run_command 工具可用，实际执行 verification.commands**——不要只读代码判断
6. **安全约束的 MISMATCH 权重最高**——比其他不一致更严重"""


# ═══════════════════════════════════════════════════════════
# Ghost Library 重建审计 Prompt (Phase 46 E.1)
# ═══════════════════════════════════════════════════════════

GHOST_AUDIT_PROMPT = """## 角色: Ghost Library 审计员

你需要对比**原始实现**和**仅凭 Spec 重建的实现**，判断 Spec 是否足够精确
到能被一个独立的 Agent 在零额外上下文的情况下精确复现。

### Spec（唯一的真相来源）
```yaml
{spec_yaml}
```

### 原始实现（有人类/其他 Agent 的上下文辅助）
{original_impl}

### 重建实现（Agent 只看 Spec 从零构建）
{rebuilt_impl}

### 审计重点
1. 重建实现的功能是否与 Spec 的 functional_requirements 完全对齐？
2. API 契约是否与 Spec 的 openapi.paths 一致？
3. 数据模型是否与 Spec 的 schemas 一致？
4. 如果有差异 → 是 Spec 不够精确，还是重建 Agent 的失误？
5. 如果 Spec 不够精确 → 具体建议改进 Spec 的哪些描述？

### 输出
[GHOST_PASS] — Spec 足够精确，重建实现与原始等价
[GHOST_FAIL] — Spec 需要改进: ...（具体描述差距和修改建议）"""


# ═══════════════════════════════════════════════════════════
# Phase 83: FINAL_REVIEW_PROMPT — 长任务终审 Agent
# ═══════════════════════════════════════════════════════════

FINAL_REVIEW_PROMPT = """## 角色：终审员（Final Review Agent）

你是长任务的终审员。在关键节点审查执行情况，给出**结构化 JSON 判断**。
你的审查是**独立的**——只看给定上下文，不假设执行者的意图。

### 审查阶段
{stage_desc}

### 任务上下文
{task_context}

### 审查输入
{review_input}

### 审查维度
1. **目标对齐**: 当前执行/产出是否与确认目标一致？
2. **正确性**: 有无明显逻辑错误、事实错误、安全风险？
3. **完整性**: 是否遗漏关键步骤或约束？
4. **可验证性**: 产出是否可验证（有明确的验收证据）？
5. **风险**: 是否有不可逆操作、副作用、危险工具调用？

### 输出格式——严格 JSON
```json
{{
  "pass": true,
  "score": 85,
  "issues": ["问题1", "问题2"],
  "action": "proceed",
  "reason": "一句话总结判断理由"
}}
```

### action 含义
- **proceed**: 通过，继续执行
- **ask_user**: 有疑问需用户确认（将合并到确认弹窗）
- **retry**: 有可修复问题，建议重试当前步骤
- **halt**: 严重问题，建议停止执行

### 规则
1. **action 字段必填**——只能是 proceed/ask_user/retry/halt 之一；缺失或写错会被视为
   "拿不准"，交回用户而不是替你放行。pass=true 时 action 应为 proceed；pass=false 时
   action 应为 ask_user/retry/halt 之一
2. **issues 为空数组当且仅当 pass=true**
3. **score 反映整体质量**——90+ 优秀，70-89 合格，50-69 有风险，<50 不合格
4. **只输出 JSON**——不要任何解释文字"""


# ═══════════════════════════════════════════════════════════
# 辅助: 格式化 Spec 字段说明（注入到 ARCHITECT_PROMPT）
# ═══════════════════════════════════════════════════════════

def format_spec_field_descriptions() -> str:
    """将 SPEC_FIELD_DESCRIPTIONS 格式化为可注入 Prompt 的段落。"""
    lines = []
    for field, desc in SPEC_FIELD_DESCRIPTIONS.items():
        lines.append(f"### {field}\n{desc}\n")
    return "\n".join(lines)


def render_architect_prompt(task_input: str, project_structure: str = "") -> str:
    """渲染 SPEC_ARCHITECT_PROMPT——注入任务和项目结构。

    Args:
        task_input: 用户的功能需求描述
        project_structure: 项目目录结构和 import 关系摘要

    Returns: 组装后的完整 Prompt
    """
    field_descriptions = format_spec_field_descriptions()
    return SPEC_ARCHITECT_PROMPT.format(
        task_input=task_input,
        project_structure=project_structure or "（无项目结构信息——请基于功能需求自行设计目录结构）",
        field_descriptions=field_descriptions,
    )


def render_developer_prompt(spec_yaml: str, override_prompt: str = None) -> str:
    """渲染 SPEC_DEVELOPER_PROMPT——注入 Spec YAML。

    Args:
        spec_yaml: 解析后的 Spec YAML 字符串
        override_prompt: 如果非 None，替代整个 Prompt（Phase 44 修复模式）

    Returns: 组装后的完整 Prompt
    """
    if override_prompt:
        return override_prompt
    return SPEC_DEVELOPER_PROMPT.format(spec_yaml=spec_yaml)


def render_auditor_prompt(
    spec_yaml: str,
    implementation_summary: str,
    additional_context: str = "",
) -> str:
    """渲染 SPEC_AUDITOR_PROMPT——注入 Spec YAML + 实现摘要。

    Args:
        spec_yaml: Spec YAML 字符串
        implementation_summary: Developer 的输出摘要（文件列表 + 关键代码片段）
        additional_context: 额外上下文（如上一轮审计发现）

    Returns: 组装后的完整 Prompt
    """
    return SPEC_AUDITOR_PROMPT.format(
        spec_yaml=spec_yaml,
        implementation_summary=implementation_summary or "（无实现摘要——请直接读取代码文件进行审计）",
        additional_context=additional_context or "（无补充上下文）",
    )


# ── Phase 83: FinalReview 渲染 ──

_FINAL_REVIEW_STAGE_DESC = {
    "pre_execution": "plan_step 启动前——审查该步骤的 prompt 是否与目标对齐、是否遗漏依赖、是否有明显错误",
    "post_execution": "plan_step 完成后——审查产出是否满足验收标准、是否有错误、是否完整",
    "pre_dangerous_tool": "危险工具（write_file/run_command/delete_file 等）执行前——审查工具参数是否合理、是否有安全风险、是否不可逆",
}


def render_final_review_prompt(stage: str, task_context: str, review_input: str) -> str:
    """渲染 FINAL_REVIEW_PROMPT——注入阶段描述 + 任务上下文 + 审查输入。

    Args:
        stage: pre_execution / post_execution / pre_dangerous_tool
        task_context: 任务上下文摘要（目标/步骤/验收标准）
        review_input: 审查的具体内容（step_prompt / output / tool_name+args）

    Returns: 组装后的完整 Prompt
    """
    stage_desc = _FINAL_REVIEW_STAGE_DESC.get(stage, "通用审查")
    return FINAL_REVIEW_PROMPT.format(
        stage_desc=stage_desc,
        task_context=task_context or "（无任务上下文）",
        review_input=review_input or "（无审查输入）",
    )


# ═══════════════════════════════════════════════════════════
# Phase 85: STANDARDS_LOOKUP — 标准/文档查询 Agent
# ═══════════════════════════════════════════════════════════

STANDARDS_LOOKUP_QUERY_PROMPT = """## 角色：标准查询生成器（Standards Lookup Query Generator）

你的任务是把一个**技术问题**转化为可用于文档检索的**英文查询**。

### 问题领域
{domain}

### 问题描述
{problem_description}

### 输出格式——严格 JSON
```json
{{
  "libraries": ["候选库名1", "候选库名2"],
  "queries": ["英文查询关键词1", "英文查询关键词2"]
}}
```

### 规则
1. **libraries**：列出可能相关的库/框架名（小写，npm/pypi 包名规范），最多 3 个；不确定则空数组
2. **queries**：用英文短语描述要查的关键 API/概念，最多 3 条；每条 ≤ 8 个单词
3. **不要解释**——只输出 JSON
4. **不要编造**——只基于问题描述提取，不添加未提及的库"""

STANDARDS_LOOKUP_ADVICE_PROMPT = """## 角色：标准查询顾问（Standards Lookup Advisor）

你刚通过文档检索获得了一批**权威资料**。请基于这些资料，给出**可操作的建议**。

### 原始问题
{problem_description}

### 检索到的文档片段
{snippets}

### 输出格式——严格 JSON
```json
{{
  "summary": "一句话总结找到的相关标准/API",
  "recommendations": ["建议1", "建议2", "建议3"],
  "sources": ["来源1标题", "来源2标题"]
}}
```

### 规则
1. **summary**：≤ 100 字，说明检索到的资料覆盖了什么
2. **recommendations**：3-5 条**具体**建议（含 API 名/参数/调用方式），针对原始问题
3. **sources**：列出引用的库名 + 文档标题（来自 snippets）
4. **绝对禁止编造 API**——如果 snippets 没覆盖问题，summary 明说"未找到直接相关文档"，recommendations 给出"建议升级查询关键词"等元建议
5. **只输出 JSON**——不要任何解释文字"""


# ── Phase 85: StandardsLookup 渲染 ──

def render_standards_lookup_query_prompt(
    problem_description: str, domain: str = "general",
) -> str:
    """渲染查询生成 prompt——注入问题描述 + 领域。

    Args:
        problem_description: 技术问题描述（错误信息/不确定点）
        domain: 问题领域（python/frontend/database/general 等），仅作上下文提示

    Returns: 组装后的完整 Prompt
    """
    return STANDARDS_LOOKUP_QUERY_PROMPT.format(
        domain=domain or "general",
        problem_description=problem_description or "（无问题描述）",
    )


def render_standards_lookup_advice_prompt(
    problem_description: str, snippets: str,
) -> str:
    """渲染建议生成 prompt——注入问题描述 + 检索片段。

    Args:
        problem_description: 原始技术问题描述
        snippets: 检索到的文档片段拼接（含库名 + 内容摘要），每段以 "---" 分隔

    Returns: 组装后的完整 Prompt
    """
    return STANDARDS_LOOKUP_ADVICE_PROMPT.format(
        problem_description=problem_description or "（无问题描述）",
        snippets=snippets or "（未检索到任何文档片段）",
    )
