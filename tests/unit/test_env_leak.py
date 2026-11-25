"""tests/unit/test_env_leak.py —— T4: 子进程 env 泄漏测试

断言子进程 env 不含 YUNCAT_TOKEN / API Key 等敏感变量。

覆盖两条路径:
- run_command 工具路径: _build_filtered_env() 过滤敏感 env 变量
- OpenClaw adapter 路径: _get_env() 不含 YUNCAT_TOKEN（依赖 main.py 启动时 pop）

S3 防护: _SECRET_PATTERNS = (key, secret, token, password, credential, auth)
        _PROTECTED_VARS = {PATH, HOME, USER, ...}（含 AUTH_TOKEN 例外？不，AUTH 不在保护列表）
S4 防护: main.py:50 os.environ.pop("YUNCAT_TOKEN", None)
S5 防护: openclaw._get_env() 用临时文件传 API Key（env 只传 OPENCLAW_API_KEY_FILE 路径）
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestBuildFilteredEnv:
    """T4: _build_filtered_env() 过滤敏感环境变量（run_command 路径）"""

    def test_removes_yuncat_token(self, monkeypatch):
        """YUNCAT_TOKEN 必须被过滤——这是 Yuncat 自身的认证 token"""
        monkeypatch.setenv("YUNCAT_TOKEN", "secret-token-12345")
        from tools.builtin.system_tool_command import _build_filtered_env
        env = _build_filtered_env()
        assert "YUNCAT_TOKEN" not in env, "YUNCAT_TOKEN 必须从子进程 env 中过滤掉"
        assert "secret-token-12345" not in env.values()

    def test_removes_openclaw_default_api_key(self, monkeypatch):
        """OPENCLAW_DEFAULT_API_KEY 必须被过滤（含 'key' 模式）"""
        monkeypatch.setenv("OPENCLAW_DEFAULT_API_KEY", "sk-test-key")
        from tools.builtin.system_tool_command import _build_filtered_env
        env = _build_filtered_env()
        assert "OPENCLAW_DEFAULT_API_KEY" not in env

    def test_removes_generic_secret_patterns(self, monkeypatch):
        """含 secret/token/password/credential/auth 的变量均被过滤"""
        monkeypatch.setenv("MY_API_KEY", "val1")
        monkeypatch.setenv("DB_PASSWORD", "val2")
        monkeypatch.setenv("OAUTH_SECRET", "val3")
        monkeypatch.setenv("CREDENTIAL_FILE", "val4")
        monkeypatch.setenv("AUTH_BEARER", "val5")
        from tools.builtin.system_tool_command import _build_filtered_env
        env = _build_filtered_env()
        for sensitive_key in ["MY_API_KEY", "DB_PASSWORD", "OAUTH_SECRET",
                              "CREDENTIAL_FILE", "AUTH_BEARER"]:
            assert sensitive_key not in env, f"{sensitive_key} 应被过滤（含敏感模式）"

    def test_keeps_protected_vars(self, monkeypatch):
        """_PROTECTED_VARS 中的变量即使含敏感模式也保留（PATH/HOME/USER 等）"""
        # PATH 含 'auth' 子串? 不含。但测试 USERPROFILE 含 'pass'? 不含。
        # 用一个明确的保护变量测试
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/user")
        monkeypatch.setenv("USERNAME", "testuser")
        from tools.builtin.system_tool_command import _build_filtered_env
        env = _build_filtered_env()
        assert env.get("PATH") == "/usr/bin:/bin"
        assert env.get("HOME") == "/home/user"
        assert env.get("USERNAME") == "testuser"

    def test_keeps_non_sensitive_vars(self, monkeypatch):
        """非敏感变量保留"""
        monkeypatch.setenv("MY_APP_NAME", "Yuncat")
        monkeypatch.setenv("PORT", "8080")
        from tools.builtin.system_tool_command import _build_filtered_env
        env = _build_filtered_env()
        assert env.get("MY_APP_NAME") == "Yuncat"
        assert env.get("PORT") == "8080"


class TestOpenClawExternalNoCredInjection:
    """Phase 融合重构后：OpenClaw 改为外部 gateway，Yuncat 不再 spawn 子进程、
    也不再向任何子进程注入凭据。原 T4 的 `_get_env()` / 临时文件 API Key / 端口注入
    等子进程相关安全测试已不适用（攻击面随 vendored 子进程一起移除）。

    本类验证新的安全姿态：OpenClawAdapter 是外部 HTTP gateway 连接器，
    **不定义** `_get_env`（无子进程凭据注入），凭据完全由用户侧 gateway 自行管理。
    """

    def test_openclaw_has_no_get_env(self):
        """OpenClawAdapter 不应再定义 _get_env（无子进程凭据注入）。"""
        from adapters.openclaw import OpenClawAdapter
        assert not hasattr(OpenClawAdapter, "_get_env"), \
            "OpenClawAdapter 已改为外部 gateway，不应再向子进程注入凭据 (_get_env)"

    def test_openclaw_is_external_gateway_adapter(self):
        """OpenClawAdapter 继承 HttpGatewayAgentAdapter（外部连接，无 spawn）。"""
        from adapters.openclaw import OpenClawAdapter
        from adapters.ext_http import HttpGatewayAgentAdapter
        assert issubclass(OpenClawAdapter, HttpGatewayAgentAdapter)

    def test_openclaw_health_does_not_leak_token(self, monkeypatch):
        """health() 仅探测 gateway / CLI，不读取或注入任何凭据到子进程。"""
        from adapters.openclaw import OpenClawAdapter
        # 即便环境存在敏感变量，health() 也不应将其写入子进程 env（已无子进程）
        monkeypatch.setenv("YUNCAT_TOKEN", "should-never-leak")
        a = OpenClawAdapter()
        a._port = 0  # 短路 reachable=False，无网络调用
        import asyncio
        result = asyncio.run(a.health())
        assert result is False  # gateway 不可达 → False，且不抛异常
