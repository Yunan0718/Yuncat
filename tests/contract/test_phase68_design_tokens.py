"""tests/contract/test_phase68_design_tokens.py —— Phase 68 UI 主题 token 统一合约测试

验证设计 token 在 CSS 和 Tailwind 配置中正确定义：
1. index.css :root 定义 --spacing-xs/sm/md/lg/xl
2. index.css :root 定义 --panel-padding
3. index.css :root 和 .dark 都定义 --border-subtle
4. tailwind.config.ts spacing extend 包含命名 token
5. tailwind.config.ts colors 包含 border-subtle
6. density 切换会改变 --panel-padding
"""
import os
import re

import pytest

_SRC_FE = os.path.join(os.path.dirname(__file__), "..", "..", "src-frontend")
_INDEX_CSS = os.path.join(_SRC_FE, "src", "index.css")
_TAILWIND_CONFIG = os.path.join(_SRC_FE, "tailwind.config.ts")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_spacing_tokens_defined_in_root():
    """:root 块中定义 --spacing-xs/sm/md/lg/xl"""
    css = _read(_INDEX_CSS)
    # 找到 :root { ... } 块（第一个，非 .dark）
    root_match = re.search(r":root\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}", css, re.DOTALL)
    assert root_match, "未找到 :root 块"
    root_block = root_match.group(1)
    for token in ["--spacing-xs", "--spacing-sm", "--spacing-md", "--spacing-lg", "--spacing-xl"]:
        assert token in root_block, f"{token} 未在 :root 中定义"


def test_panel_padding_defined_in_root():
    """:root 块中定义 --panel-padding"""
    css = _read(_INDEX_CSS)
    root_match = re.search(r":root\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}", css, re.DOTALL)
    assert root_match, "未找到 :root 块"
    root_block = root_match.group(1)
    assert "--panel-padding" in root_block, "--panel-padding 未在 :root 中定义"


def test_border_subtle_defined_in_root_and_dark():
    """:root 和 .dark 都定义 --border-subtle"""
    css = _read(_INDEX_CSS)
    # :root 中的 --border-subtle
    root_match = re.search(r":root\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}", css, re.DOTALL)
    assert root_match, "未找到 :root 块"
    assert "--border-subtle" in root_match.group(1), "--border-subtle 未在 :root 中定义"
    # .dark 中的 --border-subtle
    dark_match = re.search(r"\.dark\s*\{([^}]*(?:\{[^}]*\}[^}]*)*)\}", css, re.DOTALL)
    assert dark_match, "未找到 .dark 块"
    assert "--border-subtle" in dark_match.group(1), "--border-subtle 未在 .dark 中定义"


def test_panel_padding_follows_density():
    """density 切换会改变 --panel-padding"""
    css = _read(_INDEX_CSS)
    # compact density 应缩小 panel-padding
    compact_match = re.search(r'\[data-density="compact"\]\s*\{([^}]*)\}', css, re.DOTALL)
    assert compact_match, "未找到 [data-density='compact'] 块"
    assert "--panel-padding" in compact_match.group(1), "compact density 未覆盖 --panel-padding"

    # Density 仅有 compact / comfortable 两档（appStore.ts 的 Density 类型），
    # 早期设计的 spacious 档已移除，注释里仍留有历史描述。
    comfortable_match = re.search(
        r'\[data-density="comfortable"\]\s*\{([^}]*)\}', css, re.DOTALL,
    )
    assert comfortable_match, "未找到 [data-density='comfortable'] 块"
    assert "--panel-padding" in comfortable_match.group(1), (
        "comfortable density 未覆盖 --panel-padding"
    )


