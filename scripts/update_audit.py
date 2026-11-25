"""Append new sections to AUDIT_AND_GAPS.md"""
import os

base_path = r"D:\Yuncat\docs\AUDIT_AND_GAPS.md"
tmp_path = r"D:\Yuncat\docs\AUDIT_AND_GAPS_NEW.md"

with open(base_path, "r", encoding="utf-8") as f:
    content = f.read()

idx = content.rfind("| `routes/skills.py` | `800` |")
end = content.index("\n", idx) + 1
base = content[:end]

new_sections = r"""

---

## 七、思考链与行动链——差距与修复方案

### 7.1 当前状态

ChatView.tsx 中的渲染顺序（每条 assistant 消息）：
Plan 小进度条 → Thinking block(折叠) → ExecutionTimeline → ResultRenderer → 状态标签

### 7.2 差距详细

| # | 问题 | 现状 | Claude Code 对标 | 等级 |
|---|------|------|-----------------|:--:|
| 1 | Thinking 流式实时展示 | 运行中 hidden，结束后才可见 | 逐字流式输出 | 🔴 高 |
| 2 | Thinking 折叠状态 | 内联 hidden 属性，React re-render 丢失 | React state 控制 | 🟠 中 |
| 3 | ThinkingBlock 组件 | Phase 20 已开发但 ChatView 未使用 | — | 🟡 低 |
| 4 | 工具参数可读性 | 150 字符硬截断，无复制按钮 | 格式化 JSON，可复制 | 🟠 中 |
| 5 | 工具结果可展开 | 2000 字符硬截断，max-h-40 | 完整可展开/复制 | 🟠 中 |
| 6 | 多轮 tool call 步骤编号 | 仅有 index 序号，toolRound 传入但未渲染 | Round N · Tool M/K | 🟠 中 |
| 7 | Plan 步骤实时进度面板 | 仅在任务完成后显示；运行时只有小进度条 | 运行时持续可见 | 🔴 高 |
| 8 | Plan 步骤状态流转 | plan_step_start/done 消息触发路径不明确 | pending→running→done 实时更新 | 🟠 中 |
| 9 | 并发工具状态 | currentTool 单例 | 多个并行工具卡片 | 🟡 低 |
| 10 | 思考→行动因果关联 | thinking 和 tool timeline 是独立区块 | 思维文本紧接关联工具调用 | 🟡 低 |
| 11 | TaskDetail 工具检测 | 正则从 output 文本检测，可能误检 | 结构化 WS 数据 | 🟠 中 |

### 7.3 修复方案

```
7.1  Thinking 流式实时展示     → ChatView 在 running 状态也渲染 thinking 累积内容
7.2  使用 ThinkingBlock 组件    → ChatView 导入 ThinkingBlock.tsx 替换内联
7.3  工具参数格式化             → ExecutionTimeline JSON.stringify(obj,null,2)+复制按钮
7.4  渲染 toolRound             → ExecutionTimeline 头部显示 "Round N"
7.5  PlanProgress 运行时显示    → ChatView running+planSteps>0 时渲染 PlanProgress
7.6  每步推送 plan 步骤         → agent_loop.py 工具回调发送 plan_step_start/done
7.7  currentTool → currentTools[] → 支持并发工具状态展示
7.8  TaskDetail 改用结构化数据  → 替换 detectToolCalls() 正则检测
```

---

## 八、长任务与监管机制——差距与修复方案

### 8.1 当前已实现（基础较好）

13 个后台 Loop 对标 OpenAI Symphony OTP Supervisor。三个级别的监督者：
- 交叉审查 (DeliberationService 阶段 2)：每角色交叉审查其他角色提案
- Adversarial Reviewer (dynamic_orchestrator.py)：双后端对抗审查
- Spec Loop 审计 (deliberation_spec.py)：双通道审计（自然语言 + 自动验证）
- Ghost Library (deliberation_service.py)：不同 Provider 重建验证
- ReviewPipeline (review_pipeline.py)：周期性架构铁律审计

自动纠错三层：工具级重试 → Spec 级 auto-fix 回环(最多3次) → 故障分析注入下一轮

### 8.2 长任务中断点（共 10 个）

| # | 中断源 | 超时值 | 影响 |
|---|--------|:--:|------|
| 1 | Healer 30min 超时 | 1800s | running 任务被标记 failed |
| 2 | 单角色 propose() 超时 | 120s | 讨论角色被放弃 |
| 3 | Spec Developer 超时 | 360s | 实现阶段中断 |
| 4 | Spec Auditor 超时 | 240s | 审计中断 |
| 5 | 验证命令超时 | 60s | 单条命令中断 |
| 6 | Supervisor shutdown 超时 | 10s/loop | 优雅关闭丢弃 |
| 7 | 进程崩溃 | — | healer 30min 后才清理 |
| 8 | 上下文窗口溢出 | — | API 调用失败 |
| 9 | cancelling 残桩 | 300s | 取消中间态停留 5min |
| 10 | 僵局检测 | 5次空循环 | 自动退出 |

### 8.3 Overseer Agent 需求分析

用户需要一个专门的监察 Agent：
- 在长任务/复杂任务运行时，独立观察其他 Agent 的行动
- 检查行动是否合理、是否有更好的方法
- 不合理时向发起者提出建议（不直接阻止，而是建议）
- 可在关键节点（工具调用前/后、round 结束）触发审查

当前最接近的实现：AdversarialReviewer（只在动态编排审查阶段触发，非持续运行）。

### 8.4 修复方案

```
8.1  Overseer Agent Loop          → 每 N 轮/每 M 分钟独立审查运行中任务
8.2  Healer 长任务白名单           → plan.md 存在的任务不受 30min 限制
8.3  僵局检测改进                  → 有 thinking/reasoning 时不计入空循环
8.4  cancelling → 立即 cancelled    → 移除 5min 中间态
8.5  监管建议注入对话              → Overseer 建议以系统消息注入下一轮
```

---

## 九、桌面操控能力——差距分析

### 9.1 两套渠道并存

| 渠道 | 工具数 | 实现 | 优势 | 劣势 |
|------|:--:|------|------|------|
| Yuncat 原生 desktop_control | 7 | Python ctypes + user32.dll SendInput | 无额外延迟、HID 驱动级 | 无 drag/window_list/window_focus |
| OpenClaw peekaboo_* | 10 | OC Gateway 代理 | 功能完整、AI 视觉引擎 | 额外 HTTP 延迟 |

Hermes 可通过 hermes_bridge.py 的 OC_TOOLS 注入调用所有 peekaboo_* 工具。

### 9.2 差距

| 问题 | 详情 |
|------|------|
| 两套接口不统一 | Agent 需知道用 desktop_control 还是 peekaboo_* |
| 视觉分析未串联 | screenshot → openclaw_vision 需两步调用 |
| locate 精度有限 | 简单滑动窗口模板匹配，非 AI 特征匹配 |
| 无 OCR 工具 | 有视觉分析但无独立 OCR |
| 平台锁定 | desktop_control 仅 Windows |

### 9.3 修复方案

```
9.1  统一 desktop 工具接口       → 废弃 desktop_control，统一走 peekaboo_*
9.2  screenshot→vision 自动串联  → 工具内部自动调用 openclaw_vision
9.3  人类操作模拟优化             → 加随机延迟、Bezier 曲线移动轨迹
```

---

## 十、消息发送延迟优化（乐观 UI）

### 10.1 当前流程

```
用户按回车 → parseReferences → 3个并行API(resolveRefs) → new session HTTP → WS submitTask
              ↑ 1-3秒网络延迟 ↑
```

### 10.2 修复方案

```
10.1 立即创建 pending task     → 不需要等后端确认，消息立刻出现在对话流
10.2 引用解析后台异步          → 先发送不含引用的消息，引用结果通过 edit 补发
10.3 临时 sessionId            → 前端先生成，后端幂等接受
```

实现：按回车 → 消息**立即显示**（pending 动画） → 后台异步解析引用/创建会话 → WS 发送完整 prompt。

---

## 十一、DeepSeek 专项优化与多模型协调

### 11.1 已完成的优化

| 优化项 | 位置 | 状态 |
|--------|------|:--:|
| thinking mode 条件启用 | hermes.py | ✅ has_tools 时开启 |
| stream_options.include_usage | hermes.py | ✅ 已实现 |
| reasoning_content 多轮回传 | base.py | ✅ 已实现 |
| 1M 上下文窗口 | config.py | ✅ 已配置 |
| 动态 history_limit | config.py | ✅ 已实现但被硬编码绕过 |

### 11.2 未完成的优化

| 问题 | 详情 |
|------|------|
| OC adapter thinking 未条件化 | openclaw.py 直接判断 oc_tools 列表（经常非空） |
| 无 cache token 计费折扣 | DeepSeek 缓存命中 token 价格 1/4，cost_tracker 不区分 |
| 切换 provider 时 reasoning 格式不兼容 | 不同厂商的 reasoning 字段名不同 |

### 11.3 多模型协调

已实现：`_agent_providers`（OC和HE可分别绑定不同provider）、`models_url`（动态拉取）、`extra_params`（YAML扩展参数）

差距：
- 无模型能力自动探测（需手动配置 tools/vision/thinking）
- 无 provider 健康度自动降级

### 11.4 修复方案

```
11.1 OC thinking 条件化           → 对齐 hermes.py：仅需 tool 时开启
11.2 cache token 计费             → cost_tracker 读 usage.prompt_cache_hit_tokens
11.3 模型能力自动探测             → 调 /models 自动填充 supports 字段
11.4 健康度降级                   → 连续 N 次失败自动切换备用 provider
```

---

## 十二、记忆/学习/成长模块——闭环评估

### 12.1 已联通的路径

对话完成 → FeedbackLearner(评分) + SelfEvolutionEngine(模式) + MemoryService(auto_dream)
三项都向 knowledge_nodes 写入 → Agent 对话时检索 → 注入 Commander prompt

### 12.2 闭环缺口（5 个）

| # | 问题 | 详情 |
|---|------|------|
| 1 | SelfEvolution生成的Workflow无质量反馈 | 创建后标记done，不追踪采纳情况 |
| 2 | Workflow 无成功/失败反馈通路 | Feedback 调整知识节点，不调整 Workflow 本身 |
| 3 | Dream 与 SelfEvolution 不同步 | 各自独立运行，不互相触发 |
| 4 | SelfEvolution 跳过 MemoryTree | 直写 knowledge_nodes，绕过 MemoryTree 去重逻辑 |
| 5 | 模式记忆纯内存 | _patterns dict 进程重启丢失 |

### 12.3 修复方案

```
12.1 Workflow 质量追踪    → use_count/success_count/fail_count 评分驱动
12.2 闭环触发链            → Dream完成→触发Evolution→Evolution完成→触发Dream
12.3 SelfEvolution走MemoryTree → memory_tree.index_from_text() 代替直写 DB
12.4 模式持久化            → _patterns 写入 knowledge_nodes (TYPE='evolution_pattern')
```

---

## 十三、工作流——自动触发与学习固化

### 13.1 当前状态

| 能力 | 状态 |
|------|:--:|
| 手动创建/保存/运行 | ✅ |
| SelfEvolution 自动生成 | ✅ (模式≥4次→DAG；≥5次→Skill) |
| 自动触发（按意图匹配） | ❌ |
| 学习固化（使用反馈） | ⚠️ 生成后不再进化 |

### 13.2 修复方案

```
13.1 意图匹配自动触发    → intent_service 匹配意图→自动加载对应 Workflow 模板
13.2 Workflow 使用反馈   → use_count/success_rate → 低成功率降级/淘汰
13.3 临时工作流          → 复杂任务中 LLM 临时生成 DAG → 留存复用
```

---

## 十四、前端界面——优化与统一

### 14.1 Emoji 清理（共 ~20 处）

| 文件 | 数量 | 用途 |
|------|:--:|------|
| WorkflowEditor.tsx | 9 | 节点标签/右键菜单 |
| AgentsSection.tsx | 3 | 模式/状态标签 |
| ModelConfigPanel.tsx | 3 | 刷新/提示按钮 |
| AgentManager.tsx | 2 | 模式标签 |
| SkillMarket.tsx | 1 | 图标输入框 |

修复：全部替换为 Icons.tsx 中的 SVG 组件（TrophyIcon/StarIcon 等已有 9 个可用）。

### 14.2 暗色主题

| 问题 | 修复 |
|------|------|
| Diff 块硬编码 GitHub 暗色 | 改用 var(--card)/var(--border) |
| 无主题预设切换 | 加 accent 色相变量（蓝/绿/紫） |
| markdown 字体不一致 | 统一 font token |

### 14.3 风格统一

| 问题 | 修复 |
|------|------|
| ChatView/TaskCenter 双轨 | 合并为统一工作区（后续 Phase） |
| 自定义 CSS class | 迁移到 shadcn/ui 组件 |
| 布局不一致 | 统一 Tailwind spacing scale |

---

## 十五、终端——与 Warp 的差距

### 15.1 Warp 核心特性

Warp 是 Rust 编写、GPU 加速的现代终端 (github.com/warpdotdev/warp, AGPL v3)：
- Block Model：命令+输出分组为独立 Block，可单独折叠/复制/分享
- AI Agent：内联 Agent 推理+工具调用+diff 渲染
- 命令编辑器：现代 IDE 风格
- 跨平台：macOS/Linux/Windows

### 15.2 差距

| 特性 | Warp | Yuncat |
|------|------|--------|
| 渲染引擎 | Rust + GPU | xterm.js (Canvas) |
| 命令分组 | Block Model | 纯文本流 |
| PTY | 原生 PTY | WS 消息转发 |
| Tab 补全 | AI 驱动 | 无 |
| 命令建议 | 历史+AI | 基础历史面板 |
| 输出搜索 | Cmd+F | 无 |
| 会话持久化 | 自动保存 | tab 刷新丢失 |

### 15.3 适配评估

Warp 不能直接适配——是独立 Rust 应用。借鉴 Warp 思想：xterm.js + AI 命令建议面板 + 命令输出 Block 化。

### 15.4 修复方案

```
15.1 AI 命令建议面板       → 集成到终端顶部（类似 Warp 的 cmd+k）
15.2 命令输出 Block 化     → 按命令分组可折叠/复制/搜索
15.3 会话持久化             → tab sessionId 存 localStorage
15.4 输出搜索               → 终端内 Cmd+F
15.5 Tab 独立化             → 每个 tab 独立的 xterm 实例
```

---

## 十六、OC/HE 适配串联评估

### 16.1 串联状态

| 串联项 | 状态 |
|--------|:--:|
| delegate_to_agent 双向 | ✅ |
| OC 工具注入 Hermes | ✅ |
| desktop_control 共享 | ⚠️ 两套不统一 |
| 记忆共享 | ✅ |
| 成长统计注入 | ✅ |
| Skill 同步 | ✅ |
| 工作流节点 | ✅ |

### 16.2 调度问题

| 问题 | 详情 |
|------|------|
| 路由不够智能 | classify_route() 只看 prompt 关键词，不看任务复杂度/模型专长 |
| 无负载均衡 | OC 任务排队无感知 |
| delegate 无超时兜底 | Hermes 委托 OC 无超时回退 |

---

## 十七、更新后的实施路线图

### 层级 1：对话重连核心（~2h，5 项，§一）

### 层级 2：思考链+发送体验+监管（~3h）

```
2.1 Thinking 流式实时展示          ChatView.tsx
2.2 ChatView 导入 ThinkingBlock     ChatView.tsx + ThinkingBlock.tsx
2.3 工具参数格式化+复制按钮         ExecutionTimeline.tsx
2.4 ExecutionTimeline 渲染 Round    ExecutionTimeline.tsx
2.5 PlanProgress 运行时显示         ChatView.tsx
2.6 乐观消息发送（立即显示）        ChatView.tsx + taskStore.ts
2.7 Overseer Agent Loop             agent_service.py + loop_supervisor.py
2.8 Healer 长任务白名单             agent_service.py
```

### 层级 3：对话功能增强（~6h，§二）

### 层级 4：硬编码统一+多模型+终端+UI（~6h）

```
4.1 history_limit fallback 消除
4.2 SQL LIMIT/max_tokens 提取到 config
4.3 DeepSeek 缓存计费 + OC thinking 条件化
4.4 终端 AI 命令建议 + Block 化
4.5 Emoji → SVG 替换 (7文件~20处)
4.6 Diff 块暗色主题适配
4.7 desktop_control 统一到 peekaboo_*
4.8 模型能力自动探测 + 健康度降级
```

### 层级 5：记忆闭环+工作流进化+前端合并（~8h）

```
5.1 Workflow 质量追踪闭环
5.2 Dream↔Evolution 互相触发
5.3 SelfEvolution 走 MemoryTree
5.4 工作流自动匹配触发
5.5 ChatView/TaskCenter 合并规划
5.6 模式持久化
```

### 层级 6：新功能（后续 Phase）

```
6.1 消息反馈 + per-conversation 自定义指令
6.2 多备选回复 + workspace 信任度
6.3 find_conflicts 实现 + MCP 心跳
6.4 PromptManager 结构化编辑
6.5 分享对话 + 协作
```

---

## 附录 B：新增差距速查

### 思考链

| 文件 | 行 | 问题 | 等级 |
|------|:--:|------|:--:|
| ChatView.tsx | 361-381 | thinking hidden 属性非 React state | 🟠 |
| ChatView.tsx | 398 | PlanProgress 仅 completed 时渲染 | 🔴 |
| ExecutionTimeline.tsx | — | toolRound prop 传入但未渲染 | 🟠 |
| ExecutionTimeline.tsx | — | 工具参数 150 字符截断 | 🟠 |
| TaskDetail.tsx | — | 正则检测工具调用 | 🟠 |
| ThinkingBlock.tsx | — | 组件存在但 ChatView 未 import | 🟡 |

### 长任务

| 文件 | 行 | 问题 | 等级 |
|------|:--:|------|:--:|
| agent_service.py | 681-699 | Healer 30min 无白名单 | 🔴 |
| agent_loop.py | 154-159 | 5 次空循环退出 | 🟠 |
| lifespan_phases.py | 429 | 只恢复 3 个 pending task | 🟡 |

### 记忆闭环

| 文件 | 问题 | 等级 |
|------|------|:--:|
| self_evolution.py | 跳过 MemoryTree 直写 DB | 🟠 |
| self_evolution.py | _patterns 纯内存 | 🟠 |
| feedback_learning.py | 不追踪 Workflow 使用结果 | 🟡 |

### 前端

| 文件 | 问题 | 等级 |
|------|------|:--:|
| WorkflowEditor.tsx | 9 处 emoji | 🟡 |
| AgentsSection.tsx | 3 处 emoji | 🟡 |
| ModelConfigPanel.tsx | 3 处 emoji | 🟡 |
| index.css | Diff 块硬编码颜色 | 🟡 |
"""

with open(base_path, "w", encoding="utf-8") as f:
    f.write(base + new_sections)

# Clean up tmp file
if os.path.exists(tmp_path):
    os.remove(tmp_path)

# Count
with open(base_path, "r", encoding="utf-8") as f:
    lines = len(f.readlines())
print(f"OK - {lines} lines total")
