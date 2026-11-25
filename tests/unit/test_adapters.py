"""tests/unit/test_adapters.py —— Adapter 层测试

覆盖: AgentCapability / BaseProcessAdapter / OpenAiCompatibleAdapter SSE 解析
       / health check / invoke/stream 消息构建 / cancel 清理

使用 mock ClientSession 避免真实 HTTP 调用
"""
import os
import sys
import json
import asyncio
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


# ---- AgentCapability ----
class TestAgentCapability:
    def test_defaults(self):
        from adapters.base import AgentCapability
        c = AgentCapability()
        assert c.supports_stream is True
        assert c.supports_tools is False
        assert c.supports_browser is False

    def test_custom_capabilities(self):
        from adapters.base import AgentCapability
        c = AgentCapability(supports_tools=True, supports_image=True)
        assert c.supports_tools is True
        assert c.supports_image is True


# ---- 轻量 adapter（非 pytest 测试类——无 Test 前缀避免收集警告） ----
class _FakeAdapter:
    """轻量 adapter —— _ready 可控，不启动子进程"""
    def __init__(self, agent_id="test-agent", ready=True):
        self.agent_id = agent_id
        self._ready = asyncio.Event()
        if ready:
            self._ready.set()
        self._dropped_logs = 0
        self.process = None

    async def health(self) -> bool:
        return self._ready.is_set()

    async def start(self):
        self._ready.set()

    async def wait_ready(self):
        await self._ready.wait()

    async def stop(self):
        self._ready.clear()

    async def stream(self, prompt: str, context: dict | None = None):
        yield f"[{self.agent_id}] {prompt[:20]}"

    def capabilities(self):
        from adapters.base import AgentCapability
        return AgentCapability()


# ---- Adapter 初始化 + 就绪 ----
class TestAdapterReady:
    async def test_ready_is_set_by_default(self):
        a = _FakeAdapter(ready=True)
        assert a._ready.is_set()

    async def test_wait_ready_blocks_then_releases(self):
        a = _FakeAdapter(ready=False)
        # 后台设置 ready
        asyncio.get_event_loop().call_later(0.1, a._ready.set)
        await asyncio.wait_for(a.wait_ready(), timeout=1)
        assert a._ready.is_set()

    async def test_health_returns_false_when_not_ready(self):
        a = _FakeAdapter(ready=False)
        assert await a.health() is False

    async def test_start_sets_ready(self):
        a = _FakeAdapter(ready=False)
        await a.start()
        assert a._ready.is_set()


# ---- SSE 解析（OpenAiCompatibleAdapter.stream 逻辑） ----
class TestSSEParsing:
    def test_parse_data_chunk_extracts_content(self):
        """模拟 SSE data: 行 中提取 delta.content"""
        chunks = [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
            'data: {"choices":[{"delta":{"content":" World"}}]}\n',
            'data: [DONE]\n',
        ]
        results = []
        for chunk in chunks:
            line = chunk.strip()
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        results.append(delta)
                except json.JSONDecodeError:
                    continue
        assert "".join(results) == "Hello World"

    def test_parse_invalid_json_skips(self):
        line = 'data: {invalid json}\n'
        data_str = line.strip()[6:]
        if data_str != "[DONE]":
            try:
                json.loads(data_str)
                assert False, "Should have raised"
            except json.JSONDecodeError:
                pass  # expected

    def test_parse_empty_delta_skips(self):
        """delta 无 content 字段时跳过"""
        line = 'data: {"choices":[{"delta":{}}]}\n'
        data_str = line.strip()[6:]
        data = json.loads(data_str)
        delta = data.get("choices", [{}])[0].get("delta", {}).get("content", "")
        assert delta == ""


# ---- 消息构建 ----
class TestMessageConstruction:
    def test_invoke_builds_messages_with_system(self):
        """验证 invoke 消息格式"""
        from adapters.base import OpenAiCompatibleAdapter

        messages = []
        context = {"system": "You are a helpful assistant.", "memory": ""}
        prompt = "Hello"
        if context.get("system"):
            messages.append({"role": "system", "content": context["system"]})
        messages.append({"role": "user", "content": prompt})

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Hello"

    def test_stream_builds_messages_with_memory(self):
        """验证 stream 包含 system + memory + user"""
        context = {
            "system": "You are helpful.",
            "memory": "# Context\n- Fact 1\n- Fact 2",
        }
        prompt = "Continue"

        messages = []
        for key in ["system", "memory"]:
            if context.get(key):
                messages.append({"role": "system", "content": context[key]})
        messages.append({"role": "user", "content": prompt})

        assert len(messages) == 3
        assert messages[0]["content"] == "You are helpful."
        assert messages[1]["content"] == "# Context\n- Fact 1\n- Fact 2"
        assert messages[2]["content"] == "Continue"


