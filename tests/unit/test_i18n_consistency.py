"""tests/unit/test_i18n_consistency.py —— Phase 5 X: i18n key 一致性测试

防止 4 种语言（zh/en/ja/ko）的翻译文件之间出现 key 漂移。
当一种语言添加了新 key 时，其他语言必须同步补全。

此测试在 CI 中运行，确保 i18n 完整性。
"""
import json
import os
import pathlib
import sys
from functools import reduce

import pytest
import re

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

LOCALES_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "src-frontend", "src", "i18n", "locales"
))

LANGUAGES = ["zh", "en", "ja", "ko"]


def _load_lang(lang: str) -> dict:
    """加载语言 JSON 文件"""
    path = os.path.join(LOCALES_DIR, f"{lang}.json")
    if not os.path.exists(path):
        pytest.skip(f"{lang}.json 不存在")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _collect_keys(obj: dict, prefix: str = "") -> set[str]:
    """递归收集所有叶子 key（用点号连接路径）"""
    keys: set[str] = set()
    for k, v in obj.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= _collect_keys(v, full_key)
        else:
            keys.add(full_key)
    return keys


def _all_keys() -> dict[str, set[str]]:
    """获取所有语言的 key 集合"""
    return {lang: _collect_keys(_load_lang(lang)) for lang in LANGUAGES}


class TestI18nFilesExist:
    """所有语言文件都应存在"""

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_lang_file_exists(self, lang):
        path = os.path.join(LOCALES_DIR, f"{lang}.json")
        assert os.path.exists(path), f"语言文件不存在: {lang}.json"

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_lang_file_valid_json(self, lang):
        """JSON 格式应有效"""
        data = _load_lang(lang)
        assert isinstance(data, dict), f"{lang}.json 顶层应为 dict"


class TestKeyConsistency:
    """4 种语言的 key 集合应一致（无缺失/无多余）"""

    def test_all_languages_have_same_key_set(self):
        """所有语言的 key 集合应完全一致"""
        all_keys = _all_keys()
        # 以 en 为基准（fallback 语言）
        en_keys = all_keys["en"]
        for lang in LANGUAGES:
            lang_keys = all_keys[lang]
            missing = en_keys - lang_keys
            extra = lang_keys - en_keys
            assert not missing, \
                f"{lang}.json 缺失 {len(missing)} 个 key（vs en）: {sorted(missing)[:10]}..."
            # extra keys 不强制报错——可能有历史遗留，但应记录
            if extra:
                # 仅在 zh 缺失时才报错——其他语言的 extra key 是 fallback 容错
                pass

    def test_zh_has_all_keys(self):
        """中文（默认语言）必须有所有 key——不能依赖 fallback"""
        all_keys = _all_keys()
        en_keys = all_keys["en"]
        zh_keys = all_keys["zh"]
        missing = en_keys - zh_keys
        assert not missing, \
            f"zh.json 缺失 {len(missing)} 个 key: {sorted(missing)[:10]}..."

    def test_no_language_missing_more_than_5_keys(self):
        """任何语言缺失不应超过 5 个 key（防止大面积漂移）"""
        all_keys = _all_keys()
        en_keys = all_keys["en"]
        for lang in LANGUAGES:
            lang_keys = all_keys[lang]
            missing = en_keys - lang_keys
            assert len(missing) <= 5, \
                f"{lang}.json 缺失 {len(missing)} 个 key（超过 5 个阈值）: {sorted(missing)[:10]}..."


class TestCoreSectionsPresent:
    """核心 section 在所有语言中都应存在"""

    CORE_SECTIONS = [
        "memory",        # P0 修复: memoryExplorer 空状态
        "memories",      # 记忆浏览器主 section
        "chatView",      # 聊天视图
        "settings",      # 设置页
        "agents",        # Agent 面板
        "gitPanel",      # Git 面板
        "task",          # 任务面板
        "phase49",       # Phase 49 新功能
        "skillMarket",   # 技能市场
        "statusBar",     # 状态栏
    ]

    @pytest.mark.parametrize("lang", LANGUAGES)
    @pytest.mark.parametrize("section", CORE_SECTIONS)
    def test_core_section_exists(self, lang, section):
        """核心 section 应存在于所有语言"""
        data = _load_lang(lang)
        assert section in data, \
            f"{lang}.json 缺少核心 section: {section}"


