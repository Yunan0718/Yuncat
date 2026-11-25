"""tests/unit/test_lint_rules_ast.py —— Task 4.3: AST lint 规则测试

验证 lint_rules.py 从 regex 改 AST 后的检测行为：
- R1: 硬编码路径（不误报 URL http://）
- R2: print() 调用（不误报字符串字面量中的 "print("）
- R3: 裸 except Exception: pass（不误报注释）
- R4: execute(f"...") f-string SQL（不误报注释）
- ALLOWED_VIOLATIONS 从 JSON baseline 加载
- snippet 匹配替代行号（行号漂移免疫）
"""
import os
import sys
import ast
import json
import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from tools import lint_rules


# ═══════════════════════════════════════════════════════════
# R1: 硬编码路径检测
# ═══════════════════════════════════════════════════════════

def test_r1_detects_windows_absolute_path():
    """R1 检测 Windows 绝对路径 C:\\Windows"""
    code = 'path = "C:\\\\Windows\\\\System32"'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r1(tree, lines)
    assert len(issues) >= 1
    assert issues[0][1] == "硬编码路径"


def test_r1_detects_unix_home_path():
    """R1 检测 Unix /home/ 和 /Users/ 路径"""
    code = 'p1 = "/home/user/file"\np2 = "/Users/me/doc"'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r1(tree, lines)
    assert len(issues) == 2


def test_r1_does_not_false_positive_on_url():
    """R1 不误报 URL——http://127.0.0.1 里的 p:/ 不是路径。

    Task 4.3 核心改进：原 regex [A-Za-z]:[/\\] 会匹配 http:// 里的 p:/ —— 严重误报。
    """
    code = 'url = "http://127.0.0.1:8080/health"\nurl2 = "https://api.example.com/v1"'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r1(tree, lines)
    assert len(issues) == 0, f"URL 不应触发 R1，但检测到: {issues}"


def test_r1_does_not_false_positive_on_https_url():
    """R1 不误报 https:// URL"""
    code = 'url = "https://api.deepseek.com/v1/chat"'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r1(tree, lines)
    assert len(issues) == 0


# ═══════════════════════════════════════════════════════════
# R2: print() 调用检测
# ═══════════════════════════════════════════════════════════

def test_r2_detects_print_call():
    """R2 检测 print() 调用"""
    code = 'print("hello")'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r2(tree, lines)
    assert len(issues) == 1
    assert "print()" in issues[0][1]


def test_r2_does_not_false_positive_on_string_literal():
    """R2 不误报字符串字面量中的 'print('。

    Task 4.3 核心改进：原 regex 会匹配字符串字面量里的 print( —— 误报。
    """
    code = 'hint = "Use print(x) to debug"'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r2(tree, lines)
    assert len(issues) == 0, f"字符串字面量中的 print( 不应触发 R2: {issues}"


def test_r2_does_not_false_positive_on_comment():
    """R2 不误报注释中的 print()"""
    code = '# print("this is a comment")'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r2(tree, lines)
    assert len(issues) == 0


# ═══════════════════════════════════════════════════════════
# R3: 裸 except Exception: pass 检测
# ═══════════════════════════════════════════════════════════

def test_r3_detects_bare_except_exception_pass():
    """R3 检测裸 except Exception: pass"""
    code = "try:\n    x = 1\nexcept Exception:\n    pass"
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r3(tree, lines)
    assert len(issues) == 1
    assert "except Exception" in issues[0][1]


def test_r3_does_not_detect_except_with_logging():
    """R3 不检测有实际处理的 except（非 pass）"""
    code = "try:\n    x = 1\nexcept Exception:\n    log.debug('failed')"
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r3(tree, lines)
    assert len(issues) == 0


def test_r3_does_not_false_positive_on_comment():
    """R3 不误报注释中的 'except Exception: pass'"""
    code = "# except Exception: pass\n# this is just a comment"
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r3(tree, lines)
    assert len(issues) == 0


def test_r3_does_not_detect_specific_exception():
    """R3 不检测特定异常类型（如 except ValueError: pass）"""
    code = "try:\n    x = 1\nexcept ValueError:\n    pass"
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r3(tree, lines)
    assert len(issues) == 0


# ═══════════════════════════════════════════════════════════
# R4: f-string SQL 构造检测
# ═══════════════════════════════════════════════════════════

