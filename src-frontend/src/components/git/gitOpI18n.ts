/**
 * Git 日志高级操作 → i18n key 显式映射。
 *
 * 为什么不用 `t("gitPanel.advanced." + type + "Title")` 拼 key：
 * 运营 id 里带连字符（cherry-pick），拼出来是 `gitPanel.advanced.cherry-pickTitle`，
 * 语言包里根本没有这个 key——弹窗直接把 key 原文显示给用户（"gitPanel.advanced.cherry-pickTitle"）。
 * 显式映射把"哪几个 id、对应哪几个 key"写死在一处，拼错一眼可见，测试也能逐个校验。
 */
export const LOG_OP_I18N_KEYS: Record<string, { title: string; confirm: string }> = {
  "cherry-pick": {
    title: "gitPanel.advanced.cherryPickTitle",
    confirm: "gitPanel.advanced.cherryPickConfirm",
  },
  revert: {
    title: "gitPanel.advanced.revertTitle",
    confirm: "gitPanel.advanced.revertConfirm",
  },
  squash: {
    title: "gitPanel.advanced.squashTitle",
    confirm: "gitPanel.advanced.squashConfirm",
  },
  fixup: {
    title: "gitPanel.advanced.fixupTitle",
    confirm: "gitPanel.advanced.fixupConfirm",
  },
  tag: {
    title: "gitPanel.advanced.tagTitle",
    confirm: "gitPanel.advanced.tagConfirm",
  },
};

/**
 * Git 面板标签页 → i18n key 显式映射。
 *
 * 同样是为了避免 `t("gitPanel.tabs." + tabName)` 拼 key 时 key 与语言包对不上
 * （拼错时界面直接显示 key 原文，且语言包一致性测试抓不到）。
 */
export const GIT_TAB_I18N_KEYS: Record<string, string> = {
  status: "gitPanel.tabs.status",
  diff: "gitPanel.tabs.diff",
  log: "gitPanel.tabs.log",
  branch: "gitPanel.tabs.branch",
  graph: "gitPanel.tabs.graph",
  review: "gitPanel.tabs.review",
  advanced: "gitPanel.tabs.advanced",
};
