import { describe, it, expect } from "vitest";
import zh from "../../../i18n/locales/zh.json";
import en from "../../../i18n/locales/en.json";
import ja from "../../../i18n/locales/ja.json";
import ko from "../../../i18n/locales/ko.json";
import { GIT_TAB_I18N_KEYS, LOG_OP_I18N_KEYS } from "../gitOpI18n";

/**
 * Git 高级操作弹窗的 i18n key 必须真实存在。
 *
 * 之前用 `t("gitPanel.advanced." + type + "Title")` 拼 key，"cherry-pick"
 * 拼出来是 `gitPanel.advanced.cherry-pickTitle`——语言包里没有，
 * 弹窗把 key 原文显示给用户。改用显式映射后，这里逐个校验。
 */
function lookup(obj: unknown, key: string): string | undefined {
  let cur: unknown = obj;
  for (const part of key.split(".")) {
    if (typeof cur !== "object" || cur === null) return undefined;
    cur = (cur as Record<string, unknown>)[part];
  }
  return typeof cur === "string" ? cur : undefined;
}

const LOCALES: Record<string, unknown> = { zh, en, ja, ko };

describe("Git 高级操作 i18n key", () => {
  for (const [lang, locale] of Object.entries(LOCALES)) {
    it(`${lang} 语言包里每个操作都有标题和确认文案`, () => {
      for (const [op, keys] of Object.entries(LOG_OP_I18N_KEYS)) {
        expect(lookup(locale, keys.title), `${lang} ${op} 标题`).toBeTruthy();
        expect(lookup(locale, keys.confirm), `${lang} ${op} 确认文案`).toBeTruthy();
      }
    });
  }

  for (const [lang, locale] of Object.entries(LOCALES)) {
    it(`${lang} 语言包里每个 Git 标签页都有文案`, () => {
      for (const [tab, key] of Object.entries(GIT_TAB_I18N_KEYS)) {
        expect(lookup(locale, key), `${lang} ${tab} 标签`).toBeTruthy();
      }
    });
  }

  it("操作 id 与后端路由一一对应", () => {
    // cherry-pick / revert / squash / fixup / tag —— 与 runLogOp 的分支保持一致
    expect(Object.keys(LOG_OP_I18N_KEYS).sort()).toEqual(
      ["cherry-pick", "fixup", "revert", "squash", "tag"].sort(),
    );
  });
});
