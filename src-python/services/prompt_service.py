"""services/prompt_service.py —— Phase 4: 提示词热加载 + 版本管理

轮询 mtime 检测 prompts/ 目录下 .py 文件变更，自动 importlib.reload。
提供 CRUD + 预览端点，支持 JSON overlay 编辑暂存。
"""
import importlib
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger("yuncat.prompt_service")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def _get_overrides_path(data_dir: Path | str | None = None) -> Path:
    """获取 overrides JSON 的存储路径。

    优先使用 data_dir（启动时由 YUNCAT_DATA_DIR 指定），
    确保生产环境（PyInstaller 只读源码）也能正常写入。
    """
    if data_dir:
        return Path(data_dir) / "prompt_overrides.json"
    return PROMPTS_DIR / "_overrides.json"

# 需要在 prompts/*.py 中暴露为模块级常量的关键字符串变量名
TRACKED_VARS = {
    "commander": ["COMMANDER_STATIC_PREFIX", "COMMANDER_DYNAMIC_SUFFIX_TEMPLATE",
                   "COMMANDER_REMEMBER"],
    "intent_classifier": ["INTENT_CLASSIFIER_PROMPT"],
    "memory_context": ["MEMORY_CONTEXT_TEMPLATE", "DREAM_CONSOLIDATE_PROMPT"],
    "rules_formatter": [],  # 仅含函数，无可编辑字符串
    "tool_descriptions": [],  # 仅含函数
    "workflow_templates": ["BUILTIN_TEMPLATES"],
}


@dataclass
class PromptInfo:
    name: str
    version: str
    file_path: str
    variables: list[str]  # 可编辑的模块级字符串变量名


@dataclass
class PromptDetail:
    name: str
    version: str
    variables: dict[str, str]  # var_name → current_value


