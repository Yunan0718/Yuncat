import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      zIndex: {
        backdrop: "var(--z-backdrop)",
        dropdown: "var(--z-dropdown)",
        drawer: "var(--z-drawer)",
        modal: "var(--z-modal)",
        "modal-top": "var(--z-modal-top)",
        ctx: "var(--z-ctx)",
        toast: "var(--z-toast)",
        skip: "var(--z-skip)",
      },
      colors: {
        background: "var(--background)",
        foreground: "var(--foreground)",
        "foreground-secondary": "var(--foreground-secondary)",
        "foreground-tertiary": "var(--foreground-tertiary)",
        primary: { DEFAULT: "var(--primary)", foreground: "var(--primary-foreground)" },
        "primary-hover": "var(--primary-hover)",
        "primary-muted": "var(--primary-muted)",
        "primary-soft": "var(--primary-soft)",
        secondary: { DEFAULT: "var(--secondary)", foreground: "var(--foreground)" },
        muted: { DEFAULT: "var(--muted)", foreground: "var(--foreground-secondary)" },
        card: { DEFAULT: "var(--card)", foreground: "var(--card-foreground)" },
        popover: { DEFAULT: "var(--popover)", foreground: "var(--popover-foreground)" },
        accent: { DEFAULT: "var(--accent)", foreground: "var(--accent-foreground)" },
        border: "var(--border)",
        "border-default": "var(--border-default)",
        "border-hover": "var(--border-hover)",
        "border-subtle": "var(--border-subtle)",
        input: "var(--input)",
        ring: "var(--ring)",
        "ring-bg-primary": "var(--ring-bg-primary)",
        destructive: { DEFAULT: "var(--destructive)", foreground: "var(--destructive-foreground)",
                       solid: "var(--destructive-solid)" },
        success: { DEFAULT: "var(--success)", foreground: "var(--success-foreground)",
                   solid: "var(--success-solid)" },
        warning: { DEFAULT: "var(--warning)", foreground: "var(--warning-foreground)",
                   solid: "var(--warning-solid)" },
        badge: { DEFAULT: "var(--badge)", foreground: "var(--badge-foreground)",
                 warning: "var(--badge-warning)", danger: "var(--badge-danger)" },
        "chart-1": "var(--chart-1)", "chart-2": "var(--chart-2)",
        "chart-3": "var(--chart-3)", "chart-4": "var(--chart-4)",
        "chart-5": "var(--chart-5)", "chart-6": "var(--chart-6)",
        "chart-7": "var(--chart-7)", "chart-8": "var(--chart-8)",
        "entity-agent": "var(--entity-agent)",
        "entity-file": "var(--entity-file)",
        "entity-url": "var(--entity-url)",
        "entity-workflow": "var(--entity-workflow)",
        sidebar: { DEFAULT: "var(--sidebar)", foreground: "var(--sidebar-foreground)",
                   primary: "var(--sidebar-primary)", "primary-foreground": "var(--sidebar-primary-foreground)",
                   accent: "var(--sidebar-accent)", "accent-foreground": "var(--sidebar-accent-foreground)",
                   border: "var(--sidebar-border)", ring: "var(--sidebar-ring)" },
      },
      borderRadius: {
        sm: "var(--radius-sm)", md: "var(--radius-md)", lg: "var(--radius-lg)",
        "4xl": "var(--radius-4xl)", DEFAULT: "var(--radius)",
      },
      // Phase 68: 命名 spacing token——与默认数字 spacing 并存，供面板/卡片统一间距
      // 生成 p-panel / gap-panel 等 utility，不覆盖 p-4 / gap-2 等默认值。
      // Tailwind v4 关键修复: 不要在 spacing 里定义 sm/md/lg/xl/**xs**，否则
      // max-w-sm/md/lg/xl/**xs** 会 fallback 到这些 spacing token（8/12/16/4px），
      // 导致文本容器被压成 1 字符宽度的竖排。
      // 2026-09-10 血泪: 上次修复漏掉了 xs——max-w-xs 回退到 --spacing-xs(4px)，
      // 所有 EmptyState 描述段落一字一行竖排。全仓无 p-xs/gap-xs 用法，删除 xs 即可。
      spacing: {
        panel: "var(--panel-padding)",
      },
      boxShadow: { xs: "var(--shadow-xs)", sm: "var(--shadow-sm)", md: "var(--shadow-md)", lg: "var(--shadow-lg)" },
      maxWidth: { "4xl": "896px" },
      fontSize: {
        "2xs": ["11px", "16px"], xs: ["12px", "18px"],
        sm: ["13px", "20px"], base: ["14px", "21px"],
      },
      fontFamily: {
        heading: "var(--font-heading)",
      },
    },
  },
  plugins: [],
} satisfies Config;
