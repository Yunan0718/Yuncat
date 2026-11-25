"""tests/unit/test_redact.py —— Task 3.1: 工具结果回传 LLM 前兜底脱敏测试

对标:
- LangChain SecretStr（读后脱敏）
- Claude Code 路径 deny（读前阻止）—— 此测试覆盖"读后脱敏"兜底层

覆盖:
- OpenAI sk- / GitHub ghp_/gho_/ghs_/ghr_ / AWS AKIA / 通用 password=/secret=/api_key= 脱敏
- 无敏感信息的文本原样返回
- 空字符串 / None 安全
- 多个敏感串同时出现
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


class TestRedactSecrets:
    """redact_secrets() —— 工具结果回传 LLM 前兜底脱敏"""

    def test_openai_api_key_redacted(self):
        """OpenAI sk- 前缀的 API key 被替换为 [REDACTED]"""
        from security.redact import redact_secrets
        text = "API key is sk-abc123def456ghi789jkl012mno345pqr678stu901vwx"
        result = redact_secrets(text)
        assert "sk-abc123" not in result
        assert "[REDACTED]" in result

    def test_github_personal_access_token_redacted(self):
        """GitHub ghp_ token 被脱敏"""
        from security.redact import redact_secrets
        # ghp_ 后跟 36 个字母数字
        text = "GITHUB_TOKEN=ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD"
        result = redact_secrets(text)
        assert "ghp_0123456789" not in result
        assert "[REDACTED]" in result

    def test_github_oauth_token_redacted(self):
        """GitHub gho_ OAuth token 被脱敏"""
        from security.redact import redact_secrets
        text = "token: gho_0123456789abcdefghijklmnopqrstuvwxyzABCD"
        result = redact_secrets(text)
        assert "gho_0123456789" not in result

    def test_github_server_token_redacted(self):
        """GitHub ghs_ server-to-server token 被脱敏"""
        from security.redact import redact_secrets
        text = "ghs_0123456789abcdefghijklmnopqrstuvwxyzABCD"
        result = redact_secrets(text)
        assert "ghs_0123456789" not in result

    def test_aws_access_key_id_redacted(self):
        """AWS AKIA 开头的 access key ID 被脱敏"""
        from security.redact import redact_secrets
        text = "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        result = redact_secrets(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED]" in result

    def test_password_assignment_redacted(self):
        """password=xxx 被脱敏"""
        from security.redact import redact_secrets
        text = "DB_PASSWORD=s3cr3t_p@ssw0rd"
        result = redact_secrets(text)
        assert "s3cr3t_p@ssw0rd" not in result
        assert "[REDACTED]" in result

    def test_password_colon_redacted(self):
        """password: xxx 被脱敏"""
        from security.redact import redact_secrets
        text = "password: my_secret_value"
        result = redact_secrets(text)
        assert "my_secret_value" not in result

    def test_api_key_assignment_redacted(self):
        """api_key=xxx / api-key=xxx / apiKey=xxx 被脱敏"""
        from security.redact import redact_secrets
        for key_form in ["api_key", "api-key", "apiKey", "API_KEY"]:
            text = f"{key_form}=some_secret_value_12345"
            result = redact_secrets(text)
            assert "some_secret_value_12345" not in result, f"{key_form} 应被脱敏"

    def test_secret_assignment_redacted(self):
        """secret=xxx 被脱敏"""
        from security.redact import redact_secrets
        text = "client_secret=abc123def456"
        result = redact_secrets(text)
        assert "abc123def456" not in result

    def test_token_assignment_redacted(self):
        """token=xxx 被脱敏"""
        from security.redact import redact_secrets
        text = "auth_token=eyJhbGciOiJIUzI1NiJ9.payload.signature"
        result = redact_secrets(text)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result

    def test_clean_text_unchanged(self):
        """无敏感信息的文本原样返回"""
        from security.redact import redact_secrets
        text = "def hello():\n    print('Hello, World!')\n    return 42"
        assert redact_secrets(text) == text

    def test_empty_string_safe(self):
        """空字符串安全返回"""
        from security.redact import redact_secrets
        assert redact_secrets("") == ""

    def test_none_safe(self):
        """None 安全返回空字符串"""
        from security.redact import redact_secrets
        assert redact_secrets(None) == ""

    def test_non_string_safe(self):
        """非字符串类型安全返回 str()"""
        from security.redact import redact_secrets
        assert redact_secrets(12345) == "12345"

    def test_multiple_secrets_in_one_text(self):
        """多个敏感串同时出现——全部脱敏"""
        from security.redact import redact_secrets
        text = (
            "OPENAI_API_KEY=sk-abc123def456ghi789jkl012mno345pqr678stu901vwx\n"
            "GITHUB_TOKEN=ghp_0123456789abcdefghijklmnopqrstuvwxyzABCD\n"
            "AWS_KEY=AKIAIOSFODNN7EXAMPLE\n"
            "DB_PASSWORD=my_db_password"
        )
        result = redact_secrets(text)
        assert "sk-abc123" not in result
        assert "ghp_0123456789" not in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "my_db_password" not in result
        assert result.count("[REDACTED]") >= 4

    def test_preserves_surrounding_text(self):
        """脱敏不破坏周围文本结构"""
        from security.redact import redact_secrets
        text = "Config file shows: api_key=secret123 and other stuff"
        result = redact_secrets(text)
        assert "Config file shows:" in result
        assert "and other stuff" in result
        assert "secret123" not in result

    def test_idempotent(self):
        """脱敏幂等——对已脱敏文本再跑一次不变"""
        from security.redact import redact_secrets
        text = "key=sk-abc123def456ghi789jkl012mno345pqr678stu901vwx"
        once = redact_secrets(text)
        twice = redact_secrets(once)
        assert once == twice


class TestDenyPath:
    """is_denied_path() —— 路径 deny 主防线（读前阻止）"""

    def test_env_file_denied(self):
        """.env 文件被拒绝"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, reason = is_denied_path(".env")
        assert denied is True
        assert reason

    def test_env_local_denied(self):
        """.env.local 被拒绝"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path(".env.local")
        assert denied is True

    def test_env_production_denied(self):
        """.env.production 被拒绝"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path(".env.production")
        assert denied is True

    def test_credentials_file_denied(self):
        """含 credentials 的路径被拒绝"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path("config/credentials.json")
        assert denied is True

    def test_secrets_file_denied(self):
        """含 secrets 的路径被拒绝"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path("secrets.yaml")
        assert denied is True

    def test_pem_file_denied(self):
        """*.pem 证书被拒绝"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path("certs/server.pem")
        assert denied is True

    def test_key_file_denied(self):
        """*.key 私钥被拒绝"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path("private.key")
        assert denied is True

    def test_id_rsa_denied(self):
        """id_rsa SSH 私钥被拒绝"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path("~/.ssh/id_rsa")
        assert denied is True

    def test_normal_file_allowed(self):
        """普通文件路径放行"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path("src/main.py")
        assert denied is False

    def test_normal_config_allowed(self):
        """普通 config 文件放行（不含 credentials/secrets 关键字）"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path("config/app.json")
        assert denied is False

    def test_environment_py_allowed(self):
        """environment.py 不是 .env 文件——放行"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path("src/environment.py")
        assert denied is False

    def test_absolute_env_path_denied(self):
        """绝对路径的 .env 也被拒绝"""
        from tools.builtin.system_tool_safety import is_denied_path
        denied, _ = is_denied_path("/home/user/project/.env")
        assert denied is True