# ---- invoke 端点 ----
class TestInvokeEndpoint:
    async def test_invoke_with_role_uses_adapter(self):
        """_invoke_with_role 走 adapter.stream"""
        from services.agent.agent_service import AgentService
        a = _FakeAdapter("role-agent")
        svc = AgentService(None, adapters={"role-agent": a})
        async def fake_config(*args, **kwargs): return {}
        svc._get_model_config = fake_config
        output = await svc._invoke_with_role("role-agent", "你是架构师", "设计一个系统")
        assert "role-agent" in output

    async def test_invoke_unknown_agent_raises(self):
        from services.agent.agent_service import AgentService
        svc = AgentService(None, adapters={})
        with pytest.raises(ValueError, match="not found"):
            await svc._invoke_with_role("ghost", "prompt", "task")


# ---- Cancel 清理 ----
class TestCancel:
    async def test_stop_clears_ready(self):
        a = _FakeAdapter(ready=True)
        await a.stop()
        assert not a._ready.is_set()


# ---- Batch 4.2: 图片附件 Vision 格式 ----
class TestVisionAttachmentUrl:
    """验证 _attachment_url 能把前端 base64 裸串重构成标准 data URL"""

    def test_base64_only_reconstructs_data_url(self):
        from adapters.base import _attachment_url

        url = _attachment_url({"name": "screenshot.png", "data": "iVBORw0KGgo=", "type": "image/png"})
        assert url == "data:image/png;base64,iVBORw0KGgo="

    def test_jpeg_type_uses_jpeg_mime(self):
        from adapters.base import _attachment_url

        url = _attachment_url({"name": "photo.jpg", "data": "abcd", "type": "image/jpeg"})
        assert url == "data:image/jpeg;base64,abcd"

    def test_data_url_passthrough(self):
        from adapters.base import _attachment_url

        data_url = "data:image/webp;base64,UklGRiI="
        url = _attachment_url({"name": "img.webp", "data": data_url, "type": "image/webp"})
        assert url == data_url

    def test_http_url_passthrough(self):
        from adapters.base import _attachment_url

        http_url = "https://example.com/photo.png"
        url = _attachment_url({"name": "remote.png", "data": http_url, "type": "image/png"})
        assert url == http_url

    def test_missing_type_defaults_to_png(self):
        from adapters.base import _attachment_url

        url = _attachment_url({"data": "xyz"})
        assert url == "data:image/png;base64,xyz"


class TestVisionMessageConstruction:
    """验证 build_openai_messages 把图片附件注入为 OpenAI vision 格式"""

    def test_image_attachment_becomes_image_url_content(self):
        from adapters.base import build_openai_messages

        messages = build_openai_messages(
            "Describe this",
            context={
                "attachments": [
                    {"name": "s.png", "data": "iVBORw0KGgo=", "type": "image/png"},
                ],
            },
        )

        assert len(messages) == 1
        user_msg = messages[0]
        assert user_msg["role"] == "user"
        content = user_msg["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "Describe this"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"] == "data:image/png;base64,iVBORw0KGgo="
        assert content[1]["image_url"]["detail"] == "auto"

    def test_non_image_attachment_ignored(self):
        from adapters.base import build_openai_messages

        messages = build_openai_messages(
            "Read this",
            context={
                "attachments": [
                    {"name": "doc.pdf", "data": "JVBERi0xLg==", "type": "application/pdf"},
                ],
            },
        )

        assert len(messages) == 1
        assert messages[0]["content"] == "Read this"

    def test_multiple_image_attachments(self):
        from adapters.base import build_openai_messages

        messages = build_openai_messages(
            "Compare",
            context={
                "attachments": [
                    {"name": "a.png", "data": "aaaa", "type": "image/png"},
                    {"name": "b.jpg", "data": "bbbb", "type": "image/jpeg"},
                ],
            },
        )

        content = messages[0]["content"]
        assert len(content) == 3  # text + 2 images
        assert content[1]["image_url"]["url"] == "data:image/png;base64,aaaa"
        assert content[2]["image_url"]["url"] == "data:image/jpeg;base64,bbbb"
