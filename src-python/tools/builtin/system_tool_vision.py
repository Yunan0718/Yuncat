"""system_tool_vision.py —— Task 6.2: 视觉分析工具

对标 Claude Code / Cursor 的图片分析能力。

设计:
- Agent 主动分析图片时调用此工具（如读截图、检查 UI、提取文字）
- 优先委托给 OpenClaw 的 openclaw_vision（Peekaboo v3 视觉引擎）
- OC 不可用时返回错误，提示用户直接附件图片让 LLM 多模态处理

注意: 用户直接拖拽/粘贴图片到聊天框时，base.py:502-511 已自动注入 LLM multimodal messages，
无需调用此工具。此工具用于 Agent 在执行任务中需要主动分析图片的场景。
"""
import base64
import logging

from ..registry import tool
from config import ERROR_PREVIEW_CHARS, OPENCLAW_AGENT_ID

_log = logging.getLogger("yuncat.tools")

# 支持的图片 MIME 类型
_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _resolve_image_source(image: str, image_path: str) -> tuple[str, str | None]:
    """解析图片来源——返回 (image_data_url, error)。

    优先级:
    1. image 是 data URL（base64）→ 直接用
    2. image 是 HTTP URL → 直接用
    3. image_path 是本地文件 → 读文件转 data URL
    4. image 是本地文件路径 → 读文件转 data URL
    """
    from security.url_guard import validate_external_url
    from services.attachment_store import decode_image, resolve_allowed_image_path

    if image:
        if image.startswith("data:"):
            mime = image[5:].split(";", 1)[0] or "image/png"
            try:
                decode_image({"type": mime, "data": image})
            except ValueError as exc:
                return "", f"Invalid image data URL: {exc}"
            return image, None
        if image.startswith(("http://", "https://")):
            try:
                return validate_external_url(image), None
            except ValueError as exc:
                return "", f"Image URL rejected: {exc}"
        # image 可能是本地路径（相对路径按共享工作区解析）
        try:
            return _file_to_data_url(image), None
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            return "", f"Image path rejected: {exc}"

    if image_path:
        try:
            return _file_to_data_url(image_path), None
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            return "", f"Image path rejected: {exc}"

    return "", "No valid image source provided. Pass image (data URL/URL/path) or image_path."


def _file_to_data_url(path: str) -> str:
    """本地图片文件 → base64 data URL。"""
    from services.attachment_store import resolve_allowed_image_path
    resolved = resolve_allowed_image_path(path)
    ext = resolved.suffix.lower()
    mime = _IMAGE_EXTENSIONS.get(ext, "image/png")
    with open(resolved, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


@tool(
    name="vision_analyze", agents=["hermes", "openclaw"],
    description=(
        "Analyze an image using vision capabilities. "
        "Use this when you need to read text from an image, inspect UI elements, "
        "check for errors in a screenshot, or understand visual content. "
        "Supports: screenshots, photos, diagrams, UI elements, code images. "
        "Note: If the user attached an image directly in the chat, you can already 'see' it "
        "without calling this tool. This tool is for analyzing images from file paths or URLs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "image": {
                "type": "string",
                "description": (
                    "Image source: base64 data URL (data:image/...;base64,...), "
                    "HTTP URL (https://...), or local file path."
                ),
            },
            "image_path": {
                "type": "string",
                "description": "Alternative: local file path to the image (used if image is empty).",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Analysis instruction — what to detect/extract. "
                    "Examples: 'Read all text', 'Check for error dialogs', "
                    "'Describe UI layout', 'Extract code from screenshot'."
                ),
            },
        },
    },
    is_read_only=True,
    groups=["vision"],
    tool_timeout=60,  # 视觉分析超时（委托 OC 可能跑完整推理）
)
async def vision_analyze(image: str = "", image_path: str = "",
                         prompt: str = "Describe this image in detail.") -> dict:
    """Task 6.2: 视觉分析——委托 OpenClaw 的 openclaw_vision（Peekaboo v3 引擎）。

    对标 Claude Code 的图片分析能力。
    流程: 解析图片来源 → 委托 delegate_to_agent("openclaw", "openclaw_vision", {...})
    """
    from tools.result import ToolResult

    # 1. 解析图片来源
    image_url, error = _resolve_image_source(image, image_path)
    if error:
        return ToolResult(
            success=False,
            error=error,
            meta={"tool_name": "vision_analyze"},
        ).to_dict()

    # 2. 委托给 OpenClaw 的 openclaw_vision
    try:
        from adapters.registry import get_global_adapters
        from context import get_current_agent_id
        from services.capability_router import delegate_tool

        adapters = get_global_adapters()
        if not adapters:
            return ToolResult(
                success=False,
                error="No agents available for vision analysis.",
                meta={"tool_name": "vision_analyze"},
            ).to_dict()

        from_agent_id = get_current_agent_id() or "unknown"

        # 检查 OC 是否可用
        oc_adapter = adapters.get(OPENCLAW_AGENT_ID)
        if not oc_adapter or not hasattr(oc_adapter, "execute_tool"):
            return ToolResult(
                success=False,
                error=(
                    "OpenClaw backend not available for vision analysis. "
                    "Alternative: ask the user to attach the image directly in the chat "
                    "so the LLM can process it via multimodal input."
                ),
                meta={"tool_name": "vision_analyze"},
            ).to_dict()

        # 委托 OC 执行 openclaw_vision
        result = await delegate_tool(
            from_agent_id=from_agent_id,
            to_agent_id="openclaw",
            tool_name="openclaw_vision",
            arguments={"image_url": image_url, "prompt": prompt},
            adapters=adapters,
        )

        if result.get("ok") or result.get("success"):
            return ToolResult(
                success=True,
                data={"analysis": result.get("result", result), "image_source": image_url[:100]},
                observation="视觉分析完成（委托 OpenClaw openclaw_vision）",
                meta={"tool_name": "vision_analyze", "delegated_to": "openclaw"},
            ).to_dict()
        else:
            return ToolResult(
                success=False,
                error=result.get("error", "Vision analysis failed")[:ERROR_PREVIEW_CHARS],
                meta={"tool_name": "vision_analyze", "delegated_to": "openclaw"},
            ).to_dict()

    except Exception as e:
        _log.exception("vision_analyze failed")
        return ToolResult(
            success=False,
            error=f"Vision analysis error: {e}"[:ERROR_PREVIEW_CHARS],
            meta={"tool_name": "vision_analyze"},
        ).to_dict()
