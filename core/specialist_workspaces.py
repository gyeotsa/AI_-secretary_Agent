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
    required_tools: tuple[str, ...]
    utterance_hints: tuple[str, ...]
    acceptance_criteria: tuple[str, ...] = ()
    artifact_types: tuple[str, ...] = ()
    readiness_checks: tuple[str, ...] = (
        "required_tools_registered", "executor_available", "verified_evidence",
    )
    acceptance_verifiers: tuple[str, ...] = ()

    @property
    def tools(self) -> tuple[str, ...]:
        """Backward-compatible alias used by existing workspace buttons."""
        return self.required_tools

    def execution_contract(self) -> dict:
        """Return the immutable workspace contract passed to every team stage."""
        return {
            "workspace_key": self.key,
            "required_tools": list(self.required_tools),
            "acceptance_criteria": list(self.acceptance_criteria),
            "artifact_types": list(self.artifact_types),
            "readiness_checks": list(self.readiness_checks),
            "acceptance_verifiers": list(self.acceptance_verifiers),
        }


class SpecialistWorkspaceRegistry:
    """Single source of truth shared by buttons, voice routing and workspace UI."""

    def __init__(self):
        self._items = {
            "document": SpecialistWorkspaceSpec(
                "document", "문서 전문가", "Word·Excel·한글·PowerPoint·PDF 작성과 검토",
                "document", ("word", "excel", "hwpx", "powerpoint", "pdf"),
                ("문서 작업", "문서 모드", "오피스 작업", "오피스 모드", "문서 전문가"),
                ("요청한 형식과 내용을 충족한다", "저장된 문서를 다시 읽거나 렌더링해 검증한다"),
                ("document", "spreadsheet", "presentation", "pdf", "file"),
                acceptance_verifiers=("document_content", "document_reopened"),
            ),
            "coding": SpecialistWorkspaceSpec(
                "coding", "개발 전문가", "프로젝트 분석·설계·구현·테스트·코드 리뷰",
                "code", ("git", "filesystem", "coding", "system_tools"),
                ("개발 작업", "코딩 모드", "개발 전문가", "코딩 전문가", "코드 작업"),
                ("실제 파일 변경이 존재한다", "관련 테스트 또는 정적 검증 근거가 존재한다"),
                ("file", "directory", "project"),
                acceptance_verifiers=("coding_changes", "coding_validation"),
            ),
            "research": SpecialistWorkspaceSpec(
                "research", "리서치 전문가", "실시간 웹 조사·출처 비교·근거 기반 보고서 작성",
                "reasoning", ("browser", "rag_knowledge", "knowledge_memory"),
                ("리서치", "조사 모드", "검색 전문가", "자료 조사", "웹 조사"),
                ("출처가 식별 가능하다", "주요 결론이 수집 근거와 연결된다"),
                ("url", "document", "file", "report"),
                acceptance_verifiers=("research_sources", "research_claims"),
            ),
            "photoshop": SpecialistWorkspaceSpec(
                "photoshop", "Photoshop 전문가", "원본 보존 Photoshop 편집·별도 저장·결과 재검증",
                "image_editing", ("photoshop", "photoshop_edit_document", "vision"),
                ("포토샵", "photoshop", "이미지 편집", "사진 편집", "디자인 작업"),
                ("편집 복사본을 별도 저장하고 다시 열어 검증한다", "요청한 타입 작업의 실제 시각 변경을 확인한다", "원본 파일과 열린 원본 문서를 보존한다"),
                ("image", "file", "image_document", "photoshop_document"),
                acceptance_verifiers=("photoshop_saved_copy", "photoshop_visual_change", "photoshop_source_preserved"),
            ),
            "mockup": SpecialistWorkspaceSpec(
                "mockup", "시안 제작 전문가", "학습용 시안 분석과 제작용 사진 기반 이미지 생성",
                "mockup_design", ("mockup_design", "vision", "image_renderer"),
                ("시안 작업", "시안 제작", "시안 전문가", "목업 작업", "목업 제작", "mockup"),
                ("최종 이미지가 렌더링된다", "사용자 지시와 스타일 제약의 검수 근거가 존재한다"),
                ("image", "file"),
            ),
            "knowledge_graph": SpecialistWorkspaceSpec(
                "knowledge_graph", "Knowledge Graph", "Obsidian 기억의 연결·중요도·RAG 상태 탐색",
                "knowledge", ("obsidian", "rag_knowledge", "knowledge_memory"),
                ("지식 그래프", "기억 그래프", "knowledge graph", "그래프 뷰", "옵시디언 그래프"),
                ("변경된 기억 또는 그래프 항목을 재조회한다", "출처와 연결 관계를 보존한다"),
                ("knowledge_entity", "knowledge_relation", "semantic_memory", "document", "file"),
            ),
        }

    def all(self) -> tuple[SpecialistWorkspaceSpec, ...]:
        return tuple(self._items.values())

    def get(self, key: str) -> SpecialistWorkspaceSpec | None:
        return self._items.get(str(key).casefold())

    def match_open_command(self, text: str) -> SpecialistWorkspaceSpec | None:
        value = " ".join(str(text or "").casefold().split())
        open_action = re.search(
            r"(?:모드|작업\s*공간|워크스페이스|전문가|그래프(?:\s*뷰)?).{0,12}(?:실행|열|시작|켜)"
            r"|(?:실행|열|시작|켜).{0,12}(?:모드|작업\s*공간|워크스페이스|전문가|그래프(?:\s*뷰)?)",
            value,
        )
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