def test_tailwind_config_spacing_extend():
    """tailwind.config.ts spacing extend 包含命名 token

    只暴露 panel —— **不得**添加 sm/md/lg/xl/**xs**：Tailwind v4 下它们会被
    同名尺寸工具类（max-w-sm/md/lg/xl/xs）复用为 fallback（8/12/16/4px），
    把文本容器压成竖排（2026-09-10 实测 xs 导致 EmptyState 一字一行竖排，
    详见 tailwind.config.ts 中 spacing 块注释）。此处同时锚定该约束。
    """
    config = _read(_TAILWIND_CONFIG)
    spacing_match = re.search(r"spacing:\s*\{([^}]*)\}", config, re.DOTALL)
    assert spacing_match, "未找到 spacing extend 块"
    spacing_block = spacing_match.group(1)
    for key in ['panel']:
        # 匹配 key: "var(--spacing-...)" 或 "key: var(--panel-padding)"
        assert re.search(rf'["\']?{key}["\']?\s*:', spacing_block), f"spacing extend 缺少 '{key}' key"
    for key in ['sm', 'md', 'lg', 'xl', 'xs']:
        assert not re.search(rf'["\']?{key}["\']?\s*:', spacing_block), (
            f"spacing 不应包含 '{key}'：会被 max-w-{key} 复用为 fallback 压坏布局"
        )


def test_tailwind_config_border_subtle_color():
    """tailwind.config.ts colors 包含 border-subtle"""
    config = _read(_TAILWIND_CONFIG)
    assert '"border-subtle"' in config or "'border-subtle'" in config, (
        "tailwind.config.ts colors 缺少 border-subtle"
    )
    assert "var(--border-subtle)" in config, "border-subtle 应引用 var(--border-subtle)"


def test_spacing_tokens_use_css_variables():
    """tailwind.config.ts spacing 值应引用 CSS 变量（而非硬编码 px）

    spacing 块只含 panel（sm/md/lg/xl/xs 会污染同名 max-w-* 尺寸工具类，
    见同文件另一测试说明），因此这里校验该 token 引用 CSS 变量、且无硬编码 px。
    """
    config = _read(_TAILWIND_CONFIG)
    spacing_match = re.search(r"spacing:\s*\{([^}]*)\}", config, re.DOTALL)
    assert spacing_match
    spacing_block = spacing_match.group(1)
    # 每个值都应是 var(--...)
    assert 'var(--panel-padding)' in spacing_block
    values = re.findall(r":\s*([^,\n}]+)", spacing_block)
    assert values, "spacing 块为空"
    for value in values:
        bare = value.strip().strip('"').strip("'")
        assert bare.startswith("var(--"), f"spacing 值应引用 CSS 变量，实际: {value}"


# ═══════════════════════════════════════════════════════════════
# 主题逃逸守卫：深色阶不得脱离主题
# ═══════════════════════════════════════════════════════════════
#
# 背景（2026-09-15 UI 审计）：本文件此前只校验 token 的**定义**，不校验**使用**，
# 于是组件里可以随便写死 `text-amber-600` / `text-green-600` —— 这类深色阶在浅色主题
# 可读、在暗色主题下变成"深字压深底"，实测 54 处逃逸、其中 11 处是真对比度问题。
# 现改为**零容忍**守卫（无基线）：深色阶要么配 `dark:` 变体，要么改用语义 token
# （text-success / text-warning / text-destructive / text-primary）。

_SRC_FE_COMPONENTS = os.path.join(_SRC_FE, "src")

# 600 及更深：浅色可读、暗色必然对比不足
_DEEP_SHADE_RE = re.compile(
    r"\b(?:text|border|bg|from|to|ring|fill|stroke)-"
    r"(?:red|orange|amber|yellow|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)"
    r"-(?:[6-9]\d0)\b"
)


def _iter_ui_sources():
    for sub in ("components", "pages"):
        base = os.path.join(_SRC_FE_COMPONENTS, sub)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("__tests__", "node_modules")]
            for name in filenames:
                if name.endswith((".tsx", ".ts")):
                    yield os.path.join(dirpath, name).replace("\\", "/")


