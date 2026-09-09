"""One semantic boundary between user language and executable intent.

The interpreter never executes a tool or rewrites the source utterance.  A
model proposes a relation/action; registry contracts and verbatim user evidence
validate it.  Unresolved interpretation stays unresolved for the planner, not
an empty conversational tool loadout or an invented tool call.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import inspect
import json
import math
import re
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class SemanticDecision:
    raw_text: str
    relation: str = "unknown"
    operation: str = "unknown"
    intent_name: str = ""
    tool_names: tuple[str, ...] = ()
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    needs_clarification: bool = False
    clarification_question: str = ""
    control_scope: str = "current"
    grounded: bool = False
    source: str = "unresolved"
    reason: str = ""

    def to_resolution(self, registry):
        """Build a router-compatible value only after semantic validation."""
        from core.intent_router import IntentResolution, IntentRouter

        intent = next((i for _, i in registry.get_all_intents()
                       if i.name == self.intent_name), None)
        if not self.grounded or self.relation not in {"new", "continue", "correct"}:
            return IntentResolution(routing_reason=self.reason)
        clarification = self.clarification_question or (
            "작업의 대상이나 내용을 한 번 더 확인해 주세요." if self.needs_clarification else "")
        if intent is None:
            if not self.tool_names:
                return IntentResolution(routing_reason=self.reason)
            contract = registry.get_capability(self.tool_names[0])
            if contract is None:
                return IntentResolution(routing_reason="semantic_tool_unavailable")
            missing = [k for k in contract.input_schema.get("required", [])
                       if self.slots.get(k) in (None, "", [])]
            question = clarification or (
                f"{contract.description}: {', '.join(missing)} 정보를 알려주세요." if missing else "")
            return IntentResolution(
                matched=True, tool_name=contract.name, slots=dict(self.slots),
                explicit=True, execution_requested=True, confidence=self.confidence,
                request_type="query" if contract.side_effect == "read" else contract.side_effect,
                question=question, compound=len(self.tool_names) > 1,
                routing_reason="semantic_request:" + self.source,
            )
        missing = [s for s in intent.slots if s.required and
                   self.slots.get(s.name) in (None, "", [])]
        return IntentRouter._resolution(
            intent, dict(self.slots), self.confidence,
            explicit=True, execution_requested=True,
            question=clarification or (missing[0].question if missing else ""),
            routing_reason="semantic_request:" + self.source,
            compound=len(self.tool_names) > 1,
        )


def explicit_control(text: str) -> tuple[str, str] | None:
    """Only standalone control instructions are safe to bypass semantic analysis.

    Quoted messages, definitions and instructions to send a word such as
    '취소' deliberately do not match this full-utterance grammar.
    """
    value = str(text or "").strip().rstrip(".!。")
    if re.fullmatch(r"(?:승인|허용|approve)(?:\s*(?:해\s*줘|합니다))?", value, re.I):
        return "approve", "current"
    match = re.fullmatch(
        r"(?:(?:이전|기존|현재|지금|모든|전체)\s*)*"
        r"(?:(?:승인\s*)?대기\s*(?:중인|중)?\s*)?"
        r"(?:작업(?:들)?(?:을|은|는)?\s*)?"
        r"(?:(?:모두|전부|전체|다)\s*)?"
        r"(?:취소|중단|멈춰|그만|cancel|stop)"
        r"(?:\s*(?:해\s*줘|해\s*주세요|해주세요|해줘|해|해요|해라|합니다|시켜줘))?",
        value, re.I,
    )
    if not match:
        return None
    scope = "all" if re.search(r"모든|전체|모두|전부|(?:^|\s)다(?:\s|$)", value) else (
        "pending" if "대기" in value else "current")
    if scope == "current" and re.search(r"이전|기존", value):
        return None
    return "cancel", scope


def literal_reply(text: str) -> str | None:
    """Extract an explicitly delimited body without changing its contents."""
    value = str(text or "").strip()
    pairs = [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")]
    for left, right in pairs:
        if value.startswith(left):
            end = value.rfind(right)
            if end > 0 and re.fullmatch(
                r"\s*(?:(?:이?라고|으?로)\s*)?(?:(?:보내|전달|전송)(?:해)?\s*"
                r"(?:줘|주세요|줄래|해줘|해주세요))?[.!?\s]*", value[end + 1:]):
                return value[1:end]
    return None


def _quoted_message_body(text: str) -> str | None:
    """Find a delimited message even when a recipient precedes its opening quote."""
    from core.utterance_scope import _LITERAL

    for match in _LITERAL.finditer(text):
        value = match.group(0)
        if value.startswith(("```", "~~~", "`")):
            continue
        if re.fullmatch(
                r"\s*(?:(?:이?라고|으?로)\s*)?(?:보내|전달|전송)(?:해)?\s*"
                r"(?:줘|주세요|줄래|해줘|해주세요)[.!?\s]*", text[match.end():]):
            return value[1:-1]
    return None


class SemanticRequestInterpreter:
    RELATIONS = {"new", "continue", "correct", "cancel", "approve", "conversation", "unknown"}
    OPERATIONS = {"read", "change", "execute", "external_send", "control", "conversation", "unknown"}
    # These values are not generative prose: they must originate in user input
    # or already-established state. Their punctuation/spacing is significant.
    LITERAL_KEYS = {"recipient", "recipient_id", "message", "filename", "file_path", "path",
                    "url", "email", "body", "content", "channel", "channel_id", "account"}
    RUNTIME_ONLY_TOOLS = {"speak_text", "listen", "execute_multi_agent", "get_task_history"}
    SINGLE_PASS_CATALOG_CHARS = 10000
    DISCOVERY_MAX_TOOLS = 16
    CONTEXT_WINDOW = 8192

    def __init__(self, llm, registry):
        self.llm = llm
        self.registry = registry

    def interpret(self, raw_text: str, history: Sequence[Mapping[str, Any]] = (),
                  pending: Mapping[str, Any] | None = None,
                  allowed_tools: Iterable[str] | None = None) -> SemanticDecision:
        raw_text = str(raw_text or "")
        pending = dict(pending or {})
        control = explicit_control(raw_text)
        if control:
            return SemanticDecision(raw_text, relation=control[0], operation="control",
                                    control_scope=control[1], confidence=1.0,
                                    grounded=True, source="explicit_control")
        allowed = {c.name for c in self.registry.get_capabilities()
                   if c.name not in self.RUNTIME_ONLY_TOOLS}
        if allowed_tools is not None:
            allowed.intersection_update(allowed_tools)
        intents = [(p, i) for p, i in self.registry.get_all_intents()
                   if allowed is None or i.tool_name in allowed]
        selected = next((i for _, i in intents if i.name == pending.get("intent_name")), None)
        prior_slots = dict(pending.get("slots") or {})
        reply = literal_reply(raw_text)
        if selected and selected.name == "messaging.send" and reply is not None:
            if prior_slots.get("recipient") and not prior_slots.get("message"):
                slots = {**prior_slots, "message": reply}
                return SemanticDecision(raw_text, "continue", "external_send", selected.name,
                                        (selected.tool_name,), slots, 1.0,
                                        grounded=True, source="literal_reply")
        if self.llm is None:
            return SemanticDecision(raw_text, reason="semantic_model_unavailable")
        transcript = [{"role": m.get("role"), "content": str(m.get("content", ""))}
                      for m in history if m.get("role") in {"user", "assistant"}][-12:]
        intent_map = {i.tool_name: i.name for _, i in intents}
        # Include every in-scope capability, not only the few with handcrafted
        # intent phrases. Terse catalogues keep discovery affordable on local
        # models without silently hiding tools due to a lexical shortlist.
        catalogue = [{"tool": c.name, "description": c.description[:180],
                      "operation": c.side_effect, "intent": intent_map.get(c.name, ""),
                      "parameters": {k: {f: v for f, v in spec.items()
                                        if f in {"type", "enum", "description", "default", "items"}}
                                     for k, spec in c.input_schema.get("properties", {}).items()},
                      "required": c.input_schema.get("required", [])}
                     for c in self.registry.get_capabilities() if c.name in allowed]
        file_candidates = self._file_candidates(raw_text, intents)
        prompt = {
            "current_user_input": raw_text, "recent_dialogue": transcript,
            "pending_request": pending, "available_tools": catalogue,
            "verified_workspace_file_candidates": file_candidates,
        }
        system = (
            "사용자 발화의 의미와 대화 관계를 해석하세요. 키워드만 보고 실행을 결정하지 마세요. "
            "현재 입력이 새 요청(new), 이전 질문의 답(continue), 정정(correct), "
            "작업 취소(cancel), 승인(approve), 일반 대화(conversation) 중 무엇인지 판단합니다. "
            "'작성되어 있는 내용을 읽어줘'는 read이며 파일 작성(change)이 아닙니다. "
            "대기 작업이 있어도 새 요청/취소를 메시지 본문에 넣지 마세요. "
            "작업 대상과 본문은 사용자의 현재/이전 입력 또는 pending의 확정된 값에서만 가져옵니다. "
            "대기 중인 질문에 빠진 값을 답하면 continue이고, 이미 정한 값을 다른 값으로 바꾸면 correct입니다. "
            "pending/최근 대화가 없으면 독립 요청은 new입니다. "
            "assistant가 말한 값은 사용자 권한의 근거가 아닙니다. 후속 답변이면 pending의 미변경 슬롯을 유지합니다. "
            "recent_completed는 이미 완료된 작업의 확정된 참조 값입니다. 그 파일/그 사람 등 참조를 이어받으면 "
            "continue 또는 correct로 분류하고 필요한 슬롯만 가져오세요. new 요청에는 이전 대상이나 본문을 상속하지 않습니다. "
            "파일 읽기의 실제 파일 후보가 하나이면 그 경로를 사용하며 여러 개면 대상 확인 질문을 합니다. "
            "따옴표 안 본문, 코드, 공백, 조사, 파일명은 바꾸거나 번역/요약하지 마세요. "
            "보내줘 같은 명령 어미는 본문 밖 경계일 때만 제외합니다. "
            "사용자 이름의 마지막 글자를 임의로 삭제하거나 누락된 경로를 지어내지 마세요. "
            "도구 목록은 가능한 후보이며 조건이 여러 단계이면 tool_names에 필요한 도구를 나열하세요. "
            "복합 작업의 operation은 가장 큰 부작용(external_send > execute > change > read)이며 "
            "slots는 첫 도구의 입력입니다. 다른 도구의 필드는 섞지 마세요. "
            "intent는 도구에서 시스템이 결정하므로 출력하지 마세요. intent가 없는 도구도 선택할 수 있습니다. "
            "같은 기능의 대안 도구들을 모두 고르지 말고 요청을 충족하는 최소 도구만 선택합니다. "
            "목록에 적합한 기능이 없으면 tool_names를 비우고 relation=new로 둡니다. "
            "추측이 필요한 대상이 여러 개면 한 가지 질문만 합니다. "
            "아래 JSON 객체만 반환하세요: "
            '{"relation":"new|continue|correct|cancel|approve|conversation|unknown",'
            '"operation":"read|change|execute|external_send|control|conversation|unknown",'
            '"tool_names":[], "slots":{},'
            '"confidence":0.0,"needs_clarification":false,"clarification_question":"",'
            '"control_scope":"current|pending|all"}'
        )
        try:
            if len(json.dumps(catalogue, ensure_ascii=False, separators=(",", ":"))) > self.SINGLE_PASS_CATALOG_CHARS:
                discovered = self._discover_tools(prompt, catalogue, allowed)
                if discovered is None:
                    return SemanticDecision(raw_text, reason="semantic_discovery_invalid")
                allowed = set(discovered)
                prompt["available_tools"] = [entry for entry in catalogue if entry["tool"] in allowed]
            messages = [{"role": "system", "content": system},
                        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))}]
            schema = self._output_schema()
            if allowed:
                schema["properties"]["tool_names"]["items"]["enum"] = sorted(allowed)
            else:
                schema["properties"]["tool_names"]["maxItems"] = 0
            if not pending and not any(m["role"] == "user" for m in transcript):
                schema["properties"]["relation"]["enum"] = sorted(self.RELATIONS - {"continue", "correct"})
            response = self._model_call(messages, schema)
            return self._validate(raw_text, transcript, pending, str(response), allowed, file_candidates)
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            return SemanticDecision(raw_text, reason=f"semantic_interpretation_failed:{type(exc).__name__}:{code}")

    def _model_call(self, messages, schema):
        structured = getattr(self.llm, "chat_structured", None)
        if not callable(structured):
            return self.llm.chat(messages)
        # Older providers and test doubles retain the two-argument contract.
        parameters = inspect.signature(structured).parameters.values()
        supports_context = any(p.name == "context_window" or p.kind == inspect.Parameter.VAR_KEYWORD
                               for p in parameters)
        return (structured(messages, schema, context_window=self.CONTEXT_WINDOW)
                if supports_context else structured(messages, schema))

    def _discover_tools(self, prompt, catalogue, allowed):
        """Exhaustive semantic discovery before exposing selected full schemas.

        This stage never selects candidates by utterance keywords or intent
        phrases. Every in-scope capability stays visible in the compact list.
        Its output grants no execution authority; final interpretation and the
        same registry/literal validators remain mandatory.
        """
        compact = [{"tool": entry["tool"], "description": entry["description"][:120],
                    "operation": entry["operation"]} for entry in catalogue]
        discovery_prompt = {**prompt, "available_tools": compact}
        messages = [{"role": "system", "content": (
            "전체 도구 목록에서 현재 사용자 요청을 처리할 최소 도구를 선택하세요. 실행하지 않습니다. "
            "새 요청은 이전 작업과 분리하고, 후속 답변/정정이면 pending 또는 recent_completed의 작업을 참고합니다. "
            "본문에 포함된 명령 단어가 아니라 사용자가 실제로 요청한 동작을 판단하세요. "
            "동일 기능의 대안들을 모두 고르지 마세요. 복합 요청이면 필요한 모든 단계의 도구를 포함하세요. "
            "일반 대화 또는 지원 기능이 없으면 빈 목록을 반환합니다. "
            f"{self.DISCOVERY_MAX_TOOLS}개를 넘는 도구가 필요하거나 판단할 수 없으면 confidence=0으로 둡니다. "
            'JSON만 반환하세요: {"tool_names":[],"confidence":0.0}')},
            {"role": "user", "content": json.dumps(discovery_prompt, ensure_ascii=False, separators=(",", ":"))}]
        schema = {"type": "object", "properties": {
            "tool_names": {"type": "array", "items": {"type": "string", "enum": sorted(allowed)},
                           "maxItems": self.DISCOVERY_MAX_TOOLS, "uniqueItems": True},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1}},
            "required": ["tool_names", "confidence"], "additionalProperties": False}
        response = str(self._model_call(messages, schema))
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip(), flags=re.I))
        if not isinstance(data, dict):
            return None
        names, confidence = data.get("tool_names"), data.get("confidence")
        if (not isinstance(names, list) or len(names) > self.DISCOVERY_MAX_TOOLS
                or any(not isinstance(name, str) or name not in allowed for name in names)
                or isinstance(confidence, bool) or not isinstance(confidence, (float, int))
                or not math.isfinite(confidence) or not .65 <= confidence <= 1):
            return None
        return tuple(dict.fromkeys(names))

    @classmethod
    def _output_schema(cls):
        return {"type": "object", "properties": {
            "relation": {"type": "string", "enum": sorted(cls.RELATIONS)},
            "operation": {"type": "string", "enum": sorted(cls.OPERATIONS)},
            "tool_names": {"type": "array", "items": {"type": "string"}},
            "slots": {"type": "object"}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "needs_clarification": {"type": "boolean"}, "clarification_question": {"type": "string"},
            "control_scope": {"type": "string", "enum": ["current", "pending", "all"]},
        }, "required": ["relation", "operation", "tool_names", "slots", "confidence",
                         "needs_clarification", "clarification_question", "control_scope"], "additionalProperties": False}

    @staticmethod
    def _file_candidates(raw, intents):
        for plugin, intent in intents:
            resolver = getattr(plugin, "resolve_file_candidates", None)
            if callable(resolver) and intent.domain == "filesystem":
                return resolver(raw)
        return []

    @staticmethod
    def _read_only_request(raw):
        from core.utterance_scope import mask_quoted_payloads
        visible = mask_quoted_payloads(raw)
        # Remove descriptive participles, not performative mutation verbs.
        visible = re.sub(r"(?:작성|기록|저장)(?:되어|돼|된|한)|(?:적혀|쓰여)", "", visible)
        mutation = re.search(r"만들|생성|삭제|지워|수정|고쳐|바꿔|변경|써\s*줘|작성해|저장해|추가해", visible)
        observation = re.search(r"읽어|읽고|알려|보여|뭐(?:야|라고)|무슨|확인해|내용.*[?？]", visible)
        return bool(observation and not mutation)

    def _validate(self, raw: str, history, pending, response: str, allowed, file_candidates=()) -> SemanticDecision:
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip(), flags=re.I)
        data = json.loads(candidate)
        if not isinstance(data, dict):
            return SemanticDecision(raw, reason="semantic_schema_not_object")
        relation, operation = data.get("relation"), data.get("operation")
        confidence = data.get("confidence", 0)
        if (relation not in self.RELATIONS or operation not in self.OPERATIONS
                or isinstance(confidence, bool) or not isinstance(confidence, (float, int))
                or not math.isfinite(confidence) or confidence < .65 or confidence > 1):
            return SemanticDecision(raw, reason="semantic_schema_or_confidence_invalid")
        clarification = data.get("needs_clarification", False)
        question = data.get("clarification_question", "")
        if not isinstance(clarification, bool):
            return SemanticDecision(raw, reason="invalid_clarification_flag")
        if not isinstance(question, str):
            return SemanticDecision(raw, reason="invalid_clarification_question")
        if relation == "unknown":
            return SemanticDecision(raw, needs_clarification=True,
                                    clarification_question=question, reason="semantic_relation_unresolved")
        if relation in {"cancel", "approve"}:
            # Model-generated approval must not manufacture authority. Only the
            # explicit control boundary can approve an external side effect.
            if relation == "approve":
                return SemanticDecision(raw, reason="approval_requires_explicit_confirmation")
            # A semantic cancellation cannot silently expand to arbitrary old
            # tasks. Non-explicit scope requires one confirmation question.
            scope = data.get("control_scope", "current")
            if scope not in {"current", "pending", "all"}:
                return SemanticDecision(raw, reason="invalid_control_scope")
            return SemanticDecision(raw, relation="cancel", operation="control", confidence=confidence,
                                    needs_clarification=True,
                                    clarification_question="현재 작업을 취소할까요, 아니면 승인 대기 작업을 모두 취소할까요?",
                                    control_scope=scope, grounded=False, source="model",
                                    reason="cancellation_scope_requires_confirmation")
        name = data.get("intent_name") or ""
        names = data.get("tool_names") or []
        if not isinstance(names, list) or any(not isinstance(t, str) for t in names):
            return SemanticDecision(raw, reason="invalid_tool_names")
        all_intents = [i for _, i in self.registry.get_all_intents()]
        if not name and names:
            # A tool is the executable contract; an intent is only its routing
            # label. Do not ask a small model to independently predict both.
            matching = [i for i in all_intents if i.tool_name == names[0]]
            previous = pending if pending.get("intent_name") else (pending.get("recent_completed") or {})
            related_name = previous.get("intent_name") if relation in {"continue", "correct"} else None
            match = next((i for i in matching if i.name == related_name), None)
            if match is not None or len(matching) == 1:
                name = (match or matching[0]).name
        intent = next((i for i in all_intents if i.name == name), None)
        if intent and names and intent.tool_name not in names:
            return SemanticDecision(raw, reason="intent_tool_mismatch")
        if intent and not names:
            names.append(intent.tool_name)
        if name and not intent:
            return SemanticDecision(raw, reason="unknown_intent")
        if any(not self.registry.get_capability(t) or (allowed is not None and t not in allowed)
               for t in names):
            return SemanticDecision(raw, reason="unknown_or_out_of_scope_tool")
        slots = data.get("slots") or {}
        if not isinstance(slots, dict):
            return SemanticDecision(raw, reason="invalid_slots")
        if relation == "conversation":
            if names or name or slots or operation != "conversation":
                return SemanticDecision(raw, reason="conversation_cannot_execute")
            return SemanticDecision(raw, relation=relation, operation=operation, confidence=confidence,
                                    needs_clarification=clarification, clarification_question=question,
                                    grounded=True, source="model")
        if operation in {"control", "conversation", "unknown"} and names:
            return SemanticDecision(raw, reason="invalid_action_operation")
        related = {}
        if relation in {"continue", "correct"}:
            related = pending if pending.get("intent_name") or pending.get("task_id") else (
                pending.get("recent_completed") or {})
            if not related and not any(m["role"] == "user" for m in history):
                return SemanticDecision(raw, reason="continuation_without_user_context")
            if name and name == related.get("intent_name"):
                slots = {**dict(related.get("slots") or {}), **slots}
        if names:
            contract = self.registry.get_capability(intent.tool_name if intent else names[0])
            properties = contract.input_schema.get("properties", {}) if contract else {}
            if any(key not in properties for key in slots):
                return SemanticDecision(raw, reason="unknown_slot")
            # Validate provided fields even if a required clarification is still
            # pending. Complete schema validation stays at the tool boundary.
            from jsonschema import Draft202012Validator
            partial = dict(contract.input_schema)
            partial.pop("required", None)
            if list(Draft202012Validator(partial).iter_errors(slots)):
                return SemanticDecision(raw, reason="slot_type_invalid")
            effects = {self.registry.get_capability(t).side_effect for t in names}
            highest_effect = max(effects, key={"read": 0, "change": 1, "execute": 2, "external_send": 3}.get)
            if operation != highest_effect:
                return SemanticDecision(raw, reason="operation_tool_mismatch")
            if self._read_only_request(raw) and any(
                    self.registry.get_capability(t).side_effect != "read" for t in names):
                return SemanticDecision(raw, reason="read_request_cannot_mutate")
        user_sources = [raw]
        if relation in {"continue", "correct"}:
            user_sources.extend(m["content"] for m in history if m["role"] == "user")
            if related.get("original_request"):
                user_sources.append(str(related["original_request"]))
        established = related.get("slots") or {}
        for key, value in slots.items():
            if key in self.LITERAL_KEYS and isinstance(value, str):
                if key == "filename" and file_candidates and operation == "read":
                    if len(file_candidates) > 1:
                        return SemanticDecision(raw, relation=relation, operation=operation,
                                                confidence=confidence, needs_clarification=True,
                                                clarification_question="같은 이름의 파일이 여러 개 있습니다. 어떤 경로를 읽을까요? "
                                                + ", ".join(file_candidates[:5]), grounded=True, source="model")
                    if value == file_candidates[0]:
                        continue
                    return SemanticDecision(raw, reason="filename_not_verified_candidate")
                if value != established.get(key) and not any(value in s for s in user_sources):
                    return SemanticDecision(raw, reason=f"ungrounded_literal:{key}")
        if isinstance(slots.get("message"), str):
            expected = literal_reply(raw) or _quoted_message_body(raw)
            # A pending unquoted quotative reply also has an exact syntactic
            # boundary: '본문이라고 보내줘'. Preserve everything before it.
            if expected is None and related.get("intent_name") == "messaging.send":
                quoted_reply = re.fullmatch(r"(.+?)\s*(?:이라고|라고)\s*(?:보내|전달|전송)(?:해)?\s*(?:줘|주세요|줄래)[.!?\s]*", raw, re.S)
                if quoted_reply and not re.search(r"에게|한테|께", quoted_reply.group(1)):
                    expected = quoted_reply.group(1).strip()
            if expected is not None and slots["message"] != expected:
                return SemanticDecision(raw, reason="message_literal_changed")
        if relation == "continue" and name == related.get("intent_name") and any(
                key in established and established[key] not in (None, "", []) and value != established[key]
                for key, value in slots.items()):
            # A changed confirmed value is a correction by definition. This
            # preserves the model's grounded values, never invents a new action.
            relation = "correct"
        return SemanticDecision(raw, relation, operation, name, tuple(dict.fromkeys(names)),
                                dict(slots), confidence, clarification, question,
                                grounded=True, source="model")
