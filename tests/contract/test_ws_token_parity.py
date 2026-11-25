"""契约：WebSocket 端点的 token 校验必须与 HTTP 同一口径

背景（2026-09-22 实测）：Electron 主进程把「静态 TOKEN」兑换成「access token」后
才下发给渲染进程（main.js exchangeToken），HTTP 中间件与主 /ws 都在 Batch 7.6 里
改成「两者都接受」。但 /ws/terminal 与 /ws/lsp 只比静态 TOKEN——前端拿的是
access token，于是**内置终端与 Python 语言服务恒被 1008 拒绝**，界面上表现为
终端连不上、LSP 一直"不可用"，而日志里只有一句 have_token=True 被拒。

这类"某个端点漏了同一口径"的 bug 不会报错、只会静默失效，所以在这里钉死：
凡是 WebSocket 端点做首帧鉴权，都必须同时接受静态 TOKEN 与 access token。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")


def _read(rel: str) -> str:
    with open(os.path.join(SRC, rel), encoding="utf-8") as f:
        return f.read()


class TestWsTokenParity:
    def test_terminal_ws_accepts_access_token(self):
        code = _read("routes/workspace.py")
        assert "validate_access_token" in code, (
            "/ws/terminal 必须接受 access token——前端 appStore 里存的是它，"
            "只认静态 TOKEN 会让内置终端恒被 1008 拒"
        )

    def test_lsp_ws_accepts_access_token(self):
        code = _read("routes/lsp.py")
        assert "validate_access_token" in code, (
            "/ws/lsp 必须接受 access token，否则 Python 语言服务永远连不上"
        )

    def test_main_ws_accepts_both(self):
        code = _read("routes/ws.py")
        assert "validate_access_token" in code
        assert "compare_digest" in code, "主 /ws 应同时保留静态 TOKEN 比较"

    def test_http_middleware_accepts_both(self):
        code = _read("main.py")
        assert "validate_access_token" in code and "compare_digest" in code, (
            "HTTP 中间件必须同时接受静态 TOKEN 与 access token"
        )

    def test_static_token_used_only_for_token_exchange(self):
        """/auth/token 用静态 TOKEN 换 access token 是设计，不算漏口径。"""
        code = _read("routes/auth.py")
        assert "compare_digest" in code
