"""Pure-Python HWPX tools; Hancom Office is not required."""
from pathlib import Path
from typing import Any, Dict, List
from hwpx import HwpxDocument
from core.harness import SafetyLayer
from core.plugin import BasePlugin, ToolSchema, IntentSchema, SlotSchema
from core.tool_result import Artifact, Evidence, ToolRunResult
from plugins._document_slots import extract_document_slots


class HwpxPlugin(BasePlugin):
    def __init__(self): super().__init__(); self.name="hwpx"; self.description="한컴 설치 없이 HWPX 생성·읽기"
    def get_tools(self)->List[ToolSchema]:
        return [
            ToolSchema("hwpx_create_document","HWPX 문서를 생성합니다",{"type":"object","properties":{"path":{"type":"string"},"title":{"type":"string"},"paragraphs":{"type":"array","items":{"type":"string"}}},"required":["path"]},["filesystem_write"]),
            ToolSchema("hwpx_read_document","HWPX 텍스트를 읽습니다",{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]},["filesystem_read"]),
        ]
    def get_intents(self):
        return [IntentSchema("hwpx.create","아래아한글 HWPX 문서 생성","hwpx_create_document",["한글 문서","hwpx","HWPX"],[SlotSchema("path","저장 경로","HWPX 문서를 어디에 저장할까요, 보스?")],follow_up_hints=["다시"])]
    def extract_slots(self,intent_name,text,current_slots):
        return extract_document_slots(text,current_slots,".hwpx","새 한글 문서") if intent_name=="hwpx.create" else dict(current_slots)
    @staticmethod
    def _path(value:Any)->Path:
        path=Path(str(value)).expanduser().resolve(); ok,error=SafetyLayer.validate_path(str(path))
        if not ok: raise ValueError(error)
        if path.suffix.casefold() != ".hwpx": raise ValueError(".hwpx 파일만 지원합니다")
        return path
    def execute_tool(self,name:str,data:Dict[str,Any]):
        try:
            path=self._path(data["path"])
            if name=="hwpx_create_document":
                doc=HwpxDocument.new()
                if data.get("title"): doc.add_paragraph(str(data["title"]))
                for text in data.get("paragraphs") or []: doc.add_paragraph(str(text))
                path.parent.mkdir(parents=True,exist_ok=True); doc.save_to_path(path)
                saved=HwpxDocument.open(path); exported=saved.export_text()
                return ToolRunResult.successful(
                    tool_name=name,
                    raw_output=f"HWPX 생성 성공: {path}",
                    evidence=[Evidence(
                        "hwpx_structure", "저장된 HWPX를 다시 열어 문서 텍스트 구조를 확인했습니다.",
                        {"path":str(path),"text_chars":len(exported),"size":path.stat().st_size},
                    )],
                    artifacts=[Artifact("document",str(path),{"format":"hwpx"})],
                )
            if name=="hwpx_read_document":
                doc=HwpxDocument.open(path); return doc.export_text()
            return f"오류: 알 수 없는 툴 '{name}'"
        except Exception as exc:
            if name=="hwpx_create_document": return ToolRunResult.failed(tool_name=name,error=str(exc))
            return f"오류: {exc}"
