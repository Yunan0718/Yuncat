"""services/reasoning.py —— 思考强度（reasoning_effort）模型支持矩阵与参数映射

对标 OpenAI Codex CLI 的 model_reasoning_effort：
- 标准 6 档：none / minimal / low / medium / high / xhigh
- 各模型家族支持的档位不同（见 _FAMILY_SUPPORT）；
- 请求了不支持的档位时**自动 clamp 到最近支持档**（而非报错或硬发导致 400）。

参考矩阵（2026-09 核实）：
- gpt-5.1 / gpt-5.2（通用）:        none, low, medium, high      （minimal 会被服务端 400）
- gpt-5.1-codex / gpt-5.1-codex-mini: low/medium/high（mini 仅 medium/high）
- gpt-5.1-codex-max:                low, medium, high, xhigh
- gpt-5（legacy）/ o1 / o3 / o4-mini: minimal, low, medium, high
- deepseek（hybrid v3.1+/v4）:      思考开关走 thinking:{"type":"disabled"}
- qwen: enable_thinking；glm/doubao: thinking.type
- gemini: 走 OpenAI 兼容 shim（/v1beta/openai）→ 与 openai 同字段 reasoning_effort，
  **不发 thinkingBudget**（那是 Gemini 原生 API 字段，混入 OpenAI 兼容 body 必属错误）
- claude: OpenAI 兼容网关无统一关闭字段，best-effort 省略（budget 由网关自管）

规则（三层保险，按优先级）：
1. 宁可 clamp，也不发模型不认识的档位值；
2. 厂商可在 config/models/<provider>.yaml 用 `reasoning:` 块显式声明字段（见 reasoning_spec），
   代码里**不写死任何单一厂商**；
3. 万一厂商仍拒绝该参数（400/422 + 提到该字段），调用方用
   `is_reasoning_param_rejection` + `strip_reasoning_params` **摘掉参数重试一次**，
   把硬失败降级为「该模型忽略思考强度」。这条对任何新增厂商天然生效。
"""
from __future__ import annotations

import re

# 标准档位全集（顺序即强度序，clamp 按此距离就近收敛）
EFFORT_LEVELS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh")

# 各模型家族支持的档位（不在表内的档位会被 clamp）
_FAMILY_SUPPORT: dict[str, tuple[str, ...]] = {
    "openai_gpt51": ("none", "low", "medium", "high"),
    "openai_gpt5_legacy": ("minimal", "low", "medium", "high"),
    "openai_reasoning": ("minimal", "low", "medium", "high"),  # o1/o3/o4-mini
    "openai_codex_max": ("low", "medium", "high", "xhigh"),
    "openai_generic": ("low", "medium", "high"),
    "deepseek": ("low", "medium", "high"),
    "qwen": ("low", "medium", "high"),
    "glm": ("low", "medium", "high"),
    "doubao": ("low", "medium", "high"),
    "kimi": ("low", "medium", "high"),
    "gemini": ("none", "low", "medium", "high"),
    "claude": ("low", "medium", "high"),
    "generic": ("low", "medium", "high"),
}

# 供应商 id → 家族（provider 比「模型名猜测」权威：同一模型可能经不同网关托管）。
# 只登记语义确定的映射；未登记的仍走模型名嗅探，嗅不出则 generic。
_PROVIDER_FAMILY: dict[str, str] = {
    "openai": "openai_generic",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "glm": "glm",
    "doubao": "doubao",
    "kimi": "kimi",
    "gemini": "gemini",
    "anthropic": "claude",
    "grok": "generic",
    "hunyuan": "generic",
    "baidu": "generic",
    "mistral": "generic",
    "ollama": "generic",
    "siliconflow": "generic",
    "openrouter": "generic",
    "custom": "generic",
}

# 家族检测关键词（小写匹配；顺序即优先级，先命中先得）
_FAMILY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openai_codex_max", ("gpt-5.1-codex-max", "gpt-5.1-codex high", "codex-max")),
    ("openai_gpt51", ("gpt-5.1", "gpt-5.2", "gpt-5.1-codex")),
    ("openai_gpt5_legacy", ("gpt-5",)),
    ("openai_reasoning", ("o1", "o3", "o4-mini", "o4")),
    ("deepseek", ("deepseek",)),
    ("qwen", ("qwen", "qwq")),
    ("glm", ("glm", "chatglm")),
    ("doubao", ("doubao",)),
    ("kimi", ("kimi", "moonshot")),
    ("gemini", ("gemini",)),
    ("claude", ("claude",)),
)

# 「关闭思考」的家族专属字段（effort=none/minimal 且家族支持关闭时发送）
# 注意：这里只放**该家族 OpenAI 兼容端点**接受的字段名。Gemini 不在表内——
# 它的兼容端点走标准 reasoning_effort（none 即关），thinkingBudget 是原生 API 字段。
_DISABLE_THINKING_FIELDS: dict[str, dict] = {
    "deepseek": {"thinking": {"type": "disabled"}},
    "qwen": {"enable_thinking": False},
    "glm": {"thinking": {"type": "disabled"}},
    "doubao": {"thinking": {"type": "disabled"}},
}

