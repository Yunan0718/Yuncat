"""tests/unit/test_vision_analyze.py —— Task 6.2: 视觉分析工具

对标: Claude Code / Cursor 的图片分析能力。

设计:
- vision_analyze 让 Agent 主动分析图片（区别于用户被动附件，后者由 base.py:502-511 注入 LLM multimodal）
- 优先委托 OpenClaw 的 openclaw_vision（Peekaboo v3 引擎）
- OC 不可用时返回错误，提示用户直接附件图片让 LLM 多模态处理

覆盖:
- _resolve_image_source: data URL / HTTP URL / 本地文件 / image_path / 无效来源
- _file_to_data_url: MIME 类型正确（png/jpeg/webp/未知扩展名默认 png）
- vision_analyze: 委托 OC 成功 / adapters 为空 / OC 不存在 / OC 无 execute_tool /
                  委托失败 / 委托异常 / 无效图片来源 / image_path 本地文件 /
                  默认 prompt / from_agent_id 从 context 获取 / success 键判定
"""
import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


def _workspace_path(tmp_path, monkeypatch, name: str):
    monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path))
    shared = tmp_path / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    return shared / name


# ═══════════════════════════════════════════════════════════
# _resolve_image_source 辅助函数
# ═══════════════════════════════════════════════════════════

class TestResolveImageSource:
    """Task 6.2: _resolve_image_source 解析图片来源——返回 (image_data_url, error)"""

    def test_data_url_returned_directly(self):
        """data URL（base64）直接返回，不读文件"""
        from tools.builtin.system_tool_vision import _resolve_image_source

        data_url = "data:image/png;base64,iVBORw0KGgo="
        result, error = _resolve_image_source(data_url, "")
        assert error is None
        assert result == data_url

    def test_http_url_returned_directly(self):
        """HTTP URL 直接返回"""
        from tools.builtin.system_tool_vision import _resolve_image_source

        url = "https://github.com/image.png"
        result, error = _resolve_image_source(url, "")
        assert error is None
        assert result == url

    def test_local_file_path_in_image(self, tmp_path, monkeypatch):
        """image 参数是本地文件路径 → 读文件转 data URL"""
        from tools.builtin.system_tool_vision import _resolve_image_source

        img = _workspace_path(tmp_path, monkeypatch, "test.png")
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake_png")
        result, error = _resolve_image_source(str(img), "")
        assert error is None
        assert result.startswith("data:image/png;base64,")

    def test_local_file_path_in_image_path(self, tmp_path, monkeypatch):
        """image_path 参数是本地文件 → 读文件转 data URL"""
        from tools.builtin.system_tool_vision import _resolve_image_source

        img = _workspace_path(tmp_path, monkeypatch, "photo.jpg")
        img.write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")
        result, error = _resolve_image_source("", str(img))
        assert error is None
        assert result.startswith("data:image/jpeg;base64,")

    def test_no_valid_source_returns_error(self):
        """无有效图片来源 → 返回错误"""
        from tools.builtin.system_tool_vision import _resolve_image_source

        result, error = _resolve_image_source("", "")
        assert error is not None
        assert "No valid image source" in error
        assert result == ""

    def test_image_path_used_when_image_empty(self, tmp_path, monkeypatch):
        """image 为空时使用 image_path"""
        from tools.builtin.system_tool_vision import _resolve_image_source

        img = _workspace_path(tmp_path, monkeypatch, "diagram.gif")
        img.write_bytes(b"GIF89afake_gif")
        result, error = _resolve_image_source("", str(img))
        assert error is None
        assert result.startswith("data:image/gif;base64,")

    def test_nonexistent_image_path_returns_error(self):
        """image_path 指向不存在的文件 → 返回错误"""
        from tools.builtin.system_tool_vision import _resolve_image_source

        result, error = _resolve_image_source("", "/nonexistent/path/image.png")
        assert error is not None
        assert "No valid image source" in error

    def test_local_file_outside_workspace_is_rejected(self, tmp_path, monkeypatch):
        from tools.builtin.system_tool_vision import _resolve_image_source

        monkeypatch.setenv("YUNCAT_WORKSPACE", str(tmp_path / "workspace"))
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"\x89PNG\r\n\x1a\nfake_png")
        result, error = _resolve_image_source("", str(outside))
        assert result == ""
        assert "outside the allowed" in error

    def test_insecure_or_private_image_url_is_rejected(self):
        from tools.builtin.system_tool_vision import _resolve_image_source

        result, error = _resolve_image_source("http://127.0.0.1/private.png", "")
        assert result == ""
        assert "URL rejected" in error

    def test_invalid_data_url_is_rejected(self):
        from tools.builtin.system_tool_vision import _resolve_image_source

        result, error = _resolve_image_source("data:image/png;base64,NOT_BASE64!", "")
        assert result == ""
        assert "Invalid image data URL" in error


