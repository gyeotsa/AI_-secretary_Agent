"""Structure-preserving RAG catalog tools."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from core.harness import SafetyLayer
from core.plugin import BasePlugin, ToolSchema
from core.rag import get_rag_manager
from core.tool_result import Artifact, Evidence, ToolRunResult


class RagKnowledgePlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "rag_knowledge"
        self.description = "구조 보존 Chunk와 동기화·필터·인용을 제공하는 RAG"
        self.version = "1.0.0"

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("rag_sync_document", "문서 변경·삭제를 RAG 인덱스와 동기화합니다", {
                "type": "object", "properties": {"path": {"type": "string"}, "metadata": {"type": "object", "default": {}}},
                "required": ["path"], "additionalProperties": False,
            }, ["filesystem_read"], side_effect="change"),
            ToolSchema("rag_remove_document", "문서와 모든 Chunk를 RAG 인덱스에서 삭제합니다", {
                "type": "object", "properties": {"doc_id": {"type": "string"}},
                "required": ["doc_id"], "additionalProperties": False,
            }, side_effect="change"),
            ToolSchema("rag_search_evidence", "metadata filter와 reranking으로 인용 가능한 Chunk를 검색합니다", {
                "type": "object", "properties": {
                    "query": {"type": "string"}, "top_k": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
                    "metadata_filter": {"type": "object", "default": {}}, "include_stale": {"type": "boolean", "default": False},
                }, "required": ["query"], "additionalProperties": False,
            }, side_effect="read"),
            ToolSchema("rag_revalidate_web_document", "웹 출처를 다시 확인한 뒤 만료 시각을 갱신합니다", {
                "type": "object", "properties": {
                    "doc_id": {"type": "string"}, "verified_at": {"type": ["number", "null"], "default": None},
                    "ttl_seconds": {"type": ["number", "null"], "minimum": 1, "default": None},
                }, "required": ["doc_id"], "additionalProperties": False,
            }, side_effect="change"),
        ]

    def execute_tool(self, tool_name: str, data: Dict[str, Any]):
        manager = get_rag_manager()
        try:
            if tool_name == "rag_sync_document":
                ok, error = SafetyLayer.validate_path(data["path"])
                if not ok: return ToolRunResult.failed(tool_name=tool_name, error=error)
                output = manager.sync_document(data["path"], data.get("metadata"))
                key = manager._document_key(Path(data["path"]).name)
                document = manager.documents.get(key)
                return ToolRunResult.successful(tool_name=tool_name, raw_output=output,
                    evidence=[Evidence("rag_sync", "원본 해시와 Chunk 카탈로그를 동기화했습니다.", {
                        "doc_id": Path(data["path"]).name, "present": document is not None,
                        "content_sha256": (document or {}).get("content_sha256", ""),
                    })], artifacts=[Artifact("document", str(Path(data["path"]).resolve()))] if document else [])
            if tool_name == "rag_remove_document":
                removed = manager.remove_document(data["doc_id"])
                if not removed: return ToolRunResult.failed(tool_name=tool_name, error="RAG 문서를 찾을 수 없습니다.")
                return ToolRunResult.successful(tool_name=tool_name, raw_output="RAG 문서와 Chunk를 삭제했습니다.",
                    evidence=[Evidence("rag_absence", "문서 카탈로그와 Vector Chunk 삭제를 요청했습니다.", {"doc_id": data["doc_id"]})])
            if tool_name == "rag_search_evidence":
                results = manager.search_docs(data["query"], data.get("top_k", 3),
                    data.get("metadata_filter"), data.get("include_stale", False))
                return ToolRunResult.successful(tool_name=tool_name, raw_output=json.dumps(results, ensure_ascii=False),
                    evidence=[Evidence("rag_evidence", f"인용 가능한 Chunk {len(results)}건을 조회했습니다.", {"results": results})],
                    artifacts=[Artifact("document", item["source"], {"chunk_id": item.get("chunk_id", "")}) for item in results])
            if tool_name == "rag_revalidate_web_document":
                updated = manager.revalidate_document(data["doc_id"], verified_at=data.get("verified_at"),
                                                       ttl_seconds=data.get("ttl_seconds"))
                if not updated: return ToolRunResult.failed(tool_name=tool_name, error="재검증할 웹 문서를 찾을 수 없습니다.")
                document = manager.documents[manager._document_key(data["doc_id"])]
                return ToolRunResult.successful(tool_name=tool_name, raw_output="웹 문서의 재검증 시각을 갱신했습니다.",
                    evidence=[Evidence("web_revalidation", "웹 출처 재검증 후 TTL을 갱신했습니다.", {
                        "doc_id": data["doc_id"], "recorded_at": document["metadata"]["recorded_at"],
                        "expires_at": document["metadata"]["expires_at"],
                    })])
            return ToolRunResult.failed(tool_name=tool_name, error="지원하지 않는 RAG 도구입니다.")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
