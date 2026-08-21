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
    relation: str = "independent"
    context_used: bool = False


class ConversationContextResolver:
    _REFERENCE_PATTERN = re.compile(
        r"(?:^|\s)(?:그거|그걸|그것|거기|그곳|그\s*(?:파일|문서|사진|작업|내용|사람)|"
        r"이거|이걸|이것|여기|아까|방금|직전|앞에서|이어서|계속|그대로|같은\s*걸로)(?:\s|$|[을를은는이가도])",
        re.IGNORECASE,
    )
    _REVISION_PATTERN = re.compile(
        r"^\s*(?:아니|그게\s*아니라|대신|그러면|그럼|다시|좀\s*더|더\s+|"
        r"방금\s*것|이전\s*것)", re.IGNORECASE,
    )
    _ELLIPTICAL_QUESTION_PATTERN = re.compile(
        r"^\s*[^\s]{1,12}(?:은|는|이|가|도)\s*(?:어때|어느|얼마|몇|뭐|어떻게|언제|어디|누구)",
        re.IGNORECASE,
    )
    _VALUE_FRAGMENT_PATTERN = re.compile(
        r"^\s*(?:(?:19|20)\d{2}[./년-]\s*\d{1,2}(?:[./월-]\s*\d{1,2})?"
        r"|\d+(?:\.\d+)?\s*(?:초|분|시간|일|주|개월|년|퍼센트|%|px|픽셀))"
        r"(?:\s*(?:부터|까지|~|-).*)?\s*$",
        re.IGNORECASE,
    )

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
        if not self._may_depend_on_context(request):
            return ResolvedRequest(request, request, relation="independent", context_used=False)

        system = """당신은 대화 문맥 해석기입니다. 최근 대화와 활성 상태를 이용해 현재 요청의 생략된 대상·장소·시간·문서를 복원하세요.
반드시 JSON 객체만 반환하세요:
{"resolved_request":"독립적으로 실행 가능한 한국어 요청", "topic":"주제", "entities":{}, "confidence":0.0, "needs_clarification":false, "clarification_question":"", "relation":"follow_up|independent|ambiguous", "context_used":true}
규칙:
- 사용자가 말하지 않은 사실은 만들지 마세요.
- '그거', '거기', '온도는?', '내일은?', '좀 더 정중하게' 같은 후속 표현은 직전 문맥에서 대상을 상속하세요.
- 후보가 여러 개이거나 위험한 변경의 대상이 불명확하면 needs_clarification=true로 두고 한 문장으로 질문하세요.
- 최근 대화나 활성 상태에서 안전하게 추론할 수 있는 정보는 다시 묻지 마세요.
- 여러 정보가 부족해도 작업을 시작하는 데 가장 중요한 것 하나만 먼저 질문하세요.
- 대상 종류나 파일 형식이 명시되지 않았고 최근 대화를 계승한다는 표현도 없다면 임의의 형식을 선택하지 말고 필요한 정보를 질문하세요.
- 사용자의 정정 표현('아니', '그게 아니라', '대신')은 직전 요청을 폐기하지 말고 수정 지시로 반영하세요.
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
            relation=str(payload.get("relation", "follow_up")).strip().casefold(),
            context_used=bool(payload.get("context_used", True)),
        )
        if result.relation not in {"follow_up", "ambiguous"} or not result.context_used:
            result.resolved_request = request
            result.relation = "independent"
            result.context_used = False
        if session_id and not result.needs_clarification:
            with self._lock:
                state = self._states.setdefault(session_id, {})
                state["topic"] = result.topic
                state.update(result.entities)
        return result

    @classmethod
    def _may_depend_on_context(cls, request: str) -> bool:
        """도메인 키워드가 아니라 한국어 담화 표지와 생략 형태로 후속 여부를 판단한다."""
        text = str(request or "").strip()
        if not text:
            return False
        return bool(
            cls._REFERENCE_PATTERN.search(text)
            or cls._REVISION_PATTERN.search(text)
            or cls._ELLIPTICAL_QUESTION_PATTERN.search(text)
            or cls._VALUE_FRAGMENT_PATTERN.fullmatch(text)
        )
