"""Typed Memory and Knowledge tools."""
from __future__ import annotations

import json
from typing import Any, Dict, List

from core.knowledge_memory import (
    EpistemicStatus, KnowledgeRecord, MemoryKind, get_knowledge_memory,
)
from core.plugin import BasePlugin, ToolSchema
from core.tool_result import Artifact, Evidence, ToolRunResult


class KnowledgeMemoryPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "knowledge_memory"
        self.description = "유형·출처·정정·만료 정책이 있는 장기 메모리"
        self.version = "1.0.0"

    def get_tools(self) -> List[ToolSchema]:
        kinds = [item.value for item in MemoryKind]
        epistemic = [item.value for item in EpistemicStatus]
        return [
            ToolSchema("memory_remember", "출처와 유형을 지정해 장기 메모리를 저장합니다", {
                "type": "object", "properties": {
                    "content": {"type": "string"}, "kind": {"enum": kinds},
                    "subject": {"type": "string"}, "predicate": {"type": "string", "default": "describes"},
                    "epistemic_status": {"enum": epistemic, "default": "user_claim"},
                    "source_uri": {"type": "string", "default": ""},
                    "source_label": {"type": "string", "default": "사용자 진술"},
                    "workspace_namespace": {"type": "string", "default": "global"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 1},
                    "expires_at": {"type": ["number", "null"], "default": None},
                    "metadata": {"type": "object", "default": {}},
                }, "required": ["content", "kind", "subject"], "additionalProperties": False,
            }, side_effect="change"),
            ToolSchema("memory_correct", "기존 사실과 모순되는 사용자 정정을 새 버전으로 저장합니다", {
                "type": "object", "properties": {
                    "subject": {"type": "string"}, "predicate": {"type": "string"},
                    "content": {"type": "string"}, "workspace_namespace": {"type": "string", "default": "global"},
                }, "required": ["subject", "predicate", "content"], "additionalProperties": False,
            }, side_effect="change"),
            ToolSchema("memory_search", "유형과 metadata 조건으로 장기 메모리를 검색합니다", {
                "type": "object", "properties": {
                    "query": {"type": "string", "default": ""},
                    "kinds": {"type": "array", "items": {"enum": kinds}, "default": []},
                    "workspace_namespace": {"type": "string", "default": "global"},
                    "metadata_filter": {"type": "object", "default": {}},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
                }, "additionalProperties": False,
            }, side_effect="read"),
        ]

    def execute_tool(self, tool_name: str, data: Dict[str, Any]):
        store = get_knowledge_memory()
        try:
            if tool_name == "memory_remember":
                record = KnowledgeRecord(
                    content=data["content"], kind=MemoryKind(data["kind"]), subject=data["subject"],
                    predicate=data.get("predicate", "describes"),
                    epistemic_status=EpistemicStatus(data.get("epistemic_status", "user_claim")),
                    source_uri=data.get("source_uri", ""), source_label=data.get("source_label", "사용자 진술"),
                    workspace_namespace=data.get("workspace_namespace", "global"),
                    confidence=data.get("confidence", 1), expires_at=data.get("expires_at"),
                    metadata=data.get("metadata", {}),
                )
                record_id = store.remember(record, automatic=True)
                return ToolRunResult.successful(tool_name=tool_name, raw_output="장기 메모리를 저장했습니다.",
                    evidence=[Evidence("knowledge_record", "정책 검사를 통과한 메모리를 저장했습니다.", record.to_dict())],
                    artifacts=[Artifact("memory", record_id)])
            if tool_name == "memory_correct":
                record_id = store.correct(**data)
                record = store.get(record_id)
                return ToolRunResult.successful(tool_name=tool_name, raw_output="사용자 정정을 반영했습니다.",
                    evidence=[Evidence("memory_correction", "이전 모순 기록을 superseded 처리했습니다.", record.to_dict())],
                    artifacts=[Artifact("memory", record_id)])
            if tool_name == "memory_search":
                records = store.search(data.get("query", ""), kinds=data.get("kinds"),
                    workspace_namespace=data.get("workspace_namespace", "global"),
                    metadata_filter=data.get("metadata_filter"), limit=data.get("limit", 10))
                payload = [record.to_dict() for record in records]
                return ToolRunResult.successful(tool_name=tool_name, raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence("memory_query", f"장기 메모리 {len(records)}건을 조회했습니다.", {"records": payload})])
            return ToolRunResult.failed(tool_name=tool_name, error="지원하지 않는 메모리 도구입니다.")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