def test_no_deep_shade_color_without_dark_variant():
    """深色阶（600+）不得脱离主题：配 `dark:` 变体，或改用语义 token。

    例外：`bg-{deep} text-white` 形式的**实心彩色按钮**在两个主题下都成立，
    允许（判据是同一行同时出现 text-white）。
    """
    offenders = []
    for full in _iter_ui_sources():
        with open(full, encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, 1):
                if line.strip().startswith(("//", "*")) or not _DEEP_SHADE_RE.search(line):
                    continue
                if "dark:" in line:
                    continue
                # 实心按钮：深色底 + 白字，主题无关，放行
                if re.search(r"\bbg-(?:[6-9]\d0)\b", line) and "text-white" in line:
                    continue
                rel = os.path.relpath(full, _SRC_FE).replace("\\", "/")
                offenders.append(f"{rel}:{lineno}  {line.strip()[:110]}")
    assert offenders == [], (
        "以下位置使用了深色阶（600+）却没有 dark: 变体 —— 在暗色主题下会对比不足。"
        "请改用语义 token（text-success / text-warning / text-destructive / text-primary）"
        f"或补 dark: 变体：\n  " + "\n  ".join(offenders)
    )


# ═══════════════════════════════════════════════════════════════
# 对比度守卫（WCAG 2.1，明暗两套 token 都按 AA 校验）
# ═══════════════════════════════════════════════════════════════
#
# 背景（2026-09-15 对比度审计）：此前只校验 token"存在"，没人校验它们**能不能看清**。
# 实测发现：浅色主题下三种语义色作文本压在页面上只有 3.02 / 2.51 / 4.00:1（低于 AA 4.5）；
# 暗色主题下白字压在语义色实心填充上只有 3.05 / 2.56 / 3.73:1 —— 白字几乎看不清。
# 根因：一个 token 同时承担"文本色"与"实心填充色"，而暗色下两者对亮度的要求**方向相反**，
# 于是拆出 `--{name}-solid` 专供实心填充。本测试把结论固化为可执行断言。

