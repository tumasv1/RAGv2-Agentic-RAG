"""Тесты конфигурации."""

from core.config import LlmConfig, SearchConfig, get_config


class TestLlmConfig:
    """Тесты LLM-конфига."""

    def test_llm_config_defaults(self):
        """По умолчанию temperature=0.7, max_tokens=2000."""
        cfg = LlmConfig()
        assert cfg.temperature == 0.7
        assert cfg.max_tokens == 2000
        assert cfg.request_timeout == 60

    def test_llm_config_custom(self):
        """Можно переопределить значения."""
        cfg = LlmConfig(temperature=0.5, max_tokens=1000)
        assert cfg.temperature == 0.5
        assert cfg.max_tokens == 1000


class TestSearchConfig:
    """Тесты поиска-конфига."""

    def test_search_config_defaults(self):
        """По умолчанию max_chunks=10, fetch_k=40."""
        cfg = SearchConfig()
        assert cfg.max_chunks == 10
        assert cfg.fetch_k == 40
        assert cfg.bm25_top_k == 3
        assert cfg.use_reranking is False

    def test_search_config_threshold_validation(self):
        """Пороги должны быть числа (не валидируются диапазоны)."""
        cfg = SearchConfig(dense_score_threshold=0.8, reranker_score_threshold=-2.5)
        assert cfg.dense_score_threshold == 0.8
        assert cfg.reranker_score_threshold == -2.5


class TestAppConfig:
    """Тесты основной конфигурации."""

    def test_get_config_singleton(self):
        """get_config() возвращает одну и ту же инстанцию."""
        cfg1 = get_config()
        cfg2 = get_config()
        assert cfg1 is cfg2

    def test_app_config_structure(self):
        """AppConfig содержит все нужные секции."""
        cfg = get_config()
        assert hasattr(cfg, "llm")
        assert hasattr(cfg, "search")
        assert hasattr(cfg, "ingest")
        assert hasattr(cfg, "agent")
        assert hasattr(cfg, "embeddings")
        assert hasattr(cfg, "qdrant")
        assert hasattr(cfg, "persistence")
        assert isinstance(cfg.llm, LlmConfig)
        assert isinstance(cfg.search, SearchConfig)
