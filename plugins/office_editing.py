"""Template-preserving Office editing and render QA tools."""
from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.office_runtime import AtomicOfficeEditor, OfficeComAdapter, OfficeRenderer
from core.plugin import BasePlugin, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


class OfficeEditingPlugin(BasePlugin):
    def __init__(self):
        super().__init__(); self.name = "office_editing"; self.version = "1.0.0"
        self.description = "Office 템플릿 보존 편집·PDF 렌더링 QA·COM 라이브 제어"
        self.dependencies = ["win32com"] ; self.supported_os = ["Windows"]

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("office_replace_template_text", "기존 서식·테마·미디어를 보존하며 텍스트를 치환합니다", {
                "type":"object","properties":{"path":{"type":"string"},"replacements":{"type":"object","additionalProperties":{"type":"string"}}},
                "required":["path","replacements"],"additionalProperties":False}, ["filesystem_write"], side_effect="change"),
            ToolSchema("office_render_visual_qa", "Office 문서를 PDF와 페이지 PNG로 렌더링해 빈 페이지 여부를 검증합니다", {
                "type":"object","properties":{"path":{"type":"string"},"output_pdf":{"type":"string"}},
                "required":["path","output_pdf"],"additionalProperties":False}, ["filesystem_read","filesystem_write"], side_effect="change", timeout_seconds=180),
            ToolSchema("office_com_status", "Office·HWP COM 설치와 연결 가능 상태를 확인합니다", {
                "type":"object","properties":{},"additionalProperties":False}, ["windows_api"], side_effect="read"),
            ToolSchema("office_com_open", "설치된 Office/HWP 앱에서 문서를 실제로 엽니다", {
                "type":"object","properties":{"app":{"enum":["word","excel","powerpoint","hwp"]},"path":{"type":"string"}},
                "required":["app","path"],"additionalProperties":False}, ["windows_api","filesystem_read"], side_effect="execute"),
        ]

    @staticmethod
    def _safe(path):
        target=Path(path).expanduser().resolve(); ok,error=SafetyLayer.validate_path(str(target))
        if not ok: raise ValueError(error)
        return target

    def execute_tool(self, name: str, data: Dict[str, Any]):
        try:
            if name == "office_replace_template_text":
                path=self._safe(data["path"]); details=AtomicOfficeEditor.replace_text(path, dict(data["replacements"]))
                if details["replacement_count"] < 1: return ToolRunResult.failed(tool_name=name,error="치환할 원문을 찾지 못했습니다.")
                return ToolRunResult.successful(tool_name=name,raw_output="Office 템플릿 텍스트를 수정했습니다.",
                    evidence=[Evidence("office_package_preservation","서식·테마·미디어 part와 결과 해시를 검증했습니다.",details)],
                    artifacts=[Artifact("office_document",str(path),details)])
            if name == "office_render_visual_qa":
                source=self._safe(data["path"]); output=self._safe(data["output_pdf"]); details=OfficeRenderer.render_pdf(source,output)
                return ToolRunResult.successful(tool_name=name,raw_output="Office 렌더링 시각 QA를 통과했습니다.",
                    evidence=[Evidence("rendered_office","PDF 페이지 수와 비어 있지 않은 Preview를 확인했습니다.",details)],
                    artifacts=[Artifact("pdf",details["pdf"]),*[Artifact("image",item) for item in details["previews"]]])
            if name == "office_com_status":
                status={app:OfficeComAdapter.available(app) for app in OfficeComAdapter.PROG_IDS}
                return ToolRunResult.successful(tool_name=name,raw_output=str(status),
                    evidence=[Evidence("office_com_registry","각 앱 COM ProgID 연결 가능 여부를 확인했습니다.",status)])
            if name == "office_com_open":
                path=self._safe(data["path"]); details=OfficeComAdapter.open_document(data["app"],str(path))
                return ToolRunResult.successful(tool_name=name,raw_output=f"{data['app']}에서 문서를 열었습니다.",
                    evidence=[Evidence("office_com_document","COM 문서 객체 생성을 확인했습니다.",details)],
                    artifacts=[Artifact("office_document",str(path),{"app":data["app"]})])
            return ToolRunResult.failed(tool_name=name,error="지원하지 않는 Office 도구입니다.")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=name,error=str(exc))
