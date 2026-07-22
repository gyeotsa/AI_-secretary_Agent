"""RAG vector path end-to-end and persistence test."""
import gc
from pathlib import Path

import pytest

from config import Config
from core.rag import VectorRAGManager


def test_korean_vector_rag_persists_across_restart(tmp_path):
    model_path = Path(Config.RAG_EMBEDDING_MODEL_PATH).resolve()
    if not model_path.is_dir():
        pytest.skip(f"로컬 임베딩 모델 없음: {model_path}")

    original_db_path = Config.API_CONFIG.DB_PATH
    original_model_path = Config.API_CONFIG.RAG_EMBEDDING_MODEL_PATH
    Config.API_CONFIG.DB_PATH = str(tmp_path / "assistant.db")
    Config.API_CONFIG.RAG_EMBEDDING_MODEL_PATH = str(model_path)
    document = tmp_path / "jarvis_rag_test.txt"
    document.write_text(
        "자비스 프로젝트는 개인 AI 운영체제를 만드는 프로젝트입니다. "
        "권한 관리자와 도구 실행기가 안전한 작업 실행을 담당합니다.",
        encoding="utf-8",
    )

    try:
        manager = VectorRAGManager()
        assert manager.use_vector_rag
        assert "성공적으로 추가" in manager.add_document(str(document))
        first_results = manager.search_docs("권한 관리자는 무엇을 담당하나요?", top_k=2)
        assert first_results
        assert any("안전한 작업 실행" in result["content"] for result in first_results)

        del manager
        gc.collect()

        restarted = VectorRAGManager()
        assert restarted.use_vector_rag
        persisted_results = restarted.search_docs("개인 AI 운영체제 프로젝트", top_k=2)
        assert persisted_results
        assert any("자비스 프로젝트" in result["content"] for result in persisted_results)
    finally:
        Config.API_CONFIG.DB_PATH = original_db_path
        Config.API_CONFIG.RAG_EMBEDDING_MODEL_PATH = original_model_path
