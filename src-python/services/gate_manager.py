"""services/gate_manager.py —— Phase 45 D.6 Gate 模式管理器

对标 Claude Code / Fable5: Builder 没有 commit 权限。Gate 文件只读，Builder 编辑即失败。

核心原则:
  - Spec YAML 作为不可变 Gate 文件写入 worktree
  - Gate 文件权限 0o444 (只读) — Builder 进程无法 write
  - Developer 完成后验证 Gate 未被篡改 (SHA-256 对比)
  - Builder 编辑了 Gate 或 protected_files → 实现无效
"""
import hashlib
import logging
import os as _os
from pathlib import Path as _Path

_log = logging.getLogger("yuncat.gate")


class GateManager:
    """Gate 模式管理器 — Spec 文件作为不可变 Gate。

    使用方式 (在 _invoke_spec_developer 中):
        gate = GateManager(spec_dict, worktree_path)
        output = await developer.run(...)
        result = gate.check_builder_compliance(files_touched)
        if not result["compliant"]:
            return {"error": "Gate 违规", "violations": result["violations"]}
    """

    def __init__(self, spec_dict: dict, worktree_path: str):
        self._spec = spec_dict
        self._gate_path = _Path(worktree_path) / ".yuncat_gate.yaml"
        self._original_hash = self._write_gate()

    def _write_gate(self) -> str:
        """写入 Gate 文件 → 返回 SHA-256 哈希。"""
        try:
            import yaml as _yaml
            content = _yaml.dump(self._spec, allow_unicode=True, default_flow_style=False)
        except ImportError:
            import json as _json
            content = _json.dumps(self._spec, ensure_ascii=False, indent=2)

        self._gate_path.parent.mkdir(parents=True, exist_ok=True)

        # Phase 46 fix: 如果文件已存在且只读，先恢复写权限
        if self._gate_path.exists():
            try:
                if _os.name == "nt":
                    import stat
                    current = _os.stat(self._gate_path).st_mode
                    _os.chmod(self._gate_path, current | stat.S_IWRITE)
                else:
                    _os.chmod(self._gate_path, 0o644)
            except Exception:
                _log.debug("移除 Gate 只读权限失败", exc_info=True)

        self._gate_path.write_text(content, encoding="utf-8")

        # 设置为只读 (Windows: 移除写权限; Unix: 0o444)
        try:
            if _os.name == "nt":
                import stat
                current = _os.stat(self._gate_path).st_mode
                _os.chmod(self._gate_path, current & ~stat.S_IWRITE)
            else:
                _os.chmod(self._gate_path, 0o444)
        except Exception:
            _log.debug("Failed to set Gate file as read-only (non-critical)")

        gate_hash = hashlib.sha256(content.encode()).hexdigest()
        _log.info("Gate written: %s (hash=%s)", self._gate_path, gate_hash[:12])
        return gate_hash

    def verify_gate(self) -> bool:
        """验证 Gate 文件未被篡改——对标 Claude Code Gate 验证。"""
        if not self._gate_path.exists():
            _log.warning("Gate file missing: %s", self._gate_path)
            return False
        try:
            current = self._gate_path.read_text(encoding="utf-8")
            current_hash = hashlib.sha256(current.encode()).hexdigest()
            ok = current_hash == self._original_hash
            if not ok:
                _log.warning("Gate TAMPERED: hash mismatch (original=%s, current=%s)",
                           self._original_hash[:12], current_hash[:12])
            return ok
        except Exception as e:
            _log.error("Gate verification failed: %s", e)
            return False

    def check_builder_compliance(self, changed_files: list[str]) -> dict:
        """检查 Builder 是否编辑了 Gate 文件或受保护文件。

        Returns: {compliant: bool, violations: [str]}
        """
        violations: list[str] = []

        # 1. Gate 文件篡改检查
        if not self.verify_gate():
            violations.append("Gate 文件被修改 — Builder 不可编辑 Spec")

        # 2. 受保护文件检查 (Spec 中的 protected_files 列表)
        inner = self._spec.get("spec", self._spec) if isinstance(self._spec, dict) else {}
        protected = inner.get("protected_files", []) if isinstance(inner, dict) else []
        for f in changed_files:
            if f in protected:
                violations.append(f"受保护文件 '{f}' 被修改——Builder 不可编辑此文件")
            if ".yuncat_gate.yaml" in str(f):
                violations.append(f"Gate 文件 '{f}' 被修改——Builder 不可编辑 Spec")

        return {"compliant": len(violations) == 0, "violations": violations}
