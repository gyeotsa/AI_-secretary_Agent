"""Responsibility-focused services behind the compatibility Executor facade."""

from __future__ import annotations

from typing import List, Optional
from dataclasses import replace
import re

from core.plan_runtime import PlanDAG, PlanStep
from core.llm import ProseResponse, OllamaClient
from core.agent_prompt_policy import agent_response_policy
from core.response_integrity import PROTECTED, map_narrative, preserves_sources
from core.utterance_scope import mask_quoted_payloads
from core.semantic_request import resolve_coding_context, render_coding_request
from core.turn_context import check_turn_cancelled
from core.answer_verification import (
    AnswerVerificationService, build_answer_contract, requires_answer_review,
)
from core.korean_naturalizer import (
    analyze_korean_naturalness, korean_writing_guidance, light_polish_korean,
)


_ROLE_LINE = re.compile(r"^\s*(?:user|assistant|system)(?:\s*:|\s|$)", re.I | re.M)


class ConversationResponse(ProseResponse):
    """String-compatible reply carrying a failed evidence check to its owner.

    Do not store guard state on the shared service: simultaneous callers own
    independent responses. Classification confidence is never tool evidence.
    """

    def __new__(cls, text: str, *, unverified_completion: bool = False,
                unsupported_activity: bool = False, answer_review=None, **metadata):
        result = super().__new__(cls, text, **metadata)
        result.unverified_completion = unverified_completion
        result.unsupported_activity = unsupported_activity
        result.answer_review = answer_review
        return result


_UNVERIFIED_CONVERSATION_COMPLETION = (
    "아직 실제 작업을 실행하지 않았습니다. 도구 실행 증거가 없으므로 "
    "완료로 보고하지 않겠습니다."
)
# Speech-act detection is separate from the evidence decision below. The
# nominal verbs share tense grammar; irregular Korean predicates need a small
# lexical table. Creative acts (e.g. making up a story) additionally require an
# external target, whereas sending/installing/saving intrinsically change it.
_STATE_ACTIONS = (
    "삭제", "저장", "전송", "전달", "발송", "실행", "설치", "등록", "예약",
    "다운로드", "업로드", "재생", "이동", "복사",
)
_CONTENT_ACTIONS = ("생성", "작성", "수정", "변경", "편집", "변환")
_IRREGULAR_STATE_PAST = ("켰", "껐", "보냈", "지웠", "옮겼", "틀었")
_IRREGULAR_CONTENT_PAST = ("열었", "닫았", "만들었", "고쳤", "바꿨", "읽었", "썼")
_EXTERNAL_TARGET = re.compile(
    r"파일|폴더|디렉[터토]리|프로그램|메모장|브라우저|앱(?:을|이|은|에서|\s|$)|"
    r"이메일|메일|카카오톡|카톡|문자|알람|일정|설정|데이터베이스|서버|"
    r"문서|스프레드시트|프레젠테이션|워크북|"
    r"https?://|[A-Za-z]:[\\/]|\.[A-Za-z0-9]{1,8}(?=[\s\"'`]|$)", re.I,
)
_PAST_ENDING = r"(?:어(?:요)?|습니다|다|네(?:요)?|지(?:요)?|고|는데)"
_ASSERTION_END = r"(?=$|[\s,.!?，。！？])"
_ACTION_ASSERTION = re.compile(
    r"(?P<negative>(?:안|못)\s+)?(?:"
    r"(?P<nominal>" + "|".join((*_STATE_ACTIONS, *_CONTENT_ACTIONS)) + r")"
    r"(?:을|를|이|가|은|는)?\s*(?:"
    r"(?:했|하였|됐|되었)" + _PAST_ENDING + r"|"
    r"(?:완료|성공)(?:(?:했|됐|하였|되었)" + _PAST_ENDING + r")?)|"
    r"(?P<irregular>" + "|".join((*_IRREGULAR_STATE_PAST, *_IRREGULAR_CONTENT_PAST)) + r")"
    + _PAST_ENDING + r"|"
    r"(?P<bare>완료했|끝냈|마쳤)" + _PAST_ENDING + r")" + _ASSERTION_END,
    re.I,
)
_NON_ASSERTIVE_TAIL = re.compile(
    r"^\s*(?:예정|계획|전(?:에|이|$)|후(?:에|라면)|상태가\s*아니|"
    r"(?:이라고|라고|고)\s*(?:가정|말하|말했|표현|설명|쓰|적|표시)|"
    r"(?:하지|하진|되지|되진)\s*(?:않|못)|아니)", re.I,
)

