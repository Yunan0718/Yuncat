# Yuncat — AI Agent 入口

## 快速导航

- 技术栈与目录结构：见下方「技术栈」「核心规则」两节
- 构建与开发运行：见文末「构建」「开发运行」两节

## 技术栈

| 层 | 技术 | 目录 |
|----|------|------|
| 桌面壳 | Electron 42 + Node.js IPC | `src-electron/` |
| 前端 | React 19 + Vite + Tailwind + shadcn/ui | `src-frontend/src/` |
| 后端 | Python 3.12 + FastAPI + aiosqlite | `src-python/` |

## 核心规则

所有 Agent 必须遵循的硬约束：

1. 副作用唯一入口 — 文件读写/命令执行必须走 `ToolService.execute()`
2. 记忆写穿透 — 先写 DB 再更新内存缓存
3. 写完代码必须通过 `pytest` + `tsc --noEmit`
4. 函数 <50 行，文件 <800 行
5. 不硬编码密钥 / agent_id — 密钥走 `ModelConfigService`；agent_id 统一走 config 常量（`DEFAULT_AGENT_ID` / `OPENCLAW_AGENT_ID` / `HERMES_AGENT_ID`），禁止在业务代码里散落 `"openclaw"` / `"hermes"` 等字面量
6. **不硬编码厂商** — 端点/鉴权/列表解析/思考强度字段一律进 `config/models/<id>.yaml` 的
   `protocol:` / `reasoning:` 声明，Python 里不出现任何厂商名（见 `services/provider_protocol.py`）。
   厂商不认识某个可选参数时走"摘参数重试"降级，而不是逐家适配
7. **token 用量只有一个真源** — 字段名与事件类型一律引用 `services/llm_usage.py` 的
   `FIELD_*` / `TOKEN_USAGE_EVENT(S|_SQL)` 常量，禁止字面量（守卫见
   `tests/contract/test_token_usage_contract.py`：改名时读写两端不同步会静默归零）
8. **不做价格/金额计算** — 统计口径只有 token（用户决策）；定价 YAML 仅作模型目录数据
9. **不得静默吞掉异常** — 宽泛 `except Exception` 里只 `pass`/返回空值时必须二选一：
   无回退路径 → 记日志留痕；有明确回退 → 注释声明意图（含「回退/降级/best-effort」等词）。
   两者皆无即"静默吞噬"，会让"失败"与"本来就没数据"不可区分（本轮修掉的真 bug 全是这类）
10. 重要设计决策随代码记录（格式门禁 `scripts/verify_decision_notes.py`），
   约定是「同一份知识不要写两处」——写之前先问能不能收敛成一处声明
    **文档有行数预算**（`scripts/verify_doc_budget.py`）：AGENTS.md ≤ 200、CLAUDE.md ≤ 220、
    单篇决策记录 ≤ 120 行；超了先想能不能删/挪到更专门的文档，不要直接调高预算
11. **静态约束有棘轮守卫** — 修改超标文件/函数前先看 `scripts/size_baseline.json` 与
   `scripts/silent_except_baseline.json`；新增违规会被 `tests/contract/test_static_guards_ratchet.py`
   拦下，改善后需下调基线（`python scripts/static_guards.py --update`）
12. **前后端接口面必须对得上** — 前端调的每个 `/api/...` 路径都要有后端路由，
   否则只是一句「操作失败」，用户与本 agent 都看不出接口不存在（2026-09-15 一次审计
   抓到 3 处：点赞 / 文件上传 / 状态栏分支）。守卫：`tests/contract/test_api_surface_match.py`；
    例外只允许写进它的 `ALLOWED_MISSING` 并注明原因。
    **反方向同样有守卫**：后端新增路由却没人调用时，同样会制造「功能都有」的错觉
    （实测 `/llm/complete` 挂了很久没人发现，真正在用的是 `/inline-completion`）。
    守卫：`tests/contract/test_api_surface_reverse.py` + `scripts/api_surface_baseline.json`
    （只对外部调用方开放的接口要显式登记并注明原因）
13. **Electron 窗口逻辑进 `src-electron/windows.js`** — 多窗口共享**一个** Python 后端，
   没有 `mainWindow` 单例：需要单一目标时用 `wins.focused()`，需要让所有窗口知道时用
   `wins.broadcast()`。直接在 `main.js` 里再写 `mainWindow.xxx` 会让第二个窗口失去更新/就绪事件
14. **Electron 打包清单必须与主进程引用同步** — `src-electron/electron-builder.yml` 的 `files`
    是显式白名单，漏一个文件在开发期毫无征兆、用户装上后直接起不来。
    守卫：`tests/electron/main-process-guards.test.mjs` 会从 `main.js` 递归扫 `require` 与
    `path.join(__dirname, ...)`，和清单双向比对（曾漏掉过 `windows.js`）。

15. **所有 WS 端点的 token 校验必须同一口径** — 主进程把静态 `TOKEN` 兑换成短期
    `access_token` 后才能下发给渲染进程，所以 HTTP 中间件、主 `/ws`、`/ws/terminal`、
    `/ws/lsp` **都要同时接受两者**。漏一个不会报错、只会静默失效：2026-09-22 实测
    `have_token=True` 仍被 1008 拒，表现为「内置终端连不上、Python 语言服务一直不可用」。
    校验实现只写一处（`services/auth_service.py` 的 `validate_access_token`）。
    守卫：`tests/contract/test_ws_token_parity.py`。