class PromptService:
    """Phase 4: 提示词版本管理 + 热加载"""

    def __init__(self, data_dir: str | None = None) -> None:
        self._overrides_file = _get_overrides_path(data_dir)
        self._mtimes: dict[str, float] = {}
        self._versions: dict[str, str] = {}
        self._overrides: dict[str, dict[str, str]] = {}
        self._refresh_mtimes()
        self._load_overrides()

    # ---- mtime 轮询 ----

    def _refresh_mtimes(self) -> None:
        """扫描 prompts/ 目录记录所有 .py 文件的 mtime"""
        if not PROMPTS_DIR.is_dir():
            return
        for f in PROMPTS_DIR.glob("*.py"):
            self._mtimes[f.name] = f.stat().st_mtime

    def check_and_reload(self) -> list[str]:
        """检查 .py 文件是否变更，有变更则 reload。返回变更的模块名列表。"""
        changed: list[str] = []
        if not PROMPTS_DIR.is_dir():
            return changed
        for f in PROMPTS_DIR.glob("*.py"):
            if f.name.startswith("_"):  # 跳过 __init__ 和 _overrides
                continue
            current_mtime = f.stat().st_mtime
            if f.name not in self._mtimes or current_mtime > self._mtimes[f.name]:
                changed.append(f.stem)
                self._mtimes[f.name] = current_mtime

        if changed:
            self._reload_modules(changed)
            self._load_overrides()
            _log.info("Prompts reloaded: %s", changed)

        return changed

    def _reload_modules(self, module_stems: list[str]) -> None:
        """reload 指定的 prompts.* 模块"""
        for stem in module_stems:
            module_name = f"prompts.{stem}"
            if module_name in sys.modules:
                try:
                    importlib.reload(sys.modules[module_name])
                except Exception:
                    _log.exception("Failed to reload %s", module_name)

    def force_reload(self) -> list[str]:
        """强制重载所有 prompt 模块（忽略 mtime）"""
        self._mtimes.clear()
        self._refresh_mtimes()
        changed = list({f.stem for f in PROMPTS_DIR.glob("*.py") if not f.name.startswith("_")})
        self._reload_modules(changed)
        self._load_overrides()
        _log.info("Force reloaded all prompts")
        return changed

    # ---- JSON overlay ----

    def _load_overrides(self) -> None:
        """加载 overrides JSON（如果存在）"""
        if self._overrides_file.exists():
            try:
                self._overrides = json.loads(self._overrides_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                _log.warning("Failed to load prompt overrides, resetting")
                self._overrides = {}
        else:
            self._overrides = {}

    def _save_overrides(self) -> None:
        """持久化 overrides 到磁盘（原子写入：先写临时文件再 rename）"""
        self._overrides_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = self._overrides_file.with_suffix(".tmp")
        tmp_file.write_text(
            json.dumps(self._overrides, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp_file.replace(self._overrides_file)

    # ---- 模块级元数据读取 ----

    def _get_module_attr(self, module_name: str, attr: str) -> Any:
        """读取 prompts.<module_name>.<attr> 的值。

        先从 overrides JSON 查找，没有再读 Python 模块。
        """
        # overrides 优先；如果存的是 JSON 字符串，反序列化恢复原类型
        override = self._overrides.get(module_name, {}).get(attr)
        if override is not None and isinstance(override, str):
            stripped = override.strip()
            if stripped.startswith("[") or stripped.startswith("{"):
                try:
                    return json.loads(override)
                except json.JSONDecodeError:
                    pass  # 不是有效 JSON，当作普通字符串返回
            return override
        elif override is not None:
            return override

        full_name = f"prompts.{module_name}"
        try:
            if full_name in sys.modules:
                mod = sys.modules[full_name]
            else:
                mod = importlib.import_module(full_name)
            return getattr(mod, attr, None)
        except (AttributeError, ModuleNotFoundError, ImportError):
            _log.exception("Failed to read %s.%s", full_name, attr)
            return None

    def _get_module_version(self, module_name: str) -> str:
        """读取模块 __version__"""
        full_name = f"prompts.{module_name}"
        try:
            if full_name in sys.modules:
                mod = sys.modules[full_name]
            else:
                mod = importlib.import_module(full_name)
            return getattr(mod, "__version__", "0.0.0")
        except Exception:
            return "0.0.0"

    # ---- CRUD API ----

    def list_prompts(self) -> list[PromptInfo]:
        """列出所有可管理 prompt 模块"""
        result: list[PromptInfo] = []
        for name in sorted(TRACKED_VARS):
            version = self._get_module_version(name)
            file_path = str(PROMPTS_DIR / f"{name}.py")
            variables = list(TRACKED_VARS.get(name, []))
            result.append(PromptInfo(
                name=name,
                version=version,
                file_path=file_path,
                variables=variables,
            ))
        return result

    def get_prompt(self, name: str) -> PromptDetail | None:
        """获取单个 prompt 模块的详细信息（含变量当前值）"""
        if name not in TRACKED_VARS:
            return None
        version = self._get_module_version(name)
        variables: dict[str, str] = {}
        for var in TRACKED_VARS.get(name, []):
            val = self._get_module_attr(name, var)
            if isinstance(val, str):
                variables[var] = val
            elif isinstance(val, list):
                variables[var] = json.dumps(val, ensure_ascii=False, indent=2)
            else:
                variables[var] = repr(val) if val is not None else ""
        return PromptDetail(name=name, version=version, variables=variables)

    def update_prompt(self, name: str, variables: dict[str, str]) -> None:
        """更新 prompt 变量（写入 overrides JSON + 同步更新运行时模块属性）。

        双重写入：先写 overrides JSON（持久化），再直接更新 sys.modules 中
        的模块属性（运行时立即生效）。这样 agent_service / memory_service
        等模块的下一次 import / 访问会拿到更新后的值。
        """
        if name not in TRACKED_VARS:
            raise ValueError(f"Unknown prompt module: {name}")
        allowed = set(TRACKED_VARS.get(name, []))
        for var_name in variables:
            if var_name not in allowed:
                raise ValueError(
                    f"Variable '{var_name}' not in tracked vars for '{name}'. "
                    f"Allowed: {allowed}"
                )
        # 1. 持久化到 JSON
        self._overrides.setdefault(name, {}).update(variables)
        self._save_overrides()

        # 2. 立即更新运行时模块属性（让 agent_service 等直接读取生效）
        module_name = f"prompts.{name}"
        if module_name in sys.modules:
            mod = sys.modules[module_name]
            for var_name, value in variables.items():
                try:
                    setattr(mod, var_name, value)
                    _log.info("Runtime attribute updated: %s.%s", module_name, var_name)
                except Exception:
                    _log.exception("Failed to set %s.%s at runtime", module_name, var_name)

        _log.info("Prompt override saved: %s → %s", name, list(variables.keys()))

    def reset_overrides(self, name: str | None = None) -> None:
        """清除 overrides。不指定 name 则清除全部。

        同时重新 reload 模块以恢复 .py 源文件的原始值。
        """
        if name:
            self._overrides.pop(name, None)
            # 重新加载模块以恢复原始属性
            module_name = f"prompts.{name}"
            if module_name in sys.modules:
                try:
                    importlib.reload(sys.modules[module_name])
                    _log.info("Module reloaded after reset: %s", module_name)
                except Exception:
                    _log.exception("Failed to reload %s after reset", module_name)
        else:
            self._overrides.clear()
            self.force_reload()
        self._save_overrides()

    def preview(self, name: str, context: dict[str, str] | None = None) -> dict[str, str]:
        """用上下文渲染 prompt 变量，返回预览结果。

        context 可以包含: rules_context, memory_context, whitelist 等渲染参数。
        仅对支持 format() 的字符串变量做渲染。
        """
        if name not in TRACKED_VARS:
            raise ValueError(f"Unknown prompt module: {name}")
        ctx = context or {}
        result: dict[str, str] = {}
        for var in TRACKED_VARS.get(name, []):
            val = self._get_module_attr(name, var)
            if isinstance(val, str) and "{" in val:
                try:
                    # 安全渲染——只替换已有的占位符
                    result[var] = val.format(**{k: v for k, v in ctx.items() if k in val})
                except (KeyError, ValueError, IndexError):
                    result[var] = val
            elif isinstance(val, str):
                result[var] = val
            elif isinstance(val, list):
                result[var] = json.dumps(val, ensure_ascii=False, indent=2)
            else:
                result[var] = repr(val) if val is not None else ""
        return result
