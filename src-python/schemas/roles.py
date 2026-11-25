"""schemas/roles.py —— routes/roles.py 的 Pydantic 请求模型"""
from typing import Optional

from pydantic import BaseModel, Field


class PromoteTempRoleRequest(BaseModel):
    """将临时角色转化为正式角色。"""
    node_id: str = Field("", max_length=256,
                         description="知识图谱节点 ID（可选，向后兼容）")
    role_name: str = Field(..., min_length=1, max_length=256,
                          description="角色名")
    prompt_template: Optional[str] = Field(None,
                                          description="角色 prompt 模板（不填则用默认）")


class RejectTempRoleRequest(BaseModel):
    """拒绝临时角色转化。"""
    node_id: str = Field(..., min_length=1, max_length=256,
                        description="要拒绝的节点 ID")
