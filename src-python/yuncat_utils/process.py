"""utils/process.py —— 进程相关工具函数"""
import socket


def find_free_port() -> int:
    """绑定端口 0 让 OS 分配，返回分配的端口号。

    从 src-python/main.py 提取，供 Adapter 层复用。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
