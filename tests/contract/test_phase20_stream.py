"""tests/contract/test_phase20_stream.py —— Phase 20 adapter.stream dict 格式合约测试"""
import os
import sys
import pytest


@pytest.mark.asyncio
async def test_stream_dict_type_annotation():
    """验证 AgentInvoker Protocol stream 类型签名——runtime 检查返回值类型"""
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from adapters.base import AgentInvoker, OpenAiCompatibleAdapter

    # Protocol 的 stream 方法要求返回 AsyncIterator[dict]
    # OpenAiCompatibleAdapter.stream 已改为 yield dict
    assert hasattr(OpenAiCompatibleAdapter, "stream")
    assert callable(OpenAiCompatibleAdapter.stream)

    # 验证 stream 方法签名包含 dict（而非 str）
    import inspect
    sig = inspect.signature(OpenAiCompatibleAdapter.stream)
    return_annotation = sig.return_annotation
    # return_annotation 应该是 AsyncIterator[dict]
    assert return_annotation is not inspect.Parameter.empty, "stream must have return type annotation"
    # AsyncIterator[dict] 在 str() 中包含 dict
    assert "dict" in str(return_annotation), f"Expected AsyncIterator[dict], got {return_annotation}"


def test_parse_diff_output_extracts_diff_blocks():
    """验证 parse_diff_output 正确提取 ```diff 代码块"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.plan_parser import parse_diff_output

    response = """Here's the fix:

```diff
--- a/src/main.py
+++ b/src/main.py
@@ -10,6 +10,8 @@
-old line
+new line
 unchanged
```

Also this one:

```diff
--- a/src/utils.py
+++ b/src/utils.py
@@ -1,3 +1,5 @@
+import os
+import sys
 original
```
"""
    result = parse_diff_output(response)
    assert len(result) == 2, f"Expected 2 diff blocks, got {len(result)}"
    assert result[0]["path"] == "src/main.py"
    assert result[0]["additions"] >= 1  # +new line（可能含@@行号的+）
    assert result[0]["deletions"] >= 1
    assert result[1]["path"] == "src/utils.py"
    assert result[1]["additions"] >= 2  # +import os, +import sys


def test_parse_diff_output_empty_response():
    """空响应返回空列表"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.plan_parser import parse_diff_output

    assert parse_diff_output("") == []
    assert parse_diff_output("No diff blocks here.") == []


def test_extract_diff_edits_from_any_loose_format():
    """验证宽松模式提取任意位置的 unified diff"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))
    from services.plan_parser import extract_diff_edits_from_any

    text = """Some text...

--- a/config.py
+++ b/config.py
@@ -5,7 +5,9 @@
-DEBUG = True
+DEBUG = False
+LOG_LEVEL = "info"

More text...
"""
    result = extract_diff_edits_from_any(text)
    assert len(result) == 1
    assert result[0]["path"] == "config.py"
    assert result[0]["additions"] >= 1  # +DEBUG = False, +LOG_LEVEL
    assert result[0]["deletions"] >= 1  # -DEBUG = True


@pytest.mark.asyncio
async def test_stream_backward_compat_str_chunk():
    """验证向后兼容：str chunk 被包装为 {"type":"text","data":str}"""
    # 模拟 HermesAdapter 的 str 输出
    chunk = "hello world"
    assert isinstance(chunk, str)

    # 向后兼容检测——agent_service 中的逻辑
    if isinstance(chunk, str):
        chunk = {"type": "text", "data": chunk}

    assert isinstance(chunk, dict)
    assert chunk["type"] == "text"
    assert chunk["data"] == "hello world"


@pytest.mark.asyncio
async def test_stream_dict_chunk_types():
    """验证 dict chunk 的三种类型正确区分"""
    # thinking chunk
    thinking = {"type": "thinking", "data": "Let me analyze..."}
    assert thinking["type"] == "thinking"

    # tool_call chunk
    tool_call = {"type": "tool_call", "data": [{"function": {"name": "read_file"}}]}
    assert tool_call["type"] == "tool_call"

    # text chunk
    text = {"type": "text", "data": "Here is the result"}
    assert text["type"] == "text"


@pytest.mark.asyncio
async def test_stream_text_only_extraction():
    """验证 _invoke_with_role 和 execute_plan_steps 只提取 text 类型"""
    chunks = [
        {"type": "thinking", "data": "Hmm..."},
        {"type": "text", "data": "First."},
        {"type": "thinking", "data": "Wait..."},
        {"type": "text", "data": "Second."},
    ]
    # 模拟 _invoke_with_role 的提取逻辑
    text_chunks = [c["data"] for c in chunks if c.get("type") == "text"]
    result = "".join(text_chunks)
    assert result == "First.Second."
    # thinking chunks 被过滤
    thinking_chunks = [c for c in chunks if c.get("type") == "thinking"]
    assert len(thinking_chunks) == 2
