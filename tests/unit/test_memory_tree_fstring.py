"""tests/unit/test_memory_tree_fstring.py — 复现 §1.1.3 / §1.1.4

Phase 1.1.3 修复: memory_tree.py:281 f-string 直接访问 r['title'] / r['score']，
当字段缺失或 None 时崩溃（TypeError: :.1f on NoneType）。
改为 r.get('title','') / r.get('final_score',0)。

Phase 1.1.4 修复: memory_tree.py:596 m.group(2) 越界——正则用了非捕获组
(?:struct|enum|trait|impl)，只有 1 个捕获组，group(2) 抛 IndexError。
改为捕获组 (struct|enum|trait|impl)。

本测试验证:
1. 静态检查: retrieve_context 内使用 r.get('title','') 而非 r['title']
2. 静态检查: retrieve_context 内使用 r.get('final_score',0) 而非 r['score']
3. _extract_rust_symbols 正确提取 struct/enum/trait/impl（m.group(2) 不越界）
4. _extract_rust_symbols 对空内容 / 无匹配内容返回空列表
"""
import os
import sys

_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _read_source() -> str:
    """读取 memory_tree.py 源码。"""
    path = os.path.join(_SRC, "services", "memory", "memory_tree.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_retrieve_context_uses_get_for_title():
    """静态检查: retrieve_context 内用 r.get('title','') 而非 r['title']。

    修复前: f"- **{r['title'][:N]}**..." → KeyError when 'title' missing
    修复后: f"- **{r.get('title','')[:N]}**..." → 空串降级
    """
    source = _read_source()
    assert "r.get('title', '')" in source or 'r.get("title", "")' in source, (
        "retrieve_context must use r.get('title', '') for safe access"
    )
    # 不应再有 r['title'] 的直接访问
    assert "r['title']" not in source, (
        "retrieve_context must not use r['title'] directly (KeyError risk)"
    )


def test_retrieve_context_uses_get_for_final_score():
    """静态检查: retrieve_context 内用 r.get('final_score',0) 而非 r['score']。

    修复前: f"评分: {r['score']:.2f}" → TypeError when score is None
    修复后: f"评分: {r.get('final_score',0):.2f}" → 0 降级
    """
    source = _read_source()
    assert "r.get('final_score'" in source or 'r.get("final_score"' in source, (
        "retrieve_context must use r.get('final_score', 0) for safe access"
    )


def test_retrieve_context_uses_get_for_tags():
    """静态检查: retrieve_context 内用 r.get('tags',[]) 而非 r['tags']。"""
    source = _read_source()
    assert "r.get('tags'" in source or 'r.get("tags"' in source, (
        "retrieve_context must use r.get('tags', []) for safe access"
    )


def test_extract_rust_symbols_is_module_function():
    """静态检查: _extract_rust_symbols 是模块级函数（非 MemoryTree 方法）。"""
    source = _read_source()
    # 应有模块级 def _extract_rust_symbols(content: str)
    assert "def _extract_rust_symbols(content: str)" in source, (
        "_extract_rust_symbols must be a module-level function"
    )


def test_extract_rust_symbols_struct():
    """正则提取 Rust struct 定义——m.group(2) 不抛 IndexError。"""
    from services.memory.memory_tree import _extract_rust_symbols
    content = """
pub struct User {
    name: String,
    age: u32,
}

struct Point {
    x: f64,
    y: f64,
}
"""
    # 修复前: IndexError: no such group (m.group(2) on 1-group regex)
    # 修复后: 2 capture groups, group(2) = name, group(1) = kind
    symbols = _extract_rust_symbols(content)
    assert isinstance(symbols, list)
    assert len(symbols) >= 2
    names = [s["name"] for s in symbols]
    assert "User" in names
    assert "Point" in names
    kinds = [s["kind"] for s in symbols]
    assert "struct" in kinds


def test_extract_rust_symbols_enum_trait_impl():
    """提取 enum / trait / impl 定义——m.group(2) 返回名称。

    注意: 正则 r'impl\s+(\w+)' 匹配 impl 后的第一个 \w+，
    所以 `impl Drawable for Circle` 的 name=Drawable（不是 Circle）。
    """
    from services.memory.memory_tree import _extract_rust_symbols
    content = """
enum Color {
    Red,
    Green,
    Blue,
}

trait Drawable {
    fn draw(&self);
}

impl Drawable for Circle {
    fn draw(&self) {}
}
"""
    symbols = _extract_rust_symbols(content)
    names = [s["name"] for s in symbols]
    kinds = [s["kind"] for s in symbols]
    # enum Color → name=Color, kind=enum
    assert "Color" in names
    assert "enum" in kinds
    # trait Drawable → name=Drawable, kind=trait
    assert "Drawable" in names
    assert "trait" in kinds
    # impl Drawable for Circle → name=Drawable（第一个 \w+）, kind=impl
    assert "impl" in kinds
    # 验证 impl 的 name 是 Drawable（首个词），而非 Circle
    impl_symbols = [s for s in symbols if s["kind"] == "impl"]
    assert len(impl_symbols) >= 1
    assert impl_symbols[0]["name"] == "Drawable"


def test_extract_rust_symbols_empty_content():
    """空内容返回空列表。"""
    from services.memory.memory_tree import _extract_rust_symbols
    assert _extract_rust_symbols("") == []


def test_extract_rust_symbols_no_match():
    """无 Rust 类型定义的内容返回空列表。"""
    from services.memory.memory_tree import _extract_rust_symbols
    content = "This is just plain text with no Rust types."
    assert _extract_rust_symbols(content) == []


def test_extract_rust_symbols_regex_has_two_capture_groups():
    """静态检查: struct/enum/trait/impl 正则有 2 个捕获组（kind + name）。

    修复前: r'^\\s*(?:pub\\s+)?(?:struct|enum|trait|impl)\\s+(\\w+)' — 1 group
    修复后: r'^\\s*(?:pub\\s+)?(struct|enum|trait|impl)\\s+(\\w+)' — 2 groups
    """
    import re
    source = _read_source()
    # 提取 _extract_rust_symbols 函数体
    m = re.search(r"def _extract_rust_symbols\(content: str\).*?(?=\ndef |\Z)", source, re.DOTALL)
    assert m, "_extract_rust_symbols function not found"
    body = m.group(0)
    # 找 struct/enum/trait/impl 正则
    pat = re.search(
        r"r['\"]([^'\"]*(?:struct|enum|trait|impl)[^'\"]*)['\"]",
        body,
    )
    assert pat, "struct/enum/trait/impl regex pattern not found"
    pattern_str = pat.group(1)
    # 编译正则并验证捕获组数量
    compiled = re.compile(pattern_str, re.MULTILINE)
    assert compiled.groups >= 2, (
        f"Regex must have at least 2 capture groups (kind + name), got {compiled.groups}: "
        f"{pattern_str}"
    )
