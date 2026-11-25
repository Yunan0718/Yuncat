/**
 * 文件 → Monaco 语言 id 的映射（2026-09-15 从 MonacoEditor 抽出并补全）。
 *
 * 抽出来的原因：这份表原本埋在组件里，既不好测也容易漏格式——用户手动打开
 * `.h` / `.cxx` / `.xml` / `.csv` 这类常见文件时，缺映射就只剩纯文本（没有高亮、
 * 没有括号匹配）。现在只依赖 Monaco 内置 basic-languages 的 id，不引入任何新依赖。
 *
 * 注意：Monaco **自带完整语言服务**（补全/类型检查/跳转/重命名）的只有
 * TypeScript/JavaScript/JSON/CSS/HTML——其余语言的语义能力需要接 LSP（下一步）。
 */
const EXT_TO_LANG: Record<string, string> = {
  // 脚本与通用语言
  py: "python", pyw: "python", pyi: "python",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  json: "json", jsonc: "json", json5: "json",
  html: "html", htm: "html", vue: "html", svelte: "html",
  css: "css", scss: "scss", sass: "scss", less: "less",
  md: "markdown", markdown: "markdown", mdx: "markdown",
  yaml: "yaml", yml: "yaml",
  ini: "ini", cfg: "ini", conf: "ini", properties: "ini", env: "ini",
  xml: "xml", xsd: "xml", xsl: "xml", xslt: "xml", plist: "xml", svg: "xml",
  // 编译型语言
  c: "c", h: "c",
  cc: "cpp", cpp: "cpp", cxx: "cpp", hpp: "cpp", hh: "cpp", hxx: "cpp",
  cs: "csharp", java: "java", kt: "kotlin", kts: "kotlin", scala: "scala",
  go: "go", rs: "rust", swift: "swift", dart: "dart", m: "objective-c",
  // 其它常见
  rb: "ruby", php: "php", pl: "perl", lua: "lua", r: "r", sql: "sql",
  sh: "shell", bash: "shell", zsh: "shell", fish: "shell",
  bat: "bat", cmd: "bat", ps1: "powershell", psm1: "powershell",
  dockerfile: "dockerfile", proto: "protobuf", graphql: "graphql", gql: "graphql",
  toml: "ini",
};

/** 没有扩展名、只有文件名时按名字识别（Dockerfile / Makefile 这类）。 */
const NAME_TO_LANG: Record<string, string> = {
  dockerfile: "dockerfile",
  makefile: "plaintext",
  ".gitignore": "plaintext",
  ".env": "ini",
  ".editorconfig": "ini",
  "cargo.lock": "plaintext",
  "requirements.txt": "plaintext",
};

/** 由文件路径推断 Monaco 语言 id；认不出时返回 plaintext（仍可正常编辑）。 */
export function detectLanguage(filePath: string): string {
  const name = (filePath.split(/[/\\]/).pop() || "").toLowerCase();
  if (NAME_TO_LANG[name]) return NAME_TO_LANG[name];
  const ext = name.includes(".") ? name.split(".").pop() || "" : "";
  return EXT_TO_LANG[ext] || "plaintext";
}
