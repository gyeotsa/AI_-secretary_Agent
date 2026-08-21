"""Optional Adobe Photoshop COM integration for the image-editing specialist."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


class PhotoshopPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "photoshop"
        self.version = "1.0.0"
        self.description = "Adobe Photoshop 설치 탐지·문서 열기·활성 문서 조회"
        self.dependencies = ["win32com"]
        self.supported_os = ["Windows"]

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("photoshop_status", "Photoshop COM 연결 상태를 확인합니다", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, ["windows_api"], side_effect="read"),
            ToolSchema("photoshop_open_document", "Photoshop에서 이미지 또는 PSD 문서를 엽니다", {
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False,
            }, ["windows_api", "filesystem_read"], side_effect="execute"),
            ToolSchema("photoshop_active_document", "Photoshop 활성 문서의 이름과 크기를 조회합니다", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, ["windows_api"], side_effect="read"),
        ]

    def get_intents(self):
        return [
            IntentSchema(
                "photoshop.status", "Photoshop 연결 상태 조회", "photoshop_status",
                ["포토샵 상태", "photoshop 상태", "포토샵 연결"], [],
                execution_hints=["확인", "알려", "조회"], request_type="query",
            ),
            IntentSchema(
                "photoshop.open", "Photoshop 문서 열기", "photoshop_open_document",
                ["포토샵에서", "photoshop에서", "포토샵으로"],
                [SlotSchema("path", "열 이미지 경로", "Photoshop에서 열 파일을 알려주세요.", role="target")],
                execution_hints=["열", "실행"], request_type="execute",
            ),
        ]

    def extract_slots(self, intent_name, text, current_slots):
        slots = dict(current_slots)
        if intent_name == "photoshop.open" and not slots.get("path"):
            import re
            match = re.search(r"([A-Za-z]:[\\/][^\r\n]+?\.(?:psd|png|jpe?g|webp|bmp|tiff?))", text, re.IGNORECASE)
            if match:
                slots["path"] = match.group(1).strip(' "\'')
        return slots

    @staticmethod
    def _connect():
        import win32com.client
        return win32com.client.Dispatch("Photoshop.Application")

    @staticmethod
    def _safe_file(value: Any) -> Path:
        path = Path(str(value)).expanduser().resolve()
        ok, error = SafetyLayer.validate_path(str(path))
        if not ok:
            raise ValueError(error)
        if not path.is_file():
            raise ValueError(f"파일이 없습니다: {path}")
        return path

    def execute_tool(self, name: str, data: Dict[str, Any]):
        try:
            if name == "photoshop_status":
                app = self._connect()
                version = str(getattr(app, "Version", "알 수 없음"))
                return ToolRunResult.successful(
                    tool_name=name, raw_output=f"Photoshop {version} 연결 가능",
                    evidence=[Evidence("photoshop_com", "Photoshop COM Application 객체 연결을 확인했습니다.", {"version": version})],
                )
            if name == "photoshop_open_document":
                path = self._safe_file(data["path"])
                app = self._connect()
                document = app.Open(str(path))
                name_value = str(getattr(document, "Name", path.name))
                return ToolRunResult.successful(
                    tool_name=name, raw_output=f"Photoshop에서 {name_value} 파일을 열었습니다.",
                    evidence=[Evidence("photoshop_document", "Photoshop 활성 Document 객체 생성을 확인했습니다.", {"name": name_value})],
                    artifacts=[Artifact("image_document", str(path), {"application": "photoshop"})],
                )
            if name == "photoshop_active_document":
                document = self._connect().ActiveDocument
                details = {
                    "name": str(document.Name), "width": str(document.Width),
                    "height": str(document.Height), "mode": str(document.Mode),
                }
                return ToolRunResult.successful(
                    tool_name=name, raw_output=f"활성 문서: {details['name']} ({details['width']} × {details['height']})",
                    evidence=[Evidence("photoshop_active_document", "활성 Document 속성을 조회했습니다.", details)],
                )
            return ToolRunResult.failed(tool_name=name, error="지원하지 않는 Photoshop 도구입니다.")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=name, error=f"Photoshop 연결 실패: {exc}")
