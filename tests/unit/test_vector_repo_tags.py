"""tests/unit/test_vector_repo_tags.py —— Task 4.6/6.5: 向量 doc metadata 序列化

sqlite-vec 架构（chromadb 迁移后）：
- metadata 不再走 `doc.to_metadata()`（该方法已随 chromadb 删除），
  序列化等价物是 `model_dump(exclude={"id", "content"})`。
- tags 以 JSON list 存储（vec0 映射表为 SQLite 列，无 chromadb 标量限制），
  `VECTOR_TAGS_MAX` 截断逻辑已废弃。
- project_id=None 表示 global memory（SQLite NULL），不再转 "" sentinel。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src-python"))

from repositories.vector_repo import KnowledgeVectorDoc, MemoryVectorDoc


def _doc_meta(doc):
    """doc → metadata dict（对标旧 to_metadata() 的等价物）。"""
    return doc.model_dump(exclude={"id", "content"})


def test_knowledge_vector_doc_metadata_fields():
    """KnowledgeVectorDoc 序列化为 metadata dict（tags 保留 list）。"""
    doc = KnowledgeVectorDoc(id="k1", content="test", title="t", tags=["a", "b"])
    metadata = _doc_meta(doc)
    assert metadata["title"] == "t"
    assert metadata["source"] == "memory_tree"
    assert metadata["tags"] == ["a", "b"]


def test_knowledge_vector_doc_tags_preserved_full_list():
    """sqlite-vec 架构：tags 完整保留（JSON 列存储，不再按 VECTOR_TAGS_MAX 截断）。"""
    many_tags = [f"tag{i}" for i in range(20)]
    doc = KnowledgeVectorDoc(id="k1", content="test", title="t", tags=many_tags)
    metadata = _doc_meta(doc)
    assert metadata["tags"] == many_tags


def test_knowledge_vector_doc_empty_tags():
    """空 tags 列表——保留空 list。"""
    doc = KnowledgeVectorDoc(id="k1", content="test", title="t", tags=[])
    metadata = _doc_meta(doc)
    assert metadata["tags"] == []


def test_knowledge_vector_doc_default_tags():
    """tags 默认空列表（Field default_factory）。"""
    doc = KnowledgeVectorDoc(id="k1", content="test", title="t")
    metadata = _doc_meta(doc)
    assert metadata["tags"] == []


def test_memory_vector_doc_metadata_works():
    """MemoryVectorDoc 序列化正常（回归测试）。"""
    doc = MemoryVectorDoc(id="m1", content="hello", source="conversation",
                          owner="alice", memory_type="fact", verified=1)
    metadata = _doc_meta(doc)
    assert metadata["source"] == "conversation"
    assert metadata["owner"] == "alice"
    assert metadata["memory_type"] == "fact"
    assert metadata["verified"] == 1


# ---- Task 6.5: project_id 字段保留 + None 表示 global memory ----

def test_memory_vector_doc_project_id_preserved():
    """Task 6.5: MemoryVectorDoc 保留 project_id 字段（不被 extra='ignore' 剥掉）"""
    doc = MemoryVectorDoc(id="m1", content="hello", project_id="proj_a")
    metadata = _doc_meta(doc)
    assert metadata["project_id"] == "proj_a"


def test_memory_vector_doc_project_id_none_stays_none():
    """Task 6.5: project_id=None → None（SQLite NULL = global memory）

    chromadb 架构曾用 "" 作 global sentinel（不支持 null 值）；
    sqlite-vec 架构直接用 SQL NULL，不再转换。
    """
    doc = MemoryVectorDoc(id="m1", content="hello", project_id=None)
    metadata = _doc_meta(doc)
    assert metadata["project_id"] is None


def test_memory_vector_doc_project_id_empty_string_stays_empty():
    """Task 6.5: project_id="" 保持空字符串"""
    doc = MemoryVectorDoc(id="m1", content="hello", project_id="")
    metadata = _doc_meta(doc)
    assert metadata["project_id"] == ""


def test_validate_memory_metadata_preserves_project_id():
    """Task 6.5: _validate_memory_metadata 不再剥掉 project_id

    回归验证: 原 MemoryVectorDoc 无 project_id 字段 + extra='ignore'，
    导致 metadata 中的 project_id 被静默丢弃。
    """
    from repositories.vector_repo import VectorRepository
    repo = VectorRepository()
    raw_metadata = {
        "source": "conversation",
        "owner": "alice",
        "memory_type": "fact",
        "verified": 1,
        "project_id": "proj_a",
    }
    validated = repo._validate_memory_metadata(raw_metadata)
    assert validated["project_id"] == "proj_a"
    assert validated["source"] == "conversation"
    assert validated["owner"] == "alice"


def test_validate_memory_metadata_none_project_id_stays_none():
    """_validate_memory_metadata 保留 None project_id（global memory 语义）"""
    from repositories.vector_repo import VectorRepository
    repo = VectorRepository()
    raw_metadata = {
        "source": "memory_tree",
        "project_id": None,  # global memory
    }
    validated = repo._validate_memory_metadata(raw_metadata)
    assert validated["project_id"] is None
    assert validated["source"] == "memory_tree"
