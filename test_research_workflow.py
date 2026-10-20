import copy
import hashlib
import json
import time
from pathlib import Path

import pytest

from config import Config
from core.research import ResearchClaim, ResearchReport, ResearchSource
from plugins.browser import BrowserPlugin


class Rag:
    def add_text_document(self, text, **kwargs):
        self.text, self.metadata = text, kwargs
        return kwargs["doc_id"]


def _partial_rag(tmp_path, payload, *, failure=None, cleanup_failure=False, on_add=None):
    from core.rag import VectorRAGManager
    doc_id = "research-" + hashlib.sha256(
        f"{payload['query']}|{payload['created_at']}".encode("utf-8")).hexdigest()[:20]
    originals = {"workspace::unrelated": {"doc_id": "unrelated", "namespace": "workspace"},
                 f"other::{doc_id}": {"doc_id": doc_id, "namespace": "other"}}

    class Collection:
        def __init__(self):
            self.records = {key: dict(value) for key, value in originals.items()}
        def get(self, where):
            filters = {key: value for row in where["$and"] for key, value in row.items()}
            return {"ids": [key for key, value in self.records.items()
                            if all(value.get(field) == expected for field, expected in filters.items())]}
        def add(self, ids, documents, metadatas):
            self.records.update(zip(ids, metadatas))
            if on_add:
                on_add()
            if failure:
                raise failure
        def delete(self, ids):
            if cleanup_failure:
                raise RuntimeError("synthetic cleanup error")
            for key in ids:
                self.records.pop(key)

    # Exercise the real persistence helpers without loading native embedding models.
    manager = VectorRAGManager.__new__(VectorRAGManager)
    manager.documents = {key: dict(value) for key, value in originals.items()}
    manager.rag_file = str(tmp_path / "simple_rag.json")
    manager.namespace, manager.use_vector_rag = "global", True
    manager.collection = Collection()
    return manager, doc_id, originals


def _analysis_payload():
    quote = "실험 자료에는 참여자 20명의 관찰 결과가 기록되어 있습니다."
    return {"query": "조사", "created_at": time.time(), "expires_at": time.time()+60,
            "sources": [{"source_id": "S1", "url": "https://example.org", "reliability": "medium", "content": quote}],
            "claims": [{"kind": "fact", "text": "분석 결과", "citations": ["S1"],
                        "evidence": [{"source_id": "S1", "quote": quote}]}],
            "conflicts": [], "limitations": []}


def test_report_shows_deduplicated_literal_source_quotes_beside_analysis():
    payload = _analysis_payload()
    quote = "CANDIDATES\tRELATIVE ACCURACY\n10\tBaseline\n100\t+18% vs 10\n![untrusted](https://example.org/image)"
    payload["claims"][0]["evidence"][0]["quote"] = quote
    payload["recommendations"] = [{"text": "비교 실험 제안", "citations": ["S1"],
                                    "evidence": copy.deepcopy(payload["claims"][0]["evidence"])}]
    original = copy.deepcopy(payload)
    rendered = BrowserPlugin._render_research_report(payload)
    assert "## 분석에 사용한 원문" in rendered
    assert rendered.count("    100\t+18% vs 10") == 1
    assert "    ![untrusted](https://example.org/image)" in rendered
    assert "분석 결과" in rendered and "비교 실험 제안" in rendered
    assert payload == original


def test_partial_rag_failure_removes_only_this_report_and_namespace(tmp_path, monkeypatch):
    from core.turn_context import TurnExecutionContext, bind_turn_context
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    payload = _analysis_payload()
    rag, doc_id, originals = _partial_rag(tmp_path, payload, failure=RuntimeError("synthetic index failure"))
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    with bind_turn_context(TurnExecutionContext("turn", "session", memory_namespace="workspace")):
        BrowserPlugin()._save_research_report(payload)
    assert not payload["rag_saved"]
    assert rag.documents == originals == rag.collection.records
    assert json.loads(Path(rag.rag_file).read_text(encoding="utf-8")) == originals
    assert f"workspace::{doc_id}" not in rag.documents
    saved = json.loads(Path(payload["report_files"]["json"]).read_text(encoding="utf-8"))
    assert not saved["rag_saved"] and "synthetic index failure" not in str(saved)


