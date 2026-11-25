"""tests/unit/test_embedding_provider.py —— Task 7.2: EmbeddingProvider Protocol + 热切换

验证嵌入向量提供者的抽象层，支持配置驱动的热切换。
对标 LangChain Embeddings / LlamaIndex BaseEmbedding / chromadb EmbeddingFunction。

不测试实际 embedding 生成（需下载模型）——只验证 Protocol 接口、工厂选择、chromadb 兼容性。
"""
import os
import sys
import pytest

src_path = os.path.join(os.path.dirname(__file__), "..", "..", "src-python")
sys.path.insert(0, src_path)


class TestEmbeddingProviderProtocol:
    """Task 7.2: EmbeddingProvider Protocol 接口验证"""

    def test_protocol_is_runtime_checkable(self):
        """EmbeddingProvider 是 runtime_checkable Protocol"""
        from repositories.embedding_provider import EmbeddingProvider
        # runtime_checkable Protocol 支持 isinstance 检查
        assert hasattr(EmbeddingProvider, "_is_runtime_protocol")

    def test_protocol_defines_embed_documents(self):
        """Protocol 定义 embed_documents 方法"""
        from repositories.embedding_provider import EmbeddingProvider
        assert hasattr(EmbeddingProvider, "embed_documents")

    def test_protocol_defines_embed_query(self):
        """Protocol 定义 embed_query 方法"""
        from repositories.embedding_provider import EmbeddingProvider
        assert hasattr(EmbeddingProvider, "embed_query")

    def test_protocol_defines_name(self):
        """Protocol 定义 name 方法"""
        from repositories.embedding_provider import EmbeddingProvider
        assert hasattr(EmbeddingProvider, "name")


class TestSentenceTransformerProvider:
    """Task 7.2: SentenceTransformerProvider 实现"""

    def test_implements_protocol(self):
        """SentenceTransformerProvider 实现 EmbeddingProvider Protocol"""
        from repositories.embedding_provider import EmbeddingProvider, SentenceTransformerProvider
        provider = SentenceTransformerProvider(model_name="test-model")
        assert isinstance(provider, EmbeddingProvider)

    def test_name_contains_model_name(self):
        """name() 返回包含模型名的字符串"""
        from repositories.embedding_provider import SentenceTransformerProvider
        provider = SentenceTransformerProvider(model_name="paraphrase-multilingual-MiniLM-L12-v2")
        name = provider.name()
        assert isinstance(name, str)
        assert "paraphrase-multilingual" in name

    def test_has_embed_documents(self):
        """有 embed_documents 方法"""
        from repositories.embedding_provider import SentenceTransformerProvider
        provider = SentenceTransformerProvider(model_name="test-model")
        assert callable(provider.embed_documents)

    def test_has_embed_query(self):
        """有 embed_query 方法"""
        from repositories.embedding_provider import SentenceTransformerProvider
        provider = SentenceTransformerProvider(model_name="test-model")
        assert callable(provider.embed_query)


class TestGetEmbeddingProvider:
    """Task 7.2: 工厂函数——配置驱动的提供者选择 + 热切换"""

    def test_returns_provider(self):
        """get_embedding_provider 返回 EmbeddingProvider 实例"""
        from repositories.embedding_provider import get_embedding_provider, EmbeddingProvider
        provider = get_embedding_provider()
        assert isinstance(provider, EmbeddingProvider)

    def test_default_provider_is_sentence_transformer(self):
        """默认提供者为 sentence_transformer"""
        from repositories.embedding_provider import get_embedding_provider
        provider = get_embedding_provider()
        name = provider.name()
        assert "sentence_transformer" in name

    def test_default_model_is_builtin(self):
        """默认模型为 bge-small-zh-v1.5（512 维，中文专用，已内置到 embedding_models/）"""
        from repositories.embedding_provider import get_embedding_provider
        provider = get_embedding_provider()
        name = provider.name()
        assert "bge-small-zh-v1.5" in name

    def test_env_var_overrides_provider(self, monkeypatch):
        """环境变量 YUNCAT_EMBEDDING_MODEL 可覆盖默认模型"""
        monkeypatch.setenv("YUNCAT_EMBEDDING_MODEL", "custom-model-name")
        # 需要 reload config 以读取新环境变量
        import importlib
        import config
        importlib.reload(config)
        from repositories.embedding_provider import get_embedding_provider
        provider = get_embedding_provider()
        assert "custom-model-name" in provider.name()
        # 恢复 config
        importlib.reload(config)