# This channel has no running tools. A terminal reply must not imply a search,
# test or investigation is continuing in the background. Quoted source content
# and third-party state descriptions are deliberately not assistant activity.
_ONGOING_ACTION = re.compile(
    r"(?P<negative>(?:안|못)\s+)?(?:검색|조회|확인|조사|테스트|검사|점검|분석|"
    r"다운로드|업로드|전송|설치|실행|수정|저장)(?:을|를)?\s*"
    r"(?:중(?=\s|[이입에,.;!?]|$)|(?:하|되)고\s*있)", re.I,
)
_ONGOING_ENGLISH = re.compile(
    r"\bI(?:\s+am|'m)\s+(?:currently\s+)?(?:checking|searching|investigating|"
    r"testing|downloading|uploading|sending|installing|running)\b", re.I,
)
_THIRD_PARTY_SUBJECT = re.compile(
    r"(?:사용자|사람|그|그녀|친구|동료|서버|프로그램|앱|모델|브라우저|작업자|프로세스)"
    r"(?:가|이|는|은)\s",
)
_ACTIVITY_LIMITATION = (
    "아직 실제 조사나 도구 실행을 시작하지 않았습니다. "
    "확인 중이라고 안내한 내용은 근거가 없으며, 원인도 아직 확인되지 않았습니다."
)


def has_unsupported_activity_claim(text: str) -> bool:
    visible = mask_quoted_payloads(str(text or ""))
    for match in (*_ONGOING_ACTION.finditer(visible), *_ONGOING_ENGLISH.finditer(visible)):
        start = max(visible.rfind(char, 0, match.start()) for char in ".!?\n") + 1
        end = re.search(r"[.!?\n]", visible[match.end():])
        tail = visible[match.end():match.end() + end.start() if end else len(visible)]
        prefix = visible[start:match.start()]
        if (match.groupdict().get("negative") or _THIRD_PARTY_SUBJECT.search(prefix)
                or re.search(r"\b(?:예를\s*들어|가령)\b", prefix)
                or re.match(r"\s*(?:이(?:라면|라고|라는)|인(?:지|\s*것은)|인지|"
                            r"이지\s*않|지는\s*않|지\s*않|(?:이\s*)?아니)", tail)
                or (end and visible[match.end() + end.start()] == "?")):
            continue
        return True
    return False


def guard_conversation_response(response: str, user_message: str = "") -> ConversationResponse:
    """Reject affirmative external completion in this evidence-free channel.

    Source text and non-assertive grammar are not our execution claims. This
    check never grants execution authority and never trusts model confidence.
    Actual tool-backed output is rendered through ResponseRealizer instead.
    """
    if isinstance(response, ConversationResponse) and (
            response.unverified_completion or response.unsupported_activity):
        return response
    metadata = response.metadata if isinstance(response, ProseResponse) else {}
    review = getattr(response, "answer_review", None)
    text = str(response or "")
    visible = mask_quoted_payloads(text)
    external_context = bool(_EXTERNAL_TARGET.search(str(user_message or "")))
    for match in _ACTION_ASSERTION.finditer(visible):
        # A direct question is not an assertion by the assistant.
        if visible[match.end():].lstrip().startswith(("?", "？")):
            continue
        if match.group("negative") or _NON_ASSERTIVE_TAIL.match(visible[match.end():]):
            continue
        nominal, irregular = match.group("nominal"), match.group("irregular")
        intrinsic = nominal in _STATE_ACTIONS or irregular in _IRREGULAR_STATE_PAST
        if intrinsic or external_context or _EXTERNAL_TARGET.search(visible[:match.start()]):
            return ConversationResponse(
                _UNVERIFIED_CONVERSATION_COMPLETION, unverified_completion=True,
                answer_review=review, **metadata,
            )
    if has_unsupported_activity_claim(text):
        return ConversationResponse(_ACTIVITY_LIMITATION, unsupported_activity=True,
                                    answer_review=review, **metadata)
    return ConversationResponse(text, answer_review=review, **metadata)


