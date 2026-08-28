"""Plugin Registry adapter for the local Anis Obsidian knowledge vault."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote
import json
import os

from core.obsidian_vault import get_obsidian_vault
from core.plugin import BasePlugin, IntentSchema, SlotSchema, ToolSchema
from core.rag import get_rag_manager
from core.tool_result import Artifact, Evidence, ToolRunResult


class ObsidianPlugin(BasePlugin):
    def __init__(self):
        super().__init__()
        self.name = "obsidian"
        self.version = "1.0.0"
        self.description = "Obsidian Markdown Vault 지식 저장·링크 탐색·RAG 동기화"

    def get_tools(self) -> List[ToolSchema]:
        return [
            ToolSchema("obsidian_configure_vault", "사용할 Obsidian Vault 폴더를 지정합니다.", {
                "type": "object", "properties": {"path": {"type": "string"}},
                "required": ["path"], "additionalProperties": False,
            }, required_permissions=["filesystem_write"], side_effect="change"),
            ToolSchema("obsidian_status", "현재 Vault 경로와 문서 수를 확인합니다.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, side_effect="read"),
            ToolSchema("obsidian_explore", "검색 결과에서 Wikilink를 제한 깊이로 탐색합니다.", {
                "type": "object", "properties": {
                    "query": {"type": "string"}, "depth": {"type": "integer", "minimum": 0, "maximum": 3, "default": 2},
                    "max_notes": {"type": "integer", "minimum": 1, "maximum": 50, "default": 24},
                }, "required": ["query"], "additionalProperties": False,
            }, side_effect="read"),
            ToolSchema("obsidian_sync_to_rag", "Vault의 wiki 문서만 RAG에 동기화합니다.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, required_permissions=["filesystem_read"], side_effect="change"),
            ToolSchema("obsidian_lint", "깨진 링크·고아·얇은·중복 문서를 검사합니다.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, side_effect="read"),
            ToolSchema("obsidian_open_vault", "설치된 Obsidian에서 현재 Vault를 엽니다.", {
                "type": "object", "properties": {}, "additionalProperties": False,
            }, required_permissions=["windows_api"], side_effect="execute", verification_required=False),
        ]

    def get_intents(self):
        return [
            IntentSchema("obsidian.explore", "옵시디언 기억과 연결 문서 검색", "obsidian_explore",
                ["옵시디언에서", "볼트에서", "기억 볼트", "연결된 문서"],
                [SlotSchema("query", "검색 주제", "어떤 주제를 찾아볼까요?")],
                execution_hints=["찾아", "검색", "조회", "알려"], request_type="query"),
            IntentSchema("obsidian.sync", "옵시디언 위키를 RAG에 동기화", "obsidian_sync_to_rag",
                ["옵시디언 동기화", "볼트 동기화"], [], execution_hints=["동기화", "반영"],
                utterance_patterns=[r"(?:옵시디언|볼트).{0,80}(?:동기화|RAG에\s*반영|RAG로\s*반영)"],
                request_type="change"),
            IntentSchema("obsidian.open", "옵시디언 볼트 열기", "obsidian_open_vault",
                ["옵시디언 열", "볼트 열"], [], execution_hints=["열어", "실행"], request_type="execute"),
        ]

    def extract_slots(self, intent_name: str, text: str, current_slots: Dict[str, Any]):
        slots = dict(current_slots)
        if intent_name == "obsidian.explore":
            query = text
            for token in ("옵시디언에서", "볼트에서", "찾아줘", "검색해줘", "조회해줘"):
                query = query.replace(token, " ")
            slots["query"] = " ".join(query.split()) or slots.get("query", "")
            slots.setdefault("depth", 2); slots.setdefault("max_notes", 24)
        return slots

    def execute_tool(self, tool_name: str, data: Dict[str, Any]):
        vault = get_obsidian_vault(rag=get_rag_manager())
        try:
            if tool_name == "obsidian_configure_vault":
                path = vault.configure(data["path"])
                return ToolRunResult.successful(tool_name=tool_name, raw_output="Obsidian Vault를 연결했습니다.",
                    evidence=[Evidence("obsidian_vault", "쓰기 가능한 Vault 구조를 확인했습니다.", {"path": str(path)})],
                    artifacts=[Artifact("directory", str(path))])
            if tool_name == "obsidian_status":
                notes = vault._notes()
                payload = {"path": str(vault.root), "wiki_notes": len(notes), "exists": vault.root.is_dir()}
                return ToolRunResult.successful(tool_name=tool_name, raw_output=json.dumps(payload, ensure_ascii=False),
                    evidence=[Evidence("obsidian_status", "Vault 상태를 파일시스템에서 확인했습니다.", payload)])
            if tool_name == "obsidian_explore":
                results = vault.explore(data["query"], depth=data.get("depth", 2), max_notes=data.get("max_notes", 24))
                summary = [{"path": item["relative_path"], "depth": item["depth"], "links": item["links"],
                            "excerpt": item["content"][:1200]} for item in results]
                return ToolRunResult.successful(tool_name=tool_name, raw_output=json.dumps(summary, ensure_ascii=False),
                    evidence=[Evidence("obsidian_graph_search", f"연결 문서 {len(results)}개를 탐색했습니다.", {"results": summary})])
            if tool_name == "obsidian_sync_to_rag":
                result = vault.sync_to_rag()
                return ToolRunResult.successful(tool_name=tool_name, raw_output=f"Obsidian 위키 {result['indexed']}개를 RAG에 동기화했습니다.",
                    evidence=[Evidence("obsidian_rag_sync", "raw 폴더를 제외하고 wiki만 동기화했습니다.", result)])
            if tool_name == "obsidian_lint":
                result = vault.lint()
                return ToolRunResult.successful(tool_name=tool_name, raw_output=json.dumps(result, ensure_ascii=False),
                    evidence=[Evidence("obsidian_lint", "Vault 링크와 문서 구조를 검사했습니다.", result)])
            if tool_name == "obsidian_open_vault":
                uri = "obsidian://open?path=" + quote(str(vault.root))
                os.startfile(uri)
                return ToolRunResult.unverified(tool_name=tool_name, raw_output="Obsidian Vault 열기를 요청했습니다.",
                    evidence=[Evidence("obsidian_uri", uri, {"path": str(vault.root)})])
            return ToolRunResult.failed(tool_name=tool_name, error="지원하지 않는 Obsidian 도구입니다.")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
