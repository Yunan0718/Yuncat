/** 模型供应商官方品牌 SVG 图标
 *
 * 优先使用 simple-icons 库中的官方 SVG path，缺失的用内联 SVG 补充。
 * 每个图标保持原品牌形状和配色。
 */
import { memo } from "react";

// ── simple-icons 导出的图标（同步自 simple-icons v16） ──
import {
  siDeepseek,
  siAnthropic,
  siGooglegemini,
  siX,
  siQwen,
  siMoonshotai,
  siBaidu,
  siMistralai,
  siOllama,
  siOpenrouter,
  siXiaomi,
} from "simple-icons";

interface IconDef {
  path: string;
  hex: string;
}

const SIMPLE: Record<string, IconDef> = {
  deepseek: { path: siDeepseek.path, hex: `#${siDeepseek.hex}` },
  anthropic: { path: siAnthropic.path, hex: `#${siAnthropic.hex}` },
  gemini: { path: siGooglegemini.path, hex: `#${siGooglegemini.hex}` },
  grok: { path: siX.path, hex: `#${siX.hex}` },
  qwen: { path: siQwen.path, hex: `#${siQwen.hex}` },
  kimi: { path: siMoonshotai.path, hex: `#${siMoonshotai.hex}` },
  baidu: { path: siBaidu.path, hex: `#${siBaidu.hex}` },
  mistral: { path: siMistralai.path, hex: `#${siMistralai.hex}` },
  ollama: { path: siOllama.path, hex: `#${siOllama.hex}` },
  openrouter: { path: siOpenrouter.path, hex: `#${siOpenrouter.hex}` },
  xiaomi: { path: siXiaomi.path, hex: `#${siXiaomi.hex}` },
};

// ── 内联 SVG path（simple-icons 缺失的厂商） ──
const INLINE: Record<string, IconDef> = {
  // OpenAI — 六边形花 interlocking hexagon bloom
  openai: {
    hex: "#10A37F",
    path: "M22.68 1.82a13.17 13.17 0 0 0-5.82-1.43c-3.86 0-7.31 1.72-9.7 4.43l3.18 3.18a7.23 7.23 0 0 1 6.52-1.95 7.2 7.2 0 0 1 5.09 4.56 7.23 7.23 0 0 1-1.2 6.79l3.18 3.18a13.18 13.18 0 0 0-1.25-18.76zM11.36 7.18 8.18 4A13.18 13.18 0 0 0 1.6 16.06a13.2 13.2 0 0 0 5.34 6.18l3.18-3.18a7.22 7.22 0 0 1-1.94-9.88zm7.02 7.02a7.23 7.23 0 0 1-6.52 1.95 7.2 7.2 0 0 1-5.09-4.56 7.23 7.23 0 0 1 1.2-6.79L4.8 1.6A13.18 13.18 0 0 0 1.6 15.36a13.18 13.18 0 0 0 13.72 7.1l-3.18-3.18a7.24 7.24 0 0 1-1.48-2.67l3.71-3.4z",
  },
  // 智谱 GLM — 环形 AI
  glm: {
    hex: "#3859FF",
    path: "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm0 18c-4.41 0-8-3.59-8-8s3.59-8 8-8 8 3.59 8 8-3.59 8-8 8zm3.5-11.5h-3v5h-2v-5h-3l4-3.5 4 3.5z",
  },
  // 豆包 (ByteDance/Volcengine) — 音符/播放符号
  doubao: {
    hex: "#3370FF",
    path: "M8 3v14l-3-2.25V4.75L8 3zm4 0v14l6-4.5V7.5L12 3zM5 5.5v9l-2-1.5V7l2-1.5z",
  },
  // 腾讯混元 — 太极风格圆形
  hunyuan: {
    hex: "#00A870",
    path: "M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm0 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16zm0-14a6 6 0 1 0 0 12 6 6 0 0 0 0-12zm0 2a4 4 0 0 1 0 8V8z",
  },
  // 硅基流动 — 流动水滴
  siliconflow: {
    hex: "#7C3AED",
    path: "M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5a2.5 2.5 0 0 1 0-5 2.5 2.5 0 0 1 0 5z",
  },
  // 自定义 — 齿轮
  custom: {
    hex: "#6B7280",
    path: "M12 15.5A3.5 3.5 0 0 1 8.5 12 3.5 3.5 0 0 1 12 8.5a3.5 3.5 0 0 1 3.5 3.5 3.5 3.5 0 0 1-3.5 3.5zm7.43-2.53c.04-.32.07-.65.07-.97 0-.32-.03-.65-.07-.97l2.11-1.65c.19-.15.24-.42.12-.64l-2-3.46c-.12-.22-.39-.3-.61-.21l-2.49 1c-.52-.4-1.08-.73-1.69-.98l-.38-2.65A.49.49 0 0 0 14 2h-4c-.25 0-.46.18-.49.42l-.38 2.65c-.61.25-1.17.58-1.69.98l-2.49-1c-.23-.09-.49 0-.61.21l-2 3.46c-.13.22-.07.49.12.64l2.11 1.65c-.04.32-.07.65-.07.97 0 .32.03.65.07.97l-2.11 1.65c-.19.15-.24.42-.12.64l2 3.46c.12.22.39.3.61.21l2.49-1c.52.4 1.08.73 1.69.98l.38 2.65c.03.24.24.42.49.42h4c.25 0 .46-.18.49-.42l.38-2.65c.61-.25 1.17-.58 1.69-.98l2.49 1c.23.09.49 0 .61-.21l2-3.46c.13-.22.07-.49-.12-.64l-2.11-1.65z",
  },
};

function IconSvg({ def, size }: { def: IconDef; size: number }) {
  return (
    <svg width={size * 0.55} height={size * 0.55} viewBox="0 0 24 24" fill="none">
      <path d={def.path} fill={def.hex} />
    </svg>
  );
}

export const ProviderIcon = memo(function ProviderIcon({ provider, size = 40 }: { provider: string; size?: number }) {
  const def = SIMPLE[provider] || INLINE[provider];
  const hex = def?.hex || "#6B7280";

  return (
    <div
      className="rounded-xl flex items-center justify-center shrink-0 select-none"
      style={{ width: size, height: size, backgroundColor: hex + "15" }}
    >
      {def ? (
        <IconSvg def={def} size={size} />
      ) : (
        <span className="font-extrabold text-sm" style={{ color: hex }}>
          {provider.charAt(0).toUpperCase()}
        </span>
      )}
    </div>
  );
});
