"""prompts/intent_classifier.py —— P1: IntentRouter LLM 分类提示词

Phase 2: LLM 分类器作为正则引擎的降级/增强路径。
当 COMPILED_RULES 未匹配时，启用 LLM 分类（Haiku 轻量模型）。
"""
__version__ = "1.0.0"

import json
import logging

_log = logging.getLogger("yuncat.intent_classifier")

INTENT_CLASSIFIER_PROMPT = """你是意图分类器。分析用户输入，输出 JSON 路由决策。

## 可用路由
- {{"type":"direct","agent_id":"yuncat"}} —— 通用问答、搜索、文件操作、代码编写（原生 Yuncat 主 Agent）
- {{"type":"workflow","template":"data_analysis"}} —— 数据分析
- {{"type":"workflow","template":"code_review"}} —— 代码审查
- {{"type":"workflow","template":"doc_generation"}} —— 文档生成

注：视觉分析/网页搜索/跨平台消息等外部能力由 Yuncat 经 delegate_to_agent("openclaw") 委托，
不在自动分类中直接选外部 Agent（避免把任务交给未启动的外部网关）。

## 分类规则
1. "写代码/编程/函数/重构" → yuncat
2. "分析数据/CSV/Excel/图表" → data_analysis
3. "审查代码/code review" → code_review
4. "写文档/README" → doc_generation
5. 其他 → yuncat（默认，原生主 Agent）

## 输出
仅输出 JSON，不要 Markdown 包裹：
{{"type":"...","agent_id":"...","confidence":0.95,"reason":"..."}}

## 用户输入
{prompt}"""


async def llm_classify(prompt: str, summarizer=None) -> dict | None:
    """调用 LLM 做意图分类。返回 {"type","agent_id","confidence","reason"} 或 None（失败时）。

    `summarizer` 是可调用对象——与 MemoryService._summarizer 同签名。
    Phase 2 中注入外部后端的轻量 invoke 函数。
    """
    if not summarizer:
        return None
    classify_prompt = INTENT_CLASSIFIER_PROMPT.format(prompt=prompt)
    try:
        result_text = await summarizer(classify_prompt)
        # 提取 JSON（可能被 markdown 包裹）
        result_text = result_text.strip()
        if result_text.startswith("```"):
            result_text = result_text.split("\n", 1)[-1]
            result_text = result_text.rsplit("```", 1)[0]
        return json.loads(result_text)
    except Exception as e:  # JSONDecodeError 已被 Exception 覆盖
        _log.warning(f"LLM classification failed: {e}")
        return None