def _hex_luminance(value: str) -> float:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def _f(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * _f(r) + 0.7152 * _f(g) + 0.0722 * _f(b)


def _contrast(a: str, b: str) -> float:
    """WCAG 对比度（1~21）。"""
    la, lb = _hex_luminance(a), _hex_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _theme_tokens(selector: str) -> dict[str, str]:
    css = _read(_INDEX_CSS)
    match = re.search(rf"{selector}\s*\{{(.*?)\n  \}}", css, re.DOTALL)
    assert match, f"未找到 {selector} 块"
    return {
        k: v.strip()
        for k, v in re.findall(r"--([a-z0-9-]+):\s*([^;]+);", match.group(1))
        if re.fullmatch(r"#[0-9a-fA-F]{3,8}", v.strip())
    }


# (前景 token, 背景 token, 最低对比度, 说明)
_CONTRAST_MATRIX = [
    ("foreground", "background", 4.5, "正文"),
    ("foreground", "card", 4.5, "卡片正文"),
    ("foreground-secondary", "background", 4.5, "次要文本"),
    ("foreground-secondary", "secondary", 4.5, "次要文本/次级面"),
    ("foreground-secondary", "sidebar", 4.5, "次要文本/侧栏"),
    ("primary-foreground", "primary", 4.5, "主按钮"),
    ("success-foreground", "success-solid", 4.5, "成功按钮白字"),
    ("warning-foreground", "warning-solid", 4.5, "警告按钮白字"),
    ("destructive-foreground", "destructive-solid", 4.5, "危险按钮白字"),
    ("success", "background", 4.5, "成功色作文本"),
    ("warning", "background", 4.5, "警告色作文本"),
    ("destructive", "background", 4.5, "危险色作文本"),
    ("ring", "background", 3.0, "焦点环（图形 3:1）"),
]


@pytest.mark.parametrize("theme_selector", [":root", r"\.dark"])
def test_wcag_contrast_matrix(theme_selector):
    """明暗两套主题的语义配色都需通过 WCAG AA。"""
    tokens = _theme_tokens(theme_selector)
    theme_name = "浅色" if theme_selector == ":root" else "暗色"
    failures = []
    for fg, bg, need, label in _CONTRAST_MATRIX:
        if fg not in tokens or bg not in tokens:
            failures.append(f"{label}: token 缺失（{fg} / {bg}）")
            continue
        ratio = _contrast(tokens[fg], tokens[bg])
        if ratio < need:
            failures.append(
                f"{label}: {tokens[fg]} on {tokens[bg]} = {ratio:.2f}:1（需 ≥{need}）"
            )
    assert failures == [], f"[{theme_name}主题] 对比度不达标：\n  " + "\n  ".join(failures)


def test_tertiary_text_has_a_floor():
    """三级文本（快捷键提示/序号徽章等）允许低于 AA，但不得低到看不清。

    这一档是刻意的信息层级（对标 VS Code 的快捷键提示），不按 4.5 要求；
    设 2.5 作下限，防止后续调色把它压没。
    """
    for selector, theme_name in ((":root", "浅色"), (r"\.dark", "暗色")):
        tokens = _theme_tokens(selector)
        ratio = _contrast(tokens["foreground-tertiary"], tokens["background"])
        assert ratio >= 2.5, (
            f"[{theme_name}主题] 三级文本对比度过低：{ratio:.2f}:1（下限 2.5）"
        )


def test_solid_tokens_exist_for_semantic_colors():
    """实心填充必须有独立的 -solid token。

    原因：暗色下"作文本色"（要亮）与"作实心填充配白字"（要暗）对亮度要求相反，
    共用一个 token 必然有一边不达标（2026-09-15 实测）。
    """
    for selector in (":root", r"\.dark"):
        tokens = _theme_tokens(selector)
        for name in ("success", "warning", "destructive"):
            assert f"{name}-solid" in tokens, f"{selector} 缺少 --{name}-solid"


# ═══════════════════════════════════════════════════════════════
# 模态可访问性守卫
# ═══════════════════════════════════════════════════════════════
#
# 背景（2026-09-15 a11y 审计）：全仓两套模态实现并存 —— 共享原语
# （`ui/dialog.tsx` / `ui/sheet.tsx`，基于 Base UI，自带焦点陷阱/Esc/aria）
# 与 8 个自建模态。自建的里有一批缺 `role="dialog"` / `aria-modal`，
# 甚至有弹窗标题引用了**从未定义**的 i18n 键（渲染出裸键名）。
#
# 判定规则（可静态判定，已人工核对过全部分类）：
#   · 带**底色遮罩**（`fixed inset-0 ... bg-black/NN`）= 真模态 → 必须有
#     `role="dialog"` + `aria-modal`，或委托给共享原语；
#   · **透明全屏层**（`fixed inset-0` 无底色）= 点击外部关闭的遮罩 → 豁免
#     （不需要 dialog 语义，这是正确设计）。
# 注意：必须按**渲染组合**判断——`ApprovalInboxDrawer` 自身没有 role，
# 但它包了 `DetailDrawer`（已有完整语义），只看单文件会误报。

_MODAL_BACKDROP_RE = re.compile(r"fixed inset-0[^\"']*bg-black/")
_DELEGATE_RE = re.compile(
    r"<(Dialog|Sheet|DetailDrawer|AlertDialog|DialogContent|SheetContent|DialogPrimitive)"
)
# 原语自身：语义由 Base UI 提供
_PRIMITIVE_FILES = ("components/ui/dialog.tsx", "components/ui/sheet.tsx")


def _iter_tsx_sources():
    for sub in ("components", "pages"):
        base = os.path.join(_SRC_FE_COMPONENTS, sub)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("__tests__", "node_modules")]
            for name in filenames:
                if name.endswith(".tsx"):
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, _SRC_FE).replace("\\", "/")
                    yield rel, full


def test_modal_overlays_have_dialog_semantics():
    """带底色遮罩的模态必须有 dialog 语义，或委托给共享原语。"""
    offenders = []
    for rel, full in _iter_tsx_sources():
        if rel.endswith(_PRIMITIVE_FILES) or rel in _PRIMITIVE_FILES:
            continue
        with open(full, encoding="utf-8", errors="ignore") as fh:
            src = fh.read()
        if not _MODAL_BACKDROP_RE.search(src):
            continue
        has_semantics = 'role="dialog"' in src and "aria-modal" in src
        if has_semantics or _DELEGATE_RE.search(src):
            continue
        offenders.append(rel)
    assert offenders == [], (
        "以下模态层缺少可访问性语义（自建模态必须自带 role=\"dialog\" + aria-modal，"
        "或改用共享原语 ui/dialog、ui/sheet、DetailDrawer——它们已内含焦点陷阱/Esc/aria）：\n  "
        + "\n  ".join(offenders)
    )


