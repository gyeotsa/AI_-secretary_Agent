"""Declarative catalog and natural-language routing for specialist workspaces."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class SpecialistWorkspaceSpec:
    key: str
    title: str
    description: str
    model_role: str
    tools: tuple[str, ...]
    utterance_hints: tuple[str, ...]


class SpecialistWorkspaceRegistry:
    """Single source of truth shared by buttons, voice routing and workspace UI."""

    def __init__(self):
        self._items = {
            "document": SpecialistWorkspaceSpec(
                "document", "문서 전문가", "Word·Excel·한글·PowerPoint·PDF 작성과 검토",
                "document", ("word", "excel", "hwpx", "powerpoint", "pdf"),
                ("문서 작업", "문서 모드", "오피스 작업", "오피스 모드", "문서 전문가"),
            ),
            "photoshop": SpecialistWorkspaceSpec(
                "photoshop", "Photoshop 전문가", "이미지 분석·편집 지시·Photoshop 연동",
                "image_editing", ("photoshop", "vision"),
                ("포토샵", "photoshop", "이미지 편집", "사진 편집", "디자인 작업"),
            ),
            "mockup": SpecialistWorkspaceSpec(
                "mockup", "시안 제작 전문가", "학습용 시안 분석과 제작용 사진 기반 이미지 생성",
                "mockup_design", ("mockup_design", "vision", "image_renderer"),
                ("시안 작업", "시안 제작", "시안 전문가", "목업 작업", "목업 제작", "mockup"),
            ),
        }

    def all(self) -> tuple[SpecialistWorkspaceSpec, ...]:
        return tuple(self._items.values())

    def get(self, key: str) -> SpecialistWorkspaceSpec | None:
        return self._items.get(str(key).casefold())

    def match_open_command(self, text: str) -> SpecialistWorkspaceSpec | None:
        value = " ".join(str(text or "").casefold().split())
        open_action = re.search(r"(?:모드|작업\s*공간|워크스페이스|전문가).{0,12}(?:실행|열|시작|켜)|(?:실행|열|시작|켜).{0,12}(?:모드|작업\s*공간|워크스페이스|전문가)", value)
        if not open_action:
            return None
        matches = [spec for spec in self.all() if any(hint.casefold() in value for hint in spec.utterance_hints)]
        return matches[0] if len(matches) == 1 else None


_registry = None


def get_specialist_workspace_registry() -> SpecialistWorkspaceRegistry:
    global _registry
    if _registry is None:
        _registry = SpecialistWorkspaceRegistry()
    return _registry
