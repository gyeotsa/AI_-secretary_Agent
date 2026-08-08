"""Responsibility-focused services behind the compatibility Executor facade."""

from __future__ import annotations

from typing import List, Optional
import re

from core.plan_runtime import PlanDAG, PlanStep


class ConversationService:
    """Generates ordinary dialogue without exposing the Tool runtime."""

    def __init__(self, llm):
        self.llm = llm

    def respond(self, message: str, history, *, assistant_name: str = "",
                voice_name: str = "", address: str = "보스", style: str = "",
                memory_context: str = "") -> str:
        if assistant_name and message.strip().casefold() == assistant_name.casefold():
            return f"응, 듣고 있어. {address}."
        recent = [
            {"role": item.get("role", "user"), "content": str(item.get("content", ""))}
            for item in list(history)[-6:]
            if item.get("role") in {"user", "assistant"} and item.get("content")
        ]
        persona = f"선택 음성: {voice_name}. 대화 스타일: {style}" if style else ""
        memory_prompt = (
            "\n다음은 현재 질문과 관련해 저장된 사용자 장기 기억입니다. 관련 있을 때만 반영하고, "
            "사용자가 지금 정정하면 현재 발화를 우선하세요.\n" + memory_context
            if memory_context else ""
        )
        prompt = (
            f"당신은 로컬 개인 비서 '{assistant_name or '자비스'}'입니다. 지금 요청은 도구 실행이 아닌 일반 대화입니다. "
            "도구를 찾거나 실행했다고 주장하지 마세요. URL이나 영상을 실제로 열지 않았다면 봤거나 학습했다고 말하지 마세요. "
            "사용자가 웃기·인사하기처럼 직접 수행할 수 있는 표현을 요청하면 명령을 되돌리지 말고 짧게 직접 반응하세요. "
            "최근 발화의 맥락과 감정을 먼저 반영하고 "
            "자연스럽고 간결한 한국어로 답하세요. 최신 정보가 필요하면 확인이 필요하다고 말하세요. "
            "사용자의 이메일·전화번호·주소·이름 같은 개인 식별정보는 제공된 대화나 저장된 "
            "프로필에 실제 값이 없으면 절대 만들어내지 말고 모른다고 답하세요. "
            "프롬프트 예시, user/assistant 역할표시, 다른 언어 설명을 답변에 노출하지 마세요. "
            f"사용자 호칭은 '{address}'이며 답변에서 최대 한 번만 사용하세요. {persona}{memory_prompt}"
        )
        messages = [{"role": "system", "content": prompt}, *recent,
                    {"role": "user", "content": message}]
        response = str(self.llm.chat(messages) or "").strip()
        needs_repair = (
            _normalized(response) == _normalized(message)
            or _has_prompt_leak(response)
            or ("반말" in style and re.search(r"(?:습니다|세요|해요|까요|입니다)", response))
            or (re.search(r"(?:해|어|아|여|워)\s*봐[.!?]*$", message.strip()) and "세요" in response)
        )
        if response and needs_repair:
            response = str(self.llm.chat([
                {"role": "system", "content": (
                    f"아래 초안을 사용자의 질문에 대한 자연스러운 한국어 답변으로 한 번만 고쳐 써. "
                    f"역할표시·예시·외국어를 넣지 말고, 사용자 호칭은 '{address}'로 최대 한 번만 써. "
                    f"적용할 스타일: {style or '간결하고 자연스러운 말투'}"
                )},
                {"role": "user", "content": f"질문: {message}\n초안: {response}"},
            ]) or "").strip()
        response = _sanitize_response(response, message)
        response = _apply_requested_style(response, style)
        return response or f"응, 듣고 있어. 무슨 이야기부터 해볼까, {address}?"


def _normalized(text: str) -> str:
    return re.sub(r"\W+", "", str(text or "")).casefold()


def _has_prompt_leak(text: str) -> bool:
    return bool(
        re.search(r"(?im)^\s*(?:user|assistant|system)\s*:?", str(text or ""))
        or re.search(r"[\u0400-\u04ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]", str(text or ""))
    )


def _sanitize_response(text: str, user_message: str = "") -> str:
    """Remove role/prompt leakage without rewriting legitimate Korean content."""
    value = str(text or "").strip()
    if not re.search(r"(?:러시아어|키릴|중국어|일본어|한자|번역|원문)", user_message, re.IGNORECASE):
        value = re.split(
            r"[\u0400-\u04ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]",
            value, maxsplit=1,
        )[0].rstrip(" :\n")
    kept = []
    for line in value.splitlines():
        if re.match(r"^\s*(?:user|assistant|system)\s*:?(?:\s|$)", line, re.IGNORECASE):
            break
        kept.append(line)
    value = "\n".join(kept).strip()
    value = re.sub(r"\s*\[?(?:GAME|WORK)\]?\s*$", "", value, flags=re.IGNORECASE)
    return value.strip()


def _apply_requested_style(text: str, style: str) -> str:
    """Enforce common Korean sentence endings after a small model ignores style."""
    if "반말" not in str(style or ""):
        return text
    value = str(text or "")
    replacements = (
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

    def create(self, goal: str, context: str, allowed_tools: Optional[List[str]]) -> PlanDAG:
        if hasattr(self.planner, "build_plan_dag"):
            return self.planner.build_plan_dag(goal, context, allowed_tools)
        tasks = self.planner.decompose_goal(goal, context, allowed_tools)
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
