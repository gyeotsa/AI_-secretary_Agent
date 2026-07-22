"""Lightweight local PDF creation and extraction with PyMuPDF."""
from pathlib import Path
from typing import Any, Dict, List
import fitz
from core.harness import SafetyLayer
from core.plugin import BasePlugin, ToolSchema, IntentSchema, SlotSchema
from plugins._document_slots import extract_document_slots


class PdfPlugin(BasePlugin):
    def __init__(self): super().__init__(); self.name="pdf"; self.description="로컬 PDF 생성·텍스트 추출"
    def get_tools(self)->List[ToolSchema]:
        return [
            ToolSchema("pdf_create_document","텍스트 PDF를 생성합니다",{"type":"object","properties":{"path":{"type":"string"},"title":{"type":"string"},"paragraphs":{"type":"array","items":{"type":"string"}}},"required":["path"]},["filesystem_write"]),
            ToolSchema("pdf_extract_text","PDF 텍스트를 추출합니다",{"type":"object","properties":{"path":{"type":"string"},"max_pages":{"type":"integer","default":100}},"required":["path"]},["filesystem_read"]),
        ]
    def get_intents(self):
        return [IntentSchema("pdf.create","PDF 문서 생성","pdf_create_document",["pdf","PDF"],[SlotSchema("path","저장 경로","PDF 파일을 어디에 저장할까요, 보스?")],follow_up_hints=["다시"])]
    def extract_slots(self,intent_name,text,current_slots):
        return extract_document_slots(text,current_slots,".pdf","새 PDF") if intent_name=="pdf.create" else dict(current_slots)
    @staticmethod
    def _path(value:Any)->Path:
        path=Path(str(value)).expanduser().resolve(); ok,error=SafetyLayer.validate_path(str(path))
        if not ok: raise ValueError(error)
        if path.suffix.casefold() != ".pdf": raise ValueError(".pdf 파일만 지원합니다")
        return path
    def execute_tool(self,name:str,data:Dict[str,Any])->str:
        try:
            path=self._path(data["path"])
            if name=="pdf_create_document":
                doc=fitz.open(); page=doc.new_page(); y=72
                blocks=([str(data["title"])] if data.get("title") else [])+[str(x) for x in data.get("paragraphs") or []]
                for block in blocks:
                    rest=block
                    while rest:
                        if y>760: page=doc.new_page(); y=72
                        count=page.insert_textbox(fitz.Rect(72,y,523,y+80),rest,fontsize=11,fontname="korea")
                        if count>=0: rest=""; y+=90
                        else: rest=rest[:max(1,len(rest)//2)]; y+=90
                path.parent.mkdir(parents=True,exist_ok=True); doc.save(path); doc.close(); return f"PDF 생성 성공: {path}"
            if name=="pdf_extract_text":
                with fitz.open(path) as doc: return "\n".join(page.get_text() for page in list(doc)[:max(1,min(int(data.get("max_pages",100)),1000))])
            return f"오류: 알 수 없는 툴 '{name}'"
        except Exception as exc:return f"오류: {exc}"
