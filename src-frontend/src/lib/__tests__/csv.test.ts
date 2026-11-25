import { describe, expect, it } from "vitest";

import { detectDelimiter, parseDelimited } from "../csv";

/**
 * CSV / TSV 解析的边界用例。
 *
 * 这些情况在真实文件里都出现过：Excel 导出的 BOM、单元格里的逗号与换行、
 * 转义的双引号、CRLF、最后一行没有换行符。解析错了不会报错，只会让表格静静地显示错行。
 */
describe("detectDelimiter", () => {
  it("扩展名优先：.tsv/.tab → 制表符，.csv → 逗号", () => {
    expect(detectDelimiter("a.tsv", "x,y")).toBe("\t");
    expect(detectDelimiter("dir\\a.tab", "x,y")).toBe("\t");
    expect(detectDelimiter("a.csv", "x\ty")).toBe(",");
  });

  it("扩展名不认识时按首行出现次数猜，都没有分隔符则退回逗号", () => {
    expect(detectDelimiter("a.txt", "a;b;c")).toBe(";");
    expect(detectDelimiter("a.txt", "a\tb")).toBe("\t");
    expect(detectDelimiter("a.txt", "单列文本")).toBe(",");
  });
});

describe("parseDelimited", () => {
  it("普通两行三列", () => {
    expect(parseDelimited("a,b,c\n1,2,3", ",")).toEqual([
      ["a", "b", "c"],
      ["1", "2", "3"],
    ]);
  });

  it("引号里的逗号与换行保持原样", () => {
    expect(parseDelimited('name,note\n"Zhang, San","line1\nline2"', ",")).toEqual([
      ["name", "note"],
      ["Zhang, San", "line1\nline2"],
    ]);
  });

  it('"" 转义成单个双引号', () => {
    expect(parseDelimited('say\n"he said ""hi"""', ",")).toEqual([["say"], ['he said "hi"']]);
  });

  it("CRLF 与行尾空字段", () => {
    expect(parseDelimited("a,b,\r\n1,2,", ",")).toEqual([
      ["a", "b", ""],
      ["1", "2", ""],
    ]);
  });

  it("去掉 UTF-8 BOM，否则第一个表头会多一个不可见字符", () => {
    expect(parseDelimited("\ufeffa,b", ",")[0][0]).toBe("a");
  });

  it("空的末尾行不算一行", () => {
    expect(parseDelimited("a,b\n", ",")).toEqual([["a", "b"]]);
  });

  it("未闭合的引号不抛异常（读到结尾为止）", () => {
    expect(parseDelimited('a,"unterminated', ",")).toEqual([["a", "unterminated"]]);
  });

  it("制表符分隔", () => {
    expect(parseDelimited("a\tb\n1\t2", "\t")).toEqual([
      ["a", "b"],
      ["1", "2"],
    ]);
  });
});