@pytest.mark.parametrize("persistent", [False, True])
def test_research_rollback_handles_disk_replace_conflicts_honestly(tmp_path, monkeypatch, persistent):
    import core.rag as rag_module
    from core.turn_context import TurnExecutionContext, bind_turn_context

    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    payload = _analysis_payload()
    rag, doc_id, originals = _partial_rag(tmp_path, payload, failure=RuntimeError("synthetic index failure"))
    monkeypatch.setattr(rag_module, "get_rag_manager", lambda: rag)
    replace, attempts = rag_module.os.replace, []
    def locked_during_rollback(source, destination):
        if str(destination) == rag.rag_file:
            attempts.append(True)
            if len(attempts) > 1 and (persistent or len(attempts) < 4):
                error = PermissionError("synthetic rollback file lock")
                error.winerror = 5
                raise error
        replace(source, destination)
    monkeypatch.setattr(rag_module.os, "replace", locked_during_rollback)
    monkeypatch.setattr(rag_module.time, "sleep", lambda _: None)
    with bind_turn_context(TurnExecutionContext("turn", "session", memory_namespace="workspace")):
        BrowserPlugin()._save_research_report(payload)
    saved = json.loads(Path(payload["report_files"]["json"]).read_text(encoding="utf-8"))
    stored = json.loads(Path(rag.rag_file).read_text(encoding="utf-8"))
    assert len(attempts) == 4 and not saved["rag_saved"]
    if persistent:
        assert saved["rag_cleanup_failed"] and any("기록이 남았을 수" in item for item in saved["limitations"])
        assert f"workspace::{doc_id}" in stored and stored == rag.documents
        assert "RAG에는 저장되지 않았습니다" not in BrowserPlugin._render_research_report(saved)
    else:
        assert not saved.get("rag_cleanup_failed") and stored == rag.documents == originals == rag.collection.records
    assert not list(tmp_path.glob("simple_rag.json.tmp.*"))