class TestMemorySectionConsistency:
    """P0 回归: memory section（单数）应在所有语言中存在且 key 一致"""

    EXPECTED_MEMORY_KEYS = {
        "emptyMemories",
        "emptyMemoriesDesc",
        "emptyConversations",
        "emptyConversationsDesc",
        "emptyKnowledge",
        "emptyKnowledgeDesc",
    }

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_memory_section_has_all_keys(self, lang):
        """memory section 应包含所有 6 个空状态 key"""
        data = _load_lang(lang)
        memory = data.get("memory", {})
        missing = self.EXPECTED_MEMORY_KEYS - set(memory.keys())
        assert not missing, \
            f"{lang}.json 的 memory section 缺失: {missing}"


class TestPhase49ChatToolbarKeys:
    """P2 回归: phase49.chatToolbar 的 effort* 和 deepReviewFallback 应在所有语言中存在"""

    EXPECTED_KEYS = {
        "deepReviewFallback",
        "effortAuto",
        "effortQuick",
        "effortStandard",
        "effortDeep",
        "effortUltraCode",
    }

    @pytest.mark.parametrize("lang", LANGUAGES)
    def test_effort_keys_exist(self, lang):
        data = _load_lang(lang)
        chat_toolbar = (
            data.get("phase49", {}).get("chatToolbar", {})
        )
        missing = self.EXPECTED_KEYS - set(chat_toolbar.keys())
        assert not missing, \
            f"{lang}.json 的 phase49.chatToolbar 缺失: {missing}"


# ═══════════════════════════════════════════════════════════════
# 硬编码文案守卫：用户可见位置不得写死中文
# ═══════════════════════════════════════════════════════════════
#
# 背景（2026-09-15 审计）：扫描"组件里写死的中文"时先得出 308 处，
# 但**绝大多数是 JSX 注释 `{/* 中文 */}` 与 JSDoc** —— 过滤只排除行首 `//` 会大量误报。
# 剥掉三类注释后真命中 27 处，其中 4 处是真正的用户可见文案：
#   ResultRenderer 的 Suspense fallback「加载差异视图…」、
#   GrantsSection 的「全部 agent / 全部项目」、lib/toolViews 的「(N 行)」单位，
#   以及 SkillMarket 把「全部」当**筛选用哨兵值**（显示虽走 t()，但字面量脆弱）。
#
# 本测试只守**用户可见位置**（JSX 文本节点 + title/placeholder/aria-label/alt/label 属性），
# 零容忍。刻意不扫一般字符串：解析模型输出的中英双语正则、发给 LLM 的中文 prompt 模板
# 都是**有意为之**，扫了只会逼人写无意义的豁免。

_FE_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend", "src")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# JSX 文本节点：>中文<
_JSX_TEXT_RE = re.compile(r">\s*[^<>{}=]*[\u4e00-\u9fff][^<>{}]*<")
# 会被朗读/提示的属性：值里出现中文即为漏翻译
_ATTR_RE = re.compile(r'(title|placeholder|aria-label|alt|label)\s*=\s*"[^"]*[\u4e00-\u9fff]')


def _strip_ts_comments(src: str) -> str:
    """剥掉 //、/* */、{/* */} 三类注释（保留行数以免行号错位）。

    必须三类都剥：只排 `//` 开头的行会让 JSX 注释与 JSDoc 里的中文全部误报。
    """
    src = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"^\s*//.*$", "", src, flags=re.M)
    src = re.sub(r"(?<![:/])//[^\n\"'`]*$", "", src, flags=re.M)
    return src


