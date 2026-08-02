"""Distill durable user knowledge from conversation without treating chat as truth wholesale."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable

from core.knowledge_memory import (
    EpistemicStatus, KnowledgeRecord, MemoryKind, MemoryPolicyError,
    get_knowledge_memory,
)


@dataclass(frozen=True)
class MemoryCandidate:
    kind: MemoryKind
    subject: str
    predicate: str
    content: str
    confidence: float = 0.9
    correction: bool = False


class ConversationMemoryConsolidator:
    """Create a few typed memories from explicit, durable user statements."""

    _DURABLE = re.compile(
        r"(?:기억해|기억해둬|잊지\s*마|나는|내가|내\s|우리\s*프로젝트|이\s*프로젝트|"
        r"항상|앞으로|선호|좋아|싫어|원해|원하지|기본으로|규칙|정정|아니라)", re.I,
    )
    _TRANSIENT = re.compile(r"^(?:안녕|고마워|뭐해|심심|오늘\s*(?:날씨|시간)|\d+분\s*뒤)", re.I)
    _KINDS = {item.value: item for item in MemoryKind}
    _CANONICAL_PREFERENCES = {
        "assistant_name", "wake_word", "user_address", "response_style", "response_language",
    }
    _IGNORED_PROFILE_KEYS = {
        "command", "이름", "wake_word", "자비스_이름", "wake_word_detection",
    }

    def __init__(self, llm=None, store=None, rag=None):
        self.llm = llm
        self.store = store or get_knowledge_memory()
        self.rag = rag

    def should_consider(self, user_text: str) -> bool:
        value = " ".join(str(user_text or "").split())
        return 4 <= len(value) <= 1200 and bool(self._DURABLE.search(value)) and not self._TRANSIENT.search(value)

    def extract(self, user_text: str) -> list[MemoryCandidate]:
        if not self.should_consider(user_text):
            return []
        if self.llm is not None:
            candidates = self._extract_with_llm(user_text)
            if candidates is not None:
                return candidates
        return self._fallback_extract(user_text)

    def _extract_with_llm(self, user_text: str) -> list[MemoryCandidate] | None:
        prompt = (
            "사용자 발화에서 다음 대화에도 도움이 되는 명시적 장기 기억만 추출하세요. "
            "잡담, 현재 한 번만 수행할 명령, assistant의 추측은 제외하세요. 출력은 JSON 배열만 사용하세요. "
            "각 항목: kind(preference|fact|project|case), subject(짧고 안정적인 주제), "
            "predicate, content(사용자가 명시한 사실), confidence(0~1), correction(boolean). "
            "사용자가 기억·선호·지속 규칙·프로젝트 결정을 명시하지 않았다면 []를 출력하세요."
        )
        try:
            raw = str(self.llm.chat([
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ]) or "").strip()
            match = re.search(r"\[[\s\S]*\]", raw)
            if not match:
                return None
            payload = json.loads(match.group(0))
            result = []
            for item in payload[:5]:
                kind = self._KINDS.get(str(item.get("kind", "")).casefold())
                subject = " ".join(str(item.get("subject", "")).split())[:100]
                predicate = " ".join(str(item.get("predicate", "describes")).split())[:80]
                content = " ".join(str(item.get("content", "")).split())[:1000]
                confidence = max(0.0, min(float(item.get("confidence", 0.9)), 1.0))
                if (kind and subject and content and confidence >= 0.75
                        and self._grounded(content, user_text)):
                    result.append(MemoryCandidate(
                        kind, subject, predicate or "describes", content,
                        confidence, bool(item.get("correction", False)),
                    ))
            return result
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        except Exception:
            return None

    @staticmethod
    def _grounded(content: str, source: str) -> bool:
        """Reject extracted claims that introduce values absent from the user utterance."""
        protected = re.findall(
            r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|\d+(?:\.\d+)?",
            content, re.IGNORECASE,
        )
        source_folded = source.casefold()
        if any(value.casefold() not in source_folded for value in protected):
            return False
        source_tokens = set(re.findall(r"[\w가-힣]+", source.casefold()))
        content_tokens = {
            token for token in re.findall(r"[\w가-힣]+", content.casefold()) if len(token) >= 2
        }
        if not content_tokens:
            return False
        overlap = sum(
            1 for token in content_tokens
            if any(token in source_token or source_token in token for source_token in source_tokens)
        )
        return overlap / len(content_tokens) >= 0.35

    @staticmethod
    def _fallback_extract(user_text: str) -> list[MemoryCandidate]:
        text = " ".join(str(user_text).strip().split())
        correction = bool(re.search(r"(?:정정|아니라|바꿨어|변경했어)", text))
        if re.search(r"(?:선호|좋아|싫어|항상|앞으로|기본으로|원하지)", text):
            return [MemoryCandidate(MemoryKind.PREFERENCE, "사용자 작업 선호", "prefers", text, 0.82, correction)]
        if re.search(r"(?:우리\s*프로젝트|이\s*프로젝트|현재\s*프로젝트)", text):
            return [MemoryCandidate(MemoryKind.PROJECT, "현재 프로젝트 결정", "uses", text, 0.82, correction)]
        if re.search(r"(?:기억해|기억해둬|잊지\s*마)", text):
            return [MemoryCandidate(MemoryKind.FACT, "사용자가 기억하도록 지정한 내용", "describes", text, 0.8, correction)]
        return []

    def consolidate(self, user_text: str, *, session_id: str = "", workspace_namespace: str = "global") -> list[str]:
        record_ids = []
        for candidate in self.extract(user_text):
            try:
                record_id, namespace = self._persist(candidate, session_id, workspace_namespace)
                record_ids.append(record_id)
                if self.rag is not None:
                    self.rag.add_text_document(
                        f"{candidate.subject} {candidate.predicate}: {candidate.content}",
                        doc_id=f"memory-{record_id}", namespace=namespace,
                        metadata={"source_type": "memory", "kind": candidate.kind.value, "record_id": record_id},
                    )
            except MemoryPolicyError:
                continue
        return record_ids

    def bootstrap_profile(self, profile) -> list[str]:
        """Make already approved profile/settings available to semantic retrieval."""
        candidates = []
        for key, value in profile.get_all().items():
            if key not in self._IGNORED_PROFILE_KEYS and str(value).strip():
                candidates.append(MemoryCandidate(
                    MemoryKind.FACT, f"사용자 프로필 {key}", "is", str(value), 1.0,
                ))
        for key, value in profile.get_all_preferences().items():
            if key in self._CANONICAL_PREFERENCES and str(value).strip():
                candidates.append(MemoryCandidate(
                    MemoryKind.PREFERENCE, f"사용자 설정 {key}", "prefers", str(value), 1.0,
                ))
        valid_subjects = {candidate.subject for candidate in candidates}
        stale_ids = self.store.supersede_profile_records_except(valid_subjects)
        if self.rag is not None:
            for record_id in stale_ids:
                self.rag.remove_text_document(f"memory-{record_id}", namespace="global")
        record_ids = []
        for candidate in candidates:
            try:
                record_id, namespace = self._persist(candidate, "profile", "global")
                record_ids.append(record_id)
                if self.rag is not None:
                    self.rag.add_text_document(
                        f"{candidate.subject} {candidate.predicate}: {candidate.content}",
                        doc_id=f"memory-{record_id}", namespace=namespace,
                        metadata={"source_type": "memory", "kind": candidate.kind.value, "record_id": record_id},
                    )
            except MemoryPolicyError:
                continue
        return record_ids

    def _persist(self, candidate: MemoryCandidate, session_id: str,
                 workspace_namespace: str) -> tuple[str, str]:
        namespace = workspace_namespace if candidate.kind in {
            MemoryKind.PROJECT, MemoryKind.TASK, MemoryKind.CASE,
        } else "global"
        if candidate.correction:
            record_id = self.store.correct(
                subject=candidate.subject, predicate=candidate.predicate,
                content=candidate.content, workspace_namespace=namespace,
                source_label=f"사용자 정정 · session:{session_id or 'unknown'}",
            )
        else:
            record = KnowledgeRecord(
                content=candidate.content, kind=candidate.kind,
                subject=candidate.subject, predicate=candidate.predicate,
                epistemic_status=EpistemicStatus.USER_CLAIM,
                source_label=("기존 사용자 프로필" if session_id == "profile"
                              else f"사용자 발화 · session:{session_id or 'unknown'}"),
                workspace_namespace=namespace, confidence=candidate.confidence,
                metadata={"automatic_consolidation": True, "session_id": session_id},
            )
            record_id = self.store.remember(record, automatic=True)
        return record_id, namespace
