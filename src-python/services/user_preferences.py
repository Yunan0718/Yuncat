"""services/user_preferences.py —— 用户偏好（对外 API 层）

存储与调和已下沉到 repositories/user_preferences_repo.py：偏好本质是
knowledge_nodes 上一条源隔离记录，全是数据库读写；放在 services 层会让
工具层被迫跨层 import（tools → services，分层守卫禁止）。

本模块只保留对外函数名（路由、agent_context、agent_roles 都在用），
实现统一转发到仓库层——同一份知识只有仓库层那一处实现。

设计背景（2026-09-03）：用户明确表达的偏好（"回复简洁"、"用中文沟通"）
此前无捕获入口——feedback_learning 只从工具委托信号被动推导。
复用 knowledge_nodes 并以 source="user_preference" 做源隔离，
同类别采用「最新获胜」调和（避免"简洁"与"详细"长期共存互相打架）。
"""
from __future__ import annotations

from repositories.user_preferences_repo import (  # noqa: F401
    SOURCE,
    TAG_PREFERENCE,
    TAG_SUPERSEDED,
    build_preference_snippet,
    capture_preference,
    delete_preference,
    list_preferences,
)

__all__ = [
    "SOURCE",
    "TAG_PREFERENCE",
    "TAG_SUPERSEDED",
    "build_preference_snippet",
    "capture_preference",
    "delete_preference",
    "list_preferences",
]
