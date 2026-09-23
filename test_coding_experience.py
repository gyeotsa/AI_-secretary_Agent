"""Regression evidence for scoped attempts, approval safety and RAG provenance."""
import sqlite3

import pytest

from core.coding_experience import CodingExperienceStore, normalize_workspace


CODE = "def solution(n):\n    return n + 1"
ANSWER = f"수정한 코드입니다.\n```python\n{CODE}\n```"
PROBLEM = "주어진 정수에 1을 더하는 solution 함수를 작성해줘."
SCOPE = {"session_id": "session-a", "workspace_path": "project-a"}
RESULTS = [{"executed": True, "passed": True, "input": [2], "expected": 3, "actual": 3}]


@pytest.fixture
def store(tmp_path):
    return CodingExperienceStore(str(tmp_path / "attempts.db"))


def attempt(store, **kwargs):
    return store.record_attempt(
        SCOPE["session_id"], "입력 2 기대 3 실제 2", ANSWER,
        workspace_path=SCOPE["workspace_path"], problem=PROBLEM,
        failed_code="def solution(n): return n", failure_cause="1을 더하지 않음",
        tests=[{"input": [2], "expected": 3}],
        metadata={"memory_namespace": "workspace-a"}, **kwargs,
    )


@pytest.mark.parametrize("text", [
    "정답이 아니야", "통과 못했어", "성공 0개 실패 4개", "성공하지 않았어",
    "정답이야?", "정답 알려줘", "이전 문제는 정답이야", "not accepted",
    "테스트 모두 통과라고 했지만 틀렸어", "성공 4개 실패 1개",
])
def test_negative_or_unrelated_reports_are_not_success(text):
    assert not CodingExperienceStore.is_success_report(text)


@pytest.mark.parametrize("text", [
    "정답이야", "테스트 모두 통과했어", "이번엔 통과했어", "성공 4개 실패 0개", "Accepted",
])
def test_success_reports_remain_user_evidence(store, text):
    attempt_id = attempt(store)
    report = store.record_user_feedback(attempt_id, **SCOPE, feedback=text)
    assert report["user_report"] == "success"
    assert report["verification_source"] == "user_report"
    assert not report["verified"]
    assert store.verified_documents(**SCOPE) == []


def test_duplicate_approval_never_promotes_an_older_or_other_scope_attempt(store):
    old_id = attempt(store)
    current_id = attempt(store, previous_attempt_id=old_id)
    other_id = store.record_attempt("other-session", "", ANSWER, problem=PROBLEM)
    for _ in range(3):
        assert store.record_user_feedback(current_id, **SCOPE, feedback="정답이야")["id"] == current_id
    assert store.get_attempt(old_id, **SCOPE)["user_report"] == ""
    assert store.get_attempt(other_id, **SCOPE) is None
    assert store.record_user_feedback(current_id, session_id="other-session",
                                      workspace_path="project-a", feedback="정답이야") is None
    assert store.record_user_feedback(current_id, session_id="session-a",
                                      workspace_path="project-b", feedback="정답이야") is None
    assert store.latest_attempt(**SCOPE)["id"] == current_id
    assert store.verified_documents(**SCOPE) == []


def test_attempt_retains_original_problem_failure_and_scoped_parent(store):
    first = attempt(store)
    second = attempt(store, previous_attempt_id=first)
    row = store.get_attempt(second, **SCOPE)
    assert row["problem"] == PROBLEM
    assert row["failure"] == "입력 2 기대 3 실제 2"
    assert row["failed_code"] == "def solution(n): return n"
    assert row["failure_cause"] == "1을 더하지 않음"
    assert row["tests"] == [{"input": [2], "expected": 3}]
    assert row["correction"] == ANSWER
    assert row["previous_attempt_id"] == first
    assert row["workspace_path"] == normalize_workspace("project-a")
    with pytest.raises(ValueError):
        store.record_attempt("wrong-session", "", ANSWER, previous_attempt_id=first)


def test_only_exact_code_execution_can_publish_and_negative_revokes(store):
    attempt_id = attempt(store)
    assert store.record_test_result(attempt_id, **SCOPE, tested_code=CODE + "\nprint(1)",
                                    test_results=RESULTS) is None
    assert store.record_test_result(attempt_id, **SCOPE, tested_code=CODE,
                                    test_results=[{"passed": True}]) is None
    row = store.record_test_result(attempt_id, **SCOPE, tested_code=CODE, test_results=RESULTS)
    assert row["verified"] and row["verification_source"] == "executed_tests"
    assert store.verified_documents(**SCOPE, attempt_id="another-id") == []
    assert store.verified_documents(**SCOPE, attempt_id=attempt_id)[0]["id"] == attempt_id
    assert store.rag_document(attempt_id, **SCOPE, namespace="wrong-namespace") is None
    document = store.rag_document(attempt_id, **SCOPE, namespace="workspace-a")
    assert document["metadata"]["coding_attempt_id"] == attempt_id
    assert PROBLEM in document["text"] and CODE in document["text"]
    rejected = store.record_user_feedback(attempt_id, **SCOPE, feedback="정답이 아니야")
    assert not rejected["verified"] and rejected["test_results"] == RESULTS
    assert store.rag_document(attempt_id, **SCOPE, namespace="workspace-a") is None
    # A later unsupported user approval cannot reinstate stale executed evidence.
    store.record_user_feedback(attempt_id, **SCOPE, feedback="정답이야")
    assert store.verified_documents(**SCOPE) == []