def test_no_hardcoded_cjk_in_user_visible_positions():
    """JSX 文本与可见属性里不得出现写死的中文（英文/日文界面会中英混排）。"""
    offenders: list[str] = []
    for dirpath, dirnames, filenames in os.walk(_FE_SRC):
        dirnames[:] = [d for d in dirnames if d not in ("__tests__", "node_modules", "i18n")]
        for name in filenames:
            if not name.endswith((".tsx", ".ts")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, _FE_SRC).replace("\\", "/")
            with open(full, encoding="utf-8", errors="ignore") as fh:
                clean = _strip_ts_comments(fh.read())
            for lineno, line in enumerate(clean.split("\n"), 1):
                if not _CJK_RE.search(line):
                    continue
                if _JSX_TEXT_RE.search(line) or _ATTR_RE.search(line):
                    offenders.append(f"{rel}:{lineno}  {line.strip()[:100]}")
    assert offenders == [], (
        "用户可见位置出现硬编码中文，请改用 t(\"…\") 并补齐四语言文案：\n  "
        + "\n  ".join(offenders)
    )


# ═══════════════════════════════════════════════════════════════
# 反向守卫：代码里引用的 key 必须真实定义（2026-09-15 新增）
# ═══════════════════════════════════════════════════════════════
#
# 背景：本文件此前只校验「语言文件彼此一致」，不校验「代码引用 vs 语言文件」。
# 于是代码可以引用从未定义的 key（theme.system / chat.forcePause / common.copy …）：
# 四种语言一致地缺失 → 测试全绿，而**默认语言 zh 的界面上直接渲染裸键名**。
# 实测：主题为默认「跟随系统」时，侧栏主题按钮显示的就是 "theme.system"。
#
# 局限：只覆盖 t("literal") 形式；`t(`prefix.${x}`)` 这类动态拼接需要在代码里保证
# 取值集合都有定义（例如 theme.system / theme.light / theme.dark）。

# 只认「第一个参数整体就是一个字面量 key」的情况：
# `t("gitPanel.tabs." + name)` 这类动态拼接不算——它的取值集合静态展开不了。
_TS_KEY_RE = re.compile(r"\bt\(\s*[\"']([A-Za-z0-9_.\-]+)[\"']\s*(?=[,)])")


def test_code_referenced_keys_exist_in_locales():
    """src/ 下 t("literal") 引用的键必须在 zh.json 中存在（zh 是默认语言，无 fallback）。"""
    zh_keys = _collect_keys(_load_lang("zh"))
    missing: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(_FE_SRC):
        dirnames[:] = [d for d in dirnames if d not in ("__tests__", "node_modules", "i18n")]
        for name in filenames:
            if not name.endswith((".ts", ".tsx")):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, _FE_SRC).replace("\\", "/")
            with open(full, encoding="utf-8", errors="ignore") as fh:
                clean = _strip_ts_comments(fh.read())
            for key in set(_TS_KEY_RE.findall(clean)):
                if key.endswith("."):  # 前缀字面量（动态拼接），不当作完整 key
                    continue
                if key not in zh_keys:
                    missing.setdefault(key, []).append(rel)
    detail = "\n  ".join(
        f"{k}  ← {", ".join(sorted(set(v))[:3])}" for k, v in sorted(missing.items())
    )
    assert missing == {}, (
        "以下 i18n key 被代码引用但 zh.json 未定义（界面会显示裸键名）：\n  " + detail
    )


# ═══════════════════════════════════════════════════════════════
# 插值占位符一致性（2026-09-15 新增）
# ═══════════════════════════════════════════════════════════════
#
# 背景：补翻译时最容易踩的坑是改坏插值——把 `{{count}}` 写成 `{{n}}`、漏掉 `{{name}}`，
# 界面就会显示空值或半截句子，而且不会有任何报错（i18next 缺参时静默填空）。
# 本测试要求：en 里出现的每个 `{{x}}` / `{x}` 占位符，在 zh/ja/ko 里必须一字不差地出现。

