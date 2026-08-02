import json

from core.agent_services import ConversationService
from core.knowledge_memory import KnowledgeMemoryStore, MemoryKind
from core.memory_consolidator import ConversationMemoryConsolidator
from core.rag import VectorRAGManager


class JsonLLM:
    def __init__(self, payload): self.payload, self.messages = payload, []
    def chat(self, messages):
        self.messages.append(messages)
        return json.dumps(self.payload, ensure_ascii=False)


class CaptureLLM:
    def __init__(self, response="알겠어."): self.response, self.messages = response, []
    def chat(self, messages):
        self.messages.append(messages)
        return self.response


class FakeRag:
    def __init__(self): self.items = []
    def add_text_document(self, text, **kwargs): self.items.append((text, kwargs))
    def remove_text_document(self, doc_id, **kwargs): return True


def test_explicit_preference_is_stored_and_indexed(tmp_path):
    store, rag = KnowledgeMemoryStore(tmp_path / "knowledge.db"), FakeRag()
    llm = JsonLLM([{
        "kind": "preference", "subject": "보고서 기본 글꼴", "predicate": "prefers",
        "content": "보고서는 항상 맑은 고딕으로 작성한다", "confidence": 0.96, "correction": False,
    }])
    consolidator = ConversationMemoryConsolidator(llm=llm, store=store, rag=rag)
    ids = consolidator.consolidate("나는 보고서를 항상 맑은 고딕으로 작성하는 걸 선호해", session_id="s1")
    records = store.search("보고서 글꼴", kinds=[MemoryKind.PREFERENCE])
    assert len(ids) == 1 and records[0].content == "보고서는 항상 맑은 고딕으로 작성한다"
    assert rag.items[0][1]["doc_id"] == f"memory-{ids[0]}"


def test_transient_chat_is_not_consolidated(tmp_path):
    llm = JsonLLM([])
    consolidator = ConversationMemoryConsolidator(llm=llm, store=KnowledgeMemoryStore(tmp_path / "k.db"))
    assert consolidator.consolidate("안녕 오늘 날씨 어때", session_id="s1") == []
    assert llm.messages == []


def test_duplicate_memory_is_idempotent(tmp_path):
    payload = [{"kind": "project", "subject": "Python 버전", "predicate": "uses",
                "content": "프로젝트는 Python 3.12를 사용한다", "confidence": 0.95, "correction": False}]
    store = KnowledgeMemoryStore(tmp_path / "k.db")
    consolidator = ConversationMemoryConsolidator(llm=JsonLLM(payload), store=store)
    first = consolidator.consolidate("이 프로젝트는 Python 3.12를 사용해", workspace_namespace="work-a")
    second = consolidator.consolidate("이 프로젝트는 Python 3.12를 사용해", workspace_namespace="work-a")
    assert first == second
    assert len(store.search("Python", workspace_namespace="work-a")) == 1


def test_sensitive_credentials_are_not_automatically_saved(tmp_path):
    payload = [{"kind": "fact", "subject": "API key", "predicate": "is",
                "content": "내 API key는 sk_abcdefghijklmnop", "confidence": 0.99, "correction": False}]
    store = KnowledgeMemoryStore(tmp_path / "k.db")
    consolidator = ConversationMemoryConsolidator(llm=JsonLLM(payload), store=store)
    assert consolidator.consolidate("내 API key는 sk_abcdefghijklmnop이니 기억해") == []
    assert store.search("") == []


def test_conversation_service_receives_relevant_memory_context():
    llm = CaptureLLM("맑은 고딕으로 작성할게.")
    ConversationService(llm).respond(
        "보고서 만들어줘", [], assistant_name="아니스", address="지휘관님",
        memory_context="- 보고서 기본 글꼴 prefers: 맑은 고딕",
    )
    system = llm.messages[0][0]["content"]
    assert "저장된 사용자 장기 기억" in system and "맑은 고딕" in system


def test_rag_can_index_memory_text_without_source_file(tmp_path):
    manager = VectorRAGManager.__new__(VectorRAGManager)
    manager.data_dir, manager.rag_file = str(tmp_path), str(tmp_path / "simple_rag.json")
    manager.documents, manager.namespace, manager.use_vector_rag = {}, "project-a", False
    manager.add_text_document("보고서 기본 글꼴은 맑은 고딕이다", doc_id="memory-1", namespace="global")
    results = manager.search_docs("보고서 글꼴")
    assert results and results[0]["source"] == "memory://memory-1"


def test_profile_bootstrap_ignores_legacy_identity_keys(tmp_path):
    class Profile:
        def get_all(self): return {"email": "user@example.com", "이름": "자비스", "command": ""}
        def get_all_preferences(self):
            return {"assistant_name": "아니스", "user_address": "지휘관님", "응답_스타일": "보스."}
    store = KnowledgeMemoryStore(tmp_path / "k.db")
    ids = ConversationMemoryConsolidator(store=store, rag=FakeRag()).bootstrap_profile(Profile())
    records = store.search("", limit=20)
    assert len(ids) == 3
    assert {record.subject for record in records} == {
        "사용자 프로필 email", "사용자 설정 assistant_name", "사용자 설정 user_address",
    }