def test_r4_detects_fstring_execute():
    """R4 检测 execute(f"...") f-string SQL"""
    code = 'await conn.execute(f"SELECT * FROM {table}")'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r4(tree, lines)
    assert len(issues) == 1
    assert "f-string SQL" in issues[0][1]


def test_r4_does_not_detect_parameterized_query():
    """R4 不检测参数化查询 execute("...?", params)"""
    code = 'await conn.execute("SELECT * FROM users WHERE id=?", (uid,))'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r4(tree, lines)
    assert len(issues) == 0


def test_r4_does_not_false_positive_on_comment():
    """R4 不误报注释中的 execute(f'...')"""
    code = '# await conn.execute(f"SELECT * FROM {table}")'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r4(tree, lines)
    assert len(issues) == 0


def test_r4_detects_multiline_execute():
    """R4 检测跨行 execute(f"...")——snippet 拼接多行"""
    code = 'cursor = await conn.execute(\n    "SELECT * FROM users "\n    f"WHERE id IN ({placeholders})",\n    ids,\n)'
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r4(tree, lines)
    assert len(issues) == 1
    # snippet 应包含 f-string 关键内容
    assert "placeholders" in issues[0][2] or "execute" in issues[0][2]


# ═══════════════════════════════════════════════════════════
# ALLOWED_VIOLATIONS / baseline 加载
# ═══════════════════════════════════════════════════════════

def test_baseline_loaded_from_json():
    """ALLOWED_VIOLATIONS 从 lint_baseline.json 加载——非空 dict"""
    assert isinstance(lint_rules.ALLOWED_VIOLATIONS, dict)
    assert len(lint_rules.ALLOWED_VIOLATIONS) > 0
    # 每个 key 是 (file, rule, snippet) 三元组
    for key in lint_rules.ALLOWED_VIOLATIONS:
        assert len(key) == 3
        assert key[1] in ("R1", "R2", "R3", "R4")


def test_baseline_json_file_exists():
    """lint_baseline.json 文件存在"""
    assert lint_rules._BASELINE_PATH.exists()


def test_snippet_matching_immune_to_line_drift():
    """snippet 匹配——行号漂移不影响违规抑制。

    Task 4.3 核心改进：原行号匹配随代码漂移失效，snippet 匹配免疫。
    """
    # 构造一个文件内容，违规在 L5
    code = "\n\n\n\nprint('hello')"
    tree = ast.parse(code)
    lines = code.split("\n")
    issues = lint_rules._check_r2(tree, lines)
    assert len(issues) == 1
    assert issues[0][0] == 5  # 行号是 5
    # snippet 是 "print('hello')"
    assert "print('hello')" in issues[0][2]


# ═══════════════════════════════════════════════════════════
# lint_file 集成
# ═══════════════════════════════════════════════════════════

def test_lint_file_returns_empty_for_syntax_error(tmp_path):
    """语法错误文件——返回空列表（不崩溃）"""
    f = tmp_path / "bad.py"
    f.write_text("def broken(:\n    pass\n", encoding="utf-8")
    result = lint_rules.lint_file(f)
    assert result == []


def test_lint_file_detects_new_violation(tmp_path, monkeypatch):
    """新违规（不在 baseline）——被检测到"""
    # 确保这个 snippet 不在 baseline 里
    f = tmp_path / "new_violation.py"
    f.write_text('print("new violation that is not in baseline")\n', encoding="utf-8")
    # mock SRC 和 ALLOWED_VIOLATIONS
    monkeypatch.setattr(lint_rules, "SRC", tmp_path)
    monkeypatch.setattr(lint_rules, "ALLOWED_VIOLATIONS", {})
    result = lint_rules.lint_file(f)
    assert len(result) >= 1
    assert "[R2]" in result[0]


def test_lint_file_suppresses_known_violation(tmp_path, monkeypatch):
    """已知违规（在 baseline）——被抑制"""
    f = tmp_path / "known.py"
    f.write_text('print("known")\n', encoding="utf-8")
    # mock ALLOWED_VIOLATIONS 包含这个 snippet
    monkeypatch.setattr(lint_rules, "SRC", tmp_path)
    monkeypatch.setattr(lint_rules, "ALLOWED_VIOLATIONS", {
        ("known.py", "R2", 'print("known")'): "known violation"
    })
    result = lint_rules.lint_file(f)
    assert result == []
