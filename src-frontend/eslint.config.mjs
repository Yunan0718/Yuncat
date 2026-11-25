import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactPlugin from "eslint-plugin-react";
import reactHooksPlugin from "eslint-plugin-react-hooks";
import reactRefreshPlugin from "eslint-plugin-react-refresh";
// M22 修复: 移除 eslint-plugin-prettier——它把 prettier 作为 ESLint 规则跑，
// 让 lint 多跑一遍 prettier（速度 -30%）且报告混乱。
// 主流做法（prettier 官方推荐）: prettier 单独运行（npm run format:check），
// eslint-config-prettier 仅负责关闭与 prettier 冲突的 ESLint 规则。
import prettierConfig from "eslint-config-prettier";
import globals from "globals";

export default tseslint.config(
  {
    // tmp-* 是 gitignore 掉的一次性排查脚本（见根 .gitignore），不属于仓库源码。
    // 不排除的话 `eslint .` 会对它们报 29 个 no-undef / no-require-imports
    // （脚本跑在 Node 下，这里的 languageOptions 只配了 browser globals），
    // 把真实问题的信噪比压下去。
    ignores: ["dist", "node_modules", "*.config.js", "*.config.ts", "tmp-*.js", "tmp-*.mjs", "tmp-*.cjs", "tmp-*.ts"],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.ts", "**/*.tsx"],
    languageOptions: {
      parserOptions: {
        project: "./tsconfig.json",
        ecmaFeatures: { jsx: true },
      },
      globals: { ...globals.browser, ...globals.es2024 },
    },
    plugins: {
      react: reactPlugin,
      "react-hooks": reactHooksPlugin,
      "react-refresh": reactRefreshPlugin,
    },
    settings: {
      react: { version: "detect" },
    },
    rules: {
      ...reactPlugin.configs.recommended.rules,
      ...reactHooksPlugin.configs.recommended.rules,
      ...prettierConfig.rules,
      "react/react-in-jsx-scope": "off",
      "react/prop-types": "off",
      "react-refresh/only-export-components": "warn",
      "react-hooks/exhaustive-deps": "error",
      "react/jsx-key": "error",
      "react/no-array-index-key": "warn",
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  // e2e 不在根 tsconfig.json 的 include 内（那里只有 src + vitest.config.ts），
  // 沿用上面的 project 会对每个 spec 报 parsing error —— 结果是这 4 个 e2e 文件
  // 实际上从未被静态检查过。这里指向 e2e 自己的 tsconfig，让它们真正进入校验。
  {
    files: ["e2e/**/*.ts"],
    languageOptions: {
      parserOptions: {
        project: "./e2e/tsconfig.json",
      },
    },
  },
);
