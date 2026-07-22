"""Plugin Registry의 선언형 intent/slot 계약을 실행하는 범용 라우터."""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from core.plugin import BasePlugin, IntentSchema, PluginRegistry


@dataclass
class IntentResolution:
    matched: bool = False
    intent_name: str = ""
    tool_name: str = ""
    slots: Dict[str, Any] = field(default_factory=dict)
    question: str = ""
    capability_response: str = ""

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
            return IntentResolution(True, intent.name, capability_response=intent.capability_response)

        slots = plugin.extract_slots(intent.name, text, current_slots or {})
        missing = [slot for slot in intent.slots if slot.required and not slots.get(slot.name)]
        question = missing[0].question if missing else ""
        return IntentResolution(True, intent.name, intent.tool_name, slots, question)