# 本模块可能写进请求体的全部键名（用于拒绝后摘除重试）。
# stream_options 是"要 usage"的开关，与思考参数一样属于"可选增强参数"，
# 某些严格网关同样可能不认识 → 一并纳入摘除范围。
_MANAGED_KEYS: frozenset[str] = frozenset(
    {"reasoning_effort", "stream_options"}
    | {k for d in _DISABLE_THINKING_FIELDS.values() for k in d}
)

# 服务端「不认识这个参数」时的报错特征（各网关措辞不一，取并用集）
_UNKNOWN_PARAM_HINTS: tuple[str, ...] = (
    "reasoning_effort", "reasoning effort", "thinking", "enable_thinking",
    "thinkingbudget", "thinking_budget", "stream_options", "include_usage",
    "unknown parameter", "unrecognized", "extra fields not permitted",
    "unknown field", "invalid parameter",
)

# 该状态码才值得做「摘参数重试」——鉴权/限流/服务端错误重试也不会成功
_REJECTION_STATUSES: frozenset[int] = frozenset({400, 422})


def reasoning_spec(provider_cfg: dict | None) -> dict:
    """从 provider 配置里读取可选的 ``reasoning:`` 声明块（厂商自定义，代码不写死）。

    支持的键：
    - ``effort_field``: str  低/中/高档位写入的字段名；``""`` 表示**该厂商完全不发**
    - ``disable``:      dict effort=none/minimal 时写入的「关思考」字段（覆盖内置表）
    - ``family``:       str  直接指定家族，跳过模型名嗅探

    例（config/models/custom.yaml）::

        reasoning:
          effort_field: reasoning_effort
          disable: {thinking: {type: disabled}}
    """
    if not isinstance(provider_cfg, dict):
        return {}
    spec = provider_cfg.get("reasoning")
    return spec if isinstance(spec, dict) else {}


def detect_family(model: str, provider: str = "", spec: dict | None = None) -> str:
    """家族检测，优先级：``spec.family``（厂商显式声明）→ 模型名 → provider → generic。

    为什么模型名排在 provider 前面：**模型名比 provider 更具体**。同一个 provider
    可以托管别家的模型（火山方舟托管 deepseek-*、OpenRouter 托管各家），此时应按
    模型本身的语义决定参数；反过来，provider=openai 也不能覆盖 gpt-5.1 与 o3 之间
    的档位差异（两者档位表不同）。provider 只在模型名无法识别时兜底。
    """
    spec = spec or {}
    fam = spec.get("family")
    if isinstance(fam, str) and fam in _FAMILY_SUPPORT:
        return fam
    low = (model or "").lower()
    for family, patterns in _FAMILY_PATTERNS:
        for p in patterns:
            if p in low:
                return family
    if provider and provider in _PROVIDER_FAMILY:
        return _PROVIDER_FAMILY[provider]
    return "generic"


def normalize_effort(effort: str) -> str:
    """只做「取值合法性」归一，不做「厂商能力」收敛。

    2026-10-08 决策（用户明确要求）：思考强度是**透传给厂商的字段**，由厂商自己
    决定怎么解释。我们不再把 xhigh 压成 high、也不再因为某家族的档位表里没有某一档
    就替换掉用户选的值——那是替厂商做决定，用户开了最高强度却拿到更低的强度。

    这里只处理两件与厂商无关的事：
      - 空值/未知值 → 默认 medium（避免发出 None 或拼写错误的值）；
      - 其余原样返回，包含 xhigh 等新档位。
    """
    eff = str(effort or "").strip().lower()
    if not eff or eff not in EFFORT_LEVELS:
        return "medium"
    return eff


def _effort_field(family: str, spec: dict) -> str:
    """该 (family, spec) 写入低/中/高档位所用的字段名；``""`` = 不发。

    - spec.effort_field 显式声明优先（含显式 ``""`` 表示该厂商不接收该参数）；
    - 未声明时回落到标准 ``reasoning_effort``——OpenAI 及绝大多数 OpenAI 兼容网关的通用字段。
    """
    if "effort_field" in spec:
        v = spec.get("effort_field")
        return v if isinstance(v, str) else ""
    return "reasoning_effort"


def to_payload(reasoning_effort: str, model: str, provider: str = "",
               spec: dict | None = None) -> dict:
    """把思考强度映射为 /chat/completions 的额外参数——**原样透传**。

    2026-10-08 起不再 clamp：用户选了什么就发什么，厂商不支持时由厂商自己报错
    或忽略（调用方已有「摘参数重试」兜底，见 is_optional_param_rejection）。
    这样「最高思考强度」在厂商侧才真的是最高，而不是被我们悄悄降一档。
    """
    spec = spec or {}
    family = detect_family(model, provider, spec)
    eff = normalize_effort(reasoning_effort)
    field = _effort_field(family, spec)
    return {field: eff} if field else {}


