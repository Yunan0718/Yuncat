# Yuncat

本地优先的 AI 编码助手桌面应用。Electron 外壳 + React 前端 + Python 后端，模型直连，工具调用在本地执行。

## 技术栈

| 层 | 技术 | 目录 |
|---|---|---|
| 桌面壳 | Electron 42 + Node.js IPC | `src-electron/` |
| 前端 | React 19 + Vite + Tailwind + shadcn/ui | `src-frontend/` |
| 后端 | Python 3.12 + FastAPI + aiosqlite | `src-python/` |

## 开发

前端开发服务器：

```powershell
cd src-frontend
npm install
npm run dev
```

桌面壳（另开一个终端）：

```powershell
cd src-electron
npm install
npm run dev
```

单独启动后端：

```powershell
cd src-python
python main.py --port 8000
```

## 构建

```powershell
cd src-frontend
npm run build

cd ../src-electron
npm run prebuild
npm run build:win
```

## 测试

```powershell
# 后端
python -m pytest tests/unit tests/contract

# 前端
cd src-frontend
npx tsc --noEmit
npx vitest run
```

## 配置

模型配置在 `config/models/`，每个供应商一个 YAML，声明端点、鉴权和可选参数。API Key 不写入仓库：首次启动后在应用内填写，加密保存在本地数据目录。

## 许可

见 [LICENSE](LICENSE)。