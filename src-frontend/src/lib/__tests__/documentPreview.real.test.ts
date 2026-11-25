import { describe, expect, it } from "vitest";

import { loadDocxHtml, loadWorkbookSheets } from "../documentPreview";

/**
 * 用**真实文件格式**跑一遍解析（2026-09-16）。
 *
 * 其它测试用的是 mock，只能证明「我们的接线对不对」；这里现场生成一份最小但合规的
 * OOXML（docx / xlsx 本质是 zip + XML），交给 mammoth / exceljs 真解析，
 * 证明「用户丢进来一个 Word / Excel 文件，我们确实读得出来」。
 */

/** 用 zip 的 stored 模式拼一个包（不压缩，省掉依赖） */
function zipStored(entries: Array<[string, string]>): Uint8Array {
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  const u16 = (n: number) => [n & 0xff, (n >> 8) & 0xff];
  const u32 = (n: number) => [n & 0xff, (n >> 8) & 0xff, (n >> 16) & 0xff, (n >> 24) & 0xff];

  for (const [name, text] of entries) {
    const nameBytes = encoder.encode(name);
    const data = encoder.encode(text);
    const crcTable: number[] = [];
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      crcTable[n] = c >>> 0;
    }
    let crc = 0xffffffff;
    for (const byte of data) crc = crcTable[(crc ^ byte) & 0xff] ^ (crc >>> 8);
    crc = (crc ^ 0xffffffff) >>> 0;

    const local = new Uint8Array([
      ...u32(0x04034b50), ...u16(20), ...u16(0), ...u16(0), ...u16(0), ...u16(0),
      ...u32(crc), ...u32(data.length), ...u32(data.length),
      ...u16(nameBytes.length), ...u16(0), ...nameBytes, ...data,
    ]);
    chunks.push(local);
    central.push(new Uint8Array([
      ...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0), ...u16(0), ...u16(0), ...u16(0),
      ...u32(crc), ...u32(data.length), ...u32(data.length),
      ...u16(nameBytes.length), ...u16(0), ...u16(0), ...u16(0), ...u16(0), ...u32(0),
      ...u32(offset), ...nameBytes,
    ]));
    offset += local.length;
  }

  const centralStart = offset;
  const centralBytes = central.reduce((sum, c) => sum + c.length, 0);
  const end = new Uint8Array([
    ...u32(0x06054b50), ...u16(0), ...u16(0), ...u16(entries.length), ...u16(entries.length),
    ...u32(centralBytes), ...u32(centralStart), ...u16(0),
  ]);
  const total = [...chunks, ...central, end].reduce((sum, c) => sum + c.length, 0);
  const out = new Uint8Array(total);
  let cursor = 0;
  for (const chunk of [...chunks, ...central, end]) {
    out.set(chunk, cursor);
    cursor += chunk.length;
  }
  return out;
}

const CONTENT_TYPES = (parts: string) =>
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
  '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
  '<Default Extension="xml" ContentType="application/xml"/>' +
  parts +
  "</Types>";

const RELS = (target: string) =>
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
  `<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="${target}"/>` +
  "</Relationships>";

function docxBytes(): Uint8Array {
  return zipStored([
    ["[Content_Types].xml", CONTENT_TYPES(
      '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>')],
    ["_rels/.rels", RELS("word/document.xml")],
    ["word/document.xml",
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' +
      "<w:p><w:r><w:t>季度报告</w:t></w:r></w:p>" +
      "<w:p><w:r><w:t>第二季度收入增长 12%。</w:t></w:r></w:p>" +
      "</w:body></w:document>"],
  ]);
}

function xlsxBytes(): Uint8Array {
  return zipStored([
    ["[Content_Types].xml", CONTENT_TYPES(
      '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
      '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')],
    ["_rels/.rels", RELS("xl/workbook.xml")],
    ["xl/workbook.xml",
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' +
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
      '<sheets><sheet name="销售明细" sheetId="1" r:id="rId1"/></sheets></workbook>'],
    ["xl/_rels/workbook.xml.rels",
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
      '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
      "</Relationships>"],
    ["xl/worksheets/sheet1.xml",
      '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' +
      '<row r="1"><c r="A1" t="inlineStr"><is><t>产品</t></is></c><c r="B1" t="inlineStr"><is><t>数量</t></is></c></row>' +
      '<row r="2"><c r="A2" t="inlineStr"><is><t>苹果</t></is></c><c r="B2"><v>3</v></c></row>' +
      "</sheetData></worksheet>"],
  ]);
}

/**
 * jsdom 的 Blob 没有 arrayBuffer()，而浏览器里有。
 * 我们的加载器只用到这一个方法，所以这里给一个最小替身（不影响被测逻辑）。
 */
function toBlob(bytes: Uint8Array): Blob {
  const copy = bytes.slice();
  return { arrayBuffer: async () => copy.buffer } as unknown as Blob;
}

describe("真实文件解析", () => {
  it("docx：mammoth 能读出正文，且结果经过净化", async () => {
    const html = await loadDocxHtml(toBlob(docxBytes()));
    expect(html).toContain("季度报告");
    expect(html).toContain("第二季度收入增长");
    expect(html).not.toContain("<script");
  }, 20000);

  it("xlsx：exceljs 能读出工作表名与单元格", async () => {
    const sheets = await loadWorkbookSheets(toBlob(xlsxBytes()));
    expect(sheets).toHaveLength(1);
    expect(sheets[0].name).toBe("销售明细");
    expect(sheets[0].rows[0]).toEqual(["产品", "数量"]);
    expect(sheets[0].rows[1]).toEqual(["苹果", "3"]);
  }, 20000);
});