class ConversationService:
    """Generates ordinary dialogue without exposing the Tool runtime."""

    def __init__(self, llm, *, answer_verifier=None, generation_client_factory=None):
        self.llm = llm
        # An injected client must not secretly cause calls to a different
        # provider. The real Executor explicitly supplies local role factories.
        self.answer_verifier = answer_verifier or AnswerVerificationService(
            reviewer_factory=lambda: self.llm, repairer_factory=lambda: self.llm,
        )
        self.generation_client_factory = generation_client_factory

    def respond(self, message: str, history, *, assistant_name: str = "",
                voice_name: str = "", address: str = "보스", style: str = "",
                memory_context: str = "", answer_kind: str = "conversation",
                failure_feedback: str | None = None) -> str:
        coding_context = resolve_coding_context(message, history)
        bound_request = render_coding_request(message, coding_context) if coding_context else message
        contract = build_answer_contract(message, history)
        if coding_context:
            # Counts and quoted code in a problem are source material, not a
            # request for that many examples or an instruction to keep bad code.
            contract = replace(
                contract, message=bound_request, requested_count=None,
                require_code_per_item=False, generation_guidance="",
                source_segments=(), preserve_all_sources=False,
                requires_review=True, requires_code=coding_context["requires_code"],
                correction_authorized=bool(coding_context.get("failure_feedback")),
                previous_code=coding_context.get("previous_code", ""),
                failure_feedback=coding_context.get("failure_feedback", ""),
                execution_problem=coding_context["problem"] if coding_context["requires_code"] else "",
            )
            failure_feedback = coding_context.get("failure_feedback") or failure_feedback
        # A validated response mode can select an answer specialist without
        # conferring filesystem/tool authority or inventing an execution task.
        # Preserve the full current input/history, including problem conditions.
        if coding_context or answer_kind in {"code", "reasoning"}:
            guidance = ("제공된 문제를 실제로 풀어 답하세요. 풀이 과정과 경계 조건을 설명하고, "
                        "문제 본문의 조건은 풀이 규칙이지 실행 승인 조건이 아닙니다. "
                        "코드가 필요한 경우 완전한 코드 블록을 제공하되 실행했다고 주장하지 마세요."
                        if not coding_context or coding_context["requires_code"] else
                        "원래 문제와 이전 답변을 참고해 현재 질문에만 답하세요. 설명만 요청하거나 "
                        "결과를 알려준 경우 코드를 새로 생성하지 마세요. 사용자 보고는 실제 실행 증거가 아닙니다.")
            contract = replace(contract, requires_review=True,
                               requires_code=(coding_context["requires_code"] if coding_context
                                              else contract.requires_code or answer_kind == "code"),
                               generation_guidance=contract.generation_guidance + "\n" + guidance)
        if (failure_feedback and answer_kind == "code"
                and (not coding_context or coding_context["requires_code"])):
            contract = replace(
                contract, requires_review=True, requires_code=True,
                generation_guidance=contract.generation_guidance + (
                    "\n이전 코드에 대한 사용자의 테스트 실패 보고가 아래에 있습니다. "
                    "실패한 원인을 먼저 분석하고 이전 알고리즘을 그대로 반복하지 마세요. "
                    "수정된 전체 코드를 제시하되 실행했다고 주장하지 마세요.\n"
                    "[테스트 실패 보고]\n" + failure_feedback
                ),
            )
        if contract.requires_code:
            # Execution opt-out applies to the current request, including short
            # follow-ups. Code generation does not imply ignoring this choice.
            declined = re.search(r"(?:실행|테스트).{0,8}(?:하지\s*(?:마|말)|금지|없이)|"
                                 r"(?:do\s+not|don't|without)\s+(?:run|execut|test)", message, re.I)
            contract = replace(contract, execution_problem=("" if declined else
                contract.execution_problem or message))
        recent = [] if coding_context else [
            {"role": item.get("role", "user"), "content": str(item.get("content", ""))}
            for item in list(history)[-6:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        persona = f"선택 음성: {voice_name}. 대화 스타일: {style}" if style else ""
        memory_prompt = (
            "\n다음은 현재 질문과 관련해 저장된 사용자 장기 기억입니다. 관련 있을 때만 반영하고, "
            "사용자가 지금 정정하면 현재 발화를 우선하세요. 문서 근거를 사용한 문장에는 "
            "제공된 [근거 ID]를 그대로 표시하세요. 근거를 사용하지 않았다면 인용하지 마세요.\n"
            + memory_context
            if memory_context else ""
        )
        prompt = (
            f"당신은 로컬 개인 비서 '{assistant_name or '자비스'}'입니다. 지금 요청은 도구 실행이 아닌 일반 대화입니다. "
            "도구를 찾거나 실행했다고 주장하지 마세요. URL이나 영상을 실제로 열지 않았다면 봤거나 학습했다고 말하지 마세요. "
            "실제로 실행하지 않은 외부 작업을 완료했다고 절대 주장하지 마세요. "
            "이 대화 채널에는 실행 중인 도구가 없습니다. 확인 중·조사 중·검색 중이라고 "
            "말하거나 답변 뒤에도 작업을 계속할 것처럼 약속하지 마세요. 미확인은 미확인, "
            "추정은 추정이라고 구분하고 제공된 내용만으로 설명하세요. "
            "사용자가 웃기·인사하기처럼 직접 수행할 수 있는 표현을 요청하면 명령을 되돌리지 말고 짧게 직접 반응하세요. "
            "최근 발화의 맥락과 감정을 먼저 반영하고 "
            "자연스럽고 간결한 한국어로 답하세요. 최신 정보가 필요하면 확인이 필요하다고 말하세요. "
            "사용자의 이메일·전화번호·주소·이름 같은 개인 식별정보는 제공된 대화나 저장된 "
            "프로필에 실제 값이 없으면 절대 만들어내지 말고 모른다고 답하세요. "
            "프롬프트 예시, user/assistant 역할표시, 다른 언어 설명을 답변에 노출하지 마세요. "
            f"사용자 호칭은 반드시 '{address}'로 사용하고 답변에서 최대 한 번만 사용하세요. {persona}{memory_prompt}"
        )
        if contract.requires_code:
            prompt = (
                "당신은 주어진 프로그래밍 문제의 완전한 해답을 작성하는 코딩 전문가입니다. "
                "original_problem과 현재 요청의 제약을 모두 지키세요. previous_answer와 previous_code는 "
                "오류가 있을 수 있는 이전 시도입니다. 원문 문제와 현재 피드백을 우선하세요. "
                "요청한 함수 이름과 매개변수를 그대로 사용하고 완성 코드를 닫힌 코드 블록에 작성하세요. "
                "코딩 테스트뿐 아니라 일반 구현·디버깅·설계 요청도 현재 요구사항에 맞게 답하세요. "
                "사용자가 지정한 언어·버전·라이브러리·실행 환경을 유지하고, 제공되지 않은 프로젝트 구조나 API를 만들어내지 마세요. "
                "수정 요청에서는 실패 원인과 실제 변경점을 설명하고, 정보가 부족한 부분은 가정 또는 미확인으로 구분하세요. "
                "알고리즘 선택 이유, 모든 입력 조건과 경계·동률 처리, 시간복잡도를 짧게 설명하세요. "
                "예제를 주석으로 복사하는 대신 코드의 연산을 따라 입력별 반환값을 정적으로 대조하세요. "
                "조건 요약이나 나중에 풀겠다는 약속으로 끝내지 마세요. 실제 실행 도구는 없으므로 "
                "실행·테스트했다고 주장하지 마세요. 명시된 언어가 없으면 Python으로 답하세요. "
                + memory_prompt
            )
        prompt += "\n" + agent_response_policy() + "\n" + korean_writing_guidance(style)
        prompt += "\n" + contract.generation_guidance
        messages = [{"role": "system", "content": prompt}, *recent,
                    {"role": "user", "content": bound_request}]
        draft_client = (self.generation_client_factory(contract)
                        if self.generation_client_factory and contract.requires_review else self.llm)
        chat_prose = getattr(draft_client, "chat_prose", None) or draft_client.chat
        check_turn_cancelled()
        model_response = (chat_prose(messages, context_window=8192)
                          if contract.requires_review and isinstance(draft_client, OllamaClient)
                          else chat_prose(messages))
        check_turn_cancelled()
        metadata = model_response.metadata if isinstance(model_response, ProseResponse) else {}
        response = str(model_response.content if isinstance(model_response, ProseResponse)
                       else model_response or "").strip()
        original_response = response
        # Fix supported sentence endings without another model pass. Check
        # prose only: a quoted greeting or a code example is not our voice.
        response = _apply_requested_style(response, style)
        narrative = _response_narrative(response)
        original_narrative = _response_narrative(original_response)
        needs_repair = (
            _is_unanswered_echo(original_response, message)
            or _has_prompt_leak(response, message)
            or has_unsupported_activity_claim(response)
            or ("반말" in style and re.search(r"(?:습니다|세요|해요|까요|입니다)", narrative))
            # Preserve the semantic guard even if styling changes the ending:
            # "웃어 봐" -> "웃어보세요" asks the user to act instead.
            or (re.search(r"(?:해|어|아|여|워)\s*봐[.!?]*$", message.strip())
                and "세요" in original_narrative)
        )
        naturalness = analyze_korean_naturalness(narrative)
        needs_repair = needs_repair or (len(narrative) >= 180 and naturalness.score >= 4)
        substantive_review = requires_answer_review(contract, response)
        # A partial draft is useful as-is. Rewriting it cannot establish that
        # the requested answer was completed and may erase the truncation flag.
        if response and needs_repair and not substantive_review and not metadata.get("truncated"):
            draft = response
            try:
                repair_result = self.llm.chat([
                    {"role": "system", "content": (
                        f"아래 초안을 사용자의 질문에 대한 자연스러운 한국어 답변으로 한 번만 고쳐 써. "
                        f"역할표시·예시·요청하지 않은 외국어를 넣지 말고, 사용자 호칭은 '{address}'로 최대 한 번만 써. "
                        "실제로 실행하지 않은 외부 작업을 완료했다고 절대 주장하지 마세요. "
                        "실행 증거가 없으므로 확인 중·검색 중·테스트 중이라는 진행 주장을 지우고 "
                        "아직 확인하지 않았다는 사실과 추정 여부를 분명히 말하세요. "
                        "초안의 코드·인용·메시지 본문·파일 경로·[근거 ID]는 한 글자도 바꾸지 마세요. "
                        "사용자 말의 긍정/부정, 불편함과 요청 목적을 반대로 해석하지 마세요. "
                        f"적용할 스타일: {style or '간결하고 자연스러운 말투'}"
                    )},
                    {"role": "user", "content": f"질문: {message}\n초안: {response}"},
                ])
                # Strict clients raise on truncation, but compatibility clients
                # can return tagged text. Never replace a complete draft with it.
                repaired = ("" if isinstance(repair_result, ProseResponse)
                            and repair_result.truncated else str(repair_result or "").strip())
            except Exception as exc:
                # Optional presentation repair must not discard an available
                # draft on provider failure. Cancellation is not such a failure.
                from core.plugin import ToolCancelledError
                if isinstance(exc, ToolCancelledError):
                    raise
                check_turn_cancelled()
                repaired = ""
            response = repaired if repaired and preserves_sources(draft, repaired) else draft
        # Content requiring substantive review must reach the reviewer intact.
        # Cutting at the first foreign glyph can silently erase a heading and
        # the explanation that follows it. Request repair instead of deletion.
        if not substantive_review:
            response = _sanitize_response(response, message)
        response = _apply_requested_style(response, style)
        response = light_polish_korean(response)
        review = None
        if not metadata.get("truncated"):
            response, review = self.answer_verifier.verify(
                contract, response, style=style,
                repair_hint=("말투·질문 의도·근거 없는 진행 주장을 함께 교정하세요."
                             if needs_repair else ""),
            )
            if isinstance(response, ProseResponse):
                metadata = response.metadata
                response = response.content
            if (review is not None and review.status == "passed"
                    and review.execution_status == "not_requested"
                    and any(row.id == "code_semantics" for row in review.criteria_results)):
                response = ("[정적 검토만 수행했습니다. 실제 실행·환경 호환성·전체 정확성은 미확인입니다.]\n\n" + response)
        if not response and not metadata.get("truncated"):
            from core.llm import ModelCallError
            raise ModelCallError(type(self.llm).__name__, "", "empty_response", "응답 본문이 비어 있습니다.")
        return guard_conversation_response(
            ConversationResponse(
                response,
                answer_review=review, **metadata,
            ), message,
        )


def _normalized(text: str) -> str:
    return re.sub(r"\W+", "", str(text or "")).casefold()


def _is_unanswered_echo(response: str, message: str) -> bool:
    """Reciprocal greetings/acknowledgments are not unanswered questions.

    This only gates an optional rewrite, never intent routing or execution.
    Keep the exception anchored to the entire utterance so a question or
    command containing a greeting still requires an actual answer.
    """
    value = _normalized(message)
    if value != _normalized(response):
        return False
    reciprocal = {
        "안녕", "안녕하세요", "안녕하십니까", "반가워", "반가워요", "반갑습니다",
        "좋은아침", "좋은아침이에요", "좋은아침입니다", "잘자", "잘자요",
        "고마워", "고마워요", "감사합니다", "응", "네", "그래", "알겠어", "알겠어요",
        "hi", "hello", "goodmorning", "goodnight", "thanks", "thankyou", "ok", "okay",
    }
    return value not in reciprocal


def _response_narrative(text: str) -> str:
    """Keep source spans out of quality heuristics without joining words."""
    return PROTECTED.sub("\n", str(text or ""))


def _allows_foreign_text(user_message: str) -> bool:
    return bool(re.search(
        r"(?:러시아어|키릴|중국어|일본어|한자|번역|원문)",
        str(user_message or ""), re.IGNORECASE,
    ))


def _has_prompt_leak(text: str, user_message: str = "") -> bool:
    narrative = _response_narrative(text)
    return bool(
        _ROLE_LINE.search(narrative)
        or (not _allows_foreign_text(user_message)
            and re.search(r"[\u0400-\u04ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", narrative))
    )


def _sanitize_response(text: str, user_message: str = "") -> str:
    """Remove role/prompt leakage without rewriting legitimate Korean content."""
    def sanitize(value):
        if not value.strip():
            return value
        leading = value[:len(value)-len(value.lstrip())]
        trailing = value[len(value.rstrip()):]
        return leading + _sanitize_narrative(value, user_message) + trailing
    return map_narrative(str(text or ""), sanitize).strip()


def _sanitize_narrative(text: str, user_message: str = "") -> str:
    value = str(text or "").strip()
    if not _allows_foreign_text(user_message):
        value = re.split(
            r"[\u0400-\u04ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]",
            value, maxsplit=1,
        )[0].rstrip(" :\n")
    kept = []
    for line in value.splitlines():
        if _ROLE_LINE.match(line):
            break
        kept.append(line)
    value = "\n".join(kept).strip()
    value = re.sub(r"\s*\[?(?:GAME|WORK)\]?\s*$", "", value, flags=re.IGNORECASE)
    return value.strip()


def _apply_requested_style(text: str, style: str) -> str:
    """Apply the shared prose style without touching code, quotations or payloads."""
    return map_narrative(text, lambda value: _style_narrative(value, style))


def _style_narrative(text: str, style: str) -> str:
    """Enforce common Korean sentence endings after a small model ignores style."""
    if "반말" not in str(style or ""):
        return text
    value = str(text or "")
    replacements = (
        (r"말씀해\s*주세요", "말해 줘"),
        (r"말씀해\s*줘", "말해 줘"),
        (r"말씀해주세요", "말해 줘"),
        (r"도와드릴", "도와줄"),
        (r"어떠세요(?=[,.!?，\n]|$)", "어때"),
        (r"싶으신가요(?=[,.!?，\n]|$)", "싶어"),
        (r"말씀해\s*주시면", "말해 주면"),
        (r"알려\s*주시면", "알려 주면"),
        (r"요청하시면", "요청하면"),
        (r"원하시면", "원하면"),
        (r"알겠습니다(?=[,.!?，\n]|$)", "알겠어"),
        (r"알겠어요(?=[,.!?，\n]|$)", "알겠어"),
        (r"지\s*않았어요(?=[,.!?，\n]|$)", "지 않았어"),
        (r"했어요(?=[,.!?，\n]|$)", "했어"),
        (r"됐어요(?=[,.!?，\n]|$)", "됐어"),
        (r"없으시나요(?=[,.!?，\n]|$)", "없어"),
        (r"해\s*보겠습니다(?=[,.!?，\n]|$)", "해볼게"),
        (r"보겠습니다(?=[,.!?，\n]|$)", "볼게"),
        (r"필요하신가요(?=[,.!?，\n]|$)", "필요해"),
        (r"있으신가요(?=[,.!?，\n]|$)", "있어"),
        (r"건가요(?=[,.!?，\n]|$)", "거야"),
        (r"봤어요(?=[,.!?，\n]|$)", "봤어"),
        (r"있어요(?=[,.!?，\n]|$)", "있어"),
        (r"웃어보세요(?=[,.!?，\n]|$)", "웃어봐"),
        (r"해보세요(?=[,.!?，\n]|$)", "해봐"),
        (r"지\s*않겠습니다(?=[,.!?，\n]|$)", "지 않을게"),
        (r"하겠습니다(?=[,.!?，\n]|$)", "할게"),
        (r"찾아드릴게요(?=[,.!?，\n]|$)", "찾아줄게"),
        (r"제안해드릴게요(?=[,.!?，\n]|$)", "제안할게"),
        (r"드릴게요(?=[,.!?，\n]|$)", "줄게"),
        (r"할게요(?=[,.!?，\n]|$)", "할게"),
        (r"입니다(?=[,.!?，\n]|$)", "이야"),
        (r"거예요(?=[,.!?，\n]|$)", "거야"),
        (r"까요(?=[,.!?，\n]|$)", "까"),
        (r"예요(?=[,.!?，\n]|$)", "야"),
        (r"좋겠어요(?=[,.!?，\n]|$)", "좋겠어"),
        (r"해요(?=[,.!?，\n]|$)", "해"),
        (r"봐요(?=[,.!?，\n]|$)", "봐"),
        (r"주세요(?=[,.!?，\n]|$)", "줘"),
        (r"바랍니다(?=[,.!?，\n]|$)", "바라"),
        (r"했습니다(?=[,.!?，\n]|$)", "했어"),
        (r"됐습니다(?=[,.!?，\n]|$)", "됐어"),
        (r"있습니다(?=[,.!?，\n]|$)", "있어"),
        (r"없습니다(?=[,.!?，\n]|$)", "없어"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value)
    return value


class PlanningService:
    """Owns validated plan creation."""

    def __init__(self, planner):
        self.planner = planner

    def create(
        self, goal: str, context: str, allowed_tools: Optional[List[str]],
        required_tools: Optional[List[str]] = None,
    ) -> PlanDAG:
        if hasattr(self.planner, "build_plan_dag"):
            return self.planner.build_plan_dag(
                goal, context, allowed_tools, required_tools,
            )
        tasks = self.planner.decompose_goal(
            goal, context, allowed_tools, required_tools,
        )
        return PlanDAG(goal, [PlanStep(
            id=task.id, description=task.description,
            tool_name=(getattr(task, "required_tools", []) or [""])[0],
            dependencies=list(getattr(task, "dependencies", []) or []),
        ) for task in tasks])


class ResponseComposer:
    """Owns terminal status and user-facing completion summaries."""

    @staticmethod
    def terminal(
        *, response: str, cancelled: bool, terminal_error: Optional[str],
        completed_steps: int, failed_steps: int, retry_count: int,
    ) -> tuple[str, str]:
        if cancelled:
            return "cancelled", terminal_error or "사용자 요청으로 작업을 취소했습니다."
        if terminal_error:
            if completed_steps:
                return (
                    "partial",
                    f"일부 작업만 완료했습니다({completed_steps}단계 완료, "
                    f"{failed_steps or 1}단계 실패). {terminal_error}",
                )
            prefix = f"재시도 {retry_count}회 후 " if retry_count else ""
            return "failed", f"{prefix}{terminal_error}".strip()
        if retry_count:
            return "completed", f"재시도 {retry_count}회 후 완료했습니다.\n{response}"
        return "completed", response
