"""Cloud, OAuth, and communication tools with explicit draft/apply boundaries."""
from __future__ import annotations

import os
from typing import Any, Dict, List

from core.plugin import BasePlugin, ToolSchema
from core.remote_runtime import (OAuthCoordinator, ProviderApi, RemoteActionStore,
                                 RemoteCatalogStore, RemoteRuntimeError)
from core.tool_result import Evidence, ToolRunResult


class CloudCommunicationPlugin(BasePlugin):
    OPERATIONS = ["gmail_send", "google_calendar_create", "outlook_send",
                  "outlook_calendar_create", "slack_send", "teams_send"]

    def __init__(self):
        super().__init__()
        self.name = "cloud_communication"
        self.description = "OAuth, cloud catalog synchronization, and approved external messaging"
        self.oauth = OAuthCoordinator()
        self.actions = RemoteActionStore()
        self.catalog = RemoteCatalogStore()
        self.api = ProviderApi(self.oauth)

    def diagnose(self) -> List[str]:
        issues = super().diagnose()
        for variable in ("GOOGLE_OAUTH_CLIENT_ID", "MICROSOFT_OAUTH_CLIENT_ID",
                         "SLACK_BOT_TOKEN", "NOTION_API_TOKEN"):
            if not os.getenv(variable):
                issues.append(f"선택 연동 미설정: {variable}")
        return issues

    def get_tools(self) -> List[ToolSchema]:
        provider_account = {
            "provider": {"type": "string"},
            "account": {"type": "string", "minLength": 1},
        }
        return [
            ToolSchema("oauth_begin", "Google 또는 Microsoft OAuth PKCE 인증을 시작합니다.", {
                "type": "object", "properties": {**provider_account,
                    "provider": {"enum": ["google", "microsoft"]},
                    "redirect_uri": {"type": "string", "minLength": 1}},
                "required": ["provider", "account", "redirect_uri"], "additionalProperties": False,
            }, ["cloud_account"], side_effect="execute", verification_required=False),
            ToolSchema("oauth_complete", "OAuth callback code를 DPAPI 암호화 저장소에 보관합니다.", {
                "type": "object", "properties": {"state": {"type": "string"}, "code": {"type": "string"}},
                "required": ["state", "code"], "additionalProperties": False,
            }, ["cloud_account"], side_effect="change"),
            ToolSchema("oauth_status", "비밀값을 노출하지 않고 OAuth 연결 상태만 확인합니다.", {
                "type": "object", "properties": {**provider_account,
                    "provider": {"enum": ["google", "microsoft"]}},
                "required": ["provider", "account"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="read", verification_required=False),
            ToolSchema("remote_create_draft", "외부 반영 없이 승인 대기 초안을 로컬 원장에 만듭니다.", {
                "type": "object", "properties": {**provider_account,
                    "operation": {"enum": self.OPERATIONS}, "payload": {"type": "object"}},
                "required": ["provider", "account", "operation", "payload"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="change"),
            ToolSchema("remote_apply_draft", "승인된 초안을 외부에 반영하고 원격 ID를 재조회해 검증합니다.", {
                "type": "object", "properties": {"action_id": {"type": "string", "minLength": 1}},
                "required": ["action_id"], "additionalProperties": False,
            }, ["external_send"], side_effect="external_send"),
            ToolSchema("cloud_sync_catalog", "Drive, OneDrive 또는 Notion 메타데이터를 로컬 카탈로그에 동기화합니다.", {
                "type": "object", "properties": {**provider_account,
                    "provider": {"enum": ["google_drive", "onedrive", "notion"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                "required": ["provider", "account"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="change"),
            ToolSchema("communication_read_summary", "Slack 또는 Teams 메시지를 조회하고 간결한 근거 요약을 만듭니다.", {
                "type": "object", "properties": {**provider_account,
                    "provider": {"enum": ["slack", "teams"]}, "channel": {"type": "string"},
                    "team_id": {"type": "string"}, "channel_id": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
                "required": ["provider", "account"], "additionalProperties": False,
            }, ["cloud_read"], side_effect="read"),
        ]

    @staticmethod
    def _success(tool: str, raw: str, kind: str, data: Dict[str, Any]) -> ToolRunResult:
        return ToolRunResult.successful(tool_name=tool, raw_output=raw,
                                        evidence=[Evidence(kind, raw, data)])

    def execute_tool(self, tool_name: str, data: Dict[str, Any]):
        try:
            if tool_name == "oauth_begin":
                result = self.oauth.begin(data["provider"], data["account"], data["redirect_uri"])
                return self._success(tool_name, "OAuth 인증 URL을 생성했습니다.", "oauth_pkce", result)
            if tool_name == "oauth_complete":
                result = self.oauth.complete(data["state"], data["code"])
                return self._success(tool_name, "OAuth 토큰을 암호화 저장했습니다.", "oauth_token_vault", result)
            if tool_name == "oauth_status":
                result = self.oauth.status(data["provider"], data["account"])
                return self._success(tool_name, "OAuth 연결 상태를 확인했습니다.", "oauth_status", result)
            if tool_name == "remote_create_draft":
                action = self.actions.create(data["provider"], data["account"], data["operation"], data["payload"])
                detail = {"action_id": action.action_id, "operation": action.operation, "status": action.status}
                return self._success(tool_name, "외부 반영 전 승인 대기 초안을 만들었습니다.", "remote_draft", detail)
            if tool_name == "remote_apply_draft":
                action = self.actions.get(data["action_id"])
                if not action or action.status != "draft":
                    raise RemoteRuntimeError("초안이 없거나 이미 적용된 작업입니다.")
                remote_id = self.api.apply(action)
                applied = self.actions.mark_applied(action.action_id, remote_id)
                detail = {"action_id": action.action_id, "remote_id": applied.remote_id,
                          "status": applied.status, "verified": bool(applied.remote_id)}
                return self._success(tool_name, "승인된 작업을 외부에 반영하고 원격 ID를 검증했습니다.",
                                     "remote_id_verification", detail)
            if tool_name == "cloud_sync_catalog":
                items = self.api.sync(data["provider"], data["account"], data)
                ids = self.catalog.replace(data["provider"], data["account"], items)
                detail = {"provider": data["provider"], "count": len(ids), "remote_ids": ids}
                return self._success(tool_name, f"원격 항목 {len(ids)}개를 동기화했습니다.", "cloud_catalog", detail)
            if tool_name == "communication_read_summary":
                messages = self.api.read_messages(data["provider"], data["account"], data)
                normalized = []
                for message in messages:
                    text = message.get("text") or message.get("body", {}).get("content", "")
                    normalized.append({"remote_id": str(message.get("ts") or message.get("id") or ""),
                                       "author": str(message.get("user") or message.get("from", {}).get("user", {}).get("displayName", "")),
                                       "snippet": str(text)[:160]})
                detail = {"provider": data["provider"], "count": len(normalized), "messages": normalized}
                return self._success(tool_name, f"메시지 {len(normalized)}개를 조회해 요약했습니다.",
                                     "communication_summary", detail)
            raise ValueError(f"지원하지 않는 도구: {tool_name}")
        except Exception as exc:
            return ToolRunResult.failed(tool_name=tool_name, error=str(exc))
