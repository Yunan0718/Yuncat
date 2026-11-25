/**
 * lib/csv.ts —— CSV / TSV 解析（只服务「表格预览」）
 *
 * 为什么自己写而不引依赖：exceljs 的 csv 读取走 Node 流，浏览器里行为不可预期；
 * papaparse 能用，但为了一处「把文本渲染成表格」再挂一个运行时依赖并不划算。
 * 规则是**明确支持一部分、不猜**：双引号包裹、引号内换行与分隔符、`""` 转义、
 * CRLF、UTF-8 BOM；其余一律按普通字符处理。
 */

/** 去掉 UTF-8 BOM——Excel 导出的 CSV 常带，留着会让第一个表头多一个不可见字符 */
function stripBom(text: string): string {
  return text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
}

/**
 * 猜分隔符：先看扩展名，扩展名不认识时数首行里谁出现得多。
 * 只在逗号 / 制表符 / 分号三者里选——别的字符当分隔符只会把表格拆坏。
 */
export function detectDelimiter(nameOrPath: string, sample: string): string {
  const lower = nameOrPath.toLowerCase();
  if (lower.endsWith(".tsv") || lower.endsWith(".tab")) return "\t";
  if (lower.endsWith(".csv")) return ",";
  const firstLine = sample.split(/\r?\n/, 1)[0] || "";
  const candidates: Array<[string, number]> = [
    [",", firstLine.split(",").length - 1],
    ["\t", firstLine.split("\t").length - 1],
    [";", firstLine.split(";").length - 1],
  ];
  candidates.sort((a, b) => b[1] - a[1]);
  return candidates[0][1] > 0 ? candidates[0][0] : ",";
}

/** 解析为二维字符串数组；未闭合的引号按「到文本结尾」处理，不抛异常 */
export function parseDelimited(rawText: string, delimiter: string): string[][] {
  const text = stripBom(rawText ?? "");
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  let i = 0;
  const endField = () => {
    row.push(field);
    field = "";
  };
  const endRow = () => {
    endField();
    rows.push(row);
    row = [];
  };
  while (i < text.length) {
    const ch = text[i];
    if (inQuotes) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 2;
          continue;
        }
        inQuotes = false;
        i += 1;
        continue;
      }
      field += ch;
      i += 1;
      continue;
    }
    if (ch === '"' && field === "") {
      inQuotes = true;
      i += 1;
      continue;
    }
    if (delimiter && text.startsWith(delimiter, i)) {
      endField();
      i += delimiter.length;
      continue;
    }
    if (ch === "\r") {
      i += 1;
      continue;
    }
    if (ch === "\n") {
      endRow();
      i += 1;
      continue;
    }
    field += ch;
    i += 1;
  }
  if (field !== "" || row.length > 0) endRow();
  return rows;
}