def test_aria_labels_use_defined_i18n_keys():
    """`aria-label={t("...")}` 引用的键必须真实存在。

    审计中踩到：弹窗标题用了 `memories.editTitle`，而该键**从未定义** →
    屏幕阅读器读到的是 "memories.editTitle" 这种裸键名，等于没有标签。
    """
    import json

    locales = {}
    for loc in ("zh", "en", "ja", "ko"):
        path = os.path.join(_SRC_FE, "src", "i18n", "locales", f"{loc}.json")
        with open(path, encoding="utf-8") as fh:
            locales[loc] = json.load(fh)

    def flatten(obj, prefix=""):
        out = set()
        for k, v in obj.items():
            key = f"{prefix}{k}"
            if isinstance(v, dict):
                out |= flatten(v, f"{key}.")
            else:
                out.add(key)
        return out

    known = {loc: flatten(data) for loc, data in locales.items()}
    label_re = re.compile(r'aria-label=\{t\("([^"]+)"')
    missing = []
    for rel, full in _iter_tsx_sources():
        with open(full, encoding="utf-8", errors="ignore") as fh:
            src = fh.read()
        for key in set(label_re.findall(src)):
            for loc, keys in known.items():
                if key not in keys:
                    missing.append(f"{rel}: aria-label 用了未定义的键 {key!r}（{loc}.json 缺失）")
    assert missing == [], "aria-label 引用了未定义的 i18n 键：\n  " + "\n  ".join(sorted(set(missing)))


# ═══════════════════════════════════════════════════════════════
# 层级（z-index）守卫
# ═══════════════════════════════════════════════════════════════
#
# 背景（2026-09-15 审计）：z-index 原是散落魔法值 —— z-50×34、z-[60]、z-[100]、
# z-[200]、z-[999]、CSS 里还有个 9999。没有约定时"谁盖住谁"靠 DOM 顺序决定，
# 实测 **Toast(z-50) 被模态(z-50/60/100) 盖住**（保存成功的提示看不见）。
#
# 现收敛为命名层级（index.css 的 --z-* + tailwind 的 z-*），本测试守住：
#   · 禁止任意值 z-[数字]（必须用具名层级）
#   · 禁止 z-30/z-40/z-50（这三个正是当年"多义"的全局层值）
#   · 允许 z-0/z-10/z-20 —— 页面内局部堆叠（父子容器内部），不涉及全局层序
_Z_TOKEN_RE = re.compile(r"\bz-(?!\[)(\d+)\b")
_FORBIDDEN_BARE = {"30", "40", "50"}
_Z_ARBITRARY_RE = re.compile(r"\bz-\[[^\]]*\]")