16. **工具私有键与任务身份** — hook 通过 `arguments` 传递 `_turn_seq` / `_snapshot_pre_id`
    这类私有键；`ToolService` 调 `tool.func(**arguments)` 前必须剔除 `_` 前缀键，否则
    工具会因 unexpected keyword 直接 TypeError（2026-10-21 快照真正生效时才暴露）。
    当前任务号一律取 `context.get_current_task_id()`（`ToolService.execute()` 负责
    set/reset）；**不要**再从 `arguments["_task_id"]` 取——工具循环从来不写这个键，
    历史上因此让快照表的 task_id 全是随机 uuid、撤销功能整体失效。

17. **声明与实现必须对账** — 接口面都是"一处声明、一处实现"，声明方改了而实现方没跟上，
    症状一律是「看起来有、其实用不了」，且**不会报错**：
    - 工具 schema ↔ 实现签名：schema 里声明了实现接不住的参数 → 模型一传就 TypeError
      （`run_command` 的 `shell_id` 曾让整个持久 Shell 从入口不可用）。
      守卫：`tests/contract/test_tool_schema_signature_parity.py`。
    - 代码里的 i18n key ↔ 语言包：缺 key → 界面直接显示 key 原文。
      守卫：`tests/unit/test_i18n_consistency.py::TestUsedKeysExist`；
      动态 key 必须收敛成**显式映射**（如 `components/git/gitOpI18n.ts`），
      不许用 `t("前缀" + 变量 + "后缀")` 拼。
    - 后端 WS 事件 ↔ 前端 handler：后端发了而前端没注册 → 事件被静默丢弃
      （`tool_output_chunk`、`memory_saved` 曾长期只在后端存在）。
      守卫：`tests/contract/test_ws_event_surface.py`（扫描 `scripts/ws_event_scan.py`，
      基线 `scripts/ws_event_baseline.json` 必须写明"前端为什么不处理"）。
      **注意**：handler 写好但忘了在 `wsHandlers/index.ts` 注册 = 事件照样被丢弃，
      同一个守卫也会拦（"导出了 handler 却没注册"）。

## Agent 架构（2026-08-29 重构）

Yuncat 既是**强原生 Agent**，也是**外部 Agent 网关**：

- `yuncat`（原生，默认 `DEFAULT_AGENT_ID`）：进程内直接调用 Provider API，无需子进程 / 网关
- 外部网关（用户本地已启动）：`openclaw`（HTTP Gateway，默认端口 18789）、`hermes`（hermes-agent 的 API server，默认端口 8642，需 `API_SERVER_KEY`；启动 `hermes gateway run`）
- 外部 CLI（按需 spawn）：`codex`、`claude`（headless CLI，经 `shutil.which` 探活）

适配器基类在 `src-python/adapters/`；`node-embed`（vendored Node.js + openclaw + hermes-agent）已移除，不再随安装包分发，OpenClaw / Hermes 改为外部 agent 模式。

**桌面壳是多窗口的**（2026-09-15）：一个 Python 后端进程 + 多个独立窗口（对标 VS Code / Cursor），
窗口登记、几何状态、关闭语义在 `src-electron/windows.js`，单测见 `tests/electron/`（随 CI 跑）。
`Ctrl+Shift+N` = 新建窗口；「新建对话」在应用菜单与命令面板。

**内置浏览器**（2026-09-16）：Electron 自带 Chromium + CDP，零新增运行时。
窗口与标签在 `src-electron/browser.js`，页面原语在 `browser-cdp.js`，
Python → Electron 的反向通道是 `browser-bridge.js`（loopback + 随机 token + 拒 Origin），
Agent 侧 13 个 `browser_*` 工具受 `browser` 能力开关约束。
改浏览器相关代码前先读那一页的「安全边界」——那几条是刻意设计，不要顺手放宽。

## 构建

```bat
cd src-frontend && npm run build
cd src-electron && npm run prebuild && npm run build:win
```

## 开发运行

```bat
:: 终端 1: 前端 dev server
cd src-frontend && npm run dev

:: 终端 2: Electron
cd src-electron && npm run dev
```

## 本文件的自更新约定

<!-- AI agents：按以下规则维护本文件，避免文档随代码腐化（借鉴 Clicky 的 AGENTS.md 实践）。 -->

改动影响到下列信息时，**同一次改动里**把本文件一起更新：

1. **新增硬约束** — 一旦踩到"同一类错误犯第二次"，把结论写进「核心规则」，不要在注释里孤零零留着
2. **架构变化** — 新增 agent 后端、适配器基类、目录分层变化 → 更新「Agent 架构」一节
3. **构建/运行方式变化** — 更新「构建」「开发运行」两节
4. **新增权威文档** — 在本文件「快速导航」补上入口
5. **路径漂移** — 文中提到的目录/文件被移动或删除时同步修正

不要为一次性 bug 修复、内部重构细节、或与本文件无关的改动更新它——本文件只承载
"新 agent 上来必须先知道的事"。判断标准：**一个没有任何上下文的 agent 读完本文件，
会不会做错事？**