def test_claimed_pass_with_mismatching_actual_result_is_not_verified(store):
    attempt_id = attempt(store)
    row = store.record_test_result(attempt_id, **SCOPE, tested_code=CODE,
                                   test_results=[{**RESULTS[0], "actual": 2}])
    assert not row["verified"]
    assert store.verified_documents(**SCOPE) == []


def test_legacy_keyword_approvals_migrate_without_trusting_or_losing_data(tmp_path):
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE coding_experiences (id TEXT PRIMARY KEY, session_id TEXT, "
                     "problem TEXT, failure TEXT, corrected_code TEXT, verified INTEGER NOT NULL DEFAULT 0, "
                     "created_at REAL NOT NULL, metadata TEXT)")
        conn.execute("INSERT INTO coding_experiences VALUES (?,?,?,?,?,?,?,?)",
                     ("old", "session-a", PROBLEM, "오답", ANSWER, 1, 1.0, '{}'))
    migrated = CodingExperienceStore(path)
    record = migrated.get_attempt("old", session_id="session-a", workspace_path="")
    assert record["correction"] == ANSWER and record["failure"] == "오답"
    assert not record["verified"] and record["verification_source"] == "legacy_unverified"
    # Reopening leaves newly executed evidence intact instead of downgrading each startup.
    new_id = migrated.record_attempt("s", "", ANSWER, problem=PROBLEM)
    migrated.record_test_result(new_id, session_id="s", workspace_path="", tested_code=CODE,
                                test_results=RESULTS)
    assert CodingExperienceStore(path).get_attempt(new_id, session_id="s", workspace_path="")["verified"]


def test_rag_filters_legacy_unverified_wrong_identity_and_revoked_records(store, monkeypatch):
    import core.coding_experience as experience_module
    from core.rag import VectorRAGManager

    monkeypatch.setattr(experience_module, "get_coding_experience_store", lambda: store)
    rag = VectorRAGManager.__new__(VectorRAGManager)
    rag.documents = {}
    rag.namespace = "workspace-a"
    attempt_id = attempt(store)
    store.record_test_result(attempt_id, **SCOPE, tested_code=CODE, test_results=RESULTS)
    document = store.rag_document(attempt_id, **SCOPE, namespace="workspace-a")
    candidate = {**document["metadata"], "namespace": document["namespace"],
                 "content": rag.chunk_text(document["text"])[0]}
    ordinary = {"content": "일반 문서", "source_type": "file"}
    legacy = {"content": "오답 코드", "source_type": "verified_coding_experience", "verified": True}
    wrong_scope = {**candidate, "coding_session_id": "other-session"}
    wrong_namespace = {**candidate, "namespace": "global"}
    stale_content = {**candidate, "content": "unrelated unverified code"}
    def retrieve(candidates):
        return rag._filter_and_rerank("코드", candidates, 10, None, False, namespace="workspace-a")
    filtered = retrieve([ordinary, legacy, wrong_scope, wrong_namespace, stale_content, candidate])
    assert len(filtered) == 2
    assert {item["content"] for item in filtered} == {ordinary["content"], candidate["content"]}
    store.record_user_feedback(attempt_id, **SCOPE, feedback="통과 못했어")
    assert retrieve([candidate]) == []
    # Ordinary or legacy missing-identity lookups do not open a production DB.
    def forbidden():
        raise AssertionError("Store must not open without a complete coding identity")
    monkeypatch.setattr(experience_module, "get_coding_experience_store", forbidden)
    assert len(retrieve([ordinary, legacy])) == 1


def test_persisted_rag_document_is_rechecked_after_feedback(store, tmp_path, monkeypatch):
    import core.coding_experience as experience_module
    from core.rag import VectorRAGManager

    monkeypatch.setattr(experience_module, "get_coding_experience_store", lambda: store)
    monkeypatch.setattr("core.rag.Config.DB_PATH", str(tmp_path / "rag.db"))
    monkeypatch.setattr(VectorRAGManager, "_init_vector_rag", lambda self: None)
    rag = VectorRAGManager()
    attempt_id = attempt(store)
    store.record_test_result(attempt_id, **SCOPE, tested_code=CODE, test_results=RESULTS)
    payload = store.rag_document(attempt_id, **SCOPE, namespace="workspace-a")
    rag.add_text_document(**payload)
    restarted = VectorRAGManager()
    found = restarted.search_docs("solution", namespace="workspace-a")
    assert found and found[0]["coding_attempt_id"] == attempt_id
    assert restarted.search_docs("solution", namespace="workspace-b") == []
    store.record_user_feedback(attempt_id, **SCOPE, feedback="성공 0개 실패 4개")
    assert restarted.search_docs("solution", namespace="workspace-a") == []
