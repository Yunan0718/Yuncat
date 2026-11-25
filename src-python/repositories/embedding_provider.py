"""repositories/embedding_provider.py —— Task 7.2: EmbeddingProvider Protocol + 热切换

嵌入向量提供者抽象层——将对标 LangChain Embeddings / LlamaIndex BaseEmbedding。

定位: 本模块属 Repository 层基础设施（向量是存储概念），被 vector_repo / memory_tree
等需要 embedding 的模块共享。只依赖 config + sentence-transformers，不依赖 Service 层——
符合分层规则 Types → Config → Repo → Service → Runtime → UI。

设计目标:
  1. Protocol 抽象——应用代码依赖接口而非具体实现
  2. 配置驱动——通过 config.EMBEDDING_PROVIDER / EMBEDDING_MODEL 选择后端
  3. 热切换——修改环境变量后重新调用 get_embedding_provider() 即可切换
  4. 向量维度暴露——vec0 虚拟表需要知道 dim 才能建表

当前实现:
  - SentenceTransformerProvider: 本地 SentenceTransformer 模型（默认，多语言）

未来可扩展（YAGNI——仅当实际需求出现时才添加）:
  - OpenAIEmbeddingProvider: OpenAI text-embedding-3-small/large
  - CohereEmbeddingProvider: Cohere embed-multilingual-v3
"""
from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

_log = logging.getLogger("yuncat.embedding")

# 内置模型根目录：src-python/embedding_models/<model>/（开发态与打包后 _up_/src-python/ 均适用）
_MODEL_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "embedding_models")


def _resolve_local_path(model_name: str) -> str | None:
    """若项目内置了该模型，返回本地目录路径；否则返回 None（走 HF hub 联网加载）。

    model_name 形如 "all-MiniLM-L6-v2" 或 "org/name"，取最后一段作为目录名。
    判据：目录存在且含 model.safetensors（或 pytorch_model.bin）。
    """
    base = model_name.split("/")[-1]
    local = os.path.join(_MODEL_ROOT, base)
    if os.path.isdir(local) and (
        os.path.isfile(os.path.join(local, "model.safetensors"))
        or os.path.isfile(os.path.join(local, "pytorch_model.bin"))
    ):
        return local
    return None


def _embedding_dimension(model) -> int:
    """取模型输出维度——兼容新旧 sentence-transformers API。

    get_sentence_embedding_dimension 已重命名为 get_embedding_dimension（触发 FutureWarning）。
    """
    if hasattr(model, "get_embedding_dimension"):
        return model.get_embedding_dimension()
    return model.get_sentence_embedding_dimension()


@runtime_checkable
class EmbeddingProvider(Protocol):
    """嵌入向量提供者抽象——对标 LangChain Embeddings / LlamaIndex BaseEmbedding。

    实现者需:
      - embed_documents(texts) → 批量嵌入文档
      - embed_query(text) → 嵌入单条查询
      - name() → 返回提供者名称（用于日志/调试）
      - embedding_dim() → 返回向量维度
    """

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入文档 → 向量列表"""
        ...

    def embed_query(self, text: str) -> list[float]:
        """嵌入单条查询 → 向量"""
        ...

    def name(self) -> str:
        """提供者名称（用于日志/调试）"""
        ...

    def embedding_dim(self) -> int:
        """返回向量维度（用于 vec0 虚拟表建表）。"""
        ...


class SentenceTransformerProvider:
    """基于 SentenceTransformer 的本地嵌入提供者。

    直接使用 sentence_transformers.SentenceTransformer，不再包装 chromadb。
    sqlite-vec 只负责向量存储搜索，embedding 生成单由本模块负责。

    对标 LangChain HuggingFaceEmbeddings / LlamaIndex HuggingFaceEmbedding。
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self._model_name = model_name
        self._model = None  # 懒加载——首次 embed 时才加载模型
        self._dim = None
        self._load_failed = False  # 加载失败后不再反复重试（避免每次检索都重新下载/超时）

    def _ensure_loaded(self) -> None:
        """懒加载 SentenceTransformer 模型。

        优先使用项目内置模型（embedding_models/ 本地路径，离线、免下载）；
        内置缺失时回退到 HF hub 模型名（联网）。
        """
        if self._model is None and not self._load_failed:
            try:
                from sentence_transformers import SentenceTransformer
                path = _resolve_local_path(self._model_name) or self._model_name
                self._model = SentenceTransformer(path)
                self._dim = _embedding_dimension(self._model)
                _log.info("SentenceTransformer loaded: %s (dim=%d)", self._model_name, self._dim)
            except Exception:
                _log.warning(
                    "SentenceTransformer model '%s' unavailable, falling back to default",
                    self._model_name, exc_info=True,
                )
                try:
                    from sentence_transformers import SentenceTransformer
                    fallback = _resolve_local_path("all-MiniLM-L6-v2") or "all-MiniLM-L6-v2"
                    self._model = SentenceTransformer(fallback)
                    self._dim = _embedding_dimension(self._model)
                    _log.info("Fallback SentenceTransformer loaded: all-MiniLM-L6-v2 (dim=%d)", self._dim)
                except Exception:
                    # 主模型 + fallback 均不可用（未下载/无网络）——标记失败，
                    # 后续调用直接抛错而非每次重试下载，避免拖慢每次向量检索。
                    self._load_failed = True
                    _log.error("SentenceTransformer unavailable; vector search disabled until restart",
                               exc_info=True)
                    raise

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入文档。"""
        self._ensure_loaded()
        return self._model.encode(texts, show_progress_bar=False).tolist()  # type: ignore[union-attr]

    def embed_query(self, text: str) -> list[float]:
        """嵌入单条查询。"""
        self._ensure_loaded()
        return self._model.encode([text], show_progress_bar=False)[0].tolist()  # type: ignore[union-attr]

    def embedding_dim(self) -> int:
        """返回向量维度。"""
        self._ensure_loaded()
        return self._dim or 768

    def name(self) -> str:
        """返回提供者名称。"""
        return f"sentence_transformer:{self._model_name}"


def get_embedding_provider() -> EmbeddingProvider:
    """工厂函数——根据 config 选择 embedding 提供者。

    支持热切换：修改 YUNCAT_EMBEDDING_PROVIDER / YUNCAT_EMBEDDING_MODEL 环境变量后
    重新调用本函数 + VectorRepository.reinitialize() 即可切换后端。

    Returns:
        EmbeddingProvider 实例
    """
    from config import EMBEDDING_PROVIDER, EMBEDDING_MODEL

    if EMBEDDING_PROVIDER == "sentence_transformer":
        return SentenceTransformerProvider(model_name=EMBEDDING_MODEL)

    # 未知 provider → fallback 到 sentence_transformer（稳健默认）
    _log.warning(
        "Unknown EMBEDDING_PROVIDER='%s', falling back to sentence_transformer",
        EMBEDDING_PROVIDER,
    )
    return SentenceTransformerProvider(model_name=EMBEDDING_MODEL)
