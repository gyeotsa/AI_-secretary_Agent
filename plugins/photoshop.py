"""Optional Adobe Photoshop COM integration for the image-editing specialist."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.photoshop_runtime import EDIT_INPUT_SCHEMA, PhotoshopRuntime, PhotoshopUnavailable, PhotoshopVerificationError
from core.tool_result import Artifact, Evidence, ToolRunResult


class PhotoshopPlugin(BasePlugin):
    def __init__(self, runtime: PhotoshopRuntime | None = None):
        super().__init__()
        self.name = "photoshop"
        self.version = "2.0.0"
        self.description = "Adobe Photoshop 조회·타입 편집·원본 보존 복사 저장·결과 재열기 검증"
        self.dependencies = ["win32com"]
        self.supported_os = ["Windows"]
        self.runtime = runtime or PhotoshopRuntime()

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("photoshop_status", "앱을 실행하지 않고 Photoshop COM 설치 등록 여부를 확인합니다. 실제 편집 검증과는 다릅니다", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, ["windows_api"], side_effect="read"),
            ToolSchema("photoshop_open_document", "Photoshop에서 이미지 또는 PSD 문서를 엽니다", {
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False,
            }, ["windows_api", "filesystem_read"], side_effect="execute"),
            ToolSchema("photoshop_active_document", "Photoshop 활성 문서의 이름과 크기를 조회합니다", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, ["windows_api"], side_effect="read"),
            ToolSchema("photoshop_list_fonts", "Photoshop에서 사용 가능한 실제 글꼴 이름과 PostScriptName을 조회합니다", {
                "type": "object", "properties": {"query": {"type": "string", "maxLength": 200},
                                                     "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                "additionalProperties": False,
            }, ["windows_api"], side_effect="read"),
            ToolSchema("photoshop_edit_document", (
                "Photoshop에서 사진/PSD를 복제해 순서대로 크기 변경(resize), 영역 자르기(crop), "
                "90/180/270도 회전(rotate), 글꼴·색상·위치가 명시된 문구(text)를 편집합니다. "
                "원본과 기존 출력은 덮어쓰지 않고 새 PNG/PSD로 저장한 뒤 다시 열어 실제 픽셀을 검증합니다. "
                "입력 경로·새 출력 경로·작업 값이 불명확하면 먼저 질문하세요. 임의 코드/JS/액션 실행은 지원하지 않습니다."
            ), EDIT_INPUT_SCHEMA, ["windows_api", "filesystem_read", "filesystem_write"],
                side_effect="change", max_retries=0, cancellable=False, timeout_seconds=120),
        ]

    def get_intents(self):
        return [
            IntentSchema(
                "photoshop.status", "Photoshop 연결 상태 조회", "photoshop_status",
                ["포토샵 상태", "photoshop 상태", "포토샵 연결"], [],
                execution_hints=["확인", "알려", "조회"],
                utterance_patterns=[r"(?:포토샵|photoshop).{0,20}(?:상태|연결).{0,12}(?:확인|알려|조회)"],
                request_type="query",
            ),
            IntentSchema(
                "photoshop.open", "Photoshop 문서 열기", "photoshop_open_document",
                ["포토샵 파일 열기", "포토샵에서 파일 열", "photoshop open"],
                [SlotSchema("path", "열 이미지 경로", "Photoshop에서 열 파일을 알려주세요.", role="target")],
                execution_hints=["열", "실행"], request_type="execute",
                utterance_patterns=[r"(?:포토샵|photoshop).{0,180}(?:파일|문서|이미지|사진|\.psd|\.png|\.jpe?g).{0,20}열"],
            ),
            IntentSchema(
                "photoshop.fonts", "Photoshop 글꼴 목록 조회", "photoshop_list_fonts",
                ["포토샵 글꼴 목록", "포토샵 폰트 목록", "photoshop fonts"], [],
                execution_hints=["조회", "알려", "보여"], request_type="query",
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
                import os
                if os.name != "nt":
                    raise PhotoshopUnavailable("Photoshop COM은 Windows에서만 사용할 수 있습니다.")
                try:
                    import winreg
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"Photoshop.Application\CLSID"):
                        installed = True
                except OSError:
                    installed = False
                return ToolRunResult.successful(
                    tool_name=name, raw_output=("Photoshop COM 등록을 확인했습니다. 실제 편집 연결 검증은 아직 실행하지 않았습니다."
                                                if installed else "Photoshop COM이 등록되어 있지 않아 현재 편집할 수 없습니다."),
                    evidence=[Evidence("photoshop_installation", "앱을 실행하지 않고 COM 등록 여부를 확인했습니다.",
                                       {"installed": installed, "edit_verified": False})],
                )
            if name == "photoshop_open_document":
                path = self._safe_file(data["path"])
                with self.runtime.session() as (app, _factory):
                    document = app.Open(str(path))
                    name_value = str(getattr(document, "Name", path.name))
                return ToolRunResult.successful(
                    tool_name=name, raw_output=f"Photoshop에서 {name_value} 파일을 열었습니다.",
                    evidence=[Evidence("photoshop_document", "Photoshop 활성 Document 객체 생성을 확인했습니다.", {"name": name_value})],
                    artifacts=[Artifact("image_document", str(path), {"application": "photoshop", "role": "input"})],
                )
            if name == "photoshop_active_document":
                with self.runtime.session() as (app, _factory):
                    document = app.ActiveDocument
                    details = {
                        "name": str(document.Name), "width": str(document.Width),
                        "height": str(document.Height), "mode": str(document.Mode),
                    }
                return ToolRunResult.successful(
                    tool_name=name, raw_output=f"활성 문서: {details['name']} ({details['width']} × {details['height']})",
                    evidence=[Evidence("photoshop_active_document", "활성 Document 속성을 조회했습니다.", details)],
                )
            if name == "photoshop_list_fonts":
                import json
                fonts = self.runtime.fonts(query=str(data.get("query", "")), limit=data.get("limit", 100))
                return ToolRunResult.successful(
                    tool_name=name, raw_output=json.dumps({"fonts": fonts}, ensure_ascii=False),
                    evidence=[Evidence("photoshop_fonts", "Photoshop의 실제 설치 글꼴 컬렉션을 조회했습니다.", {"fonts": fonts})],
                )
            if name == "photoshop_edit_document":
                for key in ("source_path", "output_path"):
                    path = Path(str(data.get(key, ""))).expanduser().resolve()
                    ok, error = SafetyLayer.validate_path(str(path))
                    if not ok:
                        raise ValueError(error)
                details = self.runtime.edit_copy(data)
                output = Path(details["output_path"])
                return ToolRunResult.successful(
                    tool_name=name,
                    raw_output=f"Photoshop에서 {len(details['operations'])}개 편집을 적용하고 복사본 저장·재열기·픽셀 변경을 검증했습니다: {output}",
                    evidence=[Evidence("photoshop_edit_verification", "타입 작업별 실제 픽셀, 저장 결과 재열기, 원본 해시 보존을 검증했습니다.", details)],
                    artifacts=[Artifact("photoshop_document" if output.suffix.casefold() == ".psd" else "image",
                                        str(output), {"application": "photoshop", "role": "output",
                                                      "verification_id": details["verification_id"],
                                                      "source_path": details["source_path"]})],
                )
            return ToolRunResult.failed(tool_name=name, error="지원하지 않는 Photoshop 도구입니다.")
        except PhotoshopVerificationError as exc:
            return ToolRunResult.unverified(tool_name=name, raw_output=f"Photoshop 편집 결과를 검증하지 못해 완료로 처리하지 않았습니다: {exc}",
                                            evidence=[Evidence("photoshop_verification_failed", str(exc))])
        except PhotoshopUnavailable as exc:
            return ToolRunResult.failed(tool_name=name, error=str(exc),
                                       evidence=[Evidence("photoshop_unavailable", str(exc))])
        except Exception as exc:
            return ToolRunResult.failed(tool_name=name, error=f"Photoshop 작업 실패: {exc}")