def to_payload_with_disable(reasoning_effort: str, model: str, provider: str = "",
                            spec: dict | None = None) -> dict:
    """思考强度 → 请求参数（含「关闭思考」语义），**档位原样透传**。

    2026-10-08 起不再把档位 clamp 到家族档位表：选了什么就发什么。
    唯一保留的映射是「用户要关思考、而该厂商不用 none 表示关闭」这一种——那是
    语义翻译（关 = thinking.type: disabled），不是把强度降档，仍然由厂商声明决定
    字段名（spec.disable 优先，其次内置表）。

    具体行为：
      - none/minimal：厂商有关闭字段 → 发关闭字段（并摘掉 effort 字段，避免打架）；
                     厂商没有关闭字段 → 省略全部参数（网关多半不认这些值）；
      - 其余档位：一律原样发到该厂商声明的字段（含 xhigh 等高档位）。
    """
    spec = spec or {}
    family = detect_family(model, provider, spec)
    raw = normalize_effort(reasoning_effort)
    field = _effort_field(family, spec)
    # 厂商**原生支持**这个档位值 → 直接原样发（含 openai 的 none）。
    # 只有厂商档位表里没有它、且它是"不思考"语义时，才翻译成该厂商的关闭字段。
    if raw in _FAMILY_SUPPORT.get(family, ()) or raw not in ("none", "minimal"):
        return {field: raw} if field else {}
    disable = spec.get("disable") if isinstance(spec.get("disable"), dict) else None
    if disable is None:
        disable = _DISABLE_THINKING_FIELDS.get(family)
    if disable:
        return dict(disable)
    # 无关闭字段的厂商（claude/generic 等）：best-effort 省略，不硬发它不认的值
    return {}


def strip_optional_params(payload: dict) -> list[str]:
    """从请求体中摘除本模块写入的全部可选参数，返回被摘掉的键名列表。

    覆盖两类"可选增强参数"：思考强度（reasoning_effort / 家族关闭字段）与
    ``stream_options``（流式要 usage 的开关）。供「厂商不认识该参数 → 摘掉重试一次」
    的降级链路使用（对任意厂商通用）。
    """
    removed = [k for k in list(payload) if k in _MANAGED_KEYS]
    for k in removed:
        payload.pop(k, None)
    return removed


def is_optional_param_rejection(status: int, body: str = "") -> bool:
    """判断一次 4xx 是否是「本模块写的可选参数不被该厂商接受」。

    只在 400/422 且报文提到相关字段名时返回 True——避免把真正的
    「模型不存在」「上下文超限」等错误误判成参数问题而白白重试。
    """
    if status not in _REJECTION_STATUSES:
        return False
    low = (body or "").lower()
    return any(h in low for h in _UNKNOWN_PARAM_HINTS)


# 兼容别名（2026-09-10 之前只管思考参数，现在也管 stream_options，故改名）
is_reasoning_param_rejection = is_optional_param_rejection
strip_reasoning_params = strip_optional_params


def describe_model_reasoning(model: str, provider: str = "",
                             spec: dict | None = None) -> dict:
    """模型自描述——该模型实际支持的思考档位与默认档（对标 dsh 的 resolveModel.reasoning）。

    前端据此渲染下拉：只列出/启用模型真实支持的档位，而不是固定 5 档后靠后端静默 clamp。
    返回 ``{"family", "efforts": [...], "default_effort", "off_supported", "effort_supported"}``。
    """
    spec = spec or {}
    family = detect_family(model, provider, spec)
    vendor_efforts = list(_FAMILY_SUPPORT.get(family, _FAMILY_SUPPORT["generic"]))
    default = "high" if "high" in vendor_efforts else vendor_efforts[-1]
    off_supported = "none" in vendor_efforts or family in _DISABLE_THINKING_FIELDS \
        or isinstance(spec.get("disable"), dict)
    # 2026-10-08 决策（用户明确要求）：**不再用厂商档位表限制用户能选什么**。
    # 思考强度会原样发给厂商，由厂商自己解释；厂商侧不认识时调用方已有「摘参数重试」兜底。
    # 因此前端拿到的是完整档位全集（none…xhigh），而不是被裁到某家族的 3 档。
    # vendor_efforts 仍返回，仅作「该厂商原生支持哪些档」的提示信息，不参与选择限制。
    return {
        "family": family,
        "efforts": list(EFFORT_LEVELS),
        "default_effort": default,
        "off_supported": off_supported,
        "effort_supported": _effort_field(family, spec) != "",
        "vendor_efforts": vendor_efforts,
    }