# ═══════════════════════════════════════════════════════════
# _file_to_data_url 辅助函数
# ═══════════════════════════════════════════════════════════

class TestFileToDataUrl:
    """Task 6.2: _file_to_data_url 本地文件转 base64 data URL"""

    def test_png_file(self, tmp_path, monkeypatch):
        """PNG 文件 → data:image/png;base64,..."""
        from tools.builtin.system_tool_vision import _file_to_data_url

        img = _workspace_path(tmp_path, monkeypatch, "test.png")
        content = b"\x89PNG\r\n\x1a\nfake_png_data"
        img.write_bytes(content)
        result = _file_to_data_url(str(img))
        assert result.startswith("data:image/png;base64,")
        # 验证 base64 内容可逆
        b64_part = result.split(",", 1)[1]
        assert base64.b64decode(b64_part) == content

    def test_jpeg_file(self, tmp_path, monkeypatch):
        """JPEG 文件 → data:image/jpeg;base64,..."""
        from tools.builtin.system_tool_vision import _file_to_data_url

        img = _workspace_path(tmp_path, monkeypatch, "photo.jpeg")
        img.write_bytes(b"\xff\xd8\xff\xe0fake_jpeg")
        result = _file_to_data_url(str(img))
        assert result.startswith("data:image/jpeg;base64,")

    def test_webp_file(self, tmp_path, monkeypatch):
        """WebP 文件 → data:image/webp;base64,..."""
        from tools.builtin.system_tool_vision import _file_to_data_url

        img = _workspace_path(tmp_path, monkeypatch, "img.webp")
        img.write_bytes(b"RIFFfake_webp")
        result = _file_to_data_url(str(img))
        assert result.startswith("data:image/webp;base64,")

    def test_unknown_extension_defaults_to_png(self, tmp_path, monkeypatch):
        """未知扩展名 → 默认 image/png（安全 fallback）"""
        from tools.builtin.system_tool_vision import _file_to_data_url

        img = _workspace_path(tmp_path, monkeypatch, "img.xyz")
        img.write_bytes(b"unknown_format")
        result = _file_to_data_url(str(img))
        assert result.startswith("data:image/png;base64,")


# ═══════════════════════════════════════════════════════════
# vision_analyze 工具主函数
# ═══════════════════════════════════════════════════════════

