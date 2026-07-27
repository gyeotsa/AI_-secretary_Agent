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

    @property
    def ready(self) -> bool:
        return self.matched and bool(self.tool_name) and not self.question and not self.capability_response


class IntentRouter:
    CAPABILITY_HINTS = ("가능", "지원", "할 수 있", "아니었어")

    def __init__(self, registry: PluginRegistry):
        self.registry = registry

    def _find(self, text: str) -> Optional[tuple[BasePlugin, IntentSchema]]:
        normalized = text.casefold()
        candidates = []
        for plugin, intent in self.registry.get_all_intents():
            score = sum(len(hint) for hint in intent.utterance_hints if hint.casefold() in normalized)
            score += sum(
                100 + len(match.group(0))
                for pattern in intent.utterance_patterns
                if (match := re.search(pattern, text, re.IGNORECASE))
            )
            if score:
                candidates.append((score, plugin, intent))
        if not candidates:
            return None
        _, plugin, intent = max(candidates, key=lambda item: item[0])
        return plugin, intent

    def resolve(self, text: str, intent_name: str = "",
                current_slots: Optional[Dict[str, Any]] = None) -> IntentResolution:
        selected = None
        if intent_name:
            selected = next(
                ((plugin, intent) for plugin, intent in self.registry.get_all_intents()
                 if intent.name == intent_name), None,
            )
        else:
            selected = self._find(text)
        if not selected:
            return IntentResolution()

        plugin, intent = selected
        normalized = text.casefold()
        is_execution = any(hint.casefold() in normalized for hint in intent.execution_hints)
        is_capability = any(hint in normalized for hint in self.CAPABILITY_HINTS)
        if not intent_name and is_capability and not is_execution:
            return IntentResolution(
                True, intent.name, capability_response=intent.capability_response,
                explicit=not bool(intent_name), execution_requested=False,
            )

        slots = plugin.extract_slots(intent.name, text, current_slots or {})
        missing = [slot for slot in intent.slots if slot.required and not slots.get(slot.name)]
        question = missing[0].question if missing else ""
        return IntentResolution(
            True, intent.name, intent.tool_name, slots, question,
            explicit=not bool(intent_name), execution_requested=is_execution,
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
        plugin, intent = selected
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
