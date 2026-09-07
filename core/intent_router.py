"""Plugin Registry의 선언형 intent/slot 계약을 실행하는 범용 라우터."""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.plugin import BasePlugin, IntentSchema, PluginRegistry
from core.utterance_scope import analyze_utterance_scope, mask_quoted_payloads


@dataclass
class IntentResolution:
    matched: bool = False
    intent_name: str = ""
    tool_name: str = ""
    slots: Dict[str, Any] = field(default_factory=dict)
    question: str = ""
    capability_response: str = ""
    explicit: bool = False
    execution_requested: bool = False
    confidence: float = 0.0
    domain: str = ""
    action: str = ""
    target: Any = None
    constraints: Dict[str, Any] = field(default_factory=dict)
    reference: Dict[str, Any] = field(default_factory=dict)
    request_type: str = "conversation"
    routing_reason: str = ""
    alternatives: List[Dict[str, Any]] = field(default_factory=list)
    ambiguous: bool = False
    freshness: str = "static"
    requires_sources: bool = False
    negated: bool = False
    compound: bool = False

    @property
    def ready(self) -> bool:
        return (
            self.matched and bool(self.tool_name) and not self.question
            and not self.capability_response and not self.negated and not self.compound
        )


class IntentRouter:
    CAPABILITY_HINTS = ("가능", "지원", "할 수 있", "아니었어")
    TEMPORAL_PATTERN = re.compile(
        r"(?:현재|지금|오늘|어제|내일|최근|최신|실시간|방금|이번\s*(?:주|달|분기|해|년도)|"
        r"(?:20)?\d{2}년|\d{1,2}월\s*\d{1,2}일)", re.IGNORECASE,
    )
    INFORMATION_PATTERN = re.compile(
        r"(?:무엇|뭐|누구|어디|언제|어떻게|어때|알려|확인|찾아|검색|조회|"
        r"소식|뉴스|결과|현황|상태|가격|시세|환율|얼마|증시|순위|일정|\?)", re.IGNORECASE,
    )
    CONVERSATION_PATTERN = re.compile(
        r"(?:안녕|반가워|고마워|감사해|잘\s*지내|기분|너는|넌|네\s*생각|"
        r"뭐\s*하고\s*싶|심심|힘들어|속상|행복|재미있)", re.IGNORECASE,
    )
    EXPLICIT_RESEARCH_PATTERN = re.compile(
        r"(?:검색|찾아\s*봐|찾아\s*줘|조사|뉴스|소식|출처|웹에서|인터넷에서|"
        r"확인해\s*줘|조회해\s*줘)", re.IGNORECASE,
    )
    # A generic "check it" request still belongs to a dedicated weather,
    # finance, status, or calendar capability when one exists.  Only an
    # explicit request to browse/research should keep generic web.search ahead
    # of such a capability.
    EXPLICIT_WEB_RESEARCH_PATTERN = re.compile(
        r"(?:검색|찾아\s*봐|찾아\s*줘|조사|뉴스|소식|출처|웹에서|인터넷에서)",
        re.IGNORECASE,
    )
    CONTENT_REQUEST_PATTERN = re.compile(
        r"(?:제목|본문|문구|내용)(?:은|는|을|를|\s*[:：=])", re.IGNORECASE,
    )
    CONTENT_SLOT_NAMES = frozenset({
        "title", "paragraphs", "content", "body", "text", "message",
        "slides", "rows", "bullets", "sections",
    })
    NEGATED_ACTION_PATTERN = re.compile(
        r"(?:지|하지)\s*(?:는\s*)?(?:마|말아|마세요|말아\s*줘|말아\s*주세요)"
        r"[.!?\s]*$",
        re.IGNORECASE,
    )
    COMPOUND_CONNECTOR_PATTERN = re.compile(
        r"(?:그리고|그다음|그\s*다음|동시에|한\s*뒤|한\s*다음|"
        r"고\s*(?:나서|난\s*뒤)|"
        r",\s*(?:그리고|그다음))",
        re.IGNORECASE,
    )
    NEGATED_AFFIRMATIVE_ENDINGS = (
        (re.compile(r"하지\s*(?:는\s*)?(?:마|말아|마세요|말아\s*줘|말아\s*주세요)[.!?\s]*$", re.I), "해줘"),
        (re.compile(r"보내지\s*(?:는\s*)?(?:마|말아|마세요|말아\s*줘|말아\s*주세요)[.!?\s]*$", re.I), "보내줘"),
        (re.compile(r"만들지\s*(?:는\s*)?(?:마|말아|마세요|말아\s*줘|말아\s*주세요)[.!?\s]*$", re.I), "만들어줘"),
        (re.compile(r"열지\s*(?:는\s*)?(?:마|말아|마세요|말아\s*줘|말아\s*주세요)[.!?\s]*$", re.I), "열어줘"),
        (re.compile(r"켜지\s*(?:는\s*)?(?:마|말아|마세요|말아\s*줘|말아\s*주세요)[.!?\s]*$", re.I), "켜줘"),
        (re.compile(r"끄지\s*(?:는\s*)?(?:마|말아|마세요|말아\s*줘|말아\s*주세요)[.!?\s]*$", re.I), "꺼줘"),
        (re.compile(r"틀지\s*(?:는\s*)?(?:마|말아|마세요|말아\s*줘|말아\s*주세요)[.!?\s]*$", re.I), "틀어줘"),
        (re.compile(r"지\s*(?:는\s*)?(?:마|말아|마세요|말아\s*줘|말아\s*주세요)[.!?\s]*$", re.I), "줘"),
    )

    def __init__(self, registry: PluginRegistry):
        self.registry = registry

    @staticmethod
    def _normalize_routing_text(text: str) -> str:
        """Normalize harmless STT/typing variants without rewriting user slots."""
        normalized = " ".join(str(text or "").casefold().split())
        replacements = {
            "유투브": "유튜브", "유 튜브": "유튜브", "카톡 으로": "카톡으로",
            "열어 줘": "열어줘", "틀어 줘": "틀어줘", "보내 줘": "보내줘",
            "만들어 줘": "만들어줘", "찾아 줘": "찾아줘", "알려 줘": "알려줘",
        }
        for source, target in replacements.items():
            normalized = normalized.replace(source, target)
        return normalized

    @classmethod
    def _affirmative_routing_probe(cls, text: str) -> str:
        """Restore only the final verb ending for intent lookup, never execution."""
        for pattern, replacement in cls.NEGATED_AFFIRMATIVE_ENDINGS:
            if pattern.search(text):
                return pattern.sub(replacement, text)
        return text

    @staticmethod
    def _terms(text: str) -> set[str]:
        normalized = re.sub(r"[^0-9a-zA-Z가-힣]+", " ", text.casefold()).strip()
        words = {word for word in normalized.split() if len(word) > 1}
        compact = normalized.replace(" ", "")
        words.update(compact[index:index + 2] for index in range(max(0, len(compact) - 1)))
        return words

    @staticmethod
    def _quoted_literals(text: str) -> list[str]:
        values: list[str] = []
        for pattern in (
            r'"([^"\r\n]+)"', r"'([^'\r\n]+)'",
            r"“([^”\r\n]+)”", r"‘([^’\r\n]+)’",
        ):
            values.extend(
                match.strip() for match in re.findall(pattern, str(text or ""))
                if match.strip()
            )
        return list(dict.fromkeys(values))

    @staticmethod
    def _scalar_slot_values(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [
                scalar
                for nested in value.values()
                for scalar in IntentRouter._scalar_slot_values(nested)
            ]
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                scalar
                for nested in value
                for scalar in IntentRouter._scalar_slot_values(nested)
            ]
        if isinstance(value, (str, int, float, bool)):
            text = str(value).strip()
            return [text] if text else []
        return []

    def resolution_preserves_user_content(
        self, text: str, resolution: IntentResolution,
    ) -> bool:
        """Return False when a fast-path tool call dropped explicit user data.

        A deterministic intent may bypass the Planner, so it must enforce the
        same grounding invariant: quoted literals and supported content fields
        cannot disappear between the utterance and Registry tool input.
        """
        slot_values = self._scalar_slot_values(resolution.slots)
        joined = "\n".join(slot_values)
        if any(literal not in joined for literal in self._quoted_literals(text)):
            return False
        if not self.CONTENT_REQUEST_PATTERN.search(str(text or "")):
            return True
        capability = self.registry.get_capability(resolution.tool_name)
        properties = set(
            ((capability.input_schema if capability else {}).get("properties") or {}).keys()
        )
        supported = properties & self.CONTENT_SLOT_NAMES
        if not supported:
            return True
        return any(resolution.slots.get(name) not in (None, "", []) for name in supported)

    def _rank(self, text: str) -> List[tuple[float, BasePlugin, IntentSchema, str]]:
        normalized = self._normalize_routing_text(mask_quoted_payloads(text))
        query_terms = self._terms(normalized)
        candidates = []
        for plugin, intent in self.registry.get_all_intents():
            hint_hits = [hint for hint in intent.utterance_hints if hint.casefold() in normalized]
            execution_hits = [hint for hint in intent.execution_hints if hint.casefold() in normalized]
            pattern_hits = [
                match.group(0) for pattern in intent.utterance_patterns
                if (match := re.search(pattern, normalized, re.IGNORECASE))
            ]
            descriptor = " ".join([
                intent.name, intent.domain, intent.action, intent.description,
                *intent.utterance_hints, *intent.execution_hints,
            ])
            descriptor_terms = self._terms(descriptor)
            overlap = len(query_terms & descriptor_terms) / max(1, len(query_terms))
            # Descriptor similarity ranks already-declared candidates; it must not
            # manufacture a domain match from generic words such as "파일" alone.
            # An action verb alone ("만들어", "열어") is never domain
            # evidence.  Plugins must declare a domain hint or an utterance
            # pattern for inflected commands; otherwise a generic file request
            # can silently inherit an unrelated calendar/Office intent.
            if not hint_hits and not pattern_hits:
                continue
            score = (
                sum(12 + len(hit) for hit in hint_hits)
                + sum(5 + len(hit) for hit in execution_hits)
                + sum(100 + len(hit) for hit in pattern_hits)
                + overlap * 30
            )
            if score:
                reason = (
                    f"hint={hint_hits or '-'}, action={execution_hits or '-'}, "
                    f"pattern={bool(pattern_hits)}, descriptor_overlap={overlap:.2f}"
                )
                candidates.append((score, plugin, intent, reason))
        return sorted(candidates, key=lambda item: item[0], reverse=True)

    def _find(self, text: str) -> Optional[tuple[BasePlugin, IntentSchema, float]]:
        candidates = self._rank(text)
        if not candidates:
            return None
        if candidates[0][2].name == "web.search" and not self.EXPLICIT_WEB_RESEARCH_PATTERN.search(text):
            specialized = next((item for item in candidates[1:]
                                if item[2].request_type == "query" and item[2].name != "web.search"), None)
            if specialized:
                candidates.remove(specialized)
                candidates.insert(0, specialized)
        score, plugin, intent, _reason = candidates[0]
        runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
        margin = max(0.0, score - runner_up) / max(1.0, score)
        confidence = min(0.99, 0.45 + min(score, 120) / 300 + margin * 0.25)
        return plugin, intent, confidence

    def _fresh_information_intent(self, text: str):
        if not (self.TEMPORAL_PATTERN.search(text) and self.INFORMATION_PATTERN.search(text)):
            return None
        # Temporal words also occur in social conversation ("오늘 기분 어때?").
        # A social utterance must stay conversational unless the user explicitly asks
        # for research; otherwise every greeting containing "오늘" becomes a web search.
        if (self.CONVERSATION_PATTERN.search(text)
                and not self.EXPLICIT_RESEARCH_PATTERN.search(text)):
            return None
        return next(
            (
                (plugin, intent, 0.82)
                for plugin, intent in self.registry.get_all_intents()
                if intent.request_type == "query"
                and intent.freshness == "live"
                and intent.requires_sources
            ),
            None,
        )

    def resolve(self, text: str, intent_name: str = "",
                current_slots: Optional[Dict[str, Any]] = None) -> IntentResolution:
        scope = analyze_utterance_scope(text)
        if scope.discussion:
            return IntentResolution(request_type="conversation", routing_reason=scope.reason)
        text = scope.action_text
        visible_text = scope.routing_text
        negated = scope.negated or bool(self.NEGATED_ACTION_PATTERN.search(visible_text))
        routing_text = self._affirmative_routing_probe(text) if negated else text
        selected = None
        clause_matches: list[tuple[BasePlugin, IntentSchema, float]] = []
        if not intent_name and self.COMPOUND_CONNECTOR_PATTERN.search(visible_text):
            clauses = [part.strip(" ,.!?") for part in self.COMPOUND_CONNECTOR_PATTERN.split(visible_text)
                       if part.strip(" ,.!?")]
            for clause in clauses:
                found = self._find(clause) or self._fresh_information_intent(clause)
                if found:
                    clause_matches.append(found)
        if intent_name:
            selected = next(
                ((plugin, intent, 0.9) for plugin, intent in self.registry.get_all_intents()
                 if intent.name == intent_name), None,
            )
        elif clause_matches:
            selected = clause_matches[0]
        else:
            # Some intents treat negation as the requested persistent
            # constraint (e.g. "내 말 따라하지마").  Preserve an
            # explicitly declared negative utterance before probing an
            # affirmative form solely to identify prohibited actions.
            selected = self._find(text)
            if not selected and negated:
                selected = self._find(routing_text)
            if not selected:
                selected = self._fresh_information_intent(text)
        if not selected:
            return IntentResolution(
                request_type="prohibition" if negated else "conversation",
                negated=negated, routing_reason=scope.reason,
            )

        plugin, intent, confidence = selected
        ranked = self._rank(text) if not intent_name else []
        if not ranked and negated and not intent_name:
            ranked = self._rank(routing_text)
        if ranked:
            routing_reason = ranked[0][3]
        elif not intent_name and intent.freshness == "live":
            routing_reason = "시간 민감 정보 질문과 live/source-required Capability 계약 일치"
        else:
            routing_reason = "기존 intent 문맥과 Slot을 이어받음"
        alternatives = [
            {"intent": candidate.name, "tool": candidate.tool_name,
             "tool_name": candidate.tool_name,
             "score": round(score, 3)}
            for score, _plugin, candidate, _reason in ranked[1:4]
        ]
        for _plugin, candidate, score in clause_matches[1:]:
            if candidate.name != intent.name and not any(
                    item.get("intent") == candidate.name for item in alternatives):
                alternatives.insert(0, {
                    "intent": candidate.name, "tool": candidate.tool_name,
                    "tool_name": candidate.tool_name, "score": round(score, 3),
                })
        ambiguous = bool(
            not intent_name and len(ranked) > 1 and ranked[0][0] < 100
            and ranked[1][0] / max(1.0, ranked[0][0]) >= 0.88
            and ranked[0][2].name != ranked[1][2].name
        )
        ambiguity_question = ""
        if ambiguous:
            ambiguity_question = (
                f"'{ranked[0][2].description}'과 '{ranked[1][2].description}' 중 "
                "어떤 작업을 원하시는지 말씀해 주세요."
            )
        normalized = self._normalize_routing_text(visible_text)
        has_action_hint = any(hint.casefold() in normalized for hint in intent.execution_hints)
        is_execution = has_action_hint or intent.request_type == "query"
        is_capability = any(hint in normalized for hint in self.CAPABILITY_HINTS)
        if negated and not intent.negation_is_constraint:
            return self._resolution(
                intent, {}, confidence, explicit=not bool(intent_name),
                execution_requested=False, request_type="prohibition",
                routing_reason=f"{routing_reason}; explicit_action_negation",
                alternatives=alternatives, negated=True,
            )
        distinct_ranked_intents = {
            candidate.name for _score, _plugin, candidate, _reason in ranked
        }
        compound = bool(
            not intent_name and (len(clause_matches) >= 2 or len(distinct_ranked_intents) >= 2)
            and self.COMPOUND_CONNECTOR_PATTERN.search(visible_text)
        )
        if not intent_name and is_capability and not has_action_hint:
            return self._resolution(
                intent, {}, confidence, capability_response=intent.capability_response,
                explicit=not bool(intent_name), execution_requested=False,
                request_type="capability", routing_reason=routing_reason,
                alternatives=alternatives,
            )

        slots = plugin.extract_slots(intent.name, text, current_slots or {})
        missing = [slot for slot in intent.slots if slot.required and not slots.get(slot.name)]
        question = ambiguity_question or (missing[0].question if missing else "")
        if scope.conditional and intent.request_type in {"change", "execute", "external_send"}:
            # A future/external condition is not proof that it is true now.
            # Keep the real request, but never flatten it to an immediate tool
            # call while the Registry contract has no condition observer.
            question = (
                f"‘{scope.condition}’ 조건이 충족됐다는 근거가 없어 바로 실행하지 않았습니다. "
                "조건을 확인한 뒤 실행할 작업을 다시 명확히 요청해 주시겠어요?"
            )
        return self._resolution(
            intent, slots, confidence, question=question,
            explicit=not bool(intent_name), execution_requested=is_execution,
            request_type=intent.request_type, routing_reason=routing_reason,
            alternatives=alternatives, ambiguous=ambiguous, compound=compound,
        )

    @staticmethod
    def _resolution(intent: IntentSchema, slots: Dict[str, Any], confidence: float,
                    **values) -> IntentResolution:
        constraints = {name: slots[name] for name in intent.constraint_slots if name in slots}
        reference = {name: slots[name] for name in intent.reference_slots if name in slots}
        return IntentResolution(
            matched=True,
            intent_name=intent.name,
            tool_name=intent.tool_name,
            slots=slots,
            confidence=confidence,
            domain=intent.domain,
            action=intent.action,
            target=slots.get(intent.target_slot) if intent.target_slot else None,
            constraints=constraints,
            reference=reference,
            request_type=values.pop("request_type", intent.request_type),
            freshness=intent.freshness,
            requires_sources=intent.requires_sources,
            **values,
        )

    def resolve_from_history(self, text: str, history: List[Dict[str, str]]) -> IntentResolution:
        """Recover a follow-up's domain and slots from recent dialogue contracts."""
        selected_index = -1
        selected = None
        for index in range(len(history) - 1, -1, -1):
            if history[index].get("role") != "user":
                continue
            prior_text = str(history[index].get("content", ""))
            prior_scope = analyze_utterance_scope(prior_text)
            if prior_scope.discussion or prior_scope.negated or prior_scope.conditional:
                # "다시" after a discussion/prohibition must not resurrect an
                # older command, nor use an assistant's suggestion as authority.
                return IntentResolution()
            candidate = self._find(prior_scope.action_text)
            if candidate:
                selected_index, selected = index, candidate
                break
        if not selected:
            return IntentResolution()
        plugin, intent, _confidence = selected
        slots: Dict[str, Any] = {}
        for message in history[selected_index:]:
            if message.get("role") == "user":
                slots = plugin.extract_slots(intent.name, str(message.get("content", "")), slots)
        return self.resolve(text, intent.name, slots)

    def is_contextual_follow_up(self, text: str, intent_name: str = "") -> bool:
        """Check follow-up phrases declared by registered intent contracts."""
        normalized = text.casefold()
        contracts = [
            intent for _plugin, intent in self.registry.get_all_intents()
            if not intent_name or intent.name == intent_name
        ]
        return any(
            hint.casefold() in normalized
            for intent in contracts for hint in intent.follow_up_hints
        )
