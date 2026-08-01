"""Plugin Registry의 선언형 intent/slot 계약을 실행하는 범용 라우터."""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.plugin import BasePlugin, IntentSchema, PluginRegistry


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

    @property
    def ready(self) -> bool:
        return self.matched and bool(self.tool_name) and not self.question and not self.capability_response


class IntentRouter:
    CAPABILITY_HINTS = ("가능", "지원", "할 수 있", "아니었어")

    def __init__(self, registry: PluginRegistry):
        self.registry = registry

    @staticmethod
    def _terms(text: str) -> set[str]:
        normalized = re.sub(r"[^0-9a-zA-Z가-힣]+", " ", text.casefold()).strip()
        words = {word for word in normalized.split() if len(word) > 1}
        compact = normalized.replace(" ", "")
        words.update(compact[index:index + 2] for index in range(max(0, len(compact) - 1)))
        return words

    def _rank(self, text: str) -> List[tuple[float, BasePlugin, IntentSchema, str]]:
        normalized = text.casefold()
        query_terms = self._terms(text)
        candidates = []
        for plugin, intent in self.registry.get_all_intents():
            hint_hits = [hint for hint in intent.utterance_hints if hint.casefold() in normalized]
            execution_hits = [hint for hint in intent.execution_hints if hint.casefold() in normalized]
            pattern_hits = [
                match.group(0) for pattern in intent.utterance_patterns
                if (match := re.search(pattern, text, re.IGNORECASE))
            ]
            descriptor = " ".join([
                intent.name, intent.domain, intent.action, intent.description,
                *intent.utterance_hints, *intent.execution_hints,
            ])
            descriptor_terms = self._terms(descriptor)
            overlap = len(query_terms & descriptor_terms) / max(1, len(query_terms))
            # Descriptor similarity ranks already-declared candidates; it must not
            # manufacture a domain match from generic words such as "파일" alone.
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
        score, plugin, intent, _reason = candidates[0]
        runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
        margin = max(0.0, score - runner_up) / max(1.0, score)
        confidence = min(0.99, 0.45 + min(score, 120) / 300 + margin * 0.25)
        return plugin, intent, confidence

    def resolve(self, text: str, intent_name: str = "",
                current_slots: Optional[Dict[str, Any]] = None) -> IntentResolution:
        selected = None
        if intent_name:
            selected = next(
                ((plugin, intent, 0.9) for plugin, intent in self.registry.get_all_intents()
                 if intent.name == intent_name), None,
            )
        else:
            selected = self._find(text)
        if not selected:
            return IntentResolution()

        plugin, intent, confidence = selected
        ranked = self._rank(text) if not intent_name else []
        routing_reason = ranked[0][3] if ranked else "기존 intent 문맥과 Slot을 이어받음"
        alternatives = [
            {"intent": candidate.name, "score": round(score, 3)}
            for score, _plugin, candidate, _reason in ranked[1:4]
        ]
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
        normalized = text.casefold()
        is_execution = any(hint.casefold() in normalized for hint in intent.execution_hints)
        is_capability = any(hint in normalized for hint in self.CAPABILITY_HINTS)
        if not intent_name and is_capability and not is_execution:
            return self._resolution(
                intent, {}, confidence, capability_response=intent.capability_response,
                explicit=not bool(intent_name), execution_requested=False,
                request_type="capability", routing_reason=routing_reason,
                alternatives=alternatives,
            )

        slots = plugin.extract_slots(intent.name, text, current_slots or {})
        missing = [slot for slot in intent.slots if slot.required and not slots.get(slot.name)]
        question = ambiguity_question or (missing[0].question if missing else "")
        return self._resolution(
            intent, slots, confidence, question=question,
            explicit=not bool(intent_name), execution_requested=is_execution,
            request_type=intent.request_type, routing_reason=routing_reason,
            alternatives=alternatives, ambiguous=ambiguous,
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
            **values,
        )

    def resolve_from_history(self, text: str, history: List[Dict[str, str]]) -> IntentResolution:
        """Recover a follow-up's domain and slots from recent dialogue contracts."""
        selected_index = -1
        selected = None
        for index in range(len(history) - 1, -1, -1):
            candidate = self._find(str(history[index].get("content", "")))
            if candidate:
                selected_index, selected = index, candidate
                break
        if not selected:
            return IntentResolution()
        plugin, intent, _confidence = selected
        slots: Dict[str, Any] = {}
        for message in history[selected_index:]:
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
