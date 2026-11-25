"""prompts/workflow_templates.py —— 内置工作流模板

每个模板是一个 {name, display_name, description, graph: {nodes, edges}} 字典。
在 lifespan 中自动插入到 workflows 表 (is_template=1)。
name 为内部标识（唯一），display_name 为前端显示名（中文）。
"""
__version__ = "1.1.0"

BUILTIN_TEMPLATES = [
    {
        "name": "zh_data_analysis",
        "display_name": "数据分析",
        "description": "数据分析工作流：读取CSV → 分析数据 → 生成图表 → 输出报告",
        "graph": {
            "nodes": [
                {"id": "read_csv", "type": "default", "prompt": "读取并解析数据文件 {{input}}"},
                {"id": "analyze", "type": "default", "prompt": "分析数据趋势和异常值，找出关键洞察"},
                {"id": "generate_chart", "type": "default", "prompt": "基于分析结果生成可视化图表代码（使用 matplotlib/echarts）"},
                {"id": "write_report", "type": "default", "prompt": "汇总分析结果和图表，生成中文分析报告，包含结论和行动建议"},
            ],
            "edges": [
                {"from": "read_csv", "to": "analyze"},
                {"from": "analyze", "to": "generate_chart"},
                {"from": "generate_chart", "to": "write_report"},
            ],
        },
    },
    {
        "name": "zh_code_review",
        "display_name": "代码审查",
        "description": "代码审查工作流：读取代码 → 静态检查 → 审查逻辑 → 生成建议",
        "graph": {
            "nodes": [
                {"id": "read_code", "type": "default", "prompt": "读取并理解目标代码 {{input}}，梳理模块结构和依赖关系"},
                {"id": "lint", "type": "default", "prompt": "检查代码风格、命名规范和潜在错误（语法/类型/导入）"},
                {"id": "review", "type": "default", "prompt": "审查架构设计、逻辑正确性、安全性、性能和可维护性"},
                {"id": "generate_suggestions", "type": "default", "prompt": "汇总所有发现，按严重度排序（严重/高/中/低），生成结构化的代码审查报告和改进建议"},
            ],
            "edges": [
                {"from": "read_code", "to": "lint"},
                {"from": "read_code", "to": "review"},
                {"from": "lint", "to": "generate_suggestions"},
                {"from": "review", "to": "generate_suggestions"},
            ],
        },
    },
    {
        "name": "zh_doc_generation",
        "display_name": "文档生成",
        "description": "文档生成工作流：读取源码 → 提取API → 生成文档",
        "graph": {
            "nodes": [
                {"id": "read_source", "type": "default", "prompt": "读取并理解源代码结构 {{input}}，识别模块、类、函数和公共接口"},
                {"id": "extract_api", "type": "default", "prompt": "提取所有公开的 API、函数签名、参数说明和返回值类型"},
                {"id": "write_readme", "type": "default", "prompt": "基于 API 提取结果，生成完整的 README 中文文档，含安装说明、使用示例和 API 参考"},
            ],
            "edges": [
                {"from": "read_source", "to": "extract_api"},
                {"from": "extract_api", "to": "write_readme"},
            ],
        },
    },
    {
        "name": "zh_craft_fix_loop",
        "display_name": "修复循环",
        "description": "CRAFT 修复循环：发现 → 修复 → 验证 → 判决（不通过则重试）",
        "graph": {
            "nodes": [
                {"id": "discover", "type": "default", "prompt": "诊断问题根因：分析 {{input}}，输出发现的 bug 或需修复的内容，包括复现条件和影响范围"},
                {"id": "fix", "type": "default", "prompt": "根据发现的问题，以 diff 格式输出具体修复代码。确保不引入新问题"},
                {"id": "verify", "type": "default", "prompt": "验证修复是否正确：检查代码逻辑、语法、边界条件、回归测试"},
                {"id": "decide", "type": "human", "prompt": "审核修复结果：是否通过？拒绝则返回 fix 重试"},
            ],
            "edges": [
                {"from": "discover", "to": "fix"},
                {"from": "fix", "to": "verify"},
                {"from": "verify", "to": "decide"},
            ],
        },
    },
    {
        "name": "zh_auto_fix",
        "display_name": "自动修复",
        "description": "自动 Lint/Test 修复：编辑 → 检查 → 测试 → 自动修复 → 判决（最多 3 次重试）",
        "graph": {
            "nodes": [
                {"id": "edit", "type": "default",
                 "prompt": "根据需求 {{input}} 执行代码修改。用 diff 格式输出，不要重写整个文件"},
                {"id": "lint", "type": "default",
                 "prompt": "对修改的文件运行 lint 检查。Python 用 ruff，TypeScript 用 tsc --noEmit。输出所有错误"},
                {"id": "test", "type": "default",
                 "prompt": "运行测试套件。Python 用 pytest，TypeScript 用 vitest。输出失败详情"},
                {"id": "decide", "type": "human",
                 "prompt": "lint 和 test 是否全部通过？不通过则返回 edit 节点重试（最多3次）"},
            ],
            "edges": [
                {"from": "edit", "to": "lint"},
                {"from": "lint", "to": "test"},
                {"from": "test", "to": "decide"},
            ],
        },
    },
    {
        "name": "zh_architect_editor",
        "display_name": "架构师-工程师协作",
        "description": "双模型协作：架构师规划 → 工程师实现 → 审查者验证",
        "graph": {
            "nodes": [
                {"id": "architect", "type": "default",
                 "model": "deepseek-v4-flash",
                 "prompt": "## 角色：架构师\n分析需求 {{input}}。理解现有代码结构。生成执行计划：\n1. 需要修改哪些文件\n2. 每个文件改什么\n3. 接口如何变更\n输出 JSON: {\"plan\": \"...\", \"files\": [\"文件路径\"], \"risks\": [\"风险点\"]}"},
                {"id": "editor", "type": "default",
                 "model": "deepseek-v4-pro",
                 "prompt": "## 角色：高级工程师\n按照架构师的计划执行代码修改。逐个文件生成 unified diff 格式的修改。修改完成后自动运行 lint + test 验证"},
                {"id": "reviewer", "type": "default",
                 "model": "deepseek-v4-flash",
                 "prompt": "## 角色：代码审查者\n验证工程师的修改是否符合架构师计划：\n1. 每个计划中的文件都被修改了吗\n2. 是否有计划外的修改\n3. 接口变更与计划一致吗\n如果不一致，指出具体差异"},
            ],
            "edges": [
                {"from": "architect", "to": "editor"},
                {"from": "editor", "to": "reviewer"},
            ],
        },
    },
    {
        "name": "zh_speckit_pipeline",
        "display_name": "需求→实现流水线",
        "description": "speckit 规格→实现：指定规格 → 澄清 → 规划 → 分解任务 → 分析 → 实现",
        "graph": {
            "nodes": [
                {"id": "specify", "type": "default",
                 "prompt": "## 角色：规格分析师\n将需求「{{input}}」转换为结构化功能规格 JSON：\n{\n  \"user_roles\": [\"谁会使用\"],\n  \"functional_requirements\": [\"功能1\", \"功能2\"],\n  \"success_criteria\": [\"如何判断成功\"],\n  \"non_functional\": {\"performance\": \"...\", \"security\": \"...\"}\n}\n只输出 JSON"},
                {"id": "clarify", "type": "human",
                 "prompt": "## 检查模糊点\n阅读上述 spec。对每个用户角色、功能边界、成功标准中的不明确处，生成具体追问。最多 5 个问题。用户回答后继续"},
                {"id": "plan", "type": "default",
                 "model": "deepseek-v4-flash",
                 "prompt": "## 角色：技术方案设计师\n基于 spec 和用户回答，生成实现方案 JSON：\n{\n  \"files_to_create\": [{\"path\": \"...\", \"purpose\": \"...\"}],\n  \"files_to_modify\": [{\"path\": \"...\", \"changes\": \"...\"}],\n  \"data_flow\": \"数据流转\",\n  \"dependencies\": [\"依赖\"]\n}\n只输出 JSON"},
                {"id": "tasks", "type": "default",
                 "model": "deepseek-v4-flash",
                 "prompt": "## 角色：任务分解师\n将 plan 分解为独立任务列表 JSON：\n[\n  {\"id\": 1, \"task\": \"做什么\", \"file\": \"涉及的文件\",\n   \"complexity\": \"S|M|L\", \"depends_on\": [],\n   \"acceptance_criteria\": [\"验收标准\"]}\n]\n按依赖排序，无依赖的任务可并行。只输出 JSON"},
                {"id": "analyze", "type": "default",
                 "model": "deepseek-v4-flash",
                 "prompt": "## 角色：一致性检查员\n对比 spec → plan → tasks 三方一致性：\n1. plan 是否覆盖了全部 functional_requirements\n2. tasks 是否覆盖了 plan 中列出的全部文件\n3. 是否有遗漏的功能点\n4. 任务粒度是否合适\n输出不一致项和修正建议"},
                {"id": "implement", "type": "default",
                 "model": "deepseek-v4-pro",
                 "prompt": "## 角色：执行工程师\n按照 tasks 列表逐个执行。完成一个标记 done。遇阻塞记录原因并跳过。修改代码时使用 diff 格式输出。每次修改后自动运行测试验证"},
            ],
            "edges": [
                {"from": "specify", "to": "clarify"},
                {"from": "clarify", "to": "plan"},
                {"from": "plan", "to": "tasks"},
                {"from": "tasks", "to": "analyze"},
                {"from": "analyze", "to": "implement"},
            ],
        },
    },
    {
        "name": "zh_security_review",
        "display_name": "安全审查",
        "description": "安全审查：专家审查 → 生成安全报告",
        "graph": {
            "nodes": [
                {"id": "review", "type": "default", "prompt": "## 角色：安全审查专家\n对项目「{{input}}」进行全面安全审查：\n1. 密钥管理——硬编码的密钥/密码/token\n2. 注入风险——SQL/命令/XSS/路径遍历\n3. 权限控制——认证/授权/CSRF\n4. 依赖漏洞——检查已知 CVE\n输出：按严重度排序的发现列表（严重/高/中/低）+ 修复方案"},
                {"id": "report", "type": "default", "prompt": "汇总审查发现，生成结构化安全报告：\n- 总览（审查范围/文件数/发现问题数）\n- 严重项（必须立即修复）\n- 高优项（本次发布前修复）\n- 建议修复顺序和时间估算"},
            ],
            "edges": [{"from": "review", "to": "report"}],
        },
    },
    {
        "name": "zh_tdd_loop",
        "display_name": "TDD 测试驱动开发",
        "description": "TDD 循环：写测试（红）→ 实现（绿）→ 重构（优化）",
        "graph": {
            "nodes": [
                {"id": "write_test", "type": "default", "prompt": "## TDD Step 1: RED 红\n为功能「{{input}}」编写测试用例。遵循 AAA（Arrange-Act-Assert）结构，覆盖正常路径和边界条件"},
                {"id": "implement", "type": "default", "prompt": "## TDD Step 2: GREEN 绿\n编写最小实现代码使测试通过。只写让测试变绿所需的最少代码——不追求完美"},
                {"id": "refactor", "type": "default", "prompt": "## TDD Step 3: REFACTOR 重构\n在测试全绿的前提下优化代码：消除重复、改善命名、提取公共逻辑。确保重构后测试仍然通过"},
            ],
            "edges": [
                {"from": "write_test", "to": "implement"},
                {"from": "implement", "to": "refactor"},
            ],
        },
    },
    {
        "name": "zh_diagnose_loop",
        "display_name": "诊断排查",
        "description": "诊断循环：复现 → 最小化 → 假设 → 验证 → 修复",
        "graph": {
            "nodes": [
                {"id": "reproduce", "type": "default", "prompt": "复现 bug「{{input}}」——确认问题确实存在。描述复现步骤和观察到的行为"},
                {"id": "minimise", "type": "default", "prompt": "最小化复现——去掉无关代码/数据，找到触发 bug 的最小条件"},
                {"id": "hypothesis", "type": "default", "prompt": "提出假设——根据最小复现条件，分析可能的根因。列出 2-3 个最可能的假设"},
                {"id": "instrument", "type": "default", "prompt": "仪器化——在最可能的根因位置加日志/断点/断言，收集证据验证假设"},
                {"id": "fix", "type": "default", "prompt": "修复——基于验证后的假设，编写修复代码。修复后用复现步骤确认问题已解决"},
            ],
            "edges": [
                {"from": "reproduce", "to": "minimise"},
                {"from": "minimise", "to": "hypothesis"},
                {"from": "hypothesis", "to": "instrument"},
                {"from": "instrument", "to": "fix"},
            ],
        },
    },
    {
        "name": "zh_prototype",
        "display_name": "快速原型",
        "description": "快速原型：生成 → 评估",
        "graph": {
            "nodes": [
                {"id": "generate", "type": "default", "prompt": "快速生成「{{input}}」的工作原型。不追求完美——只要可运行、可演示即可。输出完整代码"},
                {"id": "eval", "type": "default", "prompt": "评估原型：核心需求满足度、技术可行性、主要风险和未解决问题、到达生产就绪的差距"},
            ],
            "edges": [{"from": "generate", "to": "eval"}],
        },
    },
    {
        "name": "zh_triage",
        "display_name": "Bug 分诊",
        "description": "Bug 分诊：分诊 → 分配 → 修复 → 验证",
        "graph": {
            "nodes": [
                {"id": "triage", "type": "default", "prompt": "对 Bug「{{input}}」进行分诊：\n1. 严重度（P0=阻断/P1=严重/P2=一般/P3=建议）\n2. 影响范围\n3. 是否已知问题\n输出分诊报告"},
                {"id": "assign", "type": "human", "prompt": "审核分诊结果：分配给谁？接受还是拒绝这个优先级？"},
                {"id": "fix", "type": "default", "model": "deepseek-v4-pro", "prompt": "修复此 bug——基于分诊严重度采用相应的严谨度"},
                {"id": "verify", "type": "default", "prompt": "验证修复：复现步骤是否不再触发、是否引入新问题、相关测试是否通过"},
            ],
            "edges": [
                {"from": "triage", "to": "assign"},
                {"from": "assign", "to": "fix"},
                {"from": "fix", "to": "verify"},
            ],
        },
    },
    {
        "name": "zh_dead_code_cleanup",
        "display_name": "死代码清理",
        "description": "死代码清理：扫描 → 移除 → 验证",
        "graph": {
            "nodes": [
                {"id": "scan", "type": "default", "prompt": "扫描项目「{{input}}」中的死代码：未被引用的模块/函数/类、不可达分支、已注释代码块、未使用变量"},
                {"id": "remove", "type": "default", "prompt": "安全移除扫描发现的死代码。使用 diff 格式输出。每次移除后确认不破坏任何引用"},
                {"id": "verify", "type": "default", "prompt": "验证清理：构建是否通过、测试是否全绿、是否有新的导入错误"},
            ],
            "edges": [{"from": "scan", "to": "remove"}, {"from": "remove", "to": "verify"}],
        },
    },
    {
        "name": "zh_coverage_boost",
        "display_name": "测试覆盖率提升",
        "description": "测试覆盖率提升：分析缺口 → 生成测试 → 验证",
        "graph": {
            "nodes": [
                {"id": "analyze", "type": "default", "prompt": "分析「{{input}}」的测试覆盖率缺口：识别覆盖最低的模块/文件/函数"},
                {"id": "generate", "type": "default", "prompt": "为覆盖率最低的模块生成测试：优先覆盖公开 API 边界条件、错误处理路径、高复杂度分支"},
                {"id": "verify", "type": "default", "prompt": "验证：新测试是否全过、覆盖率是否提升、是否引入 flaky"},
            ],
            "edges": [{"from": "analyze", "to": "generate"}, {"from": "generate", "to": "verify"}],
        },
    },
    {
        "name": "zh_prp_plan",
        "display_name": "方案规划",
        "description": "PRP 规划：需求分析 → 研究 → 方案 → 人工审核",
        "graph": {
            "nodes": [
                {"id": "analyze", "type": "default", "prompt": "分析需求「{{input}}」——提取关键需求、约束条件和成功标准"},
                {"id": "research", "type": "default", "prompt": "研究可行方案——搜索类似实现、评估库/工具、对比技术路线"},
                {"id": "plan", "type": "default", "model": "deepseek-v4-flash", "prompt": "生成实施方案：文件变更清单、数据流设计、接口定义、依赖关系、风险评估"},
                {"id": "validate", "type": "human", "prompt": "审核计划：是否可行？是否有遗漏？是否需要调整？"},
            ],
            "edges": [
                {"from": "analyze", "to": "research"},
                {"from": "research", "to": "plan"},
                {"from": "plan", "to": "validate"},
            ],
        },
    },
    {
        "name": "zh_prp_pr",
        "display_name": "生成 PR",
        "description": "生成 PR：分析 diff → 写摘要 → 推送",
        "graph": {
            "nodes": [
                {"id": "diff", "type": "default", "prompt": "获取当前分支的完整 diff。分析变更内容——新增/修改/删除的文件、关键代码段"},
                {"id": "summary", "type": "default", "prompt": "生成 PR 摘要：标题、概述、变更清单、测试计划、风险评估。输出 Markdown"},
                {"id": "push", "type": "default", "prompt": "检查是否准备好 push：测试通过、无 lint 错误、commit message 规范。通过则输出推送命令"},
            ],
            "edges": [{"from": "diff", "to": "summary"}, {"from": "summary", "to": "push"}],
        },
    },
    {
        "name": "zh_quality_gate",
        "display_name": "质量门禁",
        "description": "质量门禁：lint 检查 → 跑测试 → 安全扫描 → 出具报告",
        "graph": {
            "nodes": [
                {"id": "lint", "type": "default", "prompt": "运行 lint 检查（ruff/tsc/eslint）。列出所有错误"},
                {"id": "test", "type": "default", "prompt": "运行测试套件（pytest/vitest）。输出测试结果和覆盖率"},
                {"id": "security", "type": "default", "prompt": "安全扫描——检查是否引入新的安全问题"},
                {"id": "report", "type": "default", "prompt": "汇总门禁结果：判定 ready_to_merge 或 blocked"},
            ],
            "edges": [
                {"from": "lint", "to": "test"},
                {"from": "test", "to": "security"},
                {"from": "security", "to": "report"},
            ],
        },
    },
    {
        "name": "zh_multi_model_plan",
        "display_name": "多模型方案",
        "description": "多模型方案生成：3 个 Agent 并行 → 合并 → 综合",
        "graph": {
            "nodes": [
                {"id": "fan_out", "type": "default", "prompt": "将「{{input}}」分发给 3 个独立 Agent（侧重可行性/创新性/风险评估）。各自独立生成方案"},
                {"id": "merge", "type": "default", "prompt": "合并 3 个 Agent 的方案——去重、找冲突、找互补点"},
                {"id": "synthesize", "type": "default", "model": "deepseek-v4-pro", "prompt": "从合并方案中综合最优方案——取各方案精髓融合为连贯的整体方案"},
            ],
            "edges": [{"from": "fan_out", "to": "merge"}, {"from": "merge", "to": "synthesize"}],
        },
    },
    # Phase 33: AI 自动配置工作流
    {
        "name": "zh_auto_install_skill",
        "display_name": "自动安装 Skill",
        "description": "AI 自动配置：搜索 GitHub 或 ClawHub → 下载 skill.yaml → 安全审查 → 安装",
        "graph": {
            "nodes": [
                {"id": "search", "type": "default", "prompt": "搜索「{{input}}」相关的 Skill。用 search_files 或 web 搜索 GitHub/ClawHub，找到最匹配的 skill.yaml URL"},
                {"id": "review_security", "type": "default", "prompt": "审查找到的 Skill 的安全性：检查 prompt 内容是否有注入风险、是否有越权操作声明。用 POST /api/v1/skills/check-security 验证"},
                {"id": "install", "type": "default", "prompt": "安全审查通过后，用 install_skill_from_url 工具安装 Skill。如果审查未通过，向用户解释风险并建议修改后重试"},
            ],
            "edges": [{"from": "search", "to": "review_security"}, {"from": "review_security", "to": "install"}],
        },
    },
    {
        "name": "zh_auto_configure_mcp",
        "display_name": "自动配置 MCP",
        "description": "AI 自动配置：分析 GitHub MCP 仓库 → 提取配置命令 → 安装到 Yuncat",
        "graph": {
            "nodes": [
                {"id": "analyze_repo", "type": "default", "prompt": "分析「{{input}}」GitHub 仓库。用 web 搜索或直接 fetch README。提取 npx 启动命令、环境变量、参数说明"},
                {"id": "configure", "type": "default", "prompt": "用 configure_mcp 工具自动配置 MCP 服务器。参数：source=GitHub URL, server_name=仓库名"},
                {"id": "verify", "type": "default", "prompt": "验证 MCP 配置是否已写入。用 read_file 读取 config 文件检查 JSON 结构是否正确"},
            ],
            "edges": [{"from": "analyze_repo", "to": "configure"}, {"from": "configure", "to": "verify"}],
        },
    },
    {
        "name": "zh_auto_setup_project",
        "display_name": "项目一键配置",
        "description": "AI 自动配置项目：检测语言 → 推荐 Skills → 配置 MCP → 初始化规则 → 生成 CLAUDE.md",
        "graph": {
            "nodes": [
                {"id": "detect_lang", "type": "default", "prompt": "检测「{{input}}」项目的技术栈：用 list_dir 看项目根目录，识别语言/框架（Python/TypeScript/Go/Rust 等）"},
                {"id": "recommend_skills", "type": "default", "prompt": "根据检测到的技术栈，用 GET /api/v1/skills/recommend?language=xxx 获取推荐的 Skill。列出推荐结果并建议安装"},
                {"id": "install_skills", "type": "default", "prompt": "安装推荐的 Skills。搜索 GitHub/ClawHub 找到对应的 skill.yaml 并用 install_skill_from_url 安装。至少安装 2 个最相关的"},
                {"id": "setup_rules", "type": "default", "prompt": "初始化项目规则：根据技术栈创建 .yuncat/rules/ 目录，写入安全规则、编码规范、测试要求"},
                {"id": "generate_claude_md", "type": "default", "prompt": "生成 CLAUDE.md 格式的项目上下文文件。包含：技术栈、构建命令、项目结构、编码规范、测试框架。写入项目根目录"},
            ],
            "edges": [
                {"from": "detect_lang", "to": "recommend_skills"},
                {"from": "recommend_skills", "to": "install_skills"},
                {"from": "install_skills", "to": "setup_rules"},
                {"from": "setup_rules", "to": "generate_claude_md"},
            ],
        },
    },
    # Phase 43 G2.3: 混合协作模板 — 跨后端工作流
    {
        "name": "zh_mixed_search_code_review",
        "display_name": "搜索→编码→审查 (OC+原生 混合)",
        "description": "OC 搜索最新资料 → 原生 Yuncat 根据搜索结果编码并审查",
        "graph": {
            "nodes": [
                {"id": "search", "type": "oc_search", "prompt": "搜索「{{input}}」相关的最新资料、文档和实现方案"},
                {"id": "code", "type": "default", "prompt": "根据搜索结果实现代码。严格按搜索到的文档/API 规范编写"},
                {"id": "review", "type": "default", "prompt": "审查生成的代码。检查：是否符合规范、是否有 bug、是否完整"},
            ],
            "edges": [{"from": "search", "to": "code"}, {"from": "code", "to": "review"}],
        },
    },
    {
        "name": "zh_mixed_vision_spec_implement",
        "display_name": "视觉→Spec→实现 (OC+原生 混合)",
        "description": "OC 视觉分析设计稿 → 原生 Yuncat 生成 Spec 并实现代码",
        "graph": {
            "nodes": [
                {"id": "vision", "type": "oc_vision", "prompt": "分析「{{input}}」中的设计稿/截图。提取：UI元素·布局·颜色·字体·交互逻辑"},
                {"id": "spec", "type": "default", "prompt": "根据视觉分析结果生成 Spec。定义组件树、数据模型、API 契约"},
                {"id": "implement", "type": "default", "prompt": "严格按照 Spec 实现代码。创建/修改指定文件"},
            ],
            "edges": [{"from": "vision", "to": "spec"}, {"from": "spec", "to": "implement"}],
        },
    },
    {
        "name": "zh_api_design",
        "display_name": "API 设计流程",
        "description": "API 设计：需求 → 契约 → 实现 → 文档 → 测试",
        "graph": {
            "nodes": [
                {"id": "requirement", "type": "default", "prompt": "## API 设计 Step 1: 需求分析\n分析「{{input}}」的 API 需求：\n1. 识别资源和实体（名词）\n2. 确定操作类型（CRUD）\n3. 列出输入/输出字段\n4. 标注权限要求和速率限制\n输出：资源列表 + 操作矩阵"},
                {"id": "contract", "type": "default", "prompt": "## API 设计 Step 2: 契约定义\n基于需求定义 API 契约：\n1. 资源命名用复数名词（/users 而非 /getUsers）\n2. HTTP 方法语义严格：GET=读取, POST=创建, PUT=全量更新, PATCH=部分更新, DELETE=删除\n3. 状态码：201 Created, 204 No Content, 400/422 参数错误, 409 冲突\n4. 分页响应包含 next/prev 链接\n5. 错误格式统一：error 含 code/message/details\n输出：OpenAPI 3.0 schema 或等效契约文档"},
                {"id": "implement", "type": "default", "prompt": "## API 设计 Step 3: 实现\n按契约实现 API 端点：\n1. 路由层——参数解析 + 调用 service\n2. service 层——业务逻辑 + 调用 repository\n3. repository 层——数据访问\n4. 输入验证在边界处（pydantic/zod）\n5. 错误处理统一中间件\n输出：可运行的代码 + 路由注册"},
                {"id": "document", "type": "default", "prompt": "## API 设计 Step 4: 文档\n生成 API 文档：\n1. 每个端点的示例请求/响应\n2. 认证方式说明\n3. 错误码对照表\n4. 版本控制策略\n5. SDK 调用示例（curl + 至少一种语言）\n输出：README 片段或独立 API.md"},
                {"id": "test", "type": "default", "prompt": "## API 设计 Step 5: 测试\n编写 API 测试：\n1. 正常路径——每个端点的成功场景\n2. 边界条件——空输入/超长/特殊字符\n3. 错误路径——权限不足/资源不存在/参数错误\n4. 幂等性——重复请求的行为\n5. 性能基准——响应时间上限\n输出：可运行的测试套件 + 覆盖率报告"},
            ],
            "edges": [
                {"from": "requirement", "to": "contract"},
                {"from": "contract", "to": "implement"},
                {"from": "implement", "to": "document"},
                {"from": "document", "to": "test"},
            ],
        },
    },
]
