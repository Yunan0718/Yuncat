"""schemas/ —— Pydantic 请求模型

Phase 47 修复（2.3 Pydantic 改造）：
将 routes/ 层高频端点的 `await request.json()` + 手动校验改为 FastAPI 标准的
Pydantic 模型注入，自动获得：
- 类型校验（422 Unprocessable Entity 替代 200 + {"ok": False, "error": ...}）
- OpenAPI 文档生成
- IDE 类型提示

仅改造 routes/skills.py（8 处）和 routes/roles.py（2 处），
其他 7 个 routes 文件保留现状（避免大面积回归）。
"""