def test_failed_rag_cleanup_discloses_possible_remaining_records(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    payload = _analysis_payload()
    rag, _, _ = _partial_rag(tmp_path, payload, failure=RuntimeError("index failure"), cleanup_failure=True)
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    BrowserPlugin()._save_research_report(payload)
    assert not payload["rag_saved"] and payload["rag_cleanup_failed"]
    assert any("기록이 남았을 수" in item for item in payload["limitations"])
    answer = BrowserPlugin._render_research_report(payload)
    assert "RAG 저장을 완료하지 못했고 일부 기록이 남았을 수" in answer
    assert "RAG에는 저장되지 않았습니다" not in answer


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_cancellation_after_rag_add_records_rollback_and_propagates(tmp_path, monkeypatch, cleanup_failure):
    from core.plugin import ToolCancelledError
    from core.turn_context import TurnExecutionContext, bind_turn_context
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    payload = _analysis_payload()
    context = TurnExecutionContext("turn", "session", memory_namespace="workspace")
    rag, _, originals = _partial_rag(tmp_path, payload, on_add=context.cancel, cleanup_failure=cleanup_failure)
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    with bind_turn_context(context), pytest.raises(ToolCancelledError) as caught:
        BrowserPlugin()._save_research_report(payload)
    assert not payload["rag_saved"]
    assert rag.documents == originals
    saved = json.loads(Path(payload["report_files"]["json"]).read_text(encoding="utf-8"))
    assert not saved["rag_saved"]
    if cleanup_failure:
        assert saved["rag_cleanup_failed"] and "RAG 기록이 남았을 수" in str(caught.value)
    else:
        assert rag.collection.records == originals


def test_final_report_publication_failure_rolls_back_rag(tmp_path, monkeypatch):
    from core.artifact_transaction import commit_artifact_bundle
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    payload = _analysis_payload()
    rag, _, originals = _partial_rag(tmp_path, payload)
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    calls = []
    def fail_final_state_once(*args):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("synthetic final publication failure")
        return commit_artifact_bundle(*args)
    monkeypatch.setattr("core.artifact_transaction.commit_artifact_bundle", fail_final_state_once)
    BrowserPlugin()._save_research_report(payload)
    assert len(calls) == 3 and not payload["rag_saved"]
    assert rag.documents == originals == rag.collection.records
    saved = json.loads(Path(payload["report_files"]["json"]).read_text(encoding="utf-8"))
    assert not saved["rag_saved"]


def test_korean_credential_labels_are_redacted():
    from core.productization import SensitiveDataRedactor
    secret = "synthetic-credential-for-test"
    text = f"비밀번호={secret} 토큰: {secret} 인증키={secret} 암호={secret} https://user:{secret}@example.org/page"
    assert secret not in SensitiveDataRedactor.redact(text)
    assert SensitiveDataRedactor.redact({"비밀번호": secret})["비밀번호"] == "[REDACTED]"


def test_research_reserves_context_for_collected_bodies(monkeypatch):
    calls = []
    class Model:
        base_url = "http://localhost:11434"
        def chat_structured(self, messages, **kwargs):
            calls.append(kwargs)
            return '{"claims": []}'
    monkeypatch.setattr("core.llm.get_llm_client", lambda role: Model())
    assert BrowserPlugin()._research_reasoner([]) == '{"claims": []}'
    assert calls[0]["context_window"] == 8192
    assert calls[0]["json_schema"] == {"type": "object"}
    assert "local_only" not in calls[0] and "think" not in calls[0]
    schema = {"type": "object", "properties": {"queries": {"type": "array", "maxItems": 3}}}
    BrowserPlugin()._research_reasoner([], json_schema=schema)
    assert calls[1]["json_schema"] == schema


def test_local_research_preserves_selected_model_and_device_policy(monkeypatch):
    from core.llm import OllamaClient
    calls = []
    class Model(OllamaClient):
        def __init__(self):
            self.base_url = "http://127.0.0.1:11434"
            self.model = "selected-model"
        def chat_structured(self, messages, **kwargs):
            calls.append(kwargs)
            return "{}"
    client = Model()
    monkeypatch.setattr("core.llm.get_llm_client", lambda role: client)
    BrowserPlugin()._research_reasoner([])
    assert "cpu_only" not in calls[0] and calls[0]["local_only"] is True
    assert calls[0]["think"] is False
    assert client.model == "selected-model"


def test_research_provider_options_follow_the_bound_method_after_model_switch(monkeypatch):
    from core.llm import OllamaClient, SelectedLLMClient
    calls = []
    class Local(OllamaClient):
        def __init__(self):
            self.base_url = "http://127.0.0.1:11434"
        def chat_structured(self, messages, **kwargs):
            calls.append(kwargs)
            return "{}"
    class Other:
        base_url = "http://127.0.0.1:11434"
        def chat_structured(self, messages, json_schema=None, *, context_window=None,
                            request_timeout=None, max_output_tokens=None):
            calls.append({"json_schema": json_schema, "context_window": context_window,
                          "request_timeout": request_timeout, "max_output_tokens": max_output_tokens})
            return "{}"
    active = Local()
    monkeypatch.setattr(SelectedLLMClient, "_active_client", lambda self: active)
    client = object.__new__(SelectedLLMClient)
    monkeypatch.setattr("core.llm.get_llm_client", lambda role: client)
    assert BrowserPlugin()._research_reasoner([]) == "{}"
    assert calls[0]["think"] is False and calls[0]["local_only"] is True
    active = Other()
    assert BrowserPlugin()._research_reasoner([]) == "{}"
    assert calls[1] == {"json_schema": {"type": "object"}, "context_window": 8192,
                        "request_timeout": 60, "max_output_tokens": 2048}


def test_research_passage_ranking_uses_existing_embedding_without_storing_sources(monkeypatch):
    from types import SimpleNamespace
    inputs = []
    def embedding(*, input):
        inputs.append(input)
        return [[1, 0], [0, 100], [1, 0], [100, 100]]
    rag = SimpleNamespace(embedding_model=embedding, namespace="workspace")
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    passages = [{"id": f"S1:P{index}", "content": text} for index, text in enumerate(
        ["머리말", "뒤쪽의 관련 방법", "일부 관련 내용"], 1)]
    assert BrowserPlugin()._research_passage_candidates("조사 질문", passages) == ["S1:P2", "S1:P3", "S1:P1"]
    assert inputs == [["조사 질문", "머리말", "뒤쪽의 관련 방법", "일부 관련 내용"]]
    assert rag.namespace == "workspace" and set(vars(rag)) == {"embedding_model", "namespace"}


@pytest.mark.parametrize("vectors", [[[1, 0]], [[1, 0], [0, 0]], [[1, 0], [float("nan"), 0]]])
def test_research_passage_ranking_rejects_invalid_embeddings(monkeypatch, vectors):
    from types import SimpleNamespace
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: SimpleNamespace(embedding_model=lambda **_: vectors))
    with pytest.raises(ValueError):
        BrowserPlugin()._research_passage_candidates("질문", [{"id": "S1:P1", "content": "자료"}])


