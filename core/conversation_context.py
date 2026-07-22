"""최근 대화와 활성 상태를 이용해 생략된 사용자 요청을 복원한다."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import json
import re
import threading


@dataclass
class ResolvedRequest:
    original_request: str
    resolved_request: str
    topic: str = "general"
    entities: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    needs_clarification: bool = False
    clarification_question: str = ""


class ConversationContextResolver:
    def __init__(self, llm):
        self.llm = llm
        self._states: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _json_object(text: str) -> Optional[Dict[str, Any]]:
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
        try:
            value = json.loads(candidate)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", candidate, re.S)
            if not match:
                return None
            try:
                value = json.loads(match.group(0))
                return value if isinstance(value, dict) else None
            except json.JSONDecodeError:
                return None

    def resolve(self, request: str, history: Optional[List[Dict[str, str]]] = None,
                session_id: str = "") -> ResolvedRequest:
        history = [m for m in (history or []) if m.get("role") in {"user", "assistant"}][-8:]
        with self._lock:
            active_state = dict(self._states.get(session_id, {}))
        if not history and not active_state:
            return ResolvedRequest(request, request)

        system = """당신은 대화 문맥 해석기입니다. 최근 대화와 활성 상태를 이용해 현재 요청의 생략된 대상·장소·시간·문서를 복원하세요.
반드시 JSON 객체만 반환하세요:
{"resolved_request":"독립적으로 실행 가능한 한국어 요청", "topic":"주제", "entities":{}, "confidence":0.0, "needs_clarification":false, "clarification_question":""}
규칙:
- 사용자가 말하지 않은 사실은 만들지 마세요.
- '그거', '거기', '온도는?', '내일은?', '좀 더 정중하게' 같은 후속 표현은 직전 문맥에서 대상을 상속하세요.
- 후보가 여러 개이거나 위험한 변경의 대상이 불명확하면 needs_clarification=true로 두고 한 문장으로 질문하세요.
- 충분히 독립적인 요청이면 원문 의미를 유지하세요."""
        transcript = "\n".join(f"{m['role']}: {m.get('content', '')}" for m in history)
        prompt = (
            f"활성 상태: {json.dumps(active_state, ensure_ascii=False)}\n"
            f"최근 대화:\n{transcript}\n\n현재 요청: {request}"
        )
        try:
            payload = self._json_object(self.llm.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]))
        except Exception:
            payload = None
        if not payload or not str(payload.get("resolved_request", "")).strip():
            return ResolvedRequest(request, request, confidence=0.0)

        result = ResolvedRequest(
            original_request=request,
            resolved_request=str(payload["resolved_request"]).strip(),
            topic=str(payload.get("topic", "general")),
            entities=payload.get("entities") if isinstance(payload.get("entities"), dict) else {},
            confidence=max(0.0, min(float(payload.get("confidence", 0.5)), 1.0)),
            needs_clarification=bool(payload.get("needs_clarification", False)),
            clarification_question=str(payload.get("clarification_question", "")).strip(),
        )
        # 해석 과정에서 "온도는?", "내일은?", "좀 더 정중하게" 같은 후속
        # 질문의 초점이 사라지지 않도록 원문도 실행 목표에 함께 보존한다.
        if result.resolved_request != request:
            recent_user_context = " | ".join(
                str(message.get("content", "")).strip()
                for message in history[-4:]
                if message.get("role") == "user" and str(message.get("content", "")).strip()
            )
            result.resolved_request = (
                f"{result.resolved_request}\n"
                f"후속 질문의 핵심 요구: {request}"
                + (f"\n해석 근거가 된 최근 사용자 대화: {recent_user_context}" if recent_user_context else "")
            )
        if session_id and not result.needs_clarification:
            with self._lock:
                state = self._states.setdefault(session_id, {})
                state["topic"] = result.topic
                state.update(result.entities)
        return result