def test_no_ad_hoc_z_index_values():
    offenders: list[str] = []
    for rel, full in _iter_tsx_sources():
        with open(full, encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                if stripped.startswith(("//", "*")) or stripped.startswith("/*"):
                    continue
                if _Z_ARBITRARY_RE.search(line):
                    offenders.append(f"{rel}:{lineno} 任意值层级 → {line.strip()[:90]}")
                    continue
                for value in _Z_TOKEN_RE.findall(line):
                    if value in _FORBIDDEN_BARE:
                        offenders.append(
                            f"{rel}:{lineno} z-{value} 属旧的全局层值 → {line.strip()[:90]}"
                        )
    assert offenders == [], (
        "请使用命名层级（index.css 的 --z-* / tailwind 的 z-*）：\n"
        "  backdrop < dropdown < drawer < modal < modal-top < ctx < toast < skip\n"
        "页面内局部堆叠可用 z-0/z-10/z-20。违规位置：\n  " + "\n  ".join(offenders)
    )


def test_z_scale_is_defined_and_ordered():
    """层级 token 必须存在，且**语义顺序**不能乱（否则又是"谁盖住谁"说不清）。"""
    css = _read(_INDEX_CSS)
    order = ["backdrop", "dropdown", "drawer", "modal", "modal-top", "ctx", "toast", "skip"]
    values: dict[str, int] = {}
    for name in order:
        match = re.search(rf"--z-{re.escape(name)}:\s*(\d+)\s*;", css)
        assert match, f"index.css 缺少 --z-{name}"
        values[name] = int(match.group(1))
    for lower, higher in zip(order, order[1:]):
        assert values[lower] < values[higher], (
            f"层级顺序错误：--z-{lower}({values[lower]}) 应小于 --z-{higher}({values[higher]})"
        )
    # 关键关系：通知必须高于所有模态（否则"保存成功"看不见）
    assert values["toast"] > values["modal-top"]
    # 右键菜单可能出现在模态之内
    assert values["ctx"] > values["modal"]


def test_input_border_has_minimum_visibility():
    """输入框边界须可辨识（下限 1.8:1）。

    2026-09-15 决策：`--input` 原为 1.21/1.09:1，输入框边界几乎不可见；
    严格按 WCAG 1.4.11 的 3:1 需要 #767676 级重灰，与"简洁大气、线条不重"
    的设计目标矛盾（主流产品同样未达 3:1，靠填充色/标签保证可辨识）。
    折中到 ~1.8:1：可辨识而不重。设下限防止后续调色把边界调没。
    """
    for selector, theme_name in ((":root", "浅色"), (r"\.dark", "暗色")):
        tokens = _theme_tokens(selector)
        ratio = _contrast(tokens["input"], tokens["background"])
        assert ratio >= 1.8, (
            f"[{theme_name}主题] 输入框边界对比度过低：{ratio:.2f}:1（下限 1.8，"
            f"当前 --input={tokens['input']}）——边界不可辨识会让用户找不到输入区"
        )


# ═══════════════════════════════════════════════════════════════
# 层级守卫（补充）：内联 z-index 数字同样禁止（2026-09-15）
# ═══════════════════════════════════════════════════════════════
#
# 上述 test_no_ad_hoc_z_index_values 只看 Tailwind 类名，漏掉了写成内联样式的
# `z-index:9999`——CommandPalette 的「快捷键参考」正是这么写的（innerHTML 拼字符串），
# 同一个浮层还带内联 `onclick=`，在 CSP `script-src 'self'`（无 unsafe-inline）下
# 点击事件全部失效，用户会被卡在遮罩里关不掉。本守卫把这类写法钉死。

_Z_INLINE_RE = re.compile(r"z-index\s*:\s*(\d+)")


def test_no_inline_numeric_z_index():
    """内联样式里的 z-index 必须用 var(--z-*) 而不是裸数字。"""
    offenders: list[str] = []
    for rel, full in _iter_tsx_sources():
        with open(full, encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, 1):
                stripped = line.strip()
                if stripped.startswith(("//", "*", "/*")):
                    continue
                if _Z_INLINE_RE.search(line):
                    offenders.append(f"{rel}:{lineno}  {stripped[:100]}")
    assert offenders == [], (
        "内联 z-index 数字绕过命名层级（应写 var(--z-backdrop|dropdown|drawer|modal|modal-top|ctx|toast|skip)）：\n  "
        + "\n  ".join(offenders)
    )


def test_no_inline_event_handler_attributes():
    """不得用内联事件属性（`onclick="…"`）——CSP 下不生效，等于按钮点不动。

    这类代码通常和 innerHTML 拼字符串一起出现（静态分析看不见、CSP 又拦掉），
    是"看着有交互其实没有"的典型来源。React 组件用 onClick 即可。
    """
    offenders: list[str] = []
    for rel, full in _iter_tsx_sources():
        with open(full, encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, 1):
                if re.search(r"\bon(click|change|input|submit)\s*=", line) and (
                    "<" in line or "`" in line or '"' in line
                ):
                    stripped = line.strip()
                    if stripped.startswith(("//", "*", "/*")):
                        continue
                    # 只拦"字符串/模板里拼出来"的内联属性，React 的 onClick={} 不受影响
                    if re.search(r"on(click|change|input|submit)\s*=\s*[\"']", line) or re.search(
                        r"on(click|change|input|submit)=\\", line
                    ):
                        offenders.append(f"{rel}:{lineno}  {stripped[:100]}")
    assert offenders == [], (
        "内联事件属性在 CSP（script-src 'self'）下不会执行——请改用 React 的 onClick：\n  "
        + "\n  ".join(offenders)
    )
