"""Single source of truth for local model roles and runtime policies."""
from dataclasses import dataclass
from typing import Dict

from config import Config


@dataclass(frozen=True)
class ModelProfile:
    role: str
    model: str
    temperature: float
    max_tokens: int
    keep_alive: str
    modalities: tuple[str, ...] = ("text",)


class ModelRegistry:
    """Resolve a task role to one locally hosted model profile."""

    _ALIASES = {
        "default": "conversation",
        "chat": "conversation",
        "planner": "planning",
        "reasoner": "reasoning",
        "tools": "tool_selection",
        "coding": "code",
        "documents": "document",
        "multimodal": "vision",
        "photoshop": "image_editing",
        "mockup": "mockup_design",
    }

    def __init__(self):
        conversation = Config.OLLAMA_CONVERSATION_MODEL
        reasoning = Config.OLLAMA_REASONING_MODEL
        code = Config.OLLAMA_CODE_MODEL
        vision = Config.OLLAMA_VISION_MODEL
        self._profiles: Dict[str, ModelProfile] = {
            "conversation": ModelProfile("conversation", conversation, 0.65, 1024, "10m"),
            "planning": ModelProfile("planning", reasoning, 0.15, 2048, "5m"),
            "reasoning": ModelProfile("reasoning", reasoning, 0.2, 2048, "5m"),
            "tool_selection": ModelProfile("tool_selection", reasoning, 0.0, 1024, "5m"),
            "code": ModelProfile("code", code, 0.15, 4096, "5m"),
            "document": ModelProfile("document", code, 0.3, 4096, "5m"),
            "vision": ModelProfile("vision", vision, 0.2, 1024, "2m", ("text", "image")),
            "image_editing": ModelProfile("image_editing", vision, 0.25, 2048, "3m", ("text", "image")),
            "mockup_design": ModelProfile("mockup_design", vision, 0.2, 3072, "5m", ("text", "image")),
        }

    def resolve(self, role: str = "default") -> ModelProfile:
        normalized = str(role or "default").strip().casefold()
        normalized = self._ALIASES.get(normalized, normalized)
        return self._profiles.get(normalized, self._profiles["conversation"])

    def profiles(self) -> Dict[str, ModelProfile]:
        return dict(self._profiles)


class ModelRoleRouter:
    """Choose a specialist from structured capabilities, not user keywords."""

    _CODE_PREFIXES = ("git_", "run_command", "execute_python", "code_")
    _DOCUMENT_PREFIXES = (
        "excel_", "word_", "powerpoint_", "pdf_", "hwp_", "hwpx_"
    )

    def route(
        self,
        *,
        allowed_tools: tuple[str, ...] | list[str] = (),
        modalities: tuple[str, ...] | list[str] = ("text",),
        conversational: bool = False,
    ) -> str:
        if any(item in {"image", "video"} for item in modalities):
            normalized_tools = tuple(str(name).casefold() for name in allowed_tools)
            if any(name.startswith("mockup_") for name in normalized_tools):
                return "mockup_design"
            return "image_editing" if any(name.startswith("photoshop_") for name in normalized_tools) else "vision"
        if conversational:
            return "conversation"
        normalized_tools = tuple(str(name).casefold() for name in allowed_tools)
        if any(name.startswith(self._CODE_PREFIXES) for name in normalized_tools):
            return "code"
        if any(name.startswith(self._DOCUMENT_PREFIXES) for name in normalized_tools):
            return "document"
        if normalized_tools:
            return "tool_selection"
        return "reasoning"


_registry = None
_role_router = None


def get_model_registry() -> ModelRegistry:
    global _registry
    if _registry is None:
        _registry = ModelRegistry()
    return _registry


def get_model_role_router() -> ModelRoleRouter:
    global _role_router
    if _role_router is None:
        _role_router = ModelRoleRouter()
    return _role_router
