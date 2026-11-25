import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

export default defineConfig({
  plugins: [react()],
  base: './',
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  // 生产构建剥离调试日志——保留 console.warn/error 供生产排查
  // 对标 Vite 官方推荐 esbuild.pure 模式（dev 模式不生效，仅 build）
  esbuild: {
    pure: ["console.log", "console.debug", "console.info"],
    drop: ["debugger"],
  },
  server: {
    port: 1420,
    strictPort: true,
  },
  // Phase 42: Tauri 清理 — 移除 Tauri 残留配置
  clearScreen: false,
  envPrefix: ["VITE_"],
  build: {
    target: "es2020",
    minify: "esbuild",
    sourcemap: false,
    // 构建分块优化：vendor-monaco（Monaco 编辑器，约 4.2MB）已通过 manualChunks 隔离为独立
    // 可缓存 chunk，且 PromptManager 等重编辑器页面已 React.lazy 按需加载。Monaco 体积属已知且
    // 可接受（完整代码编辑器），故将告警阈值上调至其实际量级，避免每次构建误报 >1300kB 警告。
    chunkSizeWarningLimit: 5000, // Phase 56-F: vendor-monaco ~4.2MB（独立分包，可缓存）
    // Phase 56-F: 拆大 vendor chunk，降低首屏 JS 解析压力
    rollupOptions: {
      output: {
        manualChunks(id: string | undefined) {
          if (!id || !id.includes("node_modules")) return;
          // Batch 4a F4: monaco 独立 chunk——本地化后体积大（数 MB），独立分包便于缓存
          if (/monaco-editor|@monaco-editor/.test(id)) return "vendor-monaco";
          // 流程图/图表库仅被 lazy 页面引用，独立分包避免被 vendor-react 拖入首屏
          if (/@xyflow|reactflow|recharts/.test(id)) return "vendor-flow";
          if (/marked|highlight\.js|dompurify/.test(id)) return "vendor-markdown";
          if (/@xterm/.test(id)) return "vendor-xterm";
          if (/fuse\.js/.test(id)) return "vendor-search";
          // 2026-09-16: 文档预览的解析库（mammoth / exceljs / pptx-preview）只在打开
          // Office/CSV 文件时动态加载。不单独分块就会落进 vendor-react，
          // 把「启动就要加载」的包从 ~800KB 顶到 3.5MB——动态加载也就白做了。
          // 连同传递依赖一起分块：echarts/zrender 来自 pptx-preview，
          // jszip/pako/archiver/unzipper/fast-csv/readable-stream 等来自 exceljs 与 mammoth。
          // 只写主库名的话，这些会被下面的兜底规则塞进 vendor-react，等于启动就加载。
          const DOC_PREVIEW = /(mammoth|exceljs|pptx-preview|echarts|zrender|jszip|pako|archiver|archiver-utils|compress-commons|crc-32|crc32-stream|zip-stream|unzipper|fast-csv|@fast-csv|saxes|xmlchars|@xmldom|xmlbuilder|bluebird|dayjs|lodash|readable-stream|tar-stream|readdir-glob|dingbat-to-unicode|big-integer|lazystream|end-of-stream|fstream|setimmediate)/;
          if (DOC_PREVIEW.test(id)) return "vendor-docpreview";
          // Phase 56-F: 其余 node_modules 统一进 vendor-react，避免 vendor-other / vendor-ui-heavy
          // 与 React 核心包之间的循环 chunk 引用。
          return "vendor-react";
        },
      },
    },
  },
});
