"""Task-scoped tool exposure derived from Plugin Registry metadata."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from core.intent_router import IntentRouter, IntentResolution
from core.plugin import PluginRegistry


@dataclass(frozen=True)
class ToolLoadout:
    tool_names: tuple[str, ...]
    reason: str
    confidence: float


class ToolLoadoutSelector:
    """Keep local-model context small without maintaining a second tool catalogue."""

    def __init__(self, registry: PluginRegistry, max_tools: int = 6):
        self.registry = registry
        self.router = IntentRouter(registry)
        self.max_tools = max(1, max_tools)

    @staticmethod
    def _tokens(text: str) -> set[str]:
        normalized = re.sub(r"[^0-9a-zA-Z가-힣]+", " ", str(text).casefold())
        return {token for token in normalized.split() if len(token) > 1}

    def select(self, request: str, resolution: IntentResolution | None = None,
               required_tools: Iterable[str] = ()) -> ToolLoadout:
        resolution = resolution or self.router.resolve(request)
        selected = [name for name in required_tools if self.registry.get_capability(name)]
        if resolution.matched and resolution.tool_name:
            selected.insert(0, resolution.tool_name)
            return ToolLoadout(tuple(dict.fromkeys(selected))[:self.max_tools],
                               f"intent:{resolution.intent_name}", resolution.confidence)

        query = self._tokens(request)
        scored = []
        for contract in self.registry.get_capabilities():
            descriptor = self._tokens(f"{contract.name} {contract.description}")
            overlap = len(query & descriptor)
            if overlap:
                scored.append((overlap / max(1, len(query)), contract.name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected.extend(name for _, name in scored[:self.max_tools])
        unique = tuple(dict.fromkeys(selected))[:self.max_tools]
        if not unique:
            return ToolLoadout((), "conversation_or_unresolved", 0.0)
        return ToolLoadout(unique, "registry_descriptor_match", scored[0][0])