class TestVisionAnalyze:
    """Task 6.2: vision_analyze 工具行为"""

    async def test_delegate_oc_success(self, monkeypatch):
        """委托 OC openclaw_vision 成功 → 返回 analysis 数据"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_delegate(**kwargs):
            return {"ok": True, "result": "Image contains: a red button, a text field"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True, "result": "oc handled"}

        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        result = await vision_analyze(
            image="data:image/png;base64,iVBORw0KGgo=",
            prompt="Read all text",
        )

        assert result["success"] is True
        assert result["meta"]["tool_name"] == "vision_analyze"
        assert result["meta"]["delegated_to"] == "openclaw"
        assert "analysis" in result["data"]
        assert "a red button" in result["data"]["analysis"]

    async def test_adapters_empty_returns_error(self, monkeypatch):
        """adapters 为空 → 返回 'No agents available' 错误"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")
        monkeypatch.setattr("adapters.registry.get_global_adapters", lambda: {})

        result = await vision_analyze(image="data:image/png;base64,iVBORw0KGgo=")

        assert result["success"] is False
        assert "No agents available" in result["error"]

    async def test_oc_not_in_adapters_returns_error(self, monkeypatch):
        """OC 不在 adapters → 返回错误（含 fallback 提示：让用户直接附件图片）"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")
        # 只有 hermes，没有 openclaw
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"hermes": object()})

        result = await vision_analyze(image="data:image/png;base64,iVBORw0KGgo=")

        assert result["success"] is False
        assert "OpenClaw backend not available" in result["error"]
        # 错误信息应包含 fallback 提示——让用户直接附件图片让 LLM 多模态处理
        assert "attach" in result["error"].lower()

    async def test_oc_no_execute_tool_returns_error(self, monkeypatch):
        """OC adapter 无 execute_tool 方法 → 返回错误"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")
        # OC adapter 存在但没有 execute_tool 方法（普通 object()）
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": object()})

        result = await vision_analyze(image="data:image/png;base64,iVBORw0KGgo=")

        assert result["success"] is False
        assert "OpenClaw backend not available" in result["error"]

    async def test_delegate_failure_returns_error(self, monkeypatch):
        """委托失败（OC 返回 ok=False）→ 返回错误"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_delegate(**kwargs):
            return {"ok": False, "error": "Vision model unavailable"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": False, "error": "fail"}

        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        result = await vision_analyze(image="data:image/png;base64,iVBORw0KGgo=")

        assert result["success"] is False
        assert "Vision model unavailable" in result["error"]

    async def test_delegate_exception_handled(self, monkeypatch):
        """delegate_tool 抛异常 → 异常处理返回错误（不向上抛出）"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_delegate(**kwargs):
            raise RuntimeError("network timeout")
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}

        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        result = await vision_analyze(image="data:image/png;base64,iVBORw0KGgo=")

        assert result["success"] is False
        assert "Vision analysis error" in result["error"]
        assert "network timeout" in result["error"]

    async def test_invalid_image_source_returns_error_without_delegating(self, monkeypatch):
        """无效图片来源 → 返回错误，且不调用 delegate_tool"""
        from tools.builtin.system_tool_vision import vision_analyze

        delegate_called = False

        async def _mock_delegate(**kwargs):
            nonlocal delegate_called
            delegate_called = True
            return {"ok": True}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)
        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": object()})

        result = await vision_analyze(image="", image_path="")

        assert result["success"] is False
        assert "No valid image source" in result["error"]
        assert delegate_called is False  # 没有调用 delegate_tool

    async def test_image_path_local_file_delegates_with_data_url(self, monkeypatch, tmp_path):
        """image_path 传入本地文件 → 读文件转 data URL → 委托成功"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        captured_args = {}

        async def _mock_delegate(**kwargs):
            captured_args.update(kwargs)
            return {"ok": True, "result": "OCR result: hello world"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}

        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        # 创建本地图片文件
        img = _workspace_path(tmp_path, monkeypatch, "screenshot.png")
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake_screenshot")

        result = await vision_analyze(image_path=str(img), prompt="Extract text")

        assert result["success"] is True
        # 验证委托参数包含转换后的 data URL（不是原始文件路径）
        assert captured_args["arguments"]["image_url"].startswith("data:image/png;base64,")
        assert captured_args["arguments"]["prompt"] == "Extract text"
        assert captured_args["to_agent_id"] == "openclaw"
        assert captured_args["tool_name"] == "openclaw_vision"
        assert captured_args["from_agent_id"] == "hermes"

    async def test_default_prompt_used_when_empty(self, monkeypatch):
        """prompt 为空时使用默认值 'Describe this image in detail.'"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        captured_prompt = None

        async def _mock_delegate(**kwargs):
            nonlocal captured_prompt
            captured_prompt = kwargs.get("arguments", {}).get("prompt")
            return {"ok": True, "result": "described"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}

        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        result = await vision_analyze(image="data:image/png;base64,iVBORw0KGgo=")

        assert result["success"] is True
        assert captured_prompt == "Describe this image in detail."

    async def test_from_agent_id_from_context(self, monkeypatch):
        """from_agent_id 从 context.get_current_agent_id() 获取"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        captured_from = None

        async def _mock_delegate(**kwargs):
            nonlocal captured_from
            captured_from = kwargs.get("from_agent_id")
            return {"ok": True, "result": "ok"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}

        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        result = await vision_analyze(image="data:image/png;base64,iVBORw0KGgo=")

        assert result["success"] is True
        assert captured_from == "hermes"

    async def test_success_key_only_no_ok(self, monkeypatch):
        """OC 返回 success=True（无 ok 键）→ 判定成功（对齐 delegate_tool 逻辑）"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        async def _mock_delegate(**kwargs):
            return {"success": True, "data": "vision result"}  # 无 ok 键
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}

        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        result = await vision_analyze(image="data:image/png;base64,iVBORw0KGgo=")

        assert result["success"] is True

    async def test_http_url_passed_directly_to_delegate(self, monkeypatch):
        """HTTP URL 直接传给 delegate_tool（不转 base64）"""
        from tools.builtin.system_tool_vision import vision_analyze

        monkeypatch.setattr("context.get_current_agent_id", lambda: "hermes")

        captured_url = None

        async def _mock_delegate(**kwargs):
            nonlocal captured_url
            captured_url = kwargs.get("arguments", {}).get("image_url")
            return {"ok": True, "result": "ok"}
        monkeypatch.setattr("services.capability_router.delegate_tool", _mock_delegate)

        class _OCAdapter:
            async def execute_tool(self, tool_name, arguments):
                return {"ok": True}

        monkeypatch.setattr("adapters.registry.get_global_adapters",
                            lambda: {"openclaw": _OCAdapter()})

        url = "https://github.com/photo.png"
        result = await vision_analyze(image=url)

        assert result["success"] is True
        assert captured_url == url  # URL 原样传递，不转 base64
