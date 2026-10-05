"""Google Drive's explicitly selected document bodies, not a background crawl."""
from __future__ import annotations

import json

from core.cloud_content import CloudContentRuntime
from core.plugin import BasePlugin, PluginStateProbe, ToolCancelledError, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult, ToolRunStatus
from core.turn_context import check_turn_cancelled


class CloudKnowledgePlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "cloud_knowledge"
        self.description = "구글 드라이브 Google Docs/UTF-8 텍스트 본문의 계정별 RAG 동기화·근거 검색"
        self.auth_required, self.auth_type = True, "Google OAuth"
        self.runtime = CloudContentRuntime()  # No vault/RAG initialization until an explicit operation.

    def probe_connection(self):
        return PluginStateProbe("unchecked", "지정한 Google 계정과 문서 ID의 실제 조회 검증이 필요합니다.")

    def probe_authentication(self):
        return PluginStateProbe("unchecked", "Google Drive 읽기 권한과 실제 문서 접근을 검증해야 합니다.")

    def get_tools(self):
        scope = {"provider": {"enum": ["google_drive"]},
                 "account": {"type": "string", "minLength": 1, "maxLength": 16384}}
        return [
            ToolSchema("cloud_sync_documents", "구글 드라이브에서 사용자가 명시한 파일 ID의 Google Docs/UTF-8 text·markdown·csv 본문만 로컬 RAG에 동기화합니다. PDF/Office/Sheets/Slides는 미지원이며 전체 드라이브를 수집하지 않습니다.", {
                "type": "object", "additionalProperties": False,
                "properties": {**scope, "file_ids": {"type": "array", "minItems": 1, "maxItems": 10,
                    "uniqueItems": True, "items": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,256}$"},
                    "description": "사용자가 선택해 명시한 Drive 파일 ID만 전달합니다. URL/제목에서 ID를 추측하지 않습니다."}},
                "required": ["provider", "account", "file_ids"],
            }, ["cloud_read"], side_effect="change", timeout_seconds=90, cancellable=True, max_retries=0),
            ToolSchema("cloud_search_evidence", "구글 드라이브에서 명시적으로 RAG에 등록한 해당 계정의 문서 본문을 검색해 출처 URL/ID/hash와 인용 Chunk를 반환합니다. 후보 문서의 원격 접근·버전을 재확인하며 오래된/철회된 본문을 새 근거로 반환하지 않습니다.", {
                "type": "object", "additionalProperties": False,
                "properties": {**scope, "query": {"type": "string", "minLength": 1, "maxLength": 4096},
                               "top_k": {"type": "integer", "minimum": 1, "maximum": 10}},
                "required": ["provider", "account", "query"],
            }, ["cloud_read"], side_effect="change", timeout_seconds=90, cancellable=True, max_retries=0),
        ]

    def execute_tool(self, tool_name, data):
        context = self.get_execution_context()

        def checkpoint():
            check_turn_cancelled()
            if context is not None:
                context.raise_if_cancelled()

        try:
            checkpoint()
            if tool_name == "cloud_sync_documents":
                detail = self.runtime.sync(data["provider"], data["account"], data["file_ids"], check_cancelled=checkpoint)
                raw = json.dumps(detail, ensure_ascii=False)
                evidence_kind = "cloud_body_sync"
                artifacts = [Artifact("url", row["source_url"], {"remote_id": row["remote_id"],
                             "content_sha256": row["content_sha256"]}) for row in detail["results"]
                             if row["status"] in {"indexed", "unchanged"}]
            elif tool_name == "cloud_search_evidence":
                detail = self.runtime.search(data["provider"], data["account"], data["query"],
                                             top_k=data.get("top_k", 3), check_cancelled=checkpoint)
                raw = json.dumps(detail, ensure_ascii=False)
                evidence_kind = "cloud_body_retrieval"
                artifacts = [Artifact("url", row["source"], {"chunk_id": row.get("chunk_id", ""),
                             "remote_id": row["remote_id"], "content_sha256": row["content_sha256"]})
                             for row in detail["results"]]
            else:
                raise ValueError("unsupported_cloud_knowledge_tool")
            if detail.get("cancelled"):
                return ToolRunResult.cancelled(tool_name=tool_name,
                    message="클라우드 본문 작업을 취소했습니다. 이미 반영한 개별 파일과 남은 범위는 증거에 보존했습니다.",
                    evidence=[Evidence(evidence_kind, "명시 파일 범위의 부분 실행/취소", detail)])
            try:
                checkpoint()
            except ToolCancelledError:
                return ToolRunResult.cancelled(tool_name=tool_name,
                    message="클라우드 본문 작업을 취소했습니다. 취소 전에 관측/반영한 범위는 증거에 보존했습니다.",
                    evidence=[Evidence(evidence_kind, "취소 직전의 실제 개별 파일 결과", detail)])
            status = ToolRunStatus.SUCCEEDED if detail["complete"] else ToolRunStatus.PARTIAL
            return ToolRunResult(tool_name, status, raw, evidence=[Evidence(evidence_kind,
                "명시 파일 범위의 본문/원격 접근 근거입니다. 전체 클라우드 동기화나 실제 답변 사용 수락은 아닙니다.", detail)],
                artifacts=artifacts)
        except ToolCancelledError:
            return ToolRunResult.cancelled(tool_name=tool_name, message="클라우드 본문 작업을 취소했습니다.")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=f"cloud_content_failed:{type(exc).__name__}")