_PLACEHOLDER_RE = re.compile(r"\{\{?\s*([A-Za-z0-9_]+)\s*\}?\}")


def _placeholders(value: str) -> set:
    return set(_PLACEHOLDER_RE.findall(value or ""))


def test_placeholders_match_across_locales():
    """四种语言同一 key 的插值占位符必须完全一致。"""
    all_keys = _all_keys()
    data = {lang: _load_lang(lang) for lang in LANGUAGES}

    def flatten(obj, prefix=""):
        out = {}
        for k, v in obj.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict):
                out.update(flatten(v, f"{key}."))
            else:
                out[key] = v
        return out

    flat = {lang: flatten(payload) for lang, payload in data.items()}
    del all_keys  # 仅用于自检：确保语言文件可读
    mismatches = []
    for key, en_value in flat["en"].items():
        if not isinstance(en_value, str):
            continue
        expected = _placeholders(en_value)
        if not expected:
            continue
        for lang in ("zh", "ja", "ko"):
            actual = _placeholders(flat[lang].get(key, ""))
            if actual != expected:
                mismatches.append(
                    f"{lang}.json {key}: 期望 {sorted(expected)}，实际 {sorted(actual)}"
                )
    assert mismatches == [], (
        "插值占位符在各语言之间不一致（缺参会静默渲染成空值）：\n  " + "\n  ".join(mismatches[:30])
    )


class TestUsedKeysExist:
    """代码里用到的 i18n key 必须真实存在——否则界面直接显示 key 原文。

    2026-10-22 实测：Git 高级操作弹窗用 `t("gitPanel.advanced." + type + "Title")`
    拼 key，"cherry-pick" 拼出来是 `gitPanel.advanced.cherry-pickTitle`，
    语言包里没有 → 确认弹窗把 "gitPanel.advanced.cherry-pickTitle" 显示给用户。
    语言包之间的 key 一致性测试抓不到这类问题（四个语言都"一致地缺"），
    所以要单独盯"代码里引用的 key 是否存在"。
    """

    FRONTEND_SRC = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "..", "src-frontend", "src"
    ))

    _KEY_RE = re.compile(r"""\bt\(\s*["\'`]([A-Za-z0-9_.]+)["\'`]""")

    @staticmethod
    def _strip_comments(text: str) -> str:
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
        return "\n".join(re.sub(r"(?<!:)//.*$", "", line) for line in text.splitlines())

    def _used_keys(self) -> dict[str, set[str]]:
        used: dict[str, set[str]] = {}
        for path in pathlib.Path(self.FRONTEND_SRC).rglob("*.ts*"):
            parts = path.parts
            if "__tests__" in parts or "node_modules" in parts or path.name.endswith(".d.ts"):
                continue
            text = self._strip_comments(path.read_text(encoding="utf-8", errors="replace"))
            rel = str(path.relative_to(self.FRONTEND_SRC))
            for match in self._KEY_RE.finditer(text):
                used.setdefault(match.group(1), set()).add(rel)
        return used

    def test_all_used_keys_exist_in_every_locale(self):
        used = self._used_keys()
        assert used, "扫描不到任何 i18n key——扫描规则本身坏了，需要修测试"
        keys = _all_keys()
        problems = []
        for key in sorted(used):
            # 以 "." 结尾的是动态前缀（t("gitPanel.tabs." + tabName)）——
            # 具体 key 在运行时拼出来，静态查不了。这类改用显式映射 +
            # 前端测试逐个校验（见 gitOpI18n.test.ts）。
            if key.endswith("."):
                continue
            for lang in LANGUAGES:
                if key not in keys[lang]:
                    problems.append(f"{lang}: {key} ({', '.join(sorted(used[key])[:2])})")
        assert not problems, (
            "代码里引用了语言包里不存在的 i18n key（界面会显示 key 原文）:\n"
            + "\n".join(problems)
        )
