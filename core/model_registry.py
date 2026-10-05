"""Single source of truth for local model roles and runtime policies."""
from dataclasses import dataclass
from typing import Dict
import re

from config import Config


@dataclass(frozen=True)
class ModelProfile:
    role: str
    model: str
    temperature: float
    max_tokens: int
    keep_alive: str
    modalities: tuple[str, ...] = ("text",)
    vram_mb: int = 0
    ram_mb: int = 1024


def _estimate_model_resources(model: str) -> tuple[int, int]:
    """Return conservative local inference budgets from the model size label.

    Ollama model names conventionally contain ``4b``/``7b``.  The estimate is
    deliberately generic and can be replaced by live Ollama size telemetry in
    the Command Center; it is not tied to a particular product or workspace.
    """
    match = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)b(?!\w)", str(model), re.I)
    billions = float(match.group(1)) if match else 2.0
    vram_mb = max(1024, int(billions * 650 + 768))
    return vram_mb, max(1536, int(vram_mb * 1.2))


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
        "research": "reasoning",
        "knowledge": "reasoning",
        "style": "style_vision",
        "critic": "visual_critic",
    }

    def __init__(self):
        conversation = Config.OLLAMA_CONVERSATION_MODEL
        reasoning = Config.OLLAMA_REASONING_MODEL
        code = Config.OLLAMA_CODE_MODEL
        vision = Config.OLLAMA_VISION_MODEL
        design_vision = Config.OLLAMA_DESIGN_VISION_MODEL
        def profile(role: str, model: str, temperature: float, max_tokens: int,
                    keep_alive: str, modalities: tuple[str, ...] = ("text",)) -> ModelProfile:
            vram_mb, ram_mb = _estimate_model_resources(model)
            return ModelProfile(role, model, temperature, max_tokens, keep_alive,
                                modalities, vram_mb, ram_mb)
        self._profiles: Dict[str, ModelProfile] = {
            "conversation": profile("conversation", conversation, 0.65, 1024, "10m"),
            "planning": profile("planning", reasoning, 0.15, 2048, "5m"),
            "reasoning": profile("reasoning", reasoning, 0.2, 2048, "5m"),
            "tool_selection": profile("tool_selection", reasoning, 0.0, 1024, "5m"),
            "code": profile("code", code, 0.15, 4096, "5m"),
            "document": profile("document", code, 0.3, 4096, "5m"),
            "vision": profile("vision", vision, 0.2, 1024, "2m", ("text", "image")),
            "computer_use": profile("computer_use", Config.OLLAMA_COMPUTER_USE_MODEL,
                                    0.0, 1024, "2m", ("text", "image")),
            "image_editing": profile("image_editing", vision, 0.25, 2048, "3m", ("text", "image")),
            "mockup_design": profile("mockup_design", vision, 0.2, 3072, "5m", ("text", "image")),
            "style_vision": profile("style_vision", design_vision, 0.1, 3072, "0", ("text", "image")),
            "visual_critic": profile("visual_critic", design_vision, 0.0, 2048, "0", ("text", "image")),
            "design_planning": profile("design_planning", reasoning, 0.12, 4096, "0"),
            "subject_analysis": profile("subject_analysis", design_vision, 0.0, 1024, "0", ("text", "image")),
            "rendering": ModelProfile("rendering", reasoning, 0.0, 512, "0", ("text",), 0, 512),
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