def test_research_delivery_saves_cited_report_and_expiring_rag(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    rag = Rag()
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    source = ResearchSource("S1", "https://example.org/study", "연구", "실험에 사용한 실제 원문 자료입니다.", time.time())
    source.reliability = "medium"
    report = ResearchReport("키워드 조사", time.time(), time.time()+3600, [source],
                            [ResearchClaim("자료를 분석한 문장입니다.", ["S1"], 1,
                                           evidence=[{"source_id": "S1", "quote": source.content}])], [])
    plugin = BrowserPlugin()
    monkeypatch.setattr(plugin, "_get_research_agent", lambda: type("Agent", (), {"research": lambda *a, **k: report})())
    result = plugin.execute_tool("browser_research", {"query": report.query})
    assert result.succeeded, result.error
    payload = json.loads(result.raw_output)
    assert payload["rag_saved"]
    assert rag.metadata["metadata"]["source_type"] == "web"
    assert rag.metadata["metadata"]["expires_at"] == report.expires_at
    assert rag.metadata["metadata"]["evidence_status"] == "ai_selected_source_quotes"
    assert "[S1]" in rag.text and source.url in rag.text
    assert source.content in rag.text and report.claims[0].text not in rag.text
    for artifact in result.artifacts:
        if artifact.kind == "file":
            path = Path(artifact.uri)
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.metadata["sha256"]
    saved = json.loads(Path(payload["report_files"]["json"]).read_text(encoding="utf-8"))
    assert saved["sources"][0]["content"] == source.content
    assert saved["rag_saved"] is True
    answer = plugin.present_result("browser_research", result.raw_output)
    assert "[보고서 열기]" in answer and "RAG에 저장" in answer


def test_rag_failure_preserves_report_and_discloses_unsaved_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    def unavailable():
        raise RuntimeError("unavailable")
    monkeypatch.setattr("core.rag.get_rag_manager", unavailable)
    payload = _analysis_payload()
    BrowserPlugin()._save_research_report(payload)
    assert not payload["rag_saved"]
    saved = Path(payload["report_files"]["markdown"]).read_text(encoding="utf-8")
    assert "RAG 저장에 실패" in saved


def test_excerpt_only_report_does_not_pollute_rag(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    indexed = []
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: indexed.append(True))
    payload = {"query": "조사", "created_at": time.time(), "expires_at": time.time()+60,
               "sources": [], "claims": [{"kind": "excerpt", "text": "실패 후 원문", "citations": []}], "conflicts": []}
    BrowserPlugin()._save_research_report(payload)
    assert not indexed and not payload["rag_saved"]
    saved = Path(payload["report_files"]["markdown"]).read_text(encoding="utf-8")
    assert "RAG에 등록하지 않았습니다" in saved


def test_low_trust_analysis_is_not_indexed(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    indexed = []
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: indexed.append(True))
    payload = {"query": "조사", "created_at": time.time(), "expires_at": time.time()+60,
               "sources": [{"source_id": "S1", "url": "https://example.org/ad", "reliability": "low"}],
               "claims": [{"kind": "fact", "text": "홍보성 주장", "citations": ["S1"]}], "conflicts": []}
    BrowserPlugin()._save_research_report(payload)
    assert not indexed and not payload["rag_saved"]


def test_mixed_report_keeps_weak_observations_out_of_rag(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    rag = Rag()
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    quote = "A study measured the processing time for twenty participants."
    evidence = [{"source_id": "S1", "quote": quote}]
    payload = {"query": "조사", "created_at": time.time(), "expires_at": time.time()+60,
        "sources": [{"source_id": "S1", "url": "https://example.org/research", "reliability": "medium",
                     "assessment": "별도로 검토하지 않은 출처 평가", "content": quote},
                    {"source_id": "S2", "url": "https://example.org/ad", "reliability": "low", "content": quote}],
        "claims": [{"kind": "fact", "text": "실험에서 확인한 내용", "citations": ["S1"], "evidence": evidence},
                   {"kind": "inference", "text": "홍보에서 추정한 내용", "citations": ["S2"],
                    "evidence": [{"source_id": "S2", "quote": quote}]},
                   {"kind": "excerpt", "text": "분석하지 않은 발췌", "citations": ["S1"], "evidence": evidence}],
        "recommendations": [{"text": "혼합 근거로 만든 추천", "citations": ["S1", "S2"], "evidence": evidence}],
        "conflicts": [{"claim": "별도로 검토하지 않은 상충 해석", "citations": ["S1"]}]}
    BrowserPlugin()._save_research_report(payload)
    assert payload["rag_saved"] and quote in rag.text and "example.org/ad" not in rag.text
    assert all(text not in rag.text for text in ("실험에서 확인한 내용", "홍보에서 추정한 내용", "분석하지 않은 발췌", "혼합 근거로 만든 추천",
                                                "별도로 검토하지 않은 출처 평가", "별도로 검토하지 않은 상충 해석"))
    saved = Path(payload["report_files"]["markdown"]).read_text(encoding="utf-8")
    assert "홍보에서 추정한 내용" in saved
    assert "별도로 검토하지 않은 출처 평가" not in saved
    assert "별도로 검토하지 않은 상충 해석" in saved
    saved_json = json.loads(Path(payload["report_files"]["json"]).read_text(encoding="utf-8"))
    assert saved_json["sources"][0]["assessment"] == "별도로 검토하지 않은 출처 평가"


def test_rag_stores_deduplicated_cited_quotes_and_metadata_without_model_prose(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    rag = Rag()
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    payload = _analysis_payload()
    quote = "The study\ncompared twenty participants in the same environment."
    payload["sources"][0].update(content="Introduction. " + quote + " Conclusion.",
        published_at="2026-06-15", modified_at="2026-06-16", assessment="AI 출처 평가 문장")
    payload["claims"][0].update(text="인용에 없지만 모델이 추가한 성과 보장", caveat="AI 주장 한계",
                                evidence=[{"source_id": "S1", "quote": quote}])
    payload["recommendations"] = [{"kind": "recommendation", "text": "AI 실행 제안 문장", "citations": ["S1"],
                                    "evidence": [{"source_id": "S1", "quote": " ".join(quote.split())}]}]
    payload["conflicts"] = [{"claim": "AI 상충 해석 문장", "citations": ["S1"]}]
    payload["limitations"] = ["AI 조사 한계 문장"]
    payload["plan"] = {"topic": "AI 주제 문장", "source_strategy": "AI 조사 범위 문장", "objective": "AI 목적 문장"}
    original = copy.deepcopy(payload)
    BrowserPlugin()._save_research_report(payload)
    assert payload["rag_saved"] and rag.text.count(quote) == 1
    assert "Introduction." not in rag.text and "Conclusion." not in rag.text
    assert "https://example.org" in rag.text and "2026-06-15" in rag.text and "2026-06-16" in rag.text
    assert "신뢰도(AI 평가): 보통" in rag.text
    assert "AI가 선택한 출처 원문 발췌" in rag.text and "진실성이 확인되었다는 뜻은 아닙니다" in rag.text
    generated = [original["claims"][0]["text"], original["claims"][0]["caveat"],
                 original["recommendations"][0]["text"], original["conflicts"][0]["claim"],
                 original["sources"][0]["assessment"], *original["limitations"], *original["plan"].values()]
    assert all(text not in rag.text for text in generated)
    saved = json.loads(Path(payload["report_files"]["json"]).read_text(encoding="utf-8"))
    assert all(saved[key] == value for key, value in original.items())
    markdown = Path(payload["report_files"]["markdown"]).read_text(encoding="utf-8")
    assessment = original["sources"][0]["assessment"]
    assert assessment not in markdown
    assert all(text in markdown for text in generated if text != assessment)


@pytest.mark.parametrize("evidence", [
    None, {}, [], [None], ["source"],
    [{"source_id": "S999", "quote": "실제로 존재하지 않는 출처를 인용한 문장입니다."}],
    [{"source_id": [], "quote": "유효하지 않은 출처 ID를 인용한 문장입니다."}],
    [{"source_id": "S1", "quote": ["문자열이 아닌 인용입니다."]}],
    [{"source_id": "S1", "quote": "짧은 문장"}],
    [{"source_id": "S1", "quote": "수집 본문에 없는 충분히 긴 위조 인용문입니다."}],
])
def test_rag_does_not_index_missing_malformed_or_unmatched_evidence(tmp_path, monkeypatch, evidence):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    indexed = []
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: indexed.append(True))
    payload = _analysis_payload()
    payload["claims"][0]["evidence"] = evidence
    BrowserPlugin()._save_research_report(payload)
    assert not indexed and not payload["rag_saved"]
    saved = json.loads(Path(payload["report_files"]["json"]).read_text(encoding="utf-8"))
    assert saved["claims"][0]["evidence"] == evidence


def test_rag_checks_each_quote_against_its_cited_source(tmp_path, monkeypatch):
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    rag = Rag()
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: rag)
    payload = _analysis_payload()
    quote = payload["sources"][0]["content"]
    other_quote = "다른 자료에는 오십 명이 참여했다는 설명이 있습니다."
    payload["sources"].append({"source_id": "S2", "url": "https://example.org/other", "reliability": "high", "content": other_quote})
    payload["claims"][0]["evidence"].extend([
        {"source_id": "S2", "quote": other_quote},
        {"source_id": "S1", "quote": other_quote},
    ])
    BrowserPlugin()._save_research_report(payload)
    assert payload["rag_saved"] and quote in rag.text and other_quote not in rag.text
    assert "https://example.org/other" not in rag.text


def test_research_search_does_not_rewrite_or_exclude_source_types(monkeypatch):
    searched = []
    class Search:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def text(self, query, max_results):
            searched.append(query)
            return [{"href": "https://instagram.com/p/public", "title": "공개 게시물", "body": "경험"},
                    {"href": "https://reddit.com/r/topic", "title": "토론", "body": "경험"}]
    monkeypatch.setattr("plugins.browser.DDGS", Search)
    results = BrowserPlugin._search_candidates("AI가 고른 검색어", 4)
    assert searched == ["AI가 고른 검색어"]
    assert len(results) == 2


def test_publication_failure_does_not_index_missing_report(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(Config.API_CONFIG, "DB_PATH", str(tmp_path / "assistant.db"))
    indexed = []
    monkeypatch.setattr("core.rag.get_rag_manager", lambda: indexed.append(True))
    def fail(*args):
        raise OSError("cannot publish")
    monkeypatch.setattr("core.artifact_transaction.commit_artifact_bundle", fail)
    payload = {"query": "조사", "created_at": time.time(), "expires_at": time.time()+60,
               "sources": [], "claims": [], "conflicts": []}
    with pytest.raises(OSError):
        BrowserPlugin()._save_research_report(payload)
    assert not indexed
